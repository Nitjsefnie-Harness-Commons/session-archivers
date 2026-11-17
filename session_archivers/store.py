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
    never compressed. It records `{store_key: [mtime_ns, size]}`, the local
    file's signature when it was stored, for every object an upload stores in
    any form (the manifest itself excepted). A remote size cannot decide
    "already uploaded, unchanged": a compressed object's size is not the
    file's, and a plain one of the same size can hold older bytes. So an
    object counts as current, for skipping an upload and for `holds`, only
    while its recorded signature matches the file. N machines write N
    manifests and never clobber each other.
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


def _signature(st):
    '''What the manifest records for a stored object: the local file's
    `[mtime_ns, size]` when it was stored (a list, as JSON reads it back).'''
    return [st.st_mtime_ns, st.st_size]


def _stored_forms(key):
    '''Every key `upload_file(path, key)` can store under: an already-.xz
    input verbatim, anything else compressed or plain.'''
    return (key,) if key.endswith('.xz') else (key + '.xz', key)


def _dir_keys(local_dir, prefix):
    '''(path, key) for every file under `local_dir`, keyed by its
    POSIX-spelled relative path so the keys are identical whichever OS
    uploaded them.'''
    for path in local_dir.rglob('*'):
        if path.is_file():
            yield path, f'{prefix}/{path.relative_to(local_dir).as_posix()}'


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
        # Keys a dry run reported it would upload; never filled in a real run.
        self._planned = set()

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

    def holds(self, local_path, key):
        '''True when the bucket holds THIS file's current bytes under `key`,
        in either storage form.

        The check a retention gate makes before deleting a local file: a
        failed upload must never take the only copy with it, and an object
        of the same size may still hold older bytes. So a stored form counts
        only while the manifest records this file's current signature for
        it. A file that vanished proves nothing and reads False. In a dry
        run, a key this run reported it would upload counts too, so the
        preview shows the deletions a real run would make.
        '''
        try:
            st = local_path.stat()
        except OSError:
            return False
        return key in self._planned or self._held(key, _signature(st))

    def holds_dir(self, local_dir, prefix):
        '''`holds` for every file `upload_dir(local_dir, prefix)` uploads.'''
        return all(self.holds(path, key)
                   for path, key in _dir_keys(local_dir, prefix))

    def _held(self, key, sig):
        return any(form in self.remote and self.manifest.get(form) == sig
                   for form in _stored_forms(key))

    # --- uploads -------------------------------------------------------------

    def upload_file(self, local_path, key):
        '''Upload `local_path` under `key`. True when an upload happened.

        Skips when a stored form of `key` carries this file's current
        manifest signature; a size match alone never skips. The stale twin
        is deleted whenever the chosen form flips.
        '''
        try:
            st = local_path.stat()
        except OSError:
            # It vanished between the walk and here; a live session directory
            # changes under the archiver mid-run.
            return False

        if key.startswith(MANIFEST_PREFIX + '/'):
            return self._upload_verbatim(local_path, key, st, None)
        sig = _signature(st)
        if self._held(key, sig):
            return False
        if key.endswith('.xz'):
            return self._upload_verbatim(local_path, key, st, sig)
        if self.dry_run:
            self._preview(key, st.st_size)
            return True

        xz_key = key + '.xz'
        data = local_path.read_bytes()
        comp = lzma.compress(data, preset=XZ_PRESET)
        if is_text(key) or len(comp) < len(data):
            self._store_compressed(key, xz_key, comp, sig)
        else:
            self._store_plain(key, xz_key, data, sig)
        return True

    def _preview(self, key, size):
        '''The dry-run line for a key `upload_file` would compress or not.

        Only text is always xz; for anything else the form is decided by
        compressing it, which a preview does not do.
        '''
        self._planned.add(key)
        if is_text(key):
            self.log(f'  DRY upload {key}.xz ({size:,} B uncompressed; '
                     'compressed size not computed)')
        else:
            self.log(f'  DRY upload {key} or {key}.xz ({size:,} B '
                     'uncompressed; stored form undetermined until upload)')

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
        for path, key in _dir_keys(local_dir, prefix):
            if self.upload_file(path, key):
                uploaded += 1
        return uploaded

    # --- storage forms -------------------------------------------------------

    def _upload_verbatim(self, local_path, key, st, sig):
        '''Store an object exactly as it sits on disk.

        For inputs that are already compressed — re-compressing would
        double-wrap them — and for the manifest, which is read back as plain
        JSON and must never be xz'd. The manifest passes no `sig`: it records
        no signature for itself, so it is written every time it is asked.
        '''
        if self.dry_run:
            self.log(f'  DRY upload {key} ({st.st_size:,} B)')
            self._planned.add(key)
            return True
        body = local_path.read_bytes()
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
        self.remote[key] = len(body)
        if sig is not None:
            self.manifest[key] = sig
        return True

    def _store_compressed(self, key, xz_key, comp, sig):
        self.client.put_object(Bucket=self.bucket, Key=xz_key, Body=comp)
        self.manifest[xz_key] = sig
        self.remote[xz_key] = len(comp)
        self._drop_stale_twin(key)

    def _store_plain(self, key, xz_key, data, sig):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        self.manifest[key] = sig
        self.remote[key] = len(data)
        self._drop_stale_twin(xz_key)

    def _drop_stale_twin(self, key):
        '''Delete the other storage form, if the bucket has one.

        The chosen form can flip between runs — a file grows past the point
        where xz helps — and leaving both behind makes ingest see the same
        transcript under two keys. Losing this delete costs a duplicate;
        losing the upload would cost data, so a failure here is logged and
        the upload still counts. The twin's signature goes either way: its
        bytes are no longer the file's.
        '''
        self.manifest.pop(key, None)
        if key not in self.remote:
            return
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            self.remote.pop(key, None)
        except Exception as e:  # pylint: disable=broad-except
            self.log(f'  stale-twin delete failed: {key}: {e}')
