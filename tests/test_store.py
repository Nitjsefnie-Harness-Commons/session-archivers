"""The destination: compression policy, manifest, inventory, upload forms.

This is the code the three archivers used to carry three identical copies of,
so the suite that covered it ran every assertion three times over and proved
one thing three times. One implementation, one suite.

It never reaches the network. `store.client()` is the only place a real boto3
client is built, and nothing here calls it: every test constructs a `Store`
around the in-memory stub in `_util.FakeS3`.

What is asserted is the VALUE -- which key an object landed under, what bytes
come back out of it, what the manifest recorded. A stub can make "an upload
happened" true without the upload being right, so "a call was made" is never
the assertion on its own.
"""
# pylint: disable=protected-access
# `_upload_verbatim` and friends are private because they are storage forms,
# not API; testing them through upload_file only would mean guessing which
# leg ran from the outside.
# pylint: disable=too-few-public-methods
# The Path shim below stands in for one method of pathlib.Path.
import hashlib
import json
import lzma
import os
import socket
import time
from pathlib import Path

import _util

_STORE = _util.load_package_module("store")
_SETTINGS = _util.load_package_module("settings")

_BUCKET = "test-bucket"


def _store(client=None, dry_run=False, log=None):
    """A Store over the stub, logging into a list unless told otherwise."""
    client = client or _util.FakeS3(_BUCKET)
    said = [] if log is None else log
    return _STORE.Store(client, _BUCKET, said.append, dry_run), client, said


def _write(path, data, days_old=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    if days_old is not None:
        old = time.time() - days_old * 86400
        os.utime(path, (old, old))
    return path


def _incompressible(n):
    """`n` bytes xz cannot shrink -- chained sha256, so it is reproducible.

    The binary leg of the compression policy only runs when compression
    genuinely fails to help, and high-entropy data is the only way to get
    there without depending on a particular lzma version's ratio.
    """
    out = bytearray()
    seed = b"session-archivers"
    while len(out) < n:
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:n])


# --- content-type classification ---------------------------------------------

def test_is_text_accepts_the_suffixes_the_archivers_upload(tmp):
    for key in ("sessions/a/wire.jsonl", "x.txt", "y.json", "z.js"):
        assert _STORE.is_text(key), key


def test_is_text_rejects_everything_else(tmp):
    for key in ("archive.tar.gz", "image.png", "wire.jsonl.xz", "noext"):
        assert not _STORE.is_text(key), key


def test_is_text_is_suffix_anchored_not_substring(tmp):
    """`.jsonl` inside a name is not a `.jsonl` file."""
    assert not _STORE.is_text("a.jsonl.bak")
    assert not _STORE.is_text("notes-about-json")


# --- machine identity ----------------------------------------------------------

def test_machine_hash_is_stable_and_opaque(tmp):
    """Opaque because the manifest object name must not leak the hostname."""
    first = _STORE.machine_hash()
    assert first == _STORE.machine_hash()
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)
    assert socket.gethostname() not in first


def test_machine_hash_falls_back_to_the_hostname(tmp):
    """Only Linux has /etc/machine-id; Windows and macOS take this leg."""
    class _NoMachineId:
        def __init__(self, *_a):
            pass

        def read_text(self, **_kw):
            raise OSError("no machine-id here")

    original = _STORE.Path
    try:
        _STORE.Path = _NoMachineId
        value = _STORE.machine_hash()
    finally:
        _STORE.Path = original
    expected = hashlib.sha256(
        socket.gethostname().encode("utf-8")).hexdigest()[:16]
    assert value == expected


def test_manifest_key_is_per_machine_and_under_the_manifest_prefix(tmp):
    """N machines write N manifests and never clobber each other."""
    key = _STORE.manifest_key()
    assert key.startswith(_STORE.MANIFEST_PREFIX + "/")
    assert key.endswith(".json")
    assert key[len(_STORE.MANIFEST_PREFIX) + 1:-len(".json")] == _STORE.machine_hash()


# --- credential resolution -----------------------------------------------------

_R2_KEYS = ("R2_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY")


