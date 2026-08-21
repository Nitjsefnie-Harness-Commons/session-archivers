#!/usr/bin/env python3
'''Archive Claude Code sessions to R2 + sweep stale ~/.claude scratch dirs.

Designed to run hourly via cron or Task Scheduler.

Behaviour per run:
1. Acquire single-instance lock. If a previous run still holds it, exit
   silently.
2. Sweep claude-local stale paths (files / dirs older than DAYS):
   ~/.claude/debug/*, ~/.claude/file-history/*/, ~/.claude/telemetry/*
3. For every project under ~/.claude/projects/<project>/:
     Pass 1 — each *.jsonl plus its matching UUID data dir:
       upload (size-skip, idempotent), then delete locally if jsonl mtime > DAYS.
     Pass 2 — orphan UUID dirs (no matching jsonl, excluding memory/, tasks/):
       upload always, delete if dir mtime > DAYS.
4. Log a one-liner summary to ~/.claude/cleanup-sessions.log.

Only the WALK is here. The destination — key layout, compression policy,
manifest, inventory — lives in store.py, and the host-side scaffolding —
logging, locking, the retention predicate — in runtime.py, both shared with
the Kimi and Codex archivers.

Flags:
  --days N    retention threshold for the delete gate (default 3)
  --dry-run   preview every action; no R2 puts, no local removals

Exit code: 0 on success, 1 if any upload failed.
'''

import argparse
import shutil
import time
from pathlib import Path

from . import runtime, store
from .settings import setting

# R2 target. Credentials and account live in the `env` block of
# ~/.agent-bundle/settings.json, never here — this file ships in the bundle and
# is mirrored to a public git repository, so a literal key would travel with
# the code to every machine that installs it. See store.r2_config.
BUCKET = setting('R2_BUCKET_CLAUDE', 'claude')

CLAUDE_DIR = Path.home() / '.claude'
PROJECTS_DIR = CLAUDE_DIR / 'projects'
DEBUG_DIR = CLAUDE_DIR / 'debug'
FILE_HISTORY_DIR = CLAUDE_DIR / 'file-history'
TELEMETRY_DIR = CLAUDE_DIR / 'telemetry'

LOCK_FILE = CLAUDE_DIR / 'cleanup-sessions.lock'
LOG_FILE = CLAUDE_DIR / 'cleanup-sessions.log'

DAYS = runtime.DAYS

# Working state, not session history — and memory/ is private notes. Neither
# belongs in the bucket.
NOT_SESSION_DIRS = ('memory', 'tasks')


def cleanup_local(cutoff, dry_run, log):
    '''Delete ~/.claude/{debug/*, file-history/*/, telemetry/*} past `cutoff`.'''
    runtime.sweep_files(DEBUG_DIR, cutoff, dry_run, log)
    runtime.sweep_dirs(FILE_HISTORY_DIR, cutoff, dry_run, log)
    runtime.sweep_files(TELEMETRY_DIR, cutoff, dry_run, log)


def archive_projects(dest, cutoff):
    '''Upload every project, delete what is past the retention gate.

    Returns (uploaded, deleted, failed).
    '''
    n_uploaded = n_deleted = n_failed = 0
    if not PROJECTS_DIR.is_dir():
        return 0, 0, 0

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project = project_dir.name

        # Pass 1: each transcript and its matching UUID data dir.
        jsonl_stems = set()
        for jsonl in project_dir.glob('*.jsonl'):
            uuid = jsonl.stem
            jsonl_stems.add(uuid)
            r2_prefix = f'{project}/{uuid}'
            uuid_dir = project_dir / uuid

            try:
                if dest.upload_file(jsonl, f'{r2_prefix}/{uuid}.jsonl'):
                    n_uploaded += 1
            except Exception as e:  # pylint: disable=broad-except
                dest.log(f'upload failed: jsonl={jsonl} err={e}')
                n_failed += 1
                continue

            if uuid_dir.is_dir():
                try:
                    n_uploaded += dest.upload_dir(uuid_dir, f'{r2_prefix}/data')
                except Exception as e:  # pylint: disable=broad-except
                    dest.log(f'upload failed: uuid_dir={uuid_dir} err={e} '
                               '(jsonl uploaded; not deleting either)')
                    n_failed += 1
                    continue

            if runtime.is_old(jsonl, cutoff):
                if dest.dry_run:
                    dest.log(f'  DRY rm {jsonl}')
                    if uuid_dir.is_dir():
                        dest.log(f'  DRY rmtree {uuid_dir}')
                else:
                    try:
                        jsonl.unlink()
                    except OSError as e:
                        dest.log(f'  rm failed: {jsonl}: {e}')
                        continue
                    if uuid_dir.is_dir():
                        shutil.rmtree(uuid_dir, ignore_errors=True)
                n_deleted += 1

        # Pass 2: orphan UUID dirs, whose transcript is already gone.
        for sub in project_dir.iterdir():
            if not sub.is_dir() or sub.name in NOT_SESSION_DIRS:
                continue
            if sub.name in jsonl_stems:
                continue  # already handled in pass 1
            try:
                n_uploaded += dest.upload_dir(sub, f'{project}/{sub.name}/data')
            except Exception as e:  # pylint: disable=broad-except
                dest.log(f'upload failed: orphan uuid_dir={sub} err={e}')
                n_failed += 1
                continue
            if runtime.is_old(sub, cutoff):
                if dest.dry_run:
                    dest.log(f'  DRY rmtree {sub}')
                else:
                    shutil.rmtree(sub, ignore_errors=True)
                n_deleted += 1

    return n_uploaded, n_deleted, n_failed


def main():
    runtime.reconfigure_streams()
    ap = argparse.ArgumentParser(
        description=(__doc__ or '').split('\n\n', maxsplit=1)[0])
    ap.add_argument('--days', type=int, default=DAYS,
                    help=f'retention threshold for delete (default {DAYS})')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview only — no uploads, no removals')
    args = ap.parse_args()

    log = runtime.Logger(LOG_FILE)
    cutoff = time.time() - args.days * 86400

    lock = runtime.acquire_lock(LOCK_FILE)
    if lock is None:
        log('skipped: previous run still holds the lock')
        return 0

    try:
        log(f'starting cleanup-sessions (DAYS={args.days}, dry_run={args.dry_run})')
        cleanup_local(cutoff, args.dry_run, log)

        dest = store.Store(store.client(), BUCKET, log, args.dry_run)
        log(f'remote inventory: {len(dest.inventory()):,} objects '
            f'in bucket {BUCKET!r}')
        log(f'manifest {store.manifest_key()}: '
            f'{len(dest.load_manifest()):,} known compressed objects')

        n_up, n_del, n_fail = archive_projects(dest, cutoff)

        if not args.dry_run:
            try:
                dest.save_manifest()
            except Exception as e:  # pylint: disable=broad-except
                log(f'manifest save failed: {type(e).__name__}: {e}')

        log(f'done — uploaded={n_up} deleted={n_del} failures={n_fail}')
        return 1 if n_fail else 0
    finally:
        try:
            lock.close()
        except Exception as e:  # pylint: disable=broad-except
            log(f'lock.close() failed: {type(e).__name__}: {e}')


if __name__ == '__main__':
    raise SystemExit(main())
