"""Behaviour suite for the R2 upload paths, against a stubbed S3 client.

`test_archiver_behaviour.py` covers what can be checked without a client at
all -- key construction, the deletion predicate, content-type classification,
settings resolution. Everything downstream of `_client()` was left unexecuted:
the manifest round-trip, the bucket inventory, the compression policy, the
three archive walks and the `main()` drivers, which together are most of the
package.

This suite executes them, and still never touches the network. `_client()` is
the only place a real boto3 client is built, so every function below takes the
client as an argument and a stub satisfying the four calls they actually make
-- get_object, put_object, delete_object and get_paginator('list_objects_v2')
-- exercises the whole path. The two `main()` tests replace `_client` itself.

What is asserted is the VALUE: which key an object landed under, what bytes
came back out of it, which local file survived the retention gate, what the
counters returned. A stub can make "an upload happened" true without the
upload being right, so "a call was made" is never the assertion on its own.

The module-level constants (BUCKET, the ~/.claude and ~/.kimi directories,
LOCK_FILE, LOG_FILE) are resolved at import, so each test rebinds the ones it
needs onto a temp dir and restores them afterwards -- see `sandbox`. A test
that forgot would archive the real session history of whoever ran it.
"""
# pylint: disable=protected-access
# Same reason as the sibling suite: these archivers are scripts. Their
# helpers are module-private because nothing imports them, not because
# there is a public API in front of them.
# pylint: disable=too-few-public-methods
# The stub's helper classes stand in for boto3 objects that genuinely have
# one method each -- a response Body you read(), a paginator you paginate().
# Giving them a second method to satisfy a counter would mean stubbing an
# operation the archivers never call.
import contextlib
import hashlib
import io
import json
import lzma
import os
import sys
import time
from pathlib import Path

import _util

_CLAUDE = _util.load_package_module("claude")
_CODEX = _util.load_package_module("codex")
_KIMI = _util.load_package_module("kimi")

_MODULES = (("claude", _CLAUDE), ("codex", _CODEX), ("kimi", _KIMI))

_BUCKET = "test-bucket"


# --- the stub ---------------------------------------------------------------

class _Body:
    """What boto3 hands back under the 'Body' key: a stream you read once."""

    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _Paginator:
    def __init__(self, client, page_size):
        self._client = client
        self._page_size = page_size

    def paginate(self, **kwargs):
        if kwargs.get("Bucket") != self._client.bucket:
            raise AssertionError(f"listed the wrong bucket: {kwargs!r}")
        keys = sorted(self._client.objects)
        if not keys:
            # A real paginator yields one page with no Contents key at all
            # for an empty bucket, which is the branch list_remote guards.
            yield {}
            return
        for start in range(0, len(keys), self._page_size):
            chunk = keys[start:start + self._page_size]
            yield {"Contents": [{"Key": k, "Size": len(self._client.objects[k])}
                                for k in chunk]}


class FakeS3:
    """In-memory stand-in for the boto3 S3 client the archivers construct.

    Deliberately only the four operations the archivers call. A stub that
    accepted anything would let a call to a method R2 does not have pass here
    and fail in production.

    `fail_puts` / `fail_deletes` hold keys whose operation raises, so the
    error branches (which log and count a failure) are reachable.
    """

    def __init__(self, bucket=_BUCKET, page_size=2):
        self.bucket = bucket
        self.objects = {}
        self.puts = []
        self.deletes = []
        self.fail_puts = set()
        self.fail_deletes = set()
        self._page_size = page_size

    def _check_bucket(self, bucket):
        if bucket != self.bucket:
            raise AssertionError(f"wrong bucket: {bucket!r}")

    def get_object(self, Bucket, Key):
        self._check_bucket(Bucket)
        if Key not in self.objects:
            # R2 raises a generic ClientError here, not NoSuchKey -- the
            # archivers only ever catch Exception, so the type is free.
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body):
        self._check_bucket(Bucket)
        if Key in self.fail_puts:
            raise RuntimeError(f"put refused: {Key}")
        if not isinstance(Body, bytes):
            raise AssertionError(f"Body must be bytes, got {type(Body).__name__}")
        self.objects[Key] = Body
        self.puts.append(Key)

    def delete_object(self, Bucket, Key):
        self._check_bucket(Bucket)
        if Key in self.fail_deletes:
            raise RuntimeError(f"delete refused: {Key}")
        self.objects.pop(Key, None)
        self.deletes.append(Key)

    def get_paginator(self, name):
        if name != "list_objects_v2":
            raise AssertionError(f"unexpected paginator: {name!r}")
        return _Paginator(self, self._page_size)


@contextlib.contextmanager
def sandbox(mod, tmp, **attrs):
    """Point one archiver's module-level constants at `tmp` and restore them.

    BUCKET and LOG_FILE are always redirected: the first because the stub
    asserts on it, the second because `log()` otherwise appends to the real
    ~/.claude or ~/.kimi-code log of whoever runs the suite.

    Yields the captured stdout, since `log()` prints as well as writes and
    several branches are only observable through what they logged.
    """
    attrs.setdefault("BUCKET", _BUCKET)
    attrs.setdefault("LOG_FILE", Path(tmp) / "archive.log")
    saved = {k: getattr(mod, k) for k in attrs}
    out = io.StringIO()
    for key, value in attrs.items():
        setattr(mod, key, value)
    try:
        with contextlib.redirect_stdout(out):
            yield out
    finally:
        for key, value in saved.items():
            setattr(mod, key, value)


def _incompressible(n):
    """`n` bytes xz cannot shrink -- chained sha256, so it is reproducible.

    The binary leg of the compression policy only runs when compression
    genuinely fails to help, and random-looking data is the only way to get
    there without depending on a particular lzma version's ratio.
    """
    out = bytearray()
    seed = b"session-archivers"
    while len(out) < n:
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:n])