class _Deployment:
    """Present exactly these settings, and nothing else, for one test.

    The environment of whoever runs this suite may well carry real R2
    credentials -- this box does -- so they are cleared for the duration
    rather than merely overridden, or the "missing key" test would pass for
    the wrong reason.
    """

    def __init__(self, tmp, **values):
        self._tmp = tmp
        self._values = values
        self._env = {}
        self._dirs = ()

    def __enter__(self):
        self._env = {k: os.environ.pop(k, None) for k in _R2_KEYS}
        self._dirs = (_SETTINGS.SETTINGS_DIR, _SETTINGS.LEGACY_SETTINGS_DIR)
        base = Path(self._tmp) / "settings"
        base.mkdir(parents=True, exist_ok=True)
        (base / "settings.json").write_text(
            json.dumps({"env": self._values}), encoding="utf-8")
        _SETTINGS.SETTINGS_DIR = base
        _SETTINGS.LEGACY_SETTINGS_DIR = Path(self._tmp) / "absent-legacy"
        _SETTINGS._file_env(force=True)
        return self

    def __exit__(self, *_exc):
        _SETTINGS.SETTINGS_DIR, _SETTINGS.LEGACY_SETTINGS_DIR = self._dirs
        for key, value in self._env.items():
            if value is not None:
                os.environ[key] = value
        _SETTINGS._file_env(force=True)
        return False


def test_r2_config_returns_the_configured_triple(tmp):
    with _Deployment(tmp, R2_ACCOUNT_ID="acct-1", R2_ACCESS_KEY_ID="ak",
                     R2_SECRET_ACCESS_KEY="sk"):
        assert _STORE.r2_config() == ("acct-1", "ak", "sk")


def test_r2_config_falls_back_to_the_cloudflare_account(tmp):
    """The R2 bucket lives in that account: one value, not two that drift."""
    with _Deployment(tmp, CLOUDFLARE_ACCOUNT_ID="cf-acct",
                     R2_ACCESS_KEY_ID="ak", R2_SECRET_ACCESS_KEY="sk"):
        assert _STORE.r2_config()[0] == "cf-acct"


def test_r2_config_refuses_to_run_half_configured(tmp):
    """Uploading nowhere silently is worse than not starting."""
    cases = (
        ("CLOUDFLARE_ACCOUNT_ID", {"R2_ACCESS_KEY_ID": "ak",
                                   "R2_SECRET_ACCESS_KEY": "sk"}),
        ("R2_ACCESS_KEY_ID", {"R2_ACCOUNT_ID": "a",
                              "R2_SECRET_ACCESS_KEY": "sk"}),
        ("R2_SECRET_ACCESS_KEY", {"R2_ACCOUNT_ID": "a",
                                  "R2_ACCESS_KEY_ID": "ak"}),
    )
    for missing, present in cases:
        with _Deployment(tmp, **present):
            try:
                _STORE.r2_config()
            except SystemExit as e:
                assert missing in str(e), f"{missing} not named in: {e}"
            else:
                raise AssertionError(f"ran without {missing}")


# --- the bucket a Store is pointed at ------------------------------------------

def test_a_store_only_ever_touches_the_bucket_it_was_given(tmp):
    """Three archivers, three buckets -- the Store must not be able to
    address another one. The stub raises on any other bucket name."""
    client = _util.FakeS3("kimi")
    dest = _STORE.Store(client, "kimi", [].append)
    dest.inventory()
    dest.upload_bytes(b"{}", "sessions/h/project.json")
    dest.save_manifest()
    assert set(client.objects) == {"sessions/h/project.json",
                                   _STORE.manifest_key()}


def test_two_stores_over_one_client_stay_in_their_own_buckets(tmp):
    """Nothing in Store is module-level, so two can coexist in one process."""
    claude_client = _util.FakeS3("claude")
    kimi_client = _util.FakeS3("kimi")
    _STORE.Store(claude_client, "claude", [].append).upload_bytes(b"a", "k")
    _STORE.Store(kimi_client, "kimi", [].append).upload_bytes(b"bb", "k")
    assert claude_client.objects == {"k": b"a"}
    assert kimi_client.objects == {"k": b"bb"}


# --- manifest round-trip --------------------------------------------------------

def test_manifest_saves_and_loads_back_the_same_entries(tmp):
    """The manifest is how a compressed object is recognised as up to date."""
    dest, client, _ = _store()
    dest.manifest = {"a/b.jsonl.xz": [1234567890, 42]}
    dest.save_manifest()
    assert _STORE.manifest_key() in client.objects
    assert dest.load_manifest() == {"a/b.jsonl.xz": [1234567890, 42]}


