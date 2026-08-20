#!/usr/bin/env python3
'''Archive Kimi Code CLI sessions to R2 + sweep stale ~/.kimi scratch dirs.

Designed to run hourly via cron. Self-contained — credentials inline.

Behaviour per run:
1. Acquire single-instance lock (fcntl, Windows msvcrt fallback).
   If a previous run still holds it, exit silently.
2. Sweep kimi-local stale paths (files / dirs older than DAYS):
   ~/.kimi/logs/*
3. For every session under ~/.kimi/sessions/<session_hash>/<uuid>/:
   upload every file (size-skip, idempotent), then delete locally
   if the wire.jsonl mtime > DAYS.
4. Archive ~/.kimi/user-history/<session_hash>.jsonl files.
5. Archive ~/.kimi-code/sessions/<wd_slug_hash>/<session-id>/ files,
   mapping agents/main/wire.jsonl -> sessions/<hash>/<uuid>/wire.jsonl and
   agents/<agent-N>/wire.jsonl -> sessions/<hash>/<uuid>/subagents/<agent-N>/wire.jsonl.
6. Log a one-liner summary to ~/.kimi/archive-sessions.log.

Flags:
  --days N    retention threshold for the delete gate (default 3)
  --dry-run   preview every action; no R2 puts, no local removals

Exit code: 0 on success, 1 if any upload failed.
'''

__version__ = '1.3.0'
# Cross-platform: must work on Linux AND Windows. No POSIX-only calls without a
# Windows fallback. Bump __version__ (SemVer) on every substantive change.

import argparse
import hashlib
import json
import lzma
import re
import shutil
import socket
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config

# Deployment values come from the neutral bundle settings helper, never from
# literals here — this file ships in the bundle and is mirrored to git, so a
# hardcoded key travels with the code. Both archivers use the helper under
# ~/.agent-bundle/scripts; this keeps their settings contract in one place
# without making Kimi depend on a ~/.claude installation.
from .settings import required, setting

BUCKET = setting('R2_BUCKET_KIMI', 'kimi')

KIMI_DIR = Path.home() / '.kimi'
KIMI_CODE_DIR = Path.home() / '.kimi-code'
KC_SESSIONS_DIR = KIMI_CODE_DIR / 'sessions'
SESSIONS_DIR = KIMI_DIR / 'sessions'
LOGS_DIR = KIMI_DIR / 'logs'
TELEMETRY_DIR = KIMI_DIR / 'telemetry'
USER_HISTORY_DIR = KIMI_DIR / 'user-history'

# The script lives under ~/.kimi-code now; keep its operational files there too.
# (Legacy ~/.kimi/sessions is still archived as a SOURCE — see archive_sessions().)
LOCK_FILE = KIMI_CODE_DIR / 'archive-sessions.lock'
LOG_FILE = KIMI_CODE_DIR / 'archive-sessions.log'

DAYS = 3

# Compression policy (mirrors the shared Claude archiver): text
# types are ALWAYS stored xz at <key>.xz; other (binary) types are compressed
# only if xz actually shrinks them, else plain. The per-machine manifest is
# plain JSON read back verbatim — never compress it. xz is stdlib (lzma).
XZ_PRESET = 9
TEXT_SUFFIXES = ('.jsonl', '.txt', '.json', '.js')
# Per-machine upload manifest object: a compressed object's remote size can't
# be compared to the plain local file, so each machine keeps a synced manifest
# {store_key: [mtime_ns, size]} under a hashed-machine-id name — N machines
# write N manifests and never clobber each other.
MANIFEST_PREFIX = 'manifests'


def _is_text(key):
    return key.endswith(TEXT_SUFFIXES)


def _machine_hash():
    '''Stable opaque id for this machine: sha256 of /etc/machine-id (Linux) or
    the hostname (other OSes), truncated.'''
    mid = ''
    for p in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            mid = Path(p).read_text(encoding='utf-8').strip()
        except OSError:
            mid = ''
        if mid:
            break
    if not mid:
        mid = socket.gethostname()
    return hashlib.sha256(mid.encode('utf-8')).hexdigest()[:16]


def manifest_key():
    return f'{MANIFEST_PREFIX}/{_machine_hash()}.json'


def load_manifest(client):
    '''Fetch this machine's upload manifest {store_key: [mtime_ns, size]}.
    Absent (first run) or unreadable → empty dict.'''
    try:
        body = client.get_object(Bucket=BUCKET, Key=manifest_key())['Body'].read()
        manifest = json.loads(body)
    except Exception:
        return {}
    return manifest if isinstance(manifest, dict) else {}