def _age(path, days):
    """Backdate a path so the retention gate sees it as stale."""
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _write(path, data, days_old=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    if days_old is not None:
        _age(path, days_old)
    return path


# --- manifest round-trip ----------------------------------------------------

def test_manifest_saves_and_loads_back_the_same_entries(tmp):
    """The manifest is how a compressed object is recognised as up to date."""
    for name, mod in _MODULES:
        client = FakeS3()
        with sandbox(mod, tmp):
            manifest = {"a/b.jsonl.xz": [1234567890, 42]}
            mod.save_manifest(client, manifest, dry_run=False)
            assert mod.manifest_key() in client.objects, name
            assert mod.load_manifest(client) == manifest, name


def test_manifest_is_stored_as_plain_json_never_compressed(tmp):
    """A reader fetches it with get_object and json.loads, nothing else."""
    for name, mod in _MODULES:
        client = FakeS3()
        with sandbox(mod, tmp):
            mod.save_manifest(client, {"k": [1, 2]}, dry_run=False)
            raw = client.objects[mod.manifest_key()]
        assert json.loads(raw) == {"k": [1, 2]}, name


def test_absent_manifest_reads_as_empty_not_an_error(tmp):
    """First run on a new machine: no manifest object exists yet."""
    for name, mod in _MODULES:
        with sandbox(mod, tmp):
            assert mod.load_manifest(FakeS3()) == {}, name


def test_corrupt_manifest_reads_as_empty(tmp):
    """A truncated object must cost one re-upload, not the whole run."""
    for name, mod in _MODULES:
        client = FakeS3()
        with sandbox(mod, tmp):
            client.objects[mod.manifest_key()] = b"{not json"
            assert mod.load_manifest(client) == {}, name


def test_manifest_of_the_wrong_shape_reads_as_empty(tmp):
    """A JSON list parses fine and would break every .get() downstream."""
    for name, mod in _MODULES:
        client = FakeS3()
        with sandbox(mod, tmp):
            client.objects[mod.manifest_key()] = b'["a", "b"]'
            assert mod.load_manifest(client) == {}, name


def test_dry_run_save_manifest_puts_nothing(tmp):
    for name, mod in _MODULES:
        client = FakeS3()
        with sandbox(mod, tmp) as out:
            mod.save_manifest(client, {"k": [1, 2]}, dry_run=True)
        assert client.puts == [], name
        assert "DRY put manifest" in out.getvalue(), name


def test_manifest_key_is_per_machine_and_under_the_manifest_prefix(tmp):
    for name, mod in _MODULES:
        key = mod.manifest_key()
        assert key.startswith(mod.MANIFEST_PREFIX + "/"), name
        assert key.endswith(".json"), name
        digest = key[len(mod.MANIFEST_PREFIX) + 1:-len(".json")]
        assert len(digest) == 16, name
        assert all(c in "0123456789abcdef" for c in digest), name


def test_machine_hash_falls_back_to_the_hostname(tmp):
    """Only Linux has /etc/machine-id; Windows and macOS take this leg."""
    class _NoMachineId:
        def __init__(self, *_a):
            pass

        def read_text(self, **_kw):
            raise OSError("no machine-id here")

    for name, mod in _MODULES:
        original = mod.Path
        try:
            mod.Path = _NoMachineId
            value = mod._machine_hash()
        finally:
            mod.Path = original
        import socket as _socket
        expected = hashlib.sha256(
            _socket.gethostname().encode("utf-8")).hexdigest()[:16]
        assert value == expected, name


# --- remote inventory -------------------------------------------------------

def test_list_remote_returns_every_key_with_its_stored_size(tmp):
    for name, mod in _MODULES:
        client = FakeS3(page_size=2)
        client.objects = {"a": b"1", "b": b"22", "c": b"333"}
        with sandbox(mod, tmp):
            assert mod.list_remote(client) == {"a": 1, "b": 2, "c": 3}, name


def test_list_remote_spans_every_page(tmp):
    """One page holds 1000 keys on R2; a bucket is many pages."""
    for name, mod in _MODULES:
        client = FakeS3(page_size=3)
        client.objects = {f"k{i:03d}": b"x" * (i + 1) for i in range(10)}
        with sandbox(mod, tmp):
            remote = mod.list_remote(client)
        assert len(remote) == 10, name
        assert remote["k009"] == 10, name


def test_list_remote_of_an_empty_bucket_is_an_empty_dict(tmp):
    for name, mod in _MODULES:
        with sandbox(mod, tmp):
            assert mod.list_remote(FakeS3()) == {}, name


# --- compression policy -----------------------------------------------------

def test_text_is_stored_xz_and_inflates_back_to_the_original(tmp):
    """Readers (parse_session.py, the dashboards) lzma-decompress the object."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "wire.jsonl", '{"a": 1}\n' * 200)
        with sandbox(mod, tmp):
            assert mod.upload_if_changed(
                client, src, "sessions/h/u/wire.jsonl", remote, False, manifest), name
        assert client.puts == ["sessions/h/u/wire.jsonl.xz"], name
        stored = client.objects["sessions/h/u/wire.jsonl.xz"]
        assert lzma.decompress(stored) == src.read_bytes(), name
        assert "sessions/h/u/wire.jsonl" not in client.objects, name


def test_an_uploaded_text_file_is_recorded_in_the_manifest_by_mtime_and_size(tmp):
    """A compressed object's remote size cannot be compared to the local
    file, so the signature is what makes the next run a no-op."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "wire.jsonl", "hello\n" * 50)
        st = src.stat()
        with sandbox(mod, tmp):
            mod.upload_if_changed(client, src, "k/wire.jsonl", remote, False, manifest)
        assert manifest["k/wire.jsonl.xz"] == [st.st_mtime_ns, st.st_size], name
        assert remote["k/wire.jsonl.xz"] == len(client.objects["k/wire.jsonl.xz"]), name


def test_an_unchanged_text_file_is_not_uploaded_twice(tmp):
    """Hourly cron over the same tree: the second run must be free."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "wire.jsonl", "x\n" * 100)
        with sandbox(mod, tmp):
            assert mod.upload_if_changed(client, src, "k/wire.jsonl", remote, False, manifest), name
            assert not mod.upload_if_changed(client, src, "k/wire.jsonl", remote, False, manifest), name
        assert client.puts == ["k/wire.jsonl.xz"], name


def test_a_changed_text_file_is_uploaded_again(tmp):
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "wire.jsonl", "x\n" * 100)
        with sandbox(mod, tmp):
            mod.upload_if_changed(client, src, "k/wire.jsonl", remote, False, manifest)
            _write(src, "x\n" * 400)
            assert mod.upload_if_changed(client, src, "k/wire.jsonl", remote, False, manifest), name
        assert lzma.decompress(client.objects["k/wire.jsonl.xz"]) == src.read_bytes(), name


def test_an_incompressible_binary_is_stored_plain(tmp):
    """Storing it xz would make the object bigger than the file."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        blob = _incompressible(4096)
        src = _write(Path(tmp) / name / "blob.bin", blob)
        with sandbox(mod, tmp):
            assert mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest), name
        assert client.objects["k/blob.bin"] == blob, name
        assert "k/blob.bin.xz" not in client.objects, name
        assert remote["k/blob.bin"] == len(blob), name
        assert manifest == {}, name


def test_a_compressible_binary_is_stored_xz(tmp):
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "blob.bin", b"\0" * 8192)
        with sandbox(mod, tmp):
            assert mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest), name
        assert lzma.decompress(client.objects["k/blob.bin.xz"]) == b"\0" * 8192, name


def test_an_unchanged_plain_binary_is_not_uploaded_twice(tmp):
    """The plain leg skips on a remote size match, not on the manifest."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "blob.bin", _incompressible(4096))
        with sandbox(mod, tmp):
            mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest)
            assert not mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest), name
        assert client.puts == ["k/blob.bin"], name


def test_an_already_compressed_input_is_stored_verbatim(tmp):
    """Re-compressing a .xz input would double-wrap it."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        payload = lzma.compress(b"already squeezed")
        src = _write(Path(tmp) / name / "old.jsonl.xz", payload)
        with sandbox(mod, tmp):
            assert mod.upload_if_changed(client, src, "k/old.jsonl.xz", remote, False, manifest), name
        assert client.objects["k/old.jsonl.xz"] == payload, name
        assert "k/old.jsonl.xz.xz" not in client.objects, name
        assert manifest == {}, name


def test_a_verbatim_object_already_the_right_size_is_skipped(tmp):
    for name, mod in _MODULES:
        client = FakeS3()
        manifest = {}
        src = _write(Path(tmp) / name / "old.jsonl.xz", b"12345")
        remote = {"k/old.jsonl.xz": 5}
        with sandbox(mod, tmp):
            assert not mod.upload_if_changed(client, src, "k/old.jsonl.xz", remote, False, manifest), name
        assert client.puts == [], name


def test_the_manifest_object_itself_is_never_compressed_on_the_upload_path(tmp):
    """It is read back with json.loads; an xz'd manifest is unreadable."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        key = mod.manifest_key()
        src = _write(Path(tmp) / name / "manifest.json", b'{"a": [1, 2]}')
        with sandbox(mod, tmp):
            assert mod.upload_if_changed(client, src, key, remote, False, manifest), name
        assert json.loads(client.objects[key]) == {"a": [1, 2]}, name


def test_a_file_that_vanished_between_walk_and_upload_is_skipped(tmp):
    """A live session directory changes under the archiver mid-run."""
    for name, mod in _MODULES:
        client = FakeS3()
        missing = Path(tmp) / name / "gone.jsonl"
        with sandbox(mod, tmp):
            assert not mod.upload_if_changed(client, missing, "k/gone.jsonl", {}, False, {}), name
        assert client.puts == [], name


def test_dry_run_reports_the_upload_without_making_it(tmp):
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "wire.jsonl", "x\n" * 100)
        with sandbox(mod, tmp) as out:
            assert mod.upload_if_changed(client, src, "k/wire.jsonl", remote, True, manifest), name
        assert client.puts == [], name
        assert manifest == {}, name
        assert "DRY upload k/wire.jsonl" in out.getvalue(), name


def test_dry_run_reports_a_verbatim_upload_without_making_it(tmp):
    for name, mod in _MODULES:
        client = FakeS3()
        src = _write(Path(tmp) / name / "old.jsonl.xz", lzma.compress(b"x"))
        with sandbox(mod, tmp) as out:
            assert mod.upload_if_changed(client, src, "k/old.jsonl.xz", {}, True, {}), name
        assert client.puts == [], name
        assert "DRY upload k/old.jsonl.xz" in out.getvalue(), name


# --- stale twins ------------------------------------------------------------

def test_flipping_to_compressed_deletes_the_plain_twin(tmp):
    """Otherwise ingest sees one transcript under two keys."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "blob.bin", _incompressible(4096))
        with sandbox(mod, tmp):
            mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest)
            assert remote["k/blob.bin"] == 4096, name
            _write(src, b"\0" * 8192)
            assert mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest), name
        assert client.deletes == ["k/blob.bin"], name
        assert "k/blob.bin" not in remote, name
        assert "k/blob.bin" not in client.objects, name
        assert "k/blob.bin.xz" in client.objects, name