def test_the_manifest_is_stored_as_plain_json_never_compressed(tmp):
    """A reader fetches it with get_object and json.loads, nothing else."""
    dest, client, _ = _store()
    dest.manifest = {"k": [1, 2]}
    dest.save_manifest()
    assert json.loads(client.objects[_STORE.manifest_key()]) == {"k": [1, 2]}


def test_an_absent_manifest_reads_as_empty_not_an_error(tmp):
    """First run on a new machine: no manifest object exists yet."""
    dest, _, _ = _store()
    assert dest.load_manifest() == {}


def test_a_corrupt_manifest_reads_as_empty(tmp):
    """A truncated object costs one re-upload, not the whole run."""
    dest, client, _ = _store()
    client.objects[_STORE.manifest_key()] = b"{not json"
    assert dest.load_manifest() == {}


def test_a_manifest_of_the_wrong_shape_reads_as_empty(tmp):
    """A JSON list parses fine and would break every .get() downstream."""
    dest, client, _ = _store()
    client.objects[_STORE.manifest_key()] = b'["a", "b"]'
    assert dest.load_manifest() == {}


def test_a_dry_run_save_puts_nothing(tmp):
    dest, client, said = _store(dry_run=True)
    dest.manifest = {"k": [1, 2]}
    dest.save_manifest()
    assert client.puts == []
    assert any("DRY put manifest" in line for line in said)


# --- inventory -------------------------------------------------------------------

def test_inventory_returns_every_key_with_its_stored_size(tmp):
    dest, client, _ = _store(_util.FakeS3(_BUCKET, page_size=2))
    client.objects = {"a": b"1", "b": b"22", "c": b"333"}
    assert dest.inventory() == {"a": 1, "b": 2, "c": 3}
    assert dest.remote == {"a": 1, "b": 2, "c": 3}


def test_inventory_spans_every_page(tmp):
    """One page holds 1000 keys on R2; a real bucket is many pages."""
    dest, client, _ = _store(_util.FakeS3(_BUCKET, page_size=3))
    client.objects = {f"k{i:03d}": b"x" * (i + 1) for i in range(10)}
    remote = dest.inventory()
    assert len(remote) == 10
    assert remote["k009"] == 10


def test_the_inventory_of_an_empty_bucket_is_an_empty_dict(tmp):
    dest, _, _ = _store()
    assert dest.inventory() == {}


def test_holds_recognises_either_storage_form(tmp):
    """The check a retention gate makes before deleting the local copy."""
    dest, _, _ = _store()
    dest.remote = {"a/plain.bin": 10, "a/text.jsonl.xz": 40}
    assert dest.holds("a/plain.bin")
    assert dest.holds("a/text.jsonl")
    assert not dest.holds("a/missing.jsonl")


# --- compression policy ------------------------------------------------------------

def test_text_is_stored_xz_and_inflates_back_to_the_original(tmp):
    """Readers lzma-decompress the object; lzma is stdlib, so they need
    no third-party dependency to do it."""
    dest, client, _ = _store()
    src = _write(Path(tmp) / "wire.jsonl", '{"a": 1}\n' * 200)
    assert dest.upload_file(src, "sessions/h/u/wire.jsonl")
    assert client.puts == ["sessions/h/u/wire.jsonl.xz"]
    assert lzma.decompress(client.objects["sessions/h/u/wire.jsonl.xz"]) \
        == src.read_bytes()
    assert "sessions/h/u/wire.jsonl" not in client.objects


def test_an_uploaded_text_file_is_recorded_by_mtime_and_size(tmp):
    """A compressed object's remote size cannot be compared to the local
    file, so the signature is what makes the next run a no-op."""
    dest, client, _ = _store()
    src = _write(Path(tmp) / "wire.jsonl", "hello\n" * 50)
    st = src.stat()
    dest.upload_file(src, "k/wire.jsonl")
    assert dest.manifest["k/wire.jsonl.xz"] == [st.st_mtime_ns, st.st_size]
    assert dest.remote["k/wire.jsonl.xz"] == len(client.objects["k/wire.jsonl.xz"])


def test_an_unchanged_text_file_is_not_uploaded_twice(tmp):
    """Hourly cron over the same tree: the second run must be free."""
    dest, client, _ = _store()
    src = _write(Path(tmp) / "wire.jsonl", "x\n" * 100)
    assert dest.upload_file(src, "k/wire.jsonl")
    assert not dest.upload_file(src, "k/wire.jsonl")
    assert client.puts == ["k/wire.jsonl.xz"]