def save_manifest(client, manifest, dry_run):
    key = manifest_key()
    if dry_run:
        log(f'  DRY put manifest {key} ({len(manifest):,} entries)')
        return
    body = json.dumps(manifest, separators=(',', ':')).encode('utf-8')
    client.put_object(Bucket=BUCKET, Key=key, Body=body)
    log(f'manifest saved: {key} ({len(manifest):,} entries)')


def _stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def log(msg):
    line = f'{_stamp()} {msg}'
    print(line, flush=True)
    try:
        with LOG_FILE.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def _try_lock_handle(fh):
    if sys.platform == 'win32':
        import msvcrt
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open('a+b')
    try:
        fh.seek(0)
        if not fh.read(1):
            fh.write(b'\0')
            fh.flush()
        if _try_lock_handle(fh):
            return fh
    except OSError:
        pass
    fh.close()
    return None


def is_old(path, cutoff):
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return False


def cleanup_local(cutoff, dry_run):
    '''Delete ~/.kimi/logs/* older than DAYS.'''
    if LOGS_DIR.is_dir():
        for f in LOGS_DIR.iterdir():
            if f.is_file() and is_old(f, cutoff):
                if dry_run:
                    log(f'  DRY rm {f}')
                else:
                    try:
                        f.unlink()
                    except OSError as e:
                        log(f'  rm failed: {f}: {e}')

    if TELEMETRY_DIR.is_dir():
        for f in TELEMETRY_DIR.iterdir():
            if f.is_file() and is_old(f, cutoff):
                if dry_run:
                    log(f'  DRY rm {f}')
                else:
                    try:
                        f.unlink()
                    except OSError as e:
                        log(f'  rm failed: {f}: {e}')


def _r2_config():
    """(account_id, access_key, secret_key) — resolved at CALL time, not import.

    Lazily, so importing this module never demands credentials; only an
    operation that actually talks to R2 does. The account falls back to
    CLOUDFLARE_ACCOUNT_ID because it IS that account."""
    account = setting('R2_ACCOUNT_ID') or required(
        'CLOUDFLARE_ACCOUNT_ID', 'the Cloudflare account that owns the R2 bucket')
    return (account,
            required('R2_ACCESS_KEY_ID', 'R2 API token access key'),
            required('R2_SECRET_ACCESS_KEY', 'R2 API token secret'))


def _client():
    account, access_key, secret_key = _r2_config()
    return boto3.client(
        's3',
        endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


def list_remote(client):
    '''Page through the whole bucket once and return {key: size}.

    Replaces the old per-file head_object skip check. On R2 a missing key
    raises a generic ClientError "(404) Not Found" — NOT S3's NoSuchKey — so
    the head-based filter logged a spurious "non-404 error" line for every
    first-time upload (669 such lines in archive-sessions.log by 2026-06-10).
    Listing once and comparing in-memory removes that noise and collapses N
    HEAD round-trips into ceil(N/1000) list pages. The single-instance lock
    guarantees no concurrent writer races this snapshot. Mirrors
    ~/.claude/scripts/archive_sessions.py.
    '''
    remote = {}
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=BUCKET):
        for obj in page.get('Contents', []):
            remote[obj['Key']] = obj['Size']
    return remote