def test_flipping_to_plain_deletes_the_compressed_twin_and_its_manifest_entry(tmp):
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "blob.bin", b"\0" * 8192)
        with sandbox(mod, tmp):
            mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest)
            assert "k/blob.bin.xz" in manifest, name
            _write(src, _incompressible(4096))
            assert mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest), name
        assert client.deletes == ["k/blob.bin.xz"], name
        assert manifest == {}, name
        assert "k/blob.bin.xz" not in remote, name
        assert client.objects["k/blob.bin"] == _incompressible(4096), name


def test_a_failed_twin_delete_is_logged_and_the_upload_still_counts(tmp):
    """Losing the delete leaves a duplicate; losing the upload loses data."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "blob.bin", _incompressible(4096))
        with sandbox(mod, tmp) as out:
            mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest)
            client.fail_deletes.add("k/blob.bin")
            _write(src, b"\0" * 8192)
            assert mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest), name
        assert "delete failed: k/blob.bin" in out.getvalue(), name
        assert "k/blob.bin.xz" in client.objects, name


# --- upload_bytes -----------------------------------------------------------

def test_upload_bytes_stores_the_body_under_the_key(tmp):
    for name, mod in (("codex", _CODEX), ("kimi", _KIMI)):
        client = FakeS3()
        remote = {}
        with sandbox(mod, tmp):
            assert mod.upload_bytes(client, b'{"path": "/p"}', "sessions/h/project.json",
                                    remote, False), name
        assert client.objects["sessions/h/project.json"] == b'{"path": "/p"}', name


def test_upload_bytes_skips_when_the_stored_size_already_matches(tmp):
    for name, mod in (("codex", _CODEX), ("kimi", _KIMI)):
        client = FakeS3()
        with sandbox(mod, tmp):
            assert not mod.upload_bytes(client, b"12345", "k", {"k": 5}, False), name
        assert client.puts == [], name


def test_upload_bytes_in_dry_run_puts_nothing(tmp):
    for name, mod in (("codex", _CODEX), ("kimi", _KIMI)):
        client = FakeS3()
        with sandbox(mod, tmp) as out:
            assert mod.upload_bytes(client, b"12345", "k", {}, True), name
        assert client.puts == [], name
        assert "DRY upload k" in out.getvalue(), name


# --- upload_dir -------------------------------------------------------------

def test_upload_dir_keys_every_file_by_its_posix_relative_path(tmp):
    """The keys must be identical whichever OS uploaded them."""
    for name, mod in (("claude", _CLAUDE), ("kimi", _KIMI)):
        client = FakeS3()
        root = Path(tmp) / name / "data"
        _write(root / "top.json", '{"a": 1}')
        _write(root / "nested" / "deep" / "note.txt", "hello")
        with sandbox(mod, tmp):
            n = mod.upload_dir(client, root, "p/u/data", {}, False, {})
        assert n == 2, name
        assert sorted(client.objects) == [
            "p/u/data/nested/deep/note.txt.xz", "p/u/data/top.json.xz"], name


def test_upload_dir_counts_objects_uploaded_not_files_visited(tmp):
    for name, mod in (("claude", _CLAUDE), ("kimi", _KIMI)):
        client = FakeS3()
        manifest, remote = {}, {}
        root = Path(tmp) / name / "data"
        _write(root / "a.json", "{}")
        _write(root / "b.json", "{}")
        with sandbox(mod, tmp):
            assert mod.upload_dir(client, root, "p", remote, False, manifest) == 2, name
            assert mod.upload_dir(client, root, "p", remote, False, manifest) == 0, name


# --- claude: archive_projects ----------------------------------------------

def _claude_tree(tmp):
    projects = Path(tmp) / "claude" / "projects"
    return projects


def test_claude_archives_a_transcript_and_its_data_dir(tmp):
    projects = _claude_tree(tmp)
    uuid = "1111-2222"
    _write(projects / "-root-proj" / f"{uuid}.jsonl", '{"type": "user"}\n')
    _write(projects / "-root-proj" / uuid / "tasks.json", "{}")
    client = FakeS3()
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects):
        up, deleted, failed = _CLAUDE.archive_projects(
            client, {}, time.time() - 86400, False, {})
    assert (up, deleted, failed) == (2, 0, 0)
    assert sorted(client.objects) == [
        f"-root-proj/{uuid}/{uuid}.jsonl.xz",
        f"-root-proj/{uuid}/data/tasks.json.xz",
    ]


def test_claude_deletes_a_stale_transcript_and_its_data_dir(tmp):
    projects = _claude_tree(tmp)
    uuid = "aaaa-bbbb"
    jsonl = _write(projects / "p" / f"{uuid}.jsonl", '{"x": 1}\n', days_old=10)
    data = _write(projects / "p" / uuid / "blob.json", "{}")
    client = FakeS3()
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects):
        up, deleted, failed = _CLAUDE.archive_projects(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (2, 1, 0)
    assert not jsonl.exists()
    assert not data.parent.exists()
    assert f"p/{uuid}/{uuid}.jsonl.xz" in client.objects


def test_claude_keeps_a_fresh_transcript(tmp):
    projects = _claude_tree(tmp)
    jsonl = _write(projects / "p" / "fresh.jsonl", "{}\n")
    client = FakeS3()
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects):
        up, deleted, _ = _CLAUDE.archive_projects(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted) == (1, 0)
    assert jsonl.exists()


def test_claude_dry_run_uploads_nothing_and_deletes_nothing(tmp):
    projects = _claude_tree(tmp)
    jsonl = _write(projects / "p" / "old.jsonl", "{}\n", days_old=10)
    client = FakeS3()
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects) as out:
        up, deleted, failed = _CLAUDE.archive_projects(
            client, {}, time.time() - 3 * 86400, True, {})
    assert (up, deleted, failed) == (1, 1, 0)
    assert client.puts == []
    assert jsonl.exists()
    assert "DRY rm" in out.getvalue()


def test_claude_archives_an_orphan_uuid_dir(tmp):
    """A data dir whose transcript is already gone still holds artifacts."""
    projects = _claude_tree(tmp)
    orphan = projects / "p" / "cccc-dddd"
    _write(orphan / "out.txt", "left behind")
    client = FakeS3()
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects):
        up, deleted, failed = _CLAUDE.archive_projects(
            client, {}, time.time() - 86400, False, {})
    assert (up, deleted, failed) == (1, 0, 0)
    assert "p/cccc-dddd/data/out.txt.xz" in client.objects


def test_claude_deletes_a_stale_orphan_dir(tmp):
    projects = _claude_tree(tmp)
    orphan = projects / "p" / "eeee-ffff"
    _write(orphan / "out.txt", "left behind")
    _age(orphan, 10)
    client = FakeS3()
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects):
        up, deleted, _ = _CLAUDE.archive_projects(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted) == (1, 1)
    assert not orphan.exists()


def test_claude_never_archives_the_memory_and_tasks_directories(tmp):
    """They are working state, not session history, and memory/ is private."""
    projects = _claude_tree(tmp)
    _write(projects / "p" / "memory" / "note.md", "private")
    _write(projects / "p" / "tasks" / "t.output", "scratch")
    client = FakeS3()
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects):
        up, deleted, failed = _CLAUDE.archive_projects(
            client, {}, time.time() - 86400, False, {})
    assert (up, deleted, failed) == (0, 0, 0)
    assert client.objects == {}


def test_claude_does_not_delete_a_transcript_whose_upload_failed(tmp):
    """A failed upload must never take the only copy with it."""
    projects = _claude_tree(tmp)
    jsonl = _write(projects / "p" / "doomed.jsonl", "{}\n", days_old=10)
    client = FakeS3()
    client.fail_puts.add("p/doomed/doomed.jsonl.xz")
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects) as out:
        up, deleted, failed = _CLAUDE.archive_projects(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (0, 0, 1)
    assert jsonl.exists()
    assert "upload failed" in out.getvalue()


def test_claude_does_not_delete_when_the_data_dir_upload_failed(tmp):
    projects = _claude_tree(tmp)
    uuid = "gggg-hhhh"
    jsonl = _write(projects / "p" / f"{uuid}.jsonl", "{}\n", days_old=10)
    _write(projects / "p" / uuid / "blob.json", "{}")
    client = FakeS3()
    client.fail_puts.add(f"p/{uuid}/data/blob.json.xz")
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects) as out:
        up, deleted, failed = _CLAUDE.archive_projects(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (1, 0, 1)
    assert jsonl.exists()
    assert "not deleting either" in out.getvalue()


def test_claude_archive_projects_on_a_machine_with_no_projects_dir(tmp):
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=Path(tmp) / "absent"):
        assert _CLAUDE.archive_projects(FakeS3(), {}, 0, False, {}) == (0, 0, 0)


# --- claude: cleanup_local --------------------------------------------------

def test_claude_cleanup_removes_stale_scratch_and_keeps_fresh(tmp):
    root = Path(tmp) / "claude"
    stale_debug = _write(root / "debug" / "old.log", "x", days_old=10)
    fresh_debug = _write(root / "debug" / "new.log", "x")
    stale_tel = _write(root / "telemetry" / "old.jsonl", "x", days_old=10)
    hist = root / "file-history" / "session-1"
    _write(hist / "a.txt", "x")
    _age(hist, 10)
    with sandbox(_CLAUDE, tmp, DEBUG_DIR=root / "debug",
                 FILE_HISTORY_DIR=root / "file-history",
                 TELEMETRY_DIR=root / "telemetry"):
        _CLAUDE.cleanup_local(time.time() - 3 * 86400, False)
    assert not stale_debug.exists()
    assert fresh_debug.exists()
    assert not stale_tel.exists()
    assert not hist.exists()


def test_claude_cleanup_dry_run_removes_nothing(tmp):
    root = Path(tmp) / "claude"
    stale = _write(root / "debug" / "old.log", "x", days_old=10)
    hist = root / "file-history" / "s"
    _write(hist / "a.txt", "x")
    _age(hist, 10)
    with sandbox(_CLAUDE, tmp, DEBUG_DIR=root / "debug",
                 FILE_HISTORY_DIR=root / "file-history",
                 TELEMETRY_DIR=root / "absent") as out:
        _CLAUDE.cleanup_local(time.time() - 3 * 86400, True)
    assert stale.exists()
    assert hist.exists()
    assert "DRY rm" in out.getvalue()
    assert "DRY rmtree" in out.getvalue()


# --- claude: main -----------------------------------------------------------

def _run_main(mod, tmp, argv, client, **attrs):
    """Drive a module's main() with the stub client and a sandboxed tree."""
    saved_argv = sys.argv
    saved_client = mod._client
    try:
        sys.argv = argv
        mod._client = lambda: client
        with sandbox(mod, tmp, **attrs) as out:
            code = mod.main()
        return code, out.getvalue()
    finally:
        sys.argv = saved_argv
        mod._client = saved_client


