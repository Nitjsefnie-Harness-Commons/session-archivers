#!/usr/bin/env python3
'''Archive Codex CLI rollouts to R2. Third sibling of the Claude and Kimi
archivers, and deliberately not a new design: the key layout, the compression
policy, the per-machine manifest, the single-instance lock and the retention
gate are all shared, and live in store.py and runtime.py.

Designed to run hourly via cron. Behaviour per run:
1. Acquire single-instance lock. If a previous run still holds it, exit
   silently.
2. For every rollout under ~/.codex/sessions/YYYY/MM/DD/, read its
   session_meta head, derive its key, upload (size-skip, idempotent).
3. Publish one sessions/<hash>/project.json marker per project so the
   dashboard can show a path instead of a hash.
4. Delete a local rollout once it is uploaded AND older than DAYS.
5. Log a one-liner summary to ~/.codex/archive-codex-sessions.log.

WHY THE KEY LAYOUT IS THE KIMI ONE, NOT A CODEX-SHAPED ONE
----------------------------------------------------------
On disk Codex is date-bucketed (YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl) and
carries no project in the path at all. That layout cannot be uploaded
as-is: backend/ingest.py reads project_id and session_id OUT OF THE KEY,
before it fetches anything. So the archiver does the translation, exactly
as the Kimi one already does for kimi-code (mapping agents/main/wire.jsonl
onto sessions/<hash>/<uuid>/wire.jsonl):

  sessions/<md5(cwd)[:12]>/<thread_id>/wire.jsonl[.xz]
  sessions/<md5(cwd)[:12]>/<thread_id>/subagents/<rollout_uuid>/wire.jsonl[.xz]
  sessions/<md5(cwd)[:12]>/project.json                       {"path": cwd}

Both facts that shape it were measured over the 51 rollouts on this box:

  * A MAIN thread has exactly one rollout file (9 files, 9 thread ids), so
    <thread_id> is a unique key for it.
  * A SUBAGENT thread REUSES its parent's session_id — 42 subagent files
    share just 2 thread ids — so those must be keyed per file, by the uuid
    in the filename. That is why they take the subagents/ leg, which also
    gives ingest the is_main split it already looks for.

Grouping a whole fork family under one <thread_id> is the point, not a
side effect: usage_rollup is keyed by session_id, and those files are one
logical session.

Flags:
  --days N    retention threshold for the delete gate (default 3)
  --dry-run   preview every action; no R2 puts, no local removals

Exit code: 0 on success, 1 if any upload failed.
'''

__version__ = '2.0.0'
# Cross-platform: must work on Linux AND Windows. No POSIX-only calls without
# a Windows fallback. Bump __version__ (SemVer) on every substantive change.

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from . import runtime, store
from .settings import setting

BUCKET = setting('R2_BUCKET_CODEX', 'codex')

CODEX_DIR = Path.home() / '.codex'
SESSIONS_DIR = CODEX_DIR / 'sessions'
LOCK_FILE = CODEX_DIR / 'archive-codex-sessions.lock'
LOG_FILE = CODEX_DIR / 'archive-codex-sessions.log'

DAYS = runtime.DAYS


def project_hash(cwd):
    '''md5(project path), truncated to 12 hex — the shape ingest expects in
    the <hash> position, and the same digest the Kimi side buckets by.

    Not a security boundary: it exists to give a filesystem path a short,
    stable, path-safe name.
    '''
    return hashlib.md5(
        (cwd or '').encode('utf-8'), usedforsecurity=False
    ).hexdigest()[:12]


def rollout_uuid(path):
    '''The uuid embedded in rollout-<timestamp>-<uuid>.jsonl.

    Used only to key SUBAGENT rollouts, whose thread id is their parent's
    and therefore not unique per file.
    '''
    stem = Path(path).name
    if stem.endswith('.jsonl'):
        stem = stem[:-len('.jsonl')]
    parts = stem.split('-')
    # rollout-YYYY-MM-DDTHH-MM-SS-<5 uuid groups>
    return '-'.join(parts[-5:]) if len(parts) >= 5 else stem


def read_session_meta(path):
    '''First session_meta payload in a rollout, or None.

    Only the head of the file is read: session_meta is the opening record,
    and a rollout can be 20MB.
    '''
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
            for _ in range(64):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line or '"session_meta"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get('type') == 'session_meta':
                    payload = rec.get('payload')
                    return payload if isinstance(payload, dict) else None
    except OSError:
        return None
    return None


