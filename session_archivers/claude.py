#!/usr/bin/env python3
'''Archive Claude sessions to R2 + sweep stale ~/.claude scratch dirs.

Mirrors /root/.claude/cleanup-sessions.sh on Linux for Windows. Designed to
run hourly via Task Scheduler. Self-contained — credentials inline.

Behaviour per run:
1. Acquire single-instance lock (msvcrt on Windows, fcntl elsewhere).
   If a previous run still holds it, exit silently.
2. Sweep claude-local stale paths (files / dirs older than DAYS):
   ~/.claude/debug/*, ~/.claude/file-history/*/, ~/.claude/telemetry/*
3. For every project under ~/.claude/projects/<project>/:
     Pass 1 — each *.jsonl plus its matching UUID data dir:
       upload (size-skip, idempotent), then delete locally if jsonl mtime > DAYS.
     Pass 2 — orphan UUID dirs (no matching jsonl, excluding memory/, tasks/):
       upload always, delete if dir mtime > DAYS.
4. Log a one-liner summary to ~/.claude/cleanup-sessions.log.

Flags:
  --days N    retention threshold for the delete gate (default 3)
  --dry-run   preview every action; no R2 puts, no local removals

Exit code: 0 on success, 1 if any upload failed (mirrors the bash script).
'''

import argparse
import hashlib
import json
import lzma
import shutil
import socket
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config

from .settings import required, setting

# R2 target. Credentials and account live in the `env` block of
# ~/.agent-bundle/settings.json, never here — this file ships in the bundle and is
# mirrored to git, so a literal key travels with the code to every machine that
# installs it. `required` raises with the exact key name when one is missing,
# because a half-configured archiver that silently uploads nowhere is worse
# than one that refuses to start. Resolved lazily (see _r2_config) so importing
# this module — which the test suite does — never demands credentials.
BUCKET = setting('R2_BUCKET_CLAUDE', 'claude')

CLAUDE_DIR = Path.home() / '.claude'
PROJECTS_DIR = CLAUDE_DIR / 'projects'
DEBUG_DIR = CLAUDE_DIR / 'debug'
FILE_HISTORY_DIR = CLAUDE_DIR / 'file-history'
TELEMETRY_DIR = CLAUDE_DIR / 'telemetry'

LOCK_FILE = CLAUDE_DIR / 'cleanup-sessions.lock'
LOG_FILE = CLAUDE_DIR / 'cleanup-sessions.log'

DAYS = 3  # rebound from CLI in main()

# JSONL transcripts are stored xz-compressed per-object (`<name>.jsonl.xz`).
# xz is stdlib (`lzma`) so every reader (ccudash, parse_session.py) inflates
# them with no third-party dependency. preset 9 favours ratio; the per-run
# volume is just recently-active sessions, so the CPU cost is bounded.
XZ_PRESET = 9
# Compression policy: text types are ALWAYS stored xz (they compress well,
# even tiny ones we accept storing slightly larger for uniformity); every
# other (binary) type is compressed only if xz actually shrinks it, else it
# stays plain. The per-machine manifest is plain JSON read back verbatim —
# never compress it (handled in upload_if_changed).
TEXT_SUFFIXES = ('.jsonl', '.txt', '.json', '.js')


def _is_text(key):
    return key.endswith(TEXT_SUFFIXES)
# Per-machine upload manifest object. The remote size of a compressed object
# is the COMPRESSED size, so it can't be compared against a plain local file
# to decide "already uploaded, unchanged". Instead each machine keeps a synced
# manifest object {store_key: [mtime_ns, size]} under a hashed-machine-id name,
# so N machines write N manifests and never clobber each other.
MANIFEST_PREFIX = 'manifests'


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
    '''Open the lockfile and try a non-blocking exclusive lock. Return the
    file handle on success (caller keeps it alive until exit), or None.
    OS releases the lock automatically on process exit / handle close.
    '''
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open('a+b')
    try:
        # Need at least 1 byte to lock on Windows.
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
    '''Delete ~/.claude/{debug/*, file-history/*/, telemetry/*} older than DAYS.'''
    # debug — files
    if DEBUG_DIR.is_dir():
        for f in DEBUG_DIR.iterdir():
            if f.is_file() and is_old(f, cutoff):
                if dry_run:
                    log(f'  DRY rm {f}')
                else:
                    try:
                        f.unlink()
                    except OSError as e:
                        log(f'  rm failed: {f}: {e}')

    # file-history — top-level dirs
    if FILE_HISTORY_DIR.is_dir():
        for d in FILE_HISTORY_DIR.iterdir():
            if d.is_dir() and is_old(d, cutoff):
                if dry_run:
                    log(f'  DRY rmtree {d}')
                else:
                    shutil.rmtree(d, ignore_errors=True)

    # telemetry — files
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


def _machine_hash():
    '''Stable opaque id for this machine: sha256 of /etc/machine-id (Linux)
    or the hostname (other OSes), truncated. Hashing keeps the manifest object
    name from leaking the raw hostname.'''
    mid = ''
    for p in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            mid = Path(p).read_text().strip()
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
    key = manifest_key()
    try:
        body = client.get_object(Bucket=BUCKET, Key=key)['Body'].read()
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