def test_claude_main_archives_uploads_and_saves_the_manifest(tmp):
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s1.jsonl", '{"type": "user"}\n')
    client = FakeS3()
    code, out = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions", "--days", "3"], client,
        PROJECTS_DIR=projects, DEBUG_DIR=Path(tmp) / "debug",
        FILE_HISTORY_DIR=Path(tmp) / "fh", TELEMETRY_DIR=Path(tmp) / "tel",
        LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 0
    assert "p/s1/s1.jsonl.xz" in client.objects
    saved = json.loads(client.objects[_CLAUDE.manifest_key()])
    assert saved["p/s1/s1.jsonl.xz"][1] == len('{"type": "user"}\n')
    assert "done — uploaded=1 deleted=0 failures=0" in out


def test_claude_main_exits_quietly_when_another_run_holds_the_lock(tmp):
    lock = Path(tmp) / "run.lock"
    holder_mod_lock = _CLAUDE.LOCK_FILE
    _CLAUDE.LOCK_FILE = lock
    try:
        holder = _CLAUDE.acquire_lock()
    finally:
        _CLAUDE.LOCK_FILE = holder_mod_lock
    if holder is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        client = FakeS3()
        code, out = _run_main(
            _CLAUDE, tmp, ["archive-claude-sessions"], client,
            PROJECTS_DIR=Path(tmp) / "absent", DEBUG_DIR=Path(tmp) / "absent",
            FILE_HISTORY_DIR=Path(tmp) / "absent",
            TELEMETRY_DIR=Path(tmp) / "absent", LOCK_FILE=lock)
    finally:
        holder.close()
    assert code == 0
    assert client.puts == []
    assert "previous run still holds the lock" in out


def test_claude_main_returns_one_when_an_upload_failed(tmp):
    """The exit code is what cron reports; a silent 0 hides data loss."""
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s2.jsonl", "{}\n")
    client = FakeS3()
    client.fail_puts.add("p/s2/s2.jsonl.xz")
    code, out = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions"], client,
        PROJECTS_DIR=projects, DEBUG_DIR=Path(tmp) / "absent",
        FILE_HISTORY_DIR=Path(tmp) / "absent",
        TELEMETRY_DIR=Path(tmp) / "absent", LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 1
    assert "failures=1" in out


def test_claude_main_dry_run_writes_nothing_to_the_bucket(tmp):
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s3.jsonl", "{}\n")
    client = FakeS3()
    code, _ = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions", "--dry-run"], client,
        PROJECTS_DIR=projects, DEBUG_DIR=Path(tmp) / "absent",
        FILE_HISTORY_DIR=Path(tmp) / "absent",
        TELEMETRY_DIR=Path(tmp) / "absent", LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 0
    assert client.puts == []


# --- codex ------------------------------------------------------------------

def _rollout(sessions, day, uuid, meta, days_old=None):
    name = f"rollout-2026-08-{day}T10-00-00-{uuid}.jsonl"
    body = json.dumps({"type": "session_meta", "payload": meta}) + "\n"
    body += '{"type": "event_msg"}\n'
    return _write(sessions / "2026" / "08" / day / name, body, days_old=days_old)