def test_a_changed_text_file_is_uploaded_again(tmp):
    dest, client, _ = _store()
    src = _write(Path(tmp) / "wire.jsonl", "x\n" * 100)
    dest.upload_file(src, "k/wire.jsonl")
    _write(src, "x\n" * 400)
    assert dest.upload_file(src, "k/wire.jsonl")
    assert lzma.decompress(client.objects["k/wire.jsonl.xz"]) == src.read_bytes()


def test_an_incompressible_binary_is_stored_plain(tmp):
    """Storing it xz would make the object bigger than the file."""
    dest, client, _ = _store()
    blob = _incompressible(4096)
    src = _write(Path(tmp) / "blob.bin", blob)
    assert dest.upload_file(src, "k/blob.bin")
    assert client.objects["k/blob.bin"] == blob
    assert "k/blob.bin.xz" not in client.objects
    assert dest.remote["k/blob.bin"] == len(blob)
    assert dest.manifest == {}


def test_a_compressible_binary_is_stored_xz(tmp):
    dest, client, _ = _store()
    src = _write(Path(tmp) / "blob.bin", b"\0" * 8192)
    assert dest.upload_file(src, "k/blob.bin")
    assert lzma.decompress(client.objects["k/blob.bin.xz"]) == b"\0" * 8192


def test_an_unchanged_plain_binary_is_not_uploaded_twice(tmp):
    """The plain leg skips on a remote size match, not on the manifest."""
    dest, client, _ = _store()
    src = _write(Path(tmp) / "blob.bin", _incompressible(4096))
    dest.upload_file(src, "k/blob.bin")
    assert not dest.upload_file(src, "k/blob.bin")
    assert client.puts == ["k/blob.bin"]


def test_an_already_compressed_input_is_stored_verbatim(tmp):
    """Re-compressing a .xz input would double-wrap it."""
    dest, client, _ = _store()
    payload = lzma.compress(b"already squeezed")
    src = _write(Path(tmp) / "old.jsonl.xz", payload)
    assert dest.upload_file(src, "k/old.jsonl.xz")
    assert client.objects["k/old.jsonl.xz"] == payload
    assert "k/old.jsonl.xz.xz" not in client.objects
    assert dest.manifest == {}


def test_a_verbatim_object_already_the_right_size_is_skipped(tmp):
    dest, client, _ = _store()
    src = _write(Path(tmp) / "old.jsonl.xz", b"12345")
    dest.remote = {"k/old.jsonl.xz": 5}
    assert not dest.upload_file(src, "k/old.jsonl.xz")
    assert client.puts == []


def test_the_manifest_object_is_never_compressed_on_the_upload_path(tmp):
    """It is read back with json.loads; an xz'd manifest is unreadable."""
    dest, client, _ = _store()
    key = _STORE.manifest_key()
    src = _write(Path(tmp) / "manifest.json", b'{"a": [1, 2]}')
    assert dest.upload_file(src, key)
    assert json.loads(client.objects[key]) == {"a": [1, 2]}


def test_a_file_that_vanished_between_walk_and_upload_is_skipped(tmp):
    """A live session directory changes under the archiver mid-run."""
    dest, client, _ = _store()
    assert not dest.upload_file(Path(tmp) / "gone.jsonl", "k/gone.jsonl")
    assert client.puts == []


def test_a_dry_run_reports_the_upload_without_making_it(tmp):
    dest, client, said = _store(dry_run=True)
    src = _write(Path(tmp) / "wire.jsonl", "x\n" * 100)
    assert dest.upload_file(src, "k/wire.jsonl")
    assert client.puts == []
    assert dest.manifest == {}
    assert any("DRY upload k/wire.jsonl" in line for line in said)


def test_a_dry_run_reports_a_verbatim_upload_without_making_it(tmp):
    dest, client, said = _store(dry_run=True)
    src = _write(Path(tmp) / "old.jsonl.xz", lzma.compress(b"x"))
    assert dest.upload_file(src, "k/old.jsonl.xz")
    assert client.puts == []
    assert any("DRY upload k/old.jsonl.xz" in line for line in said)


# --- stale twins --------------------------------------------------------------------

