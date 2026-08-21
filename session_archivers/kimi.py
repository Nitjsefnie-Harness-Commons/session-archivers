#!/usr/bin/env python3
'''Archive Kimi Code CLI sessions to R2 + sweep stale ~/.kimi scratch dirs.

Designed to run hourly via cron.

Behaviour per run:
1. Acquire single-instance lock. If a previous run still holds it, exit
   silently.
2. Sweep kimi-local stale paths (files older than DAYS):
   ~/.kimi/logs/*, ~/.kimi/telemetry/*
3. For every session under ~/.kimi/sessions/<session_hash>/<uuid>/:
   upload every file (size-skip, idempotent), then delete locally
   if the wire.jsonl mtime > DAYS.
4. Archive ~/.kimi/user-history/<session_hash>.jsonl files.
5. Archive ~/.kimi-code/sessions/<wd_slug_hash>/<session-id>/ files,
   mapping agents/main/wire.jsonl -> sessions/<hash>/<uuid>/wire.jsonl and
   agents/<agent-N>/wire.jsonl -> sessions/<hash>/<uuid>/subagents/<agent-N>/wire.jsonl.
6. Log a one-liner summary to ~/.kimi-code/archive-sessions.log.

Only the WALKS are here. The destination — key layout, compression policy,
manifest, inventory — lives in store.py, and the host-side scaffolding —
logging, locking, the retention predicate — in runtime.py, both shared with
the Claude and Codex archivers.

Flags:
  --days N    retention threshold for the delete gate (default 3)
  --dry-run   preview every action; no R2 puts, no local removals

Exit code: 0 on success, 1 if any upload failed.
'''

__version__ = '2.0.0'
# Cross-platform: must work on Linux AND Windows. No POSIX-only calls without a
# Windows fallback. Bump __version__ (SemVer) on every substantive change.

import argparse
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from . import runtime, store
from .settings import setting

BUCKET = setting('R2_BUCKET_KIMI', 'kimi')

KIMI_DIR = Path.home() / '.kimi'
KIMI_CODE_DIR = Path.home() / '.kimi-code'
KC_SESSIONS_DIR = KIMI_CODE_DIR / 'sessions'
SESSIONS_DIR = KIMI_DIR / 'sessions'
LOGS_DIR = KIMI_DIR / 'logs'
TELEMETRY_DIR = KIMI_DIR / 'telemetry'
USER_HISTORY_DIR = KIMI_DIR / 'user-history'

# The CLI lives under ~/.kimi-code now; keep its operational files there too.
# (Legacy ~/.kimi/sessions is still archived as a SOURCE — see archive_sessions.)
LOCK_FILE = KIMI_CODE_DIR / 'archive-sessions.lock'
LOG_FILE = KIMI_CODE_DIR / 'archive-sessions.log'

DAYS = runtime.DAYS

# The 12-hex tail of a kimi-code bucket directory name (wd_<slug>_<hash>) is
# the session hash the R2 layout is keyed by.
_BUCKET_HASH = re.compile(r'_([0-9a-f]{12})$')


def cleanup_local(cutoff, dry_run, log):
    '''Delete ~/.kimi/{logs/*, telemetry/*} past `cutoff`.'''
    runtime.sweep_files(LOGS_DIR, cutoff, dry_run, log)
    runtime.sweep_files(TELEMETRY_DIR, cutoff, dry_run, log)