def test_codex_finds_every_rollout_under_the_date_buckets(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t1", "cwd": "/a"})
    _rollout(sessions, "20", "6-7-8-9-0", {"session_id": "t2", "cwd": "/b"})
    with sandbox(_CODEX, tmp, SESSIONS_DIR=sessions):
        found = _CODEX.find_rollouts()
    assert len(found) == 2
    assert [p.parent.name for p in found] == ["20", "21"]


def test_codex_find_rollouts_on_a_machine_with_no_sessions_dir(tmp):
    with sandbox(_CODEX, tmp, SESSIONS_DIR=Path(tmp) / "absent"):
        assert _CODEX.find_rollouts() == []


def test_codex_archives_a_main_rollout_under_its_thread_id(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "thread-a", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    client = FakeS3()
    with sandbox(_CODEX, tmp, SESSIONS_DIR=sessions):
        up, deleted, failed, skipped = _CODEX.archive_rollouts(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed, skipped) == (2, 0, 0, 0)
    assert f"sessions/{phash}/thread-a/wire.jsonl.xz" in client.objects
    marker = client.objects[f"sessions/{phash}/project.json"]
    assert json.loads(marker) == {"path": "/proj"}


def test_codex_keys_a_subagent_rollout_per_file(tmp):
    """Subagent threads reuse the parent's session_id, so the uuid keys them."""
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "aaaa-bb-cc-dd-eeee",
             {"session_id": "thread-a", "cwd": "/proj", "parent_thread_id": "thread-a"})
    phash = _CODEX.project_hash("/proj")
    client = FakeS3()
    with sandbox(_CODEX, tmp, SESSIONS_DIR=sessions):
        up, _, failed, _ = _CODEX.archive_rollouts(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, failed) == (2, 0)
    assert (f"sessions/{phash}/thread-a/subagents/aaaa-bb-cc-dd-eeee/wire.jsonl.xz"
            in client.objects)


def test_codex_skips_a_rollout_with_no_session_meta(tmp):
    """It has no thread identity; filing it under a guess is permanent."""
    sessions = Path(tmp) / "sessions"
    _write(sessions / "2026" / "08" / "21" / "rollout-2026-08-21T10-00-00-1-2-3-4-5.jsonl",
           '{"type": "event_msg"}\n')
    client = FakeS3()
    with sandbox(_CODEX, tmp, SESSIONS_DIR=sessions):
        up, deleted, failed, skipped = _CODEX.archive_rollouts(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed, skipped) == (0, 0, 0, 1)
    assert client.objects == {}


def test_codex_deletes_a_stale_rollout_only_once_it_is_in_the_bucket(tmp):
    sessions = Path(tmp) / "sessions"
    path = _rollout(sessions, "21", "1-2-3-4-5",
                    {"session_id": "t", "cwd": "/proj"}, days_old=10)
    client = FakeS3()
    with sandbox(_CODEX, tmp, SESSIONS_DIR=sessions):
        up, deleted, failed, _ = _CODEX.archive_rollouts(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (2, 1, 0)
    assert not path.exists()


def test_codex_keeps_a_stale_rollout_the_bucket_does_not_have(tmp):
    """`_retire_local` refuses without proof the object is stored."""
    path = _write(Path(tmp) / "rollout.jsonl", "{}\n", days_old=10)
    with sandbox(_CODEX, tmp):
        assert not _CODEX._retire_local(
            path, "sessions/h/t/wire.jsonl", {}, time.time() - 3 * 86400, False)
    assert path.exists()


def test_codex_keeps_a_fresh_rollout_that_is_in_the_bucket(tmp):
    path = _write(Path(tmp) / "rollout.jsonl", "{}\n")
    remote = {"sessions/h/t/wire.jsonl.xz": 40}
    with sandbox(_CODEX, tmp):
        assert not _CODEX._retire_local(
            path, "sessions/h/t/wire.jsonl", remote, time.time() - 3 * 86400, False)
    assert path.exists()


def test_codex_dry_run_keeps_a_stale_rollout(tmp):
    path = _write(Path(tmp) / "rollout.jsonl", "{}\n", days_old=10)
    remote = {"sessions/h/t/wire.jsonl.xz": 40}
    with sandbox(_CODEX, tmp) as out:
        assert not _CODEX._retire_local(
            path, "sessions/h/t/wire.jsonl", remote, time.time() - 3 * 86400, True)
    assert path.exists()
    assert "DRY rm" in out.getvalue()


def test_codex_counts_a_failed_rollout_upload_and_keeps_the_file(tmp):
    sessions = Path(tmp) / "sessions"
    path = _rollout(sessions, "21", "1-2-3-4-5",
                    {"session_id": "t", "cwd": "/proj"}, days_old=10)
    phash = _CODEX.project_hash("/proj")
    client = FakeS3()
    client.fail_puts.add(f"sessions/{phash}/t/wire.jsonl.xz")
    with sandbox(_CODEX, tmp, SESSIONS_DIR=sessions) as out:
        up, deleted, failed, _ = _CODEX.archive_rollouts(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (1, 0, 1)
    assert path.exists()
    assert "upload failed" in out.getvalue()


def test_codex_counts_a_failed_marker_upload(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    client = FakeS3()
    client.fail_puts.add(f"sessions/{phash}/project.json")
    with sandbox(_CODEX, tmp, SESSIONS_DIR=sessions) as out:
        up, _, failed, _ = _CODEX.archive_rollouts(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, failed) == (1, 1)
    assert "marker upload failed" in out.getvalue()


def test_codex_publishes_one_marker_per_project_not_per_rollout(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t1", "cwd": "/proj"})
    _rollout(sessions, "20", "6-7-8-9-0", {"session_id": "t2", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    client = FakeS3()
    with sandbox(_CODEX, tmp, SESSIONS_DIR=sessions):
        up, _, failed, _ = _CODEX.archive_rollouts(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, failed) == (3, 0)
    assert [k for k in client.objects if k.endswith("project.json")] == [
        f"sessions/{phash}/project.json"]


def test_codex_main_archives_and_saves_the_manifest(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    client = FakeS3()
    code, out = _run_main(
        _CODEX, tmp, ["archive-codex-sessions", "--days", "3"], client,
        SESSIONS_DIR=sessions, LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 0
    assert f"sessions/{phash}/t/wire.jsonl.xz" in client.objects
    assert _CODEX.manifest_key() in client.objects
    assert "uploaded=2 deleted=0 failed=0 skipped=0" in out


def test_codex_main_returns_one_when_an_upload_failed(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    client = FakeS3()
    client.fail_puts.add(f"sessions/{phash}/t/wire.jsonl.xz")
    code, out = _run_main(
        _CODEX, tmp, ["archive-codex-sessions"], client,
        SESSIONS_DIR=sessions, LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 1
    assert "failed=1" in out


def test_codex_main_exits_quietly_when_another_run_holds_the_lock(tmp):
    lock = Path(tmp) / "run.lock"
    saved = _CODEX.LOCK_FILE
    _CODEX.LOCK_FILE = lock
    try:
        holder = _CODEX.acquire_lock()
    finally:
        _CODEX.LOCK_FILE = saved
    if holder is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        client = FakeS3()
        code, _ = _run_main(_CODEX, tmp, ["archive-codex-sessions"], client,
                            SESSIONS_DIR=Path(tmp) / "absent", LOCK_FILE=lock)
    finally:
        holder.close()
    assert code == 0
    assert client.puts == []


# --- kimi -------------------------------------------------------------------

def test_kimi_project_map_reads_kimi_json(tmp):
    kimi_dir = Path(tmp) / "kimi"
    _write(kimi_dir / "kimi.json",
           json.dumps({"work_dirs": [{"path": "/p/one"}, {"path": "/p/two"},
                                     {"no_path": True}]}))
    with sandbox(_KIMI, tmp, KIMI_DIR=kimi_dir):
        mapping = _KIMI.load_project_map()
    assert mapping[hashlib.md5(b"/p/one").hexdigest()] == "/p/one"
    assert len(mapping) == 2


def test_kimi_project_map_is_empty_without_kimi_json(tmp):
    with sandbox(_KIMI, tmp, KIMI_DIR=Path(tmp) / "absent"):
        assert _KIMI.load_project_map() == {}


def test_kimi_project_map_is_empty_when_kimi_json_is_corrupt(tmp):
    kimi_dir = Path(tmp) / "kimi"
    _write(kimi_dir / "kimi.json", "{not json")
    with sandbox(_KIMI, tmp, KIMI_DIR=kimi_dir):
        assert _KIMI.load_project_map() == {}


def test_kimi_code_workdirs_maps_the_bucket_hash_to_the_project(tmp):
    kc = Path(tmp) / "kimi-code"
    lines = [
        json.dumps({"sessionDir": "/h/.kimi-code/sessions/wd_proj_0123456789ab/ses_1",
                    "workDir": "/h/proj"}),
        json.dumps({"sessionDir": "/h/.kimi-code/sessions/wd_x_ffffffffffff/ses_2"}),
        "",
        "{not json",
        json.dumps({"sessionDir": "/nowhere/at/all", "workDir": "/h/other"}),
    ]
    _write(kc / "session_index.jsonl", "\n".join(lines) + "\n")
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc):
        assert _KIMI.load_kimi_code_workdirs() == {"0123456789ab": "/h/proj"}


def test_kimi_code_workdirs_is_empty_without_an_index(tmp):
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=Path(tmp) / "absent"):
        assert _KIMI.load_kimi_code_workdirs() == {}


def test_kimi_workdir_falls_back_to_the_session_state(tmp):
    """Without session_index.jsonl the homedir in state.json still has it."""
    session = Path(tmp) / "ses_1"
    _write(session / "state.json", json.dumps({"agents": {"main": {
        "homedir": "/h/proj/sessions/wd_p_0123456789ab/ses_1/agents/main"}}}))
    assert _KIMI._workdir_from_state(session) == "/h/proj"


def test_kimi_workdir_from_state_returns_none_when_it_cannot_tell(tmp):
    absent = Path(tmp) / "no-session"
    assert _KIMI._workdir_from_state(absent) is None

    broken = Path(tmp) / "broken"
    _write(broken / "state.json", "{not json")
    assert _KIMI._workdir_from_state(broken) is None

    empty = Path(tmp) / "empty"
    _write(empty / "state.json", json.dumps({"agents": {"main": {}}}))
    assert _KIMI._workdir_from_state(empty) is None

    odd = Path(tmp) / "odd"
    _write(odd / "state.json",
           json.dumps({"agents": {"main": {"homedir": "/somewhere/else"}}}))
    assert _KIMI._workdir_from_state(odd) is None


def test_kimi_archives_a_legacy_session_and_its_project_marker(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    phash = hashlib.md5(b"/p/one").hexdigest()
    _write(kimi_dir / "kimi.json", json.dumps({"work_dirs": [{"path": "/p/one"}]}))
    _write(sessions / phash / "uuid-1" / "wire.jsonl", '{"a": 1}\n')
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions):
        up, deleted, failed = _KIMI.archive_sessions(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (2, 0, 0)
    assert f"sessions/{phash}/uuid-1/wire.jsonl.xz" in client.objects
    assert json.loads(client.objects[f"sessions/{phash}/project.json"]) == {
        "path": "/p/one"}


def test_kimi_deletes_a_stale_legacy_session_and_prunes_the_hash_dir(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    uuid_dir = sessions / "hash1" / "uuid-1"
    _write(uuid_dir / "wire.jsonl", '{"a": 1}\n', days_old=10)
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions):
        up, deleted, failed = _KIMI.archive_sessions(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (1, 1, 0)
    assert not uuid_dir.exists()
    assert not (sessions / "hash1").exists()


def test_kimi_archive_sessions_on_a_machine_with_no_sessions_dir(tmp):
    with sandbox(_KIMI, tmp, KIMI_DIR=Path(tmp) / "absent",
                 SESSIONS_DIR=Path(tmp) / "absent"):
        assert _KIMI.archive_sessions(FakeS3(), {}, 0, False, {}) == (0, 0, 0)


def test_kimi_archives_user_history_and_deletes_the_stale_files(tmp):
    hist = Path(tmp) / "user-history"
    fresh = _write(hist / "hash-fresh.jsonl", '{"q": 1}\n')
    stale = _write(hist / "hash-stale.jsonl", '{"q": 2}\n', days_old=10)
    _write(hist / "notes.txt", "ignored")
    client = FakeS3()
    with sandbox(_KIMI, tmp, USER_HISTORY_DIR=hist):
        up, deleted, failed = _KIMI.archive_user_history(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (2, 1, 0)
    assert sorted(client.objects) == ["user-history/hash-fresh.jsonl.xz",
                                      "user-history/hash-stale.jsonl.xz"]
    assert fresh.exists()
    assert not stale.exists()


def test_kimi_user_history_upload_failure_keeps_the_local_file(tmp):
    hist = Path(tmp) / "user-history"
    stale = _write(hist / "h.jsonl", "{}\n", days_old=10)
    client = FakeS3()
    client.fail_puts.add("user-history/h.jsonl.xz")
    with sandbox(_KIMI, tmp, USER_HISTORY_DIR=hist) as out:
        up, deleted, failed = _KIMI.archive_user_history(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (0, 0, 1)
    assert stale.exists()
    assert "upload failed" in out.getvalue()


def test_kimi_archive_user_history_on_a_machine_with_no_such_dir(tmp):
    with sandbox(_KIMI, tmp, USER_HISTORY_DIR=Path(tmp) / "absent"):
        assert _KIMI.archive_user_history(FakeS3(), {}, 0, False, {}) == (0, 0, 0)


def _kimi_code_session(kc, bucket, session, workdir=None):
    sess = kc / "sessions" / bucket / session
    _write(sess / "agents" / "main" / "wire.jsonl", '{"main": 1}\n')
    _write(sess / "agents" / "agent-1" / "wire.jsonl", '{"sub": 1}\n')
    _write(sess / "agents" / "agent-1" / "blobs" / "shot.json", "{}")
    state = {"agents": {"main": {}}}
    if workdir:
        state["agents"]["main"]["homedir"] = (
            f"{workdir}/sessions/{bucket}/{session}/agents/main")
    _write(sess / "state.json", json.dumps(state))
    return sess


def test_kimi_code_sessions_map_onto_the_legacy_key_layout(tmp):
    """One R2 layout for both, so a dashboard reads them the same way."""
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_proj_0123456789ab", "ses_1", workdir="/h/proj")
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        up, deleted, failed = _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (5, 0, 0)
    assert sorted(client.objects) == [
        "sessions/0123456789ab/project.json",
        "sessions/0123456789ab/ses_1/state.json.xz",
        "sessions/0123456789ab/ses_1/subagents/agent-1/blobs/shot.json.xz",
        "sessions/0123456789ab/ses_1/subagents/agent-1/wire.jsonl.xz",
        "sessions/0123456789ab/ses_1/wire.jsonl.xz",
    ]
    assert json.loads(client.objects["sessions/0123456789ab/project.json"]) == {
        "path": "/h/proj"}


def test_kimi_code_marker_prefers_the_session_index(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_proj_0123456789ab", "ses_1", workdir="/from/state")
    _write(kc / "session_index.jsonl", json.dumps({
        "sessionDir": "/h/.kimi-code/sessions/wd_proj_0123456789ab/ses_1",
        "workDir": "/from/index"}) + "\n")
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        _KIMI.archive_kimi_code_sessions(client, {}, time.time() - 3 * 86400, False, {})
    assert json.loads(client.objects["sessions/0123456789ab/project.json"]) == {
        "path": "/from/index"}


def test_kimi_code_skips_a_bucket_with_no_hash_suffix(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_not_a_hash", "ses_1")
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        assert _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 3 * 86400, False, {}) == (0, 0, 0)
    assert client.objects == {}


def test_kimi_code_deletes_a_stale_session_and_prunes_the_bucket(tmp):
    kc = Path(tmp) / "kimi-code"
    sess = _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    for wire in sess.rglob("wire.jsonl"):
        _age(wire, 10)
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        up, deleted, failed = _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (4, 1, 0)
    assert not sess.exists()
    assert not (kc / "sessions" / "wd_p_0123456789ab").exists()


def test_kimi_code_dry_run_keeps_the_session(tmp):
    kc = Path(tmp) / "kimi-code"
    sess = _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    for wire in sess.rglob("wire.jsonl"):
        _age(wire, 10)
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc,
                 KC_SESSIONS_DIR=kc / "sessions") as out:
        up, deleted, failed = _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 3 * 86400, True, {})
    assert (up, deleted, failed) == (4, 1, 0)
    assert client.puts == []
    assert sess.exists()
    assert "DRY rmtree" in out.getvalue()


def test_kimi_code_counts_a_failed_wire_upload(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    client = FakeS3()
    client.fail_puts.add("sessions/0123456789ab/ses_1/wire.jsonl.xz")
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc,
                 KC_SESSIONS_DIR=kc / "sessions") as out:
        up, _, failed = _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, failed) == (3, 1)
    assert "main_wire" in out.getvalue()


def test_kimi_archive_kimi_code_sessions_on_a_machine_without_them(tmp):
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=Path(tmp) / "absent",
                 KC_SESSIONS_DIR=Path(tmp) / "absent"):
        assert _KIMI.archive_kimi_code_sessions(
            FakeS3(), {}, 0, False, {}) == (0, 0, 0)


def test_kimi_cleanup_removes_stale_logs_and_telemetry(tmp):
    root = Path(tmp) / "kimi"
    stale_log = _write(root / "logs" / "old.log", "x", days_old=10)
    fresh_log = _write(root / "logs" / "new.log", "x")
    stale_tel = _write(root / "telemetry" / "old.jsonl", "x", days_old=10)
    with sandbox(_KIMI, tmp, LOGS_DIR=root / "logs",
                 TELEMETRY_DIR=root / "telemetry"):
        _KIMI.cleanup_local(time.time() - 3 * 86400, False)
    assert not stale_log.exists()
    assert fresh_log.exists()
    assert not stale_tel.exists()


def test_kimi_cleanup_dry_run_removes_nothing(tmp):
    root = Path(tmp) / "kimi"
    stale_log = _write(root / "logs" / "old.log", "x", days_old=10)
    stale_tel = _write(root / "telemetry" / "old.jsonl", "x", days_old=10)
    with sandbox(_KIMI, tmp, LOGS_DIR=root / "logs",
                 TELEMETRY_DIR=root / "telemetry") as out:
        _KIMI.cleanup_local(time.time() - 3 * 86400, True)
    assert stale_log.exists()
    assert stale_tel.exists()
    assert out.getvalue().count("DRY rm ") == 2


def test_kimi_main_archives_all_three_sources(tmp):
    kimi_dir = Path(tmp) / "kimi"
    kc = Path(tmp) / "kimi-code"
    _write(kimi_dir / "sessions" / "hash1" / "uuid-1" / "wire.jsonl", '{"a": 1}\n')
    _write(kimi_dir / "user-history" / "hash1.jsonl", '{"q": 1}\n')
    _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    client = FakeS3()
    code, out = _run_main(
        _KIMI, tmp, ["archive-kimi-sessions", "--days", "3"], client,
        KIMI_DIR=kimi_dir, SESSIONS_DIR=kimi_dir / "sessions",
        USER_HISTORY_DIR=kimi_dir / "user-history",
        LOGS_DIR=kimi_dir / "logs", TELEMETRY_DIR=kimi_dir / "telemetry",
        KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions",
        LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 0
    assert "sessions/hash1/uuid-1/wire.jsonl.xz" in client.objects
    assert "user-history/hash1.jsonl.xz" in client.objects
    assert "sessions/0123456789ab/ses_1/wire.jsonl.xz" in client.objects
    assert _KIMI.manifest_key() in client.objects
    assert "done — uploaded=6 deleted=0 failures=0" in out


def test_kimi_main_returns_one_when_an_upload_failed(tmp):
    kimi_dir = Path(tmp) / "kimi"
    _write(kimi_dir / "user-history" / "hash1.jsonl", "{}\n")
    client = FakeS3()
    client.fail_puts.add("user-history/hash1.jsonl.xz")
    code, out = _run_main(
        _KIMI, tmp, ["archive-kimi-sessions"], client,
        KIMI_DIR=kimi_dir, SESSIONS_DIR=kimi_dir / "absent",
        USER_HISTORY_DIR=kimi_dir / "user-history",
        LOGS_DIR=kimi_dir / "absent", TELEMETRY_DIR=kimi_dir / "absent",
        KIMI_CODE_DIR=Path(tmp) / "absent", KC_SESSIONS_DIR=Path(tmp) / "absent",
        LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 1
    assert "failures=1" in out


def test_kimi_main_dry_run_writes_nothing_to_the_bucket(tmp):
    kimi_dir = Path(tmp) / "kimi"
    _write(kimi_dir / "user-history" / "hash1.jsonl", "{}\n")
    client = FakeS3()
    code, out = _run_main(
        _KIMI, tmp, ["archive-kimi-sessions", "--dry-run"], client,
        KIMI_DIR=kimi_dir, SESSIONS_DIR=kimi_dir / "absent",
        USER_HISTORY_DIR=kimi_dir / "user-history",
        LOGS_DIR=kimi_dir / "absent", TELEMETRY_DIR=kimi_dir / "absent",
        KIMI_CODE_DIR=Path(tmp) / "absent", KC_SESSIONS_DIR=Path(tmp) / "absent",
        LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 0
    assert client.puts == []
    assert "DRY upload" in out


def test_kimi_main_exits_quietly_when_another_run_holds_the_lock(tmp):
    lock = Path(tmp) / "run.lock"
    saved = _KIMI.LOCK_FILE
    _KIMI.LOCK_FILE = lock
    try:
        holder = _KIMI.acquire_lock()
    finally:
        _KIMI.LOCK_FILE = saved
    if holder is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        client = FakeS3()
        code, out = _run_main(
            _KIMI, tmp, ["archive-kimi-sessions"], client,
            KIMI_DIR=Path(tmp) / "absent", SESSIONS_DIR=Path(tmp) / "absent",
            USER_HISTORY_DIR=Path(tmp) / "absent", LOGS_DIR=Path(tmp) / "absent",
            TELEMETRY_DIR=Path(tmp) / "absent",
            KIMI_CODE_DIR=Path(tmp) / "absent",
            KC_SESSIONS_DIR=Path(tmp) / "absent", LOCK_FILE=lock)
    finally:
        holder.close()
    assert code == 0
    assert client.puts == []
    assert "previous run still holds the lock" in out


# --- credential resolution --------------------------------------------------
# _r2_config is the last thing between a misconfigured box and an archiver
# that talks to the wrong account, or to nowhere at all.

_R2_KEYS = ("R2_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY")


@contextlib.contextmanager
def deployment(tmp, **values):
    """Present exactly `values` as the deployment settings, and nothing else.

    The environment of whoever runs this suite may well carry real R2
    credentials -- this box does -- so they are cleared for the duration
    rather than merely overridden, or the "missing key" tests would pass
    for the wrong reason.
    """
    settings = _util.load_package_module("settings")
    saved_env = {k: os.environ.pop(k, None) for k in _R2_KEYS}
    saved_dirs = (settings.SETTINGS_DIR, settings.LEGACY_SETTINGS_DIR)
    base = Path(tmp) / "settings"
    base.mkdir(parents=True, exist_ok=True)
    (base / "settings.json").write_text(json.dumps({"env": values}),
                                        encoding="utf-8")
    settings.SETTINGS_DIR = base
    settings.LEGACY_SETTINGS_DIR = Path(tmp) / "absent-legacy"
    settings._file_env(force=True)
    try:
        yield
    finally:
        settings.SETTINGS_DIR, settings.LEGACY_SETTINGS_DIR = saved_dirs
        for key, value in saved_env.items():
            if value is not None:
                os.environ[key] = value
        settings._file_env(force=True)


def test_r2_config_returns_the_configured_triple(tmp):
    for name, mod in _MODULES:
        with deployment(tmp, R2_ACCOUNT_ID="acct-1", R2_ACCESS_KEY_ID="ak",
                        R2_SECRET_ACCESS_KEY="sk"):
            assert mod._r2_config() == ("acct-1", "ak", "sk"), name


def test_r2_config_falls_back_to_the_cloudflare_account(tmp):
    """The R2 bucket lives in that account; one value, not two that drift."""
    for name, mod in _MODULES:
        with deployment(tmp, CLOUDFLARE_ACCOUNT_ID="cf-acct",
                        R2_ACCESS_KEY_ID="ak", R2_SECRET_ACCESS_KEY="sk"):
            assert mod._r2_config()[0] == "cf-acct", name


def test_r2_config_refuses_to_run_half_configured(tmp):
    """Uploading nowhere silently is worse than not starting."""
    for name, mod in _MODULES:
        for missing, present in (
                ("CLOUDFLARE_ACCOUNT_ID", {"R2_ACCESS_KEY_ID": "ak",
                                           "R2_SECRET_ACCESS_KEY": "sk"}),
                ("R2_ACCESS_KEY_ID", {"R2_ACCOUNT_ID": "a",
                                      "R2_SECRET_ACCESS_KEY": "sk"}),
                ("R2_SECRET_ACCESS_KEY", {"R2_ACCOUNT_ID": "a",
                                          "R2_ACCESS_KEY_ID": "ak"})):
            with deployment(tmp, **present):
                try:
                    mod._r2_config()
                except SystemExit as e:
                    assert missing in str(e), f"{name}: {missing} not named"
                else:
                    raise AssertionError(f"{name}: ran without {missing}")


# --- logging ----------------------------------------------------------------

def test_a_log_file_that_cannot_be_written_does_not_stop_the_run(tmp):
    """These run from cron; an unwritable log must not cost the archive."""
    for name, mod in _MODULES:
        with sandbox(mod, tmp, LOG_FILE=Path(tmp) / "no-such-dir" / "a.log") as out:
            mod.log("still archiving")
        assert "still archiving" in out.getvalue(), name


def test_log_lines_are_appended_to_the_log_file(tmp):
    for name, mod in _MODULES:
        path = Path(tmp) / f"{name}.log"
        with sandbox(mod, tmp, LOG_FILE=path):
            mod.log("first")
            mod.log("second")
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, name
        assert lines[0].endswith(" first"), name
        assert lines[1].endswith(" second"), name


# --- more stale-twin and dry-run legs ---------------------------------------

def test_a_failed_compressed_twin_delete_is_logged(tmp):
    """The mirror of the plain case: the flip is xz -> plain this time."""
    for name, mod in _MODULES:
        client = FakeS3()
        manifest, remote = {}, {}
        src = _write(Path(tmp) / name / "blob.bin", b"\0" * 8192)
        with sandbox(mod, tmp) as out:
            mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest)
            client.fail_deletes.add("k/blob.bin.xz")
            _write(src, _incompressible(4096))
            assert mod.upload_if_changed(client, src, "k/blob.bin", remote, False, manifest), name
        assert "delete failed: k/blob.bin.xz" in out.getvalue(), name
        assert client.objects["k/blob.bin"] == _incompressible(4096), name


# --- claude: the remaining archive_projects legs ----------------------------

def test_claude_ignores_a_loose_file_where_a_project_should_be(tmp):
    projects = _claude_tree(tmp)
    _write(projects / "stray.txt", "not a project")
    client = FakeS3()
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(
            client, {}, time.time() - 86400, False, {}) == (0, 0, 0)
    assert client.objects == {}


def test_claude_dry_run_reports_the_data_dir_it_would_remove(tmp):
    projects = _claude_tree(tmp)
    uuid = "iiii-jjjj"
    jsonl = _write(projects / "p" / f"{uuid}.jsonl", "{}\n", days_old=10)
    data = _write(projects / "p" / uuid / "blob.json", "{}")
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects) as out:
        up, deleted, _ = _CLAUDE.archive_projects(
            FakeS3(), {}, time.time() - 3 * 86400, True, {})
    assert (up, deleted) == (2, 1)
    assert jsonl.exists()
    assert data.exists()
    assert "DRY rmtree" in out.getvalue()


def test_claude_counts_a_failed_orphan_upload_and_keeps_the_dir(tmp):
    projects = _claude_tree(tmp)
    orphan = projects / "p" / "kkkk-llll"
    _write(orphan / "out.txt", "left behind")
    _age(orphan, 10)
    client = FakeS3()
    client.fail_puts.add("p/kkkk-llll/data/out.txt.xz")
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects) as out:
        up, deleted, failed = _CLAUDE.archive_projects(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (0, 0, 1)
    assert orphan.exists()
    assert "orphan uuid_dir" in out.getvalue()


def test_claude_dry_run_reports_the_orphan_dir_it_would_remove(tmp):
    projects = _claude_tree(tmp)
    orphan = projects / "p" / "mmmm-nnnn"
    _write(orphan / "out.txt", "x")
    _age(orphan, 10)
    with sandbox(_CLAUDE, tmp, PROJECTS_DIR=projects) as out:
        up, deleted, _ = _CLAUDE.archive_projects(
            FakeS3(), {}, time.time() - 3 * 86400, True, {})
    assert (up, deleted) == (1, 1)
    assert orphan.exists()
    assert "DRY rmtree" in out.getvalue()


def test_claude_main_logs_a_failed_manifest_save_without_failing_the_run(tmp):
    """The transcripts are already in the bucket; only the next run pays."""
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s4.jsonl", "{}\n")
    client = FakeS3()
    client.fail_puts.add(_CLAUDE.manifest_key())
    code, out = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions"], client,
        PROJECTS_DIR=projects, DEBUG_DIR=Path(tmp) / "absent",
        FILE_HISTORY_DIR=Path(tmp) / "absent",
        TELEMETRY_DIR=Path(tmp) / "absent", LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 0
    assert "manifest save failed" in out
    assert "p/s4/s4.jsonl.xz" in client.objects


# --- kimi: the remaining archive legs ---------------------------------------

def test_kimi_ignores_loose_files_where_directories_should_be(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    _write(sessions / "stray.txt", "x")
    _write(sessions / "hash1" / "stray.txt", "x")
    kc = Path(tmp) / "kimi-code"
    _write(kc / "sessions" / "stray.txt", "x")
    _write(kc / "sessions" / "wd_p_0123456789ab" / "stray.txt", "x")
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions,
                 KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        assert _KIMI.archive_sessions(
            client, {}, time.time() - 86400, False, {}) == (0, 0, 0)
        assert _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 86400, False, {}) == (0, 0, 0)
    assert client.objects == {}


def test_kimi_counts_a_failed_legacy_marker_upload(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    phash = hashlib.md5(b"/p/one").hexdigest()
    _write(kimi_dir / "kimi.json", json.dumps({"work_dirs": [{"path": "/p/one"}]}))
    _write(sessions / phash / "uuid-1" / "wire.jsonl", "{}\n")
    client = FakeS3()
    client.fail_puts.add(f"sessions/{phash}/project.json")
    with sandbox(_KIMI, tmp, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions) as out:
        up, _, failed = _KIMI.archive_sessions(
            client, {}, time.time() - 86400, False, {})
    assert (up, failed) == (1, 1)
    assert "marker upload failed" in out.getvalue()


def test_kimi_counts_a_failed_legacy_session_upload_and_keeps_it(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    uuid_dir = sessions / "hash1" / "uuid-1"
    _write(uuid_dir / "wire.jsonl", "{}\n", days_old=10)
    client = FakeS3()
    client.fail_puts.add("sessions/hash1/uuid-1/wire.jsonl.xz")
    with sandbox(_KIMI, tmp, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions) as out:
        up, deleted, failed = _KIMI.archive_sessions(
            client, {}, time.time() - 3 * 86400, False, {})
    assert (up, deleted, failed) == (0, 0, 1)
    assert uuid_dir.exists()
    assert "uuid_dir" in out.getvalue()


def test_kimi_legacy_dry_run_keeps_the_stale_session(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    uuid_dir = sessions / "hash1" / "uuid-1"
    _write(uuid_dir / "wire.jsonl", "{}\n", days_old=10)
    client = FakeS3()
    with sandbox(_KIMI, tmp, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions) as out:
        up, deleted, failed = _KIMI.archive_sessions(
            client, {}, time.time() - 3 * 86400, True, {})
    assert (up, deleted, failed) == (1, 1, 0)
    assert client.puts == []
    assert uuid_dir.exists()
    assert "DRY rmtree" in out.getvalue()


def test_kimi_user_history_dry_run_keeps_the_stale_file(tmp):
    hist = Path(tmp) / "user-history"
    stale = _write(hist / "h.jsonl", "{}\n", days_old=10)
    client = FakeS3()
    with sandbox(_KIMI, tmp, USER_HISTORY_DIR=hist) as out:
        up, deleted, failed = _KIMI.archive_user_history(
            client, {}, time.time() - 3 * 86400, True, {})
    assert (up, deleted, failed) == (1, 1, 0)
    assert client.puts == []
    assert stale.exists()
    assert "DRY rm" in out.getvalue()


def test_kimi_code_counts_a_failed_marker_upload(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1", workdir="/h/proj")
    client = FakeS3()
    client.fail_puts.add("sessions/0123456789ab/project.json")
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc,
                 KC_SESSIONS_DIR=kc / "sessions") as out:
        up, _, failed = _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 86400, False, {})
    assert (up, failed) == (4, 1)
    assert "marker upload failed" in out.getvalue()


def test_kimi_code_counts_a_failed_subagent_upload(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    client = FakeS3()
    client.fail_puts.add(
        "sessions/0123456789ab/ses_1/subagents/agent-1/wire.jsonl.xz")
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc,
                 KC_SESSIONS_DIR=kc / "sessions") as out:
        up, _, failed = _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 86400, False, {})
    assert (up, failed) == (3, 1)
    assert "subagent_wire" in out.getvalue()


def test_kimi_code_counts_a_failed_blob_upload(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    client = FakeS3()
    client.fail_puts.add(
        "sessions/0123456789ab/ses_1/subagents/agent-1/blobs/shot.json.xz")
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc,
                 KC_SESSIONS_DIR=kc / "sessions") as out:
        up, _, failed = _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 86400, False, {})
    assert (up, failed) == (3, 1)
    assert "blobs=" in out.getvalue()


def test_kimi_code_counts_a_failed_state_upload(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    client = FakeS3()
    client.fail_puts.add("sessions/0123456789ab/ses_1/state.json.xz")
    with sandbox(_KIMI, tmp, KIMI_CODE_DIR=kc,
                 KC_SESSIONS_DIR=kc / "sessions") as out:
        up, _, failed = _KIMI.archive_kimi_code_sessions(
            client, {}, time.time() - 86400, False, {})
    assert (up, failed) == (3, 1)
    assert "state=" in out.getvalue()


def test_kimi_main_logs_a_failed_manifest_save_without_failing_the_run(tmp):
    kimi_dir = Path(tmp) / "kimi"
    _write(kimi_dir / "user-history" / "hash1.jsonl", "{}\n")
    client = FakeS3()
    client.fail_puts.add(_KIMI.manifest_key())
    code, out = _run_main(
        _KIMI, tmp, ["archive-kimi-sessions"], client,
        KIMI_DIR=kimi_dir, SESSIONS_DIR=kimi_dir / "absent",
        USER_HISTORY_DIR=kimi_dir / "user-history",
        LOGS_DIR=kimi_dir / "absent", TELEMETRY_DIR=kimi_dir / "absent",
        KIMI_CODE_DIR=Path(tmp) / "absent", KC_SESSIONS_DIR=Path(tmp) / "absent",
        LOCK_FILE=Path(tmp) / "run.lock")
    assert code == 0
    assert "manifest save failed" in out
    assert "user-history/hash1.jsonl.xz" in client.objects


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(dict(globals()))))