def rollout_key(path, meta):
    '''R2 key for one rollout, or None when it cannot be placed.

    A rollout with no session_meta has no thread identity and no project, so
    it is skipped rather than filed under a guess — ingest would read both
    straight out of the key and be wrong for the life of the object.
    '''
    if not meta:
        return None
    thread_id = meta.get('session_id')
    cwd = meta.get('cwd')
    if not thread_id or not cwd:
        return None
    prefix = f'sessions/{project_hash(cwd)}/{thread_id}'
    if meta.get('agent_path') or meta.get('parent_thread_id'):
        # A subagent thread shares its parent's session_id, so it is keyed
        # per file. This is also what gives ingest is_main=False.
        return f'{prefix}/subagents/{rollout_uuid(path)}/wire.jsonl'
    return f'{prefix}/wire.jsonl'


def find_rollouts():
    '''Every rollout on this machine, oldest path first.'''
    if not SESSIONS_DIR.is_dir():
        return []
    return sorted(SESSIONS_DIR.glob('**/rollout-*.jsonl'))


def _upload_markers(dest, projects):
    '''One sessions/<hash>/project.json per project seen this run.

    ingest reads it for the project's display name; without it the
    dashboard shows a 12-hex hash.
    '''
    uploaded = failed = 0
    for phash, cwd in sorted(projects.items()):
        key = f'sessions/{phash}/project.json'
        body = json.dumps({'path': cwd}, ensure_ascii=False).encode('utf-8')
        try:
            if dest.upload_bytes(body, key):
                uploaded += 1
        except Exception as e:  # pylint: disable=broad-except
            dest.log(f'marker upload failed: hash={phash} err={e}')
            failed += 1
    return uploaded, failed


def _retire_local(path, key, dest, cutoff):
    '''Delete a local rollout once it is safely in the bucket and stale.

    The bucket check is not belt-and-braces: a failed upload must never
    take the only copy with it.
    '''
    if not runtime.is_old(path, cutoff):
        return False
    if not dest.holds(key):
        return False
    if dest.dry_run:
        dest.log(f'  DRY rm {path}')
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        dest.log(f'  rm failed: {path}: {e}')
        return False


def archive_rollouts(dest, cutoff):
    '''Upload every rollout, then delete the ones past the retention gate.'''
    tally = Counter()
    projects = {}

    for path in find_rollouts():
        meta = read_session_meta(path)
        key = rollout_key(path, meta)
        if key is None or meta is None:
            # No session_meta: a truncated or still-opening rollout. Left in
            # place; the next run picks it up once the head is written.
            tally['skipped'] += 1
            continue
        projects[project_hash(meta.get('cwd'))] = meta.get('cwd')
        try:
            if dest.upload_file(path, key):
                tally['uploaded'] += 1
        except Exception as e:  # pylint: disable=broad-except
            dest.log(f'upload failed: {path} err={e}')
            tally['failed'] += 1
            continue
        if _retire_local(path, key, dest, cutoff):
            tally['deleted'] += 1

    m_up, m_failed = _upload_markers(dest, projects)
    return (tally['uploaded'] + m_up, tally['deleted'],
            tally['failed'] + m_failed, tally['skipped'])


def main():
    runtime.reconfigure_streams()
    ap = argparse.ArgumentParser(
        description=(__doc__ or '').split('\n', maxsplit=1)[0])
    ap.add_argument('--days', type=int, default=DAYS,
                    help='retention threshold for the delete gate')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview every action; no R2 puts, no local removals')
    args = ap.parse_args()

    log = runtime.Logger(LOG_FILE)

    lock = runtime.acquire_lock(LOCK_FILE)
    if lock is None:
        return 0  # a previous run still holds it

    try:
        cutoff = time.time() - args.days * 86400
        dest = store.Store(store.client(), BUCKET, log, args.dry_run)
        dest.inventory()
        dest.load_manifest()
        log(f'remote inventory: {len(dest.remote):,} objects in bucket {BUCKET!r}')

        uploaded, deleted, failed, skipped = archive_rollouts(dest, cutoff)
        if uploaded and not args.dry_run:
            dest.save_manifest()
        log(f'done: uploaded={uploaded} deleted={deleted} '
            f'failed={failed} skipped={skipped}')
        return 1 if failed else 0
    finally:
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
