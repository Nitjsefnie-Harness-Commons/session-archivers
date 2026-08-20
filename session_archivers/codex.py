#!/usr/bin/env python3
'''Archive Codex CLI rollouts to R2. Third sibling of the Claude and Kimi
archivers (~/.claude/scripts/archive_sessions.py,
~/.kimi-code/scripts/archive_sessions.py), and deliberately not a new design:
the key layout, the compression policy, the per-machine manifest, the
single-instance lock and the retention gate are all theirs.

Designed to run hourly via cron. Behaviour per run:
1. Acquire single-instance lock (fcntl, Windows msvcrt fallback). If a
   previous run still holds it, exit silently.
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

__version__ = '1.1.0'
# Cross-platform: must work on Linux AND Windows. No POSIX-only calls without
# a Windows fallback. Bump __version__ (SemVer) on every substantive change.

import argparse
import hashlib
import json
import lzma
import socket
import sys
import time
from collections import Counter
from pathlib import Path

import boto3
from botocore.config import Config

# Deployment values come from the neutral bundle settings helper, never from
# literals here — same contract as the two sibling archivers, so all three
# read their credentials from one place.
#
# The helper ships with the agent bundle, NOT with this repo, so a clean
# checkout — CI, a contributor's laptop — does not have it. Importing it
# unconditionally made this module unimportable there, which took the whole
# key-derivation test suite down with it. The fallback below reads the same
# names straight from the environment, so the module imports everywhere and
# a deployment that has the bundle still reads one config file.
from .settings import required, setting

BUCKET = setting('R2_BUCKET_CODEX', 'codex')

CODEX_DIR = Path.home() / '.codex'
SESSIONS_DIR = CODEX_DIR / 'sessions'
LOCK_FILE = CODEX_DIR / 'archive-codex-sessions.lock'
LOG_FILE = CODEX_DIR / 'archive-codex-sessions.log'

DAYS = 3

# Compression policy, mirroring both siblings: text types are ALWAYS stored
# xz at <key>.xz; other types only when xz actually shrinks them. Rollouts
# are .jsonl, so in practice every one of them lands compressed — which is
# the whole reason backend/r2.py inflates `.xz` keys transparently on read.
XZ_PRESET = 9
TEXT_SUFFIXES = ('.jsonl', '.txt', '.json', '.js')
MANIFEST_PREFIX = 'manifests'


def _is_text(key):
    return key.endswith(TEXT_SUFFIXES)


def _machine_hash():
    '''Stable opaque id for this machine: sha256 of /etc/machine-id (Linux)
    or the hostname (other OSes), truncated.'''
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
        import msvcrt  # pylint: disable=import-outside-toplevel
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl  # pylint: disable=import-outside-toplevel
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


def _r2_config():
    '''(account_id, access_key, secret_key) — resolved at CALL time, not
    import, so importing this module never demands credentials.'''
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


def load_manifest(client):
    '''This machine's upload manifest {store_key: [mtime_ns, size]}.
    Absent (first run) or unreadable → empty dict.'''
    try:
        body = client.get_object(Bucket=BUCKET, Key=manifest_key())['Body'].read()
        manifest = json.loads(body)
    except Exception:  # pylint: disable=broad-except
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


def list_remote(client):
    '''Page through the whole bucket once and return {key: size}.

    One list beats N HEADs, and on R2 a missing key raises a generic
    ClientError rather than NoSuchKey, which made the head-based check log a
    spurious error for every first-time upload. The single-instance lock
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

    Text types are always stored xz at `<key>.xz`; binary types only when
    compression genuinely shrinks them. Skip when an up-to-date copy already
    exists — compressed (the manifest records local (mtime_ns, size) for the
    xz key) or plain (remote size matches). The stale twin is deleted
    whenever the chosen form flips.
    '''
    try:
        st = local_path.stat()
    except OSError:
        return False

    if key.endswith('.xz') or key.startswith(MANIFEST_PREFIX + '/'):
        return _upload_verbatim(client, local_path, key, remote, dry_run, st)

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
        _store_compressed(client, key, xz_key, comp, remote, manifest, sig)
    else:
        _store_plain(client, key, xz_key, data, remote, manifest)
    return True


def _upload_verbatim(client, local_path, key, remote, dry_run, st):
    '''Store an object exactly as it sits on disk.

    For inputs that are already compressed, and for the manifest — which is
    read back as plain JSON and must never be xz'd.
    '''
    if remote.get(key) == st.st_size:
        return False
    if dry_run:
        log(f'  DRY upload {key} ({st.st_size:,} B)')
        return True
    client.put_object(Bucket=BUCKET, Key=key, Body=local_path.read_bytes())
    remote[key] = st.st_size
    return True


def _drop_stale_twin(client, key, remote):
    '''Delete the other storage form of an object, if the bucket has one.

    The chosen form can flip between runs (a file grows past the point
    where xz helps), and leaving both behind would make ingest see the same
    transcript under two keys.
    '''
    if key not in remote:
        return
    try:
        client.delete_object(Bucket=BUCKET, Key=key)
        remote.pop(key, None)
    except Exception as e:  # pylint: disable=broad-except
        log(f'  stale-twin delete failed: {key}: {e}')


def _store_compressed(client, key, xz_key, comp, remote, manifest, sig):
    client.put_object(Bucket=BUCKET, Key=xz_key, Body=comp)
    manifest[xz_key] = sig
    remote[xz_key] = len(comp)
    _drop_stale_twin(client, key, remote)


def _store_plain(client, key, xz_key, data, remote, manifest):
    client.put_object(Bucket=BUCKET, Key=key, Body=data)
    remote[key] = len(data)
    _drop_stale_twin(client, xz_key, remote)
    manifest.pop(xz_key, None)


def upload_bytes(client, body, key, remote, dry_run):
    '''In-memory variant of upload_if_changed — skip when stored size matches.'''
    if remote.get(key) == len(body):
        return False
    if dry_run:
        log(f'  DRY upload {key} ({len(body):,} B)')
        return True
    client.put_object(Bucket=BUCKET, Key=key, Body=body)
    remote[key] = len(body)
    return True


def find_rollouts():
    '''Every rollout on this machine, oldest path first.'''
    if not SESSIONS_DIR.is_dir():
        return []
    return sorted(SESSIONS_DIR.glob('**/rollout-*.jsonl'))


def _upload_markers(client, projects, remote, dry_run):
    '''One sessions/<hash>/project.json per project seen this run.

    ingest reads it for the project's display name; without it the
    dashboard shows a 12-hex hash.
    '''
    uploaded = failed = 0
    for phash, cwd in sorted(projects.items()):
        key = f'sessions/{phash}/project.json'
        body = json.dumps({'path': cwd}, ensure_ascii=False).encode('utf-8')
        try:
            if upload_bytes(client, body, key, remote, dry_run):
                uploaded += 1
        except Exception as e:  # pylint: disable=broad-except
            log(f'marker upload failed: hash={phash} err={e}')
            failed += 1
    return uploaded, failed


def _retire_local(path, key, remote, cutoff, dry_run):
    '''Delete a local rollout once it is safely in the bucket and stale.

    The bucket check is not belt-and-braces: a failed upload must never
    take the only copy with it.
    '''
    if not is_old(path, cutoff):
        return False
    if key + '.xz' not in remote and key not in remote:
        return False
    if dry_run:
        log(f'  DRY rm {path}')
        return False
    try:
        path.unlink()
        return True
    except OSError as e:
        log(f'  rm failed: {path}: {e}')
        return False


def archive_rollouts(client, remote, cutoff, dry_run, manifest):
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
            if upload_if_changed(client, path, key, remote, dry_run, manifest):
                tally['uploaded'] += 1
        except Exception as e:  # pylint: disable=broad-except
            log(f'upload failed: {path} err={e}')
            tally['failed'] += 1
            continue
        if _retire_local(path, key, remote, cutoff, dry_run):
            tally['deleted'] += 1

    m_up, m_failed = _upload_markers(client, projects, remote, dry_run)
    return (tally['uploaded'] + m_up, tally['deleted'],
            tally['failed'] + m_failed, tally['skipped'])


def main():
    ap = argparse.ArgumentParser(
        description=(__doc__ or '').split('\n', maxsplit=1)[0])
    ap.add_argument('--days', type=int, default=DAYS,
                    help='retention threshold for the delete gate')
    ap.add_argument('--dry-run', action='store_true',
                    help='preview every action; no R2 puts, no local removals')
    args = ap.parse_args()

    lock = acquire_lock()
    if lock is None:
        return 0  # a previous run still holds it

    try:
        cutoff = time.time() - args.days * 86400
        client = _client()
        remote = list_remote(client)
        manifest = load_manifest(client)
        log(f'remote inventory: {len(remote):,} objects in bucket {BUCKET!r}')

        uploaded, deleted, failed, skipped = archive_rollouts(
            client, remote, cutoff, args.dry_run, manifest
        )
        if uploaded and not args.dry_run:
            save_manifest(client, manifest, args.dry_run)
        log(f'done: uploaded={uploaded} deleted={deleted} '
            f'failed={failed} skipped={skipped}')
        return 1 if failed else 0
    finally:
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