def load_project_map():
    '''Map md5(work_dir) -> work_dir from ~/.kimi/kimi.json.

    Source of truth for hash-to-path resolution lives on the box that ran
    kimi-cli; R2 only sees session-hash dirs. This map lets us publish a
    per-session marker so downstream consumers can resolve hashes back to
    project paths without needing kimi.json themselves.
    '''
    try:
        data = json.loads((KIMI_DIR / 'kimi.json').read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    out = {}
    for work_dir in data.get('work_dirs', []):
        path = work_dir.get('path')
        if not path:
            continue
        out[hashlib.md5(path.encode()).hexdigest()] = path
    return out


def load_kimi_code_workdirs():
    '''Map trailing 12-hex bucket hash -> workDir from
    ~/.kimi-code/session_index.jsonl.'''
    idx = KIMI_CODE_DIR / 'session_index.jsonl'
    if not idx.is_file():
        return {}
    out = {}
    try:
        for line in idx.read_text(encoding='utf-8').splitlines():
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_dir = entry.get('sessionDir', '')
            work_dir = entry.get('workDir')
            if not session_dir or not work_dir:
                continue
            m = re.search(r'/sessions/(wd_[^/]+)/', session_dir.replace('\\', '/'))
            if not m:
                continue
            hm = _BUCKET_HASH.search(m.group(1))
            if hm:
                out[hm.group(1)] = work_dir
    except OSError:
        return {}
    return out


def _workdir_from_state(session_dir: Path) -> str | None:
    '''Derive workDir from a kimi-code session's state.json homedir path.

    The fallback for a box with no session_index.jsonl.
    '''
    state = session_dir / 'state.json'
    if not state.is_file():
        return None
    try:
        data = json.loads(state.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    homedir = data.get('agents', {}).get('main', {}).get('homedir', '')
    if not homedir:
        return None
    # Strip .../sessions/wd_<bucket>/ses_<uuid>/agents/main -> project root.
    m = re.match(r'^(.*)/sessions/wd_[^/]+/ses_[^/]+/agents/main$',
                 homedir.replace('\\', '/'))
    return m.group(1) if m else None


def _publish_marker(dest, session_hash, path):
    '''sessions/<hash>/project.json, resolving the hash to a project path.

    Downstream ingest reads `.path` as the project's display name. Idempotent
    via the size-skip in upload_bytes. Returns (uploaded, failed).
    '''
    key = f'sessions/{session_hash}/project.json'
    body = json.dumps({'path': path}, ensure_ascii=False).encode()
    try:
        return (1 if dest.upload_bytes(body, key) else 0), 0
    except Exception as e:  # pylint: disable=broad-except
        dest.log(f'marker upload failed: hash={session_hash} err={e}')
        return 0, 1


def archive_kimi_code_sessions(dest, cutoff):
    '''Archive ~/.kimi-code/sessions/ using the same key layout as legacy.'''
    n_uploaded = n_deleted = n_failed = 0

    if not KC_SESSIONS_DIR.is_dir():
        return 0, 0, 0

    workdirs = load_kimi_code_workdirs()

    for bucket_dir in KC_SESSIONS_DIR.iterdir():
        if not bucket_dir.is_dir():
            continue
        hm = _BUCKET_HASH.search(bucket_dir.name)
        if not hm:
            continue
        session_hash = hm.group(1)

        path = workdirs.get(session_hash)
        if not path:
            for sess_dir in bucket_dir.iterdir():
                if sess_dir.is_dir():
                    path = _workdir_from_state(sess_dir)
                    if path:
                        break
        if path:
            up, failed = _publish_marker(dest, session_hash, path)
            n_uploaded += up
            n_failed += failed

        for session_dir in bucket_dir.iterdir():
            if not session_dir.is_dir():
                continue
            r2_prefix = f'sessions/{session_hash}/{session_dir.name}'
            agents_dir = session_dir / 'agents'

            main_wire = session_dir / 'agents' / 'main' / 'wire.jsonl'
            if main_wire.is_file():
                try:
                    if dest.upload_file(main_wire, f'{r2_prefix}/wire.jsonl'):
                        n_uploaded += 1
                except Exception as e:  # pylint: disable=broad-except
                    dest.log(f'upload failed: main_wire={main_wire} err={e}')
                    n_failed += 1

            if agents_dir.is_dir():
                for agent_dir in agents_dir.iterdir():
                    if not agent_dir.is_dir() or agent_dir.name == 'main':
                        continue
                    sub_prefix = f'{r2_prefix}/subagents/{agent_dir.name}'
                    wire = agent_dir / 'wire.jsonl'
                    if wire.is_file():
                        try:
                            if dest.upload_file(wire, f'{sub_prefix}/wire.jsonl'):
                                n_uploaded += 1
                        except Exception as e:  # pylint: disable=broad-except
                            dest.log(f'upload failed: subagent_wire={wire} err={e}')
                            n_failed += 1
                    blobs = agent_dir / 'blobs'
                    if blobs.is_dir():
                        try:
                            n_uploaded += dest.upload_dir(blobs, f'{sub_prefix}/blobs')
                        except Exception as e:  # pylint: disable=broad-except
                            dest.log(f'upload failed: blobs={blobs} err={e}')
                            n_failed += 1

            state = session_dir / 'state.json'
            if state.is_file():
                try:
                    if dest.upload_file(state, f'{r2_prefix}/state.json'):
                        n_uploaded += 1
                except Exception as e:  # pylint: disable=broad-except
                    dest.log(f'upload failed: state={state} err={e}')
                    n_failed += 1

            # Delete gate: the newest wire.jsonl mtime anywhere in the session.
            newest_wire = main_wire if main_wire.is_file() else None
            if agents_dir.is_dir():
                for agent_dir in agents_dir.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    wire = agent_dir / 'wire.jsonl'
                    if not wire.is_file():
                        continue
                    if (newest_wire is None
                            or wire.stat().st_mtime > newest_wire.stat().st_mtime):
                        newest_wire = wire
            if newest_wire is not None and runtime.is_old(newest_wire, cutoff):
                if dest.dry_run:
                    dest.log(f'  DRY rmtree {session_dir}')
                else:
                    shutil.rmtree(session_dir, ignore_errors=True)
                n_deleted += 1

        _prune_if_empty(bucket_dir, dest.dry_run)

    return n_uploaded, n_deleted, n_failed


def archive_sessions(dest, cutoff):
    '''Archive the legacy ~/.kimi/sessions/ tree.'''
    n_uploaded = n_deleted = n_failed = 0

    if not SESSIONS_DIR.is_dir():
        return 0, 0, 0

    project_map = load_project_map()

    for session_hash_dir in SESSIONS_DIR.iterdir():
        if not session_hash_dir.is_dir():
            continue
        session_hash = session_hash_dir.name

        path = project_map.get(session_hash)
        if path:
            up, failed = _publish_marker(dest, session_hash, path)
            n_uploaded += up
            n_failed += failed

        for uuid_dir in session_hash_dir.iterdir():
            if not uuid_dir.is_dir():
                continue
            r2_prefix = f'sessions/{session_hash}/{uuid_dir.name}'

            try:
                n_uploaded += dest.upload_dir(uuid_dir, r2_prefix)
            except Exception as e:  # pylint: disable=broad-except
                dest.log(f'upload failed: uuid_dir={uuid_dir} err={e}')
                n_failed += 1
                continue

            wire_jsonl = uuid_dir / 'wire.jsonl'
            if wire_jsonl.exists() and runtime.is_old(wire_jsonl, cutoff):
                if dest.dry_run:
                    dest.log(f'  DRY rmtree {uuid_dir}')
                else:
                    shutil.rmtree(uuid_dir, ignore_errors=True)
                n_deleted += 1

        _prune_if_empty(session_hash_dir, dest.dry_run)

    return n_uploaded, n_deleted, n_failed


def archive_user_history(dest, cutoff):
    '''Archive ~/.kimi/user-history/<session_hash>.jsonl.'''
    n_uploaded = n_deleted = n_failed = 0

    if not USER_HISTORY_DIR.is_dir():
        return 0, 0, 0

    for entry in USER_HISTORY_DIR.iterdir():
        if not entry.is_file() or entry.suffix != '.jsonl':
            continue
        key = f'user-history/{entry.stem}.jsonl'
        try:
            if dest.upload_file(entry, key):
                n_uploaded += 1
        except Exception as e:  # pylint: disable=broad-except
            dest.log(f'upload failed: user-history={entry} err={e}')
            n_failed += 1
            continue

        if runtime.is_old(entry, cutoff):
            if dest.dry_run:
                dest.log(f'  DRY rm {entry}')
            else:
                try:
                    entry.unlink()
                except OSError as e:
                    dest.log(f'  rm failed: {entry}: {e}')
            n_deleted += 1

    return n_uploaded, n_deleted, n_failed


def _prune_if_empty(directory, dry_run):
    '''Remove a container directory the delete gate has just emptied.'''
    if dry_run or not directory.is_dir():
        return
    try:
        if not any(directory.iterdir()):
            directory.rmdir()
    except OSError:
        pass


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
        log(f'starting archive-sessions (DAYS={args.days}, dry_run={args.dry_run})')
        cleanup_local(cutoff, args.dry_run, log)

        dest = store.Store(store.client(), BUCKET, log, args.dry_run)
        log(f'remote inventory: {len(dest.inventory()):,} objects '
            f'in bucket {BUCKET!r}')
        log(f'manifest {store.manifest_key()}: '
            f'{len(dest.load_manifest()):,} known compressed objects')

        up_sess, del_sess, fail_sess = archive_sessions(dest, cutoff)
        up_hist, del_hist, fail_hist = archive_user_history(dest, cutoff)
        up_kc, del_kc, fail_kc = archive_kimi_code_sessions(dest, cutoff)

        if not args.dry_run:
            try:
                dest.save_manifest()
            except Exception as e:  # pylint: disable=broad-except
                log(f'manifest save failed: {type(e).__name__}: {e}')

        n_up = up_sess + up_hist + up_kc
        n_del = del_sess + del_hist + del_kc
        n_fail = fail_sess + fail_hist + fail_kc

        log(f'done — uploaded={n_up} deleted={n_del} failures={n_fail}')
        return 1 if n_fail else 0
    finally:
        try:
            lock.close()
        except Exception as e:  # pylint: disable=broad-except
            log(f'lock.close() failed: {type(e).__name__}: {e}')


if __name__ == '__main__':
    raise SystemExit(main())