def test_flipping_to_compressed_deletes_the_plain_twin(tmp):
    """Otherwise ingest sees one transcript under two keys."""
    dest, client, _ = _store()
    src = _write(Path(tmp) / "blob.bin", _incompressible(4096))
    dest.upload_file(src, "k/blob.bin")
    assert dest.remote["k/blob.bin"] == 4096
    _write(src, b"\0" * 8192)
    assert dest.upload_file(src, "k/blob.bin")
    assert client.deletes == ["k/blob.bin"]
    assert "k/blob.bin" not in dest.remote
    assert "k/blob.bin" not in client.objects
    assert "k/blob.bin.xz" in client.objects


def test_flipping_to_plain_deletes_the_compressed_twin_and_its_signature(tmp):
    dest, client, _ = _store()
    src = _write(Path(tmp) / "blob.bin", b"\0" * 8192)
    dest.upload_file(src, "k/blob.bin")
    assert "k/blob.bin.xz" in dest.manifest
    _write(src, _incompressible(4096))
    assert dest.upload_file(src, "k/blob.bin")
    assert client.deletes == ["k/blob.bin.xz"]
    assert dest.manifest == {}
    assert "k/blob.bin.xz" not in dest.remote
    assert client.objects["k/blob.bin"] == _incompressible(4096)


def test_a_failed_twin_delete_is_logged_and_the_upload_still_counts(tmp):
    """Losing the delete leaves a duplicate; losing the upload loses data."""
    dest, client, said = _store()
    src = _write(Path(tmp) / "blob.bin", _incompressible(4096))
    dest.upload_file(src, "k/blob.bin")
    client.fail_deletes.add("k/blob.bin")
    _write(src, b"\0" * 8192)
    assert dest.upload_file(src, "k/blob.bin")
    assert any("stale-twin delete failed: k/blob.bin" in line for line in said)
    assert "k/blob.bin.xz" in client.objects


def test_no_twin_delete_is_attempted_when_there_is_no_twin(tmp):
    """The common case; a delete per upload would double the request count."""
    dest, client, _ = _store()
    src = _write(Path(tmp) / "wire.jsonl", "x\n" * 100)
    dest.upload_file(src, "k/wire.jsonl")
    assert client.deletes == []


# --- upload_bytes --------------------------------------------------------------------

def test_upload_bytes_stores_the_body_under_the_key(tmp):
    dest, client, _ = _store()
    assert dest.upload_bytes(b'{"path": "/p"}', "sessions/h/project.json")
    assert client.objects["sessions/h/project.json"] == b'{"path": "/p"}'


def test_upload_bytes_records_what_it_stored(tmp):
    """Without this the same marker uploads again later in the same run --
    the Kimi copy omitted it, which is the defect the shared version fixes."""
    dest, client, _ = _store()
    dest.upload_bytes(b"12345", "sessions/h/project.json")
    assert dest.remote["sessions/h/project.json"] == 5
    assert not dest.upload_bytes(b"12345", "sessions/h/project.json")
    assert client.puts == ["sessions/h/project.json"]


def test_upload_bytes_skips_when_the_stored_size_already_matches(tmp):
    dest, client, _ = _store()
    dest.remote = {"k": 5}
    assert not dest.upload_bytes(b"12345", "k")
    assert client.puts == []


def test_upload_bytes_in_a_dry_run_puts_nothing(tmp):
    dest, client, said = _store(dry_run=True)
    assert dest.upload_bytes(b"12345", "k")
    assert client.puts == []
    assert any("DRY upload k" in line for line in said)


# --- upload_dir -----------------------------------------------------------------------

def test_upload_dir_keys_every_file_by_its_posix_relative_path(tmp):
    """The keys must be identical whichever OS uploaded them."""
    dest, client, _ = _store()
    root = Path(tmp) / "data"
    _write(root / "top.json", '{"a": 1}')
    _write(root / "nested" / "deep" / "note.txt", "hello")
    assert dest.upload_dir(root, "p/u/data") == 2
    assert sorted(client.objects) == ["p/u/data/nested/deep/note.txt.xz",
                                      "p/u/data/top.json.xz"]


def test_upload_dir_counts_objects_uploaded_not_files_visited(tmp):
    dest, _, _ = _store()
    root = Path(tmp) / "data"
    _write(root / "a.json", "{}")
    _write(root / "b.json", "{}")
    assert dest.upload_dir(root, "p") == 2
    assert dest.upload_dir(root, "p") == 0


def test_upload_dir_of_an_empty_directory_uploads_nothing(tmp):
    dest, client, _ = _store()
    root = Path(tmp) / "empty"
    root.mkdir()
    assert dest.upload_dir(root, "p") == 0
    assert client.objects == {}


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(dict(globals()))))
