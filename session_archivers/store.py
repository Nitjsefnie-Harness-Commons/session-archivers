"""How an archiver talks to its bucket: one key layout, one manifest format.

Each archiver keeps its OWN bucket — R2_BUCKET_CLAUDE, R2_BUCKET_KIMI,
R2_BUCKET_CODEX, three separate destinations, and `Store` takes the bucket
name as an argument precisely so nothing here can quietly merge them. What is
shared is the WAY each one is written to, so a dashboard reads all three the
same way.

The three archivers walk three different trees — Claude by project directory,
Kimi by working directory, Codex in date buckets — but everything downstream
of "here is a file, here is the key it belongs under" was identical in all
three, down to the AST. It lives here now.

That duplication was deliberate while these were standalone scripts: each one
was copied onto a machine on its own and had to work with nothing else
present. They install as one package now, so the only thing three copies buy
is three places for a fix to land in two of.

What the destination guarantees, and why a dashboard can read all three the
same way:

  * KEY LAYOUT is the archiver's business, not this module's — it takes the
    key it is given.
  * COMPRESSION is uniform. Text types are always stored xz at `<key>.xz`;
    every other type only when xz genuinely shrinks it. Readers inflate `.xz`
    keys transparently, with no third-party dependency, because lzma is
    stdlib.
  * The MANIFEST is per machine, `manifests/<machine-hash>.json`, plain JSON,
    never compressed. A compressed object's remote size cannot be compared
    against the plain local file to decide "already uploaded, unchanged", so
    each machine records `{store_key: [mtime_ns, size]}` instead. N machines
    write N manifests and never clobber each other.
"""

import hashlib
import json
import lzma
import socket
from pathlib import Path

import boto3
from botocore.config import Config

from .settings import required, setting

XZ_PRESET = 9
"""Favours ratio over speed. The per-run volume is recently-active sessions
only, so the CPU cost is bounded."""

TEXT_SUFFIXES = ('.jsonl', '.txt', '.json', '.js')
"""Always stored compressed, even when tiny — uniformity is worth more to a
reader than the handful of bytes a small file loses."""

MANIFEST_PREFIX = 'manifests'


def is_text(key):
    return key.endswith(TEXT_SUFFIXES)


def machine_hash():
    '''Stable opaque id for this machine: sha256 of /etc/machine-id (Linux)
    or the hostname (other OSes), truncated.

    Hashed so the manifest object name does not leak the raw hostname.
    '''
    mid = ''
    for path in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
        try:
            mid = Path(path).read_text(encoding='utf-8').strip()
        except OSError:
            mid = ''
        if mid:
            break
    if not mid:
        mid = socket.gethostname()
    return hashlib.sha256(mid.encode('utf-8')).hexdigest()[:16]


def manifest_key():
    return f'{MANIFEST_PREFIX}/{machine_hash()}.json'


def r2_config():
    """(account_id, access_key, secret_key) — resolved at CALL time.

    Lazily, so importing an archiver — which tests, tooling and `--help` all
    do — never demands credentials; only an operation that actually talks to
    R2 does. The account falls back to CLOUDFLARE_ACCOUNT_ID because it IS
    that account: one value, not two that can drift apart.

    `required` raises naming the exact missing key, because a half-configured
    archiver that silently uploads nowhere is worse than one that refuses to
    start.
    """
    account = setting('R2_ACCOUNT_ID') or required(
        'CLOUDFLARE_ACCOUNT_ID', 'the Cloudflare account that owns the R2 bucket')
    return (account,
            required('R2_ACCESS_KEY_ID', 'R2 API token access key'),
            required('R2_SECRET_ACCESS_KEY', 'R2 API token secret'))