def _r2_config():
    """(account_id, access_key, secret_key) — resolved at CALL time, not import.

    Lazily, so importing this module (tests, tooling, `--help`) never requires
    credentials; only an operation that actually talks to R2 does. The account
    falls back to CLOUDFLARE_ACCOUNT_ID because it IS that account — one value,
    not two that can drift apart."""
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
    the head-based filter logged a spurious line for every first-time upload.
    Listing once and comparing in-memory removes that noise and collapses N
    HEAD round-trips into ceil(N/1000) list pages. The single-instance lock
    guarantees no concurrent writer races this snapshot.
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
    per-machine `manifest` records local (mtime_ns, size) for the xz key, since
    a compressed object's remote size can't be compared to the plain local
    size) or plain (remote size matches, for an incompressible binary). The
    stale twin (the other form) is deleted whenever the chosen form flips.
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
    # Skip when an up-to-date copy already exists — compressed (manifest match)
    # or plain (an incompressible binary stored as-is last run).
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
        # text: always xz; binary: only because it genuinely shrank.
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
        # binary that does not shrink → store plain.
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


def upload_dir(client, local_dir, prefix, remote, dry_run, manifest):
    '''Upload every file under local_dir with skip-if-size-matches.
    Returns the number of objects actually uploaded (not files visited).
    '''
    n = 0
    for f in local_dir.rglob('*'):
        if not f.is_file():
            continue
        rel = f.relative_to(local_dir).as_posix()
        if upload_if_changed(client, f, f'{prefix}/{rel}', remote, dry_run, manifest):
            n += 1
    return n


def archive_projects(client, remote, cutoff, dry_run, manifest):
    n_uploaded = 0
    n_deleted = 0
    n_failed = 0
    if not PROJECTS_DIR.is_dir():
        return 0, 0, 0

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project = project_dir.name

        # Pass 1: each jsonl + matching UUID dir
        jsonl_stems = set()
        for jsonl in project_dir.glob('*.jsonl'):
            uuid = jsonl.stem
            jsonl_stems.add(uuid)
            r2_prefix = f'{project}/{uuid}'
            uuid_dir = project_dir / uuid

            try:
                if upload_if_changed(client, jsonl, f'{r2_prefix}/{uuid}.jsonl', remote, dry_run, manifest):
                    n_uploaded += 1
            except Exception as e:
                log(f'upload failed: jsonl={jsonl} err={e}')
                n_failed += 1
                continue

            if uuid_dir.is_dir():
                try:
                    n_uploaded += upload_dir(client, uuid_dir, f'{r2_prefix}/data', remote, dry_run, manifest)
                except Exception as e:
                    log(f'upload failed: uuid_dir={uuid_dir} err={e} '
                        '(jsonl uploaded; not deleting either)')
                    n_failed += 1
                    continue

            if is_old(jsonl, cutoff):
                if dry_run:
                    log(f'  DRY rm {jsonl}')
                    if uuid_dir.is_dir():
                        log(f'  DRY rmtree {uuid_dir}')
                else:
                    try:
                        jsonl.unlink()
                    except OSError as e:
                        log(f'  rm failed: {jsonl}: {e}')
                        continue
                    if uuid_dir.is_dir():
                        shutil.rmtree(uuid_dir, ignore_errors=True)
                n_deleted += 1

        # Pass 2: orphan UUID dirs (no matching jsonl)
        for sub in project_dir.iterdir():
            if not sub.is_dir():
                continue
            if sub.name in ('memory', 'tasks'):
                continue
            if sub.name in jsonl_stems:
                continue  # already handled in pass 1
            try:
                n_uploaded += upload_dir(client, sub, f'{project}/{sub.name}/data', remote, dry_run, manifest)
            except Exception as e:
                log(f'upload failed: orphan uuid_dir={sub} err={e}')
                n_failed += 1
                continue
            if is_old(sub, cutoff):
                if dry_run:
                    log(f'  DRY rmtree {sub}')
                else:
                    shutil.rmtree(sub, ignore_errors=True)
                n_deleted += 1

    return n_uploaded, n_deleted, n_failed


def main():
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a reconfigurable stream (redirected/piped)
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
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
        log(f'starting cleanup-sessions (DAYS={args.days}, dry_run={args.dry_run})')
        cleanup_local(cutoff, args.dry_run)

        client = _client()
        remote = list_remote(client)
        log(f'remote inventory: {len(remote):,} objects in bucket {BUCKET!r}')

        manifest = load_manifest(client)
        log(f'manifest {manifest_key()}: {len(manifest):,} known jsonl objects')

        n_up, n_del, n_fail = archive_projects(client, remote, cutoff, args.dry_run, manifest)

        if not args.dry_run:
            try:
                save_manifest(client, manifest, args.dry_run)
            except Exception as e:
                log(f'manifest save failed: {type(e).__name__}: {e}')

        log(f'done — uploaded={n_up} deleted={n_del} failures={n_fail}')
        return 1 if n_fail else 0
    finally:
        try:
            lock.close()
        except Exception as e:
            log(f'lock.close() failed: {type(e).__name__}: {e}')


if __name__ == '__main__':
    sys.exit(main())