def upload_if_changed(client, local_path, key, remote, dry_run, manifest):
    '''Upload local_path to BUCKET/key. Returns True if an upload happened.

    Policy: text types (_is_text) are always stored xz at `<key>.xz`; binary
    types are stored xz only when compression genuinely shrinks them, else
    plain. Skip when an up-to-date copy already exists — compressed (the
    per-machine `manifest` records local (mtime_ns, size) for the xz key) or
    plain (remote size matches, for an incompressible binary). The stale twin
    is deleted whenever the chosen form flips.
    '''
    try:
        st = local_path.stat()
    except OSError:
        return False

    # The manifest object and already-compressed inputs are stored verbatim.
    if key.endswith('.xz') or key.startswith(MANIFEST_PREFIX + '/'):
        if remote.get(key) == st.st_size:
            return False
        if dry_run:
            log(f'  DRY upload {key} ({st.st_size:,} B)')
            return True
        client.put_object(Bucket=BUCKET, Key=key, Body=local_path.read_bytes())
        remote[key] = st.st_size
        return True

    xz_key = key + '.xz'
    sig = [st.st_mtime_ns, st.st_size]
    if xz_key in remote and manifest.get(xz_key) == sig:
        return False
    if remote.get(key) == st.st_size:
        return False
    if dry_run:
        log(f'  DRY upload {key} ({st.st_size:,} B)')
        return True

    data = local_path.read_bytes()
    comp = lzma.compress(data, preset=XZ_PRESET)
    if _is_text(key) or len(comp) < len(data):
        client.put_object(Bucket=BUCKET, Key=xz_key, Body=comp)
        manifest[xz_key] = sig
        remote[xz_key] = len(comp)
        if key in remote:  # drop a stale uncompressed twin
            try:
                client.delete_object(Bucket=BUCKET, Key=key)
                remote.pop(key, None)
            except Exception as e:
                log(f'  stale-plain delete failed: {key}: {e}')
    else:
        client.put_object(Bucket=BUCKET, Key=key, Body=data)
        remote[key] = st.st_size
        if xz_key in remote:  # drop a stale compressed twin
            try:
                client.delete_object(Bucket=BUCKET, Key=xz_key)
                remote.pop(xz_key, None)
            except Exception as e:
                log(f'  stale-xz delete failed: {xz_key}: {e}')
        manifest.pop(xz_key, None)
    return True


def upload_bytes(client, body, key, remote, dry_run):
    '''In-memory variant of upload_if_changed — skip when stored size matches.'''
    if remote.get(key) == len(body):
        return False
    if dry_run:
        log(f'  DRY upload {key} ({len(body):,} B)')
        return True
    client.put_object(Bucket=BUCKET, Key=key, Body=body)
    return True