def client():
    '''The boto3 S3 client for this account's R2 endpoint.

    The one place a real client is constructed — which is what lets the suite
    exercise everything below against a stub and still never reach the
    network.
    '''
    account, access_key, secret_key = r2_config()
    return boto3.client(
        's3',
        endpoint_url=f'https://{account}.r2.cloudflarestorage.com',
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


class Store:
    '''One archiver's view of the bucket for the length of one run.

    Holds the four things every upload needs — the client, the bucket, the
    inventory snapshot and the manifest — so they stop being four arguments
    threaded through every call. `dry_run` is in here for the same reason: it
    is fixed for the whole run.

    The inventory is a snapshot, taken once. That is safe because the
    single-instance lock guarantees no concurrent writer, and it is the point:
    one list beats N HEADs, and on R2 a missing key raises a generic
    ClientError rather than S3's NoSuchKey, which made the old head-based
    check log a spurious error for every first-time upload.
    '''

    def __init__(self, client, bucket, log, dry_run=False):
        # pylint: disable=redefined-outer-name
        self.client = client
        self.bucket = bucket
        self.log = log
        self.dry_run = dry_run
        self.remote = {}
        self.manifest = {}

    # --- inventory and manifest ---------------------------------------------

    def inventory(self):
        '''Page through the whole bucket once; fill and return {key: size}.'''
        remote = {}
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket):
            for obj in page.get('Contents', []):
                remote[obj['Key']] = obj['Size']
        self.remote = remote
        return remote

    def load_manifest(self):
        '''Fetch this machine's manifest. Absent (first run) or unreadable →
        empty, which costs one re-upload and never a failed run.'''
        try:
            body = self.client.get_object(
                Bucket=self.bucket, Key=manifest_key())['Body'].read()
            manifest = json.loads(body)
        except Exception:  # pylint: disable=broad-except
            manifest = {}
        self.manifest = manifest if isinstance(manifest, dict) else {}
        return self.manifest

    def save_manifest(self):
        key = manifest_key()
        if self.dry_run:
            self.log(f'  DRY put manifest {key} ({len(self.manifest):,} entries)')
            return
        body = json.dumps(self.manifest, separators=(',', ':')).encode('utf-8')
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
        self.log(f'manifest saved: {key} ({len(self.manifest):,} entries)')

    def holds(self, key):
        '''True when the bucket has this object in either storage form.

        The check a retention gate makes before deleting a local file: a
        failed upload must never take the only copy with it.
        '''
        return key in self.remote or key + '.xz' in self.remote

    # --- uploads -------------------------------------------------------------

    def upload_file(self, local_path, key):
        '''Upload `local_path` under `key`. True when an upload happened.

        Skips when an up-to-date copy already exists — compressed (the
        manifest signature matches) or plain (the remote size matches). The
        stale twin is deleted whenever the chosen form flips.
        '''
        try:
            st = local_path.stat()
        except OSError:
            # It vanished between the walk and here; a live session directory
            # changes under the archiver mid-run.
            return False

        if key.endswith('.xz') or key.startswith(MANIFEST_PREFIX + '/'):
            return self._upload_verbatim(local_path, key, st)

        xz_key = key + '.xz'
        sig = [st.st_mtime_ns, st.st_size]
        if xz_key in self.remote and self.manifest.get(xz_key) == sig:
            return False
        if self.remote.get(key) == st.st_size:
            return False
        if self.dry_run:
            self.log(f'  DRY upload {key} ({st.st_size:,} B)')
            return True

        data = local_path.read_bytes()
        comp = lzma.compress(data, preset=XZ_PRESET)
        if is_text(key) or len(comp) < len(data):
            self._store_compressed(key, xz_key, comp, sig)
        else:
            self._store_plain(key, xz_key, data)
        return True

    def upload_bytes(self, body, key):
        '''In-memory variant — for markers and other generated objects.'''
        if self.remote.get(key) == len(body):
            return False
        if self.dry_run:
            self.log(f'  DRY upload {key} ({len(body):,} B)')
            return True
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
        self.remote[key] = len(body)
        return True

    def upload_dir(self, local_dir, prefix):
        '''Upload every file under `local_dir`, keyed by its POSIX-spelled
        relative path so the keys are identical whichever OS uploaded them.

        Returns the number of objects uploaded, not files visited.
        '''
        uploaded = 0
        for path in local_dir.rglob('*'):
            if not path.is_file():
                continue
            rel = path.relative_to(local_dir).as_posix()
            if self.upload_file(path, f'{prefix}/{rel}'):
                uploaded += 1
        return uploaded

    # --- storage forms -------------------------------------------------------

    def _upload_verbatim(self, local_path, key, st):
        '''Store an object exactly as it sits on disk.

        For inputs that are already compressed — re-compressing would
        double-wrap them — and for the manifest, which is read back as plain
        JSON and must never be xz'd.
        '''
        if self.remote.get(key) == st.st_size:
            return False
        if self.dry_run:
            self.log(f'  DRY upload {key} ({st.st_size:,} B)')
            return True
        self.client.put_object(Bucket=self.bucket, Key=key,
                               Body=local_path.read_bytes())
        self.remote[key] = st.st_size
        return True

    def _store_compressed(self, key, xz_key, comp, sig):
        self.client.put_object(Bucket=self.bucket, Key=xz_key, Body=comp)
        self.manifest[xz_key] = sig
        self.remote[xz_key] = len(comp)
        self._drop_stale_twin(key)

    def _store_plain(self, key, xz_key, data):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        self.remote[key] = len(data)
        self._drop_stale_twin(xz_key)
        self.manifest.pop(xz_key, None)

    def _drop_stale_twin(self, key):
        '''Delete the other storage form, if the bucket has one.

        The chosen form can flip between runs — a file grows past the point
        where xz helps — and leaving both behind makes ingest see the same
        transcript under two keys. Losing this delete costs a duplicate;
        losing the upload would cost data, so a failure here is logged and
        the upload still counts.
        '''
        if key not in self.remote:
            return
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            self.remote.pop(key, None)
        except Exception as e:  # pylint: disable=broad-except
            self.log(f'  stale-twin delete failed: {key}: {e}')