def load_project_map():
    '''Map md5(work_dir) -> work_dir from ~/.kimi/kimi.json.

    Source of truth for hash↔path resolution lives on the box that ran kimi-cli;
    R2 only sees session-hash dirs. This map lets us publish a per-session
    marker so downstream consumers (kimi-dash ingest) can resolve hashes back
    to project paths without needing kimi.json themselves.
    '''
    try:
        data = json.loads((KIMI_DIR / 'kimi.json').read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    out = {}
    for w in data.get('work_dirs', []):
        path = w.get('path')
        if not path:
            continue
        out[hashlib.md5(path.encode()).hexdigest()] = path
    return out


def load_kimi_code_workdirs():
    '''Map trailing 12-hex bucket hash -> workDir from ~/.kimi-code/session_index.jsonl.'''
    idx = KIMI_CODE_DIR / 'session_index.jsonl'
    if not idx.is_file():
        return {}
    out = {}
    try:
        for line in idx.read_text(encoding='utf-8').splitlines():
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            sd = e.get('sessionDir', '')
            wd = e.get('workDir')
            if not sd or not wd:
                continue
            m = re.search(r'/sessions/(wd_[^/]+)/', sd.replace('\\', '/'))
            if not m:
                continue
            bucket = m.group(1)
            hm = re.search(r'_([0-9a-f]{12})$', bucket)
            if hm:
                out[hm.group(1)] = wd
    except OSError:
        return {}
    return out


def _workdir_from_state(session_dir: Path) -> str | None:
    '''Derive workDir from a kimi-code session's state.json homedir path.'''
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
    m = re.match(r'^(.*)/sessions/wd_[^/]+/ses_[^/]+/agents/main$', homedir.replace('\\', '/'))
    if m:
        return m.group(1)
    return None


def upload_dir(client, local_dir, prefix, remote, dry_run, manifest):
    '''Upload every file under local_dir with skip-if-size-matches.
    Returns the number of objects actually uploaded (not files visited).'''
    n = 0
    for f in local_dir.rglob('*'):
        if not f.is_file():
            continue
        rel = f.relative_to(local_dir).as_posix()
        if upload_if_changed(client, f, f'{prefix}/{rel}', remote, dry_run, manifest):
            n += 1
    return n


def archive_kimi_code_sessions(client, remote, cutoff, dry_run, manifest):
    '''Archive ~/.kimi-code/sessions/ to R2 using the same key layout as legacy.'''
    n_uploaded = 0
    n_deleted = 0
    n_failed = 0

    if not KC_SESSIONS_DIR.is_dir():
        return 0, 0, 0

    workdirs = load_kimi_code_workdirs()

    for bucket_dir in KC_SESSIONS_DIR.iterdir():
        if not bucket_dir.is_dir():
            continue
        bucket = bucket_dir.name
        hm = re.search(r'_([0-9a-f]{12})$', bucket)
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
            marker_key = f'sessions/{session_hash}/project.json'
            marker_body = json.dumps({'path': path}, ensure_ascii=False).encode()
            try:
                if upload_bytes(client, marker_body, marker_key, remote, dry_run):
                    n_uploaded += 1
            except Exception as e:
                log(f'marker upload failed: hash={session_hash} err={e}')
                n_failed += 1

        for session_dir in bucket_dir.iterdir():
            if not session_dir.is_dir():
                continue
            session_id = session_dir.name
            r2_prefix = f'sessions/{session_hash}/{session_id}'

            # Main agent wire
            main_wire = session_dir / 'agents' / 'main' / 'wire.jsonl'
            if main_wire.is_file():
                try:
                    if upload_if_changed(client, main_wire, f'{r2_prefix}/wire.jsonl', remote, dry_run, manifest):
                        n_uploaded += 1
                except Exception as e:
                    log(f'upload failed: main_wire={main_wire} err={e}')
                    n_failed += 1

            # Subagent wires and blobs
            agents_dir = session_dir / 'agents'
            if agents_dir.is_dir():
                for agent_dir in agents_dir.iterdir():
                    if not agent_dir.is_dir() or agent_dir.name == 'main':
                        continue
                    wire = agent_dir / 'wire.jsonl'
                    if wire.is_file():
                        try:
                            if upload_if_changed(client, wire, f'{r2_prefix}/subagents/{agent_dir.name}/wire.jsonl', remote, dry_run, manifest):
                                n_uploaded += 1
                        except Exception as e:
                            log(f'upload failed: subagent_wire={wire} err={e}')
                            n_failed += 1
                    blobs = agent_dir / 'blobs'
                    if blobs.is_dir():
                        try:
                            n_uploaded += upload_dir(client, blobs, f'{r2_prefix}/subagents/{agent_dir.name}/blobs', remote, dry_run, manifest)
                        except Exception as e:
                            log(f'upload failed: blobs={blobs} err={e}')
                            n_failed += 1

            # Session state
            state = session_dir / 'state.json'
            if state.is_file():
                try:
                    if upload_if_changed(client, state, f'{r2_prefix}/state.json', remote, dry_run, manifest):
                        n_uploaded += 1
                except Exception as e:
                    log(f'upload failed: state={state} err={e}')
                    n_failed += 1

            # Delete gate: newest wire.jsonl mtime in the session
            newest_wire = main_wire if main_wire.is_file() else None
            if agents_dir.is_dir():
                for agent_dir in agents_dir.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    w = agent_dir / 'wire.jsonl'
                    if w.is_file():
                        if newest_wire is None or w.stat().st_mtime > newest_wire.stat().st_mtime:
                            newest_wire = w
            if newest_wire is not None and is_old(newest_wire, cutoff):
                if dry_run:
                    log(f'  DRY rmtree {session_dir}')
                else:
                    shutil.rmtree(session_dir, ignore_errors=True)
                n_deleted += 1

        # If the bucket dir is now empty, remove it
        if not dry_run and bucket_dir.is_dir():
            try:
                if not any(bucket_dir.iterdir()):
                    bucket_dir.rmdir()
            except OSError:
                pass

    return n_uploaded, n_deleted, n_failed


def archive_sessions(client, remote, cutoff, dry_run, manifest):
    n_uploaded = 0
    n_deleted = 0
    n_failed = 0

    if not SESSIONS_DIR.is_dir():
        return 0, 0, 0

    project_map = load_project_map()

    for session_hash_dir in SESSIONS_DIR.iterdir():
        if not session_hash_dir.is_dir():
            continue
        session_hash = session_hash_dir.name

        # Publish marker resolving session_hash -> project path. Downstream
        # ingest (kimi-dash) reads sessions/<HASH>/project.json from R2 and
        # uses `.path` as the project's display name. Idempotent via size-skip.
        path = project_map.get(session_hash)
        if path:
            marker_key = f'sessions/{session_hash}/project.json'
            marker_body = json.dumps({'path': path}, ensure_ascii=False).encode()
            try:
                if upload_bytes(client, marker_body, marker_key, remote, dry_run):
                    n_uploaded += 1
            except Exception as e:
                log(f'marker upload failed: hash={session_hash} err={e}')
                n_failed += 1

        for uuid_dir in session_hash_dir.iterdir():
            if not uuid_dir.is_dir():
                continue
            uuid = uuid_dir.name
            r2_prefix = f'sessions/{session_hash}/{uuid}'

            # Upload every file inside the uuid dir
            try:
                n_uploaded += upload_dir(client, uuid_dir, r2_prefix, remote, dry_run, manifest)
            except Exception as e:
                log(f'upload failed: uuid_dir={uuid_dir} err={e}')
                n_failed += 1
                continue

            # Gate deletion on wire.jsonl age
            wire_jsonl = uuid_dir / 'wire.jsonl'
            if wire_jsonl.exists() and is_old(wire_jsonl, cutoff):
                if dry_run:
                    log(f'  DRY rmtree {uuid_dir}')
                else:
                    shutil.rmtree(uuid_dir, ignore_errors=True)
                n_deleted += 1

        # If the session_hash dir is now empty, remove it
        if not dry_run and session_hash_dir.is_dir():
            try:
                if not any(session_hash_dir.iterdir()):
                    session_hash_dir.rmdir()
            except OSError:
                pass

    return n_uploaded, n_deleted, n_failed


def archive_user_history(client, remote, cutoff, dry_run, manifest):
    n_uploaded = 0
    n_deleted = 0
    n_failed = 0

    if not USER_HISTORY_DIR.is_dir():
        return 0, 0, 0

    for f in USER_HISTORY_DIR.iterdir():
        if not f.is_file() or f.suffix != '.jsonl':
            continue
        session_hash = f.stem
        key = f'user-history/{session_hash}.jsonl'
        try:
            if upload_if_changed(client, f, key, remote, dry_run, manifest):
                n_uploaded += 1
        except Exception as e:
            log(f'upload failed: user-history={f} err={e}')
            n_failed += 1
            continue

        if is_old(f, cutoff):
            if dry_run:
                log(f'  DRY rm {f}')
            else:
                try:
                    f.unlink()
                except OSError as e:
                    log(f'  rm failed: {f}: {e}')
            n_deleted += 1

    return n_uploaded, n_deleted, n_failed


def main():
    for _stream in (sys.stdout, sys.stderr):
        # TextIO does not declare reconfigure, and a redirected or piped stream
        # may genuinely not have it -- so this is a lookup rather than a call
        # inside a bare except, which says the same thing to a reader and to a
        # type checker.
        reconfigure = getattr(_stream, 'reconfigure', None)
        if reconfigure is not None:
            try:
                reconfigure(encoding='utf-8', errors='replace')
            except (ValueError, OSError):
                pass  # not a reconfigurable stream (redirected/piped)
    ap = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n', maxsplit=1)[0])
    ap.add_argument('--days', type=int, default=DAYS,
                    help=f'retention threshold for delete (default {DAYS})')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview only — no uploads, no removals')
    args = ap.parse_args()

    cutoff = time.time() - args.days * 86400

    lock = acquire_lock()
    if lock is None:
        log('skipped: previous run still holds the lock')
        return 0

    try:
        log(f'starting archive-sessions (DAYS={args.days}, dry_run={args.dry_run})')
        cleanup_local(cutoff, args.dry_run)

        client = _client()
        remote = list_remote(client)
        log(f'remote inventory: {len(remote):,} objects in bucket {BUCKET!r}')

        manifest = load_manifest(client)
        log(f'manifest {manifest_key()}: {len(manifest):,} known compressed objects')

        n_up_sess, n_del_sess, n_fail_sess = archive_sessions(client, remote, cutoff, args.dry_run, manifest)
        n_up_hist, n_del_hist, n_fail_hist = archive_user_history(client, remote, cutoff, args.dry_run, manifest)
        n_up_kc, n_del_kc, n_fail_kc = archive_kimi_code_sessions(client, remote, cutoff, args.dry_run, manifest)

        if not args.dry_run:
            try:
                save_manifest(client, manifest, args.dry_run)
            except Exception as e:
                log(f'manifest save failed: {type(e).__name__}: {e}')

        n_up = n_up_sess + n_up_hist + n_up_kc
        n_del = n_del_sess + n_del_hist + n_del_kc
        n_fail = n_fail_sess + n_fail_hist + n_fail_kc

        log(f'done — uploaded={n_up} deleted={n_del} failures={n_fail}')
        return 1 if n_fail else 0
    finally:
        try:
            lock.close()
        except Exception as e:
            log(f'lock.close() failed: {type(e).__name__}: {e}')


if __name__ == '__main__':
    sys.exit(main())
