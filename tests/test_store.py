"""The destination: compression policy, manifest, inventory, upload forms.

The three archivers once carried a copy each, and a suite that proved one
thing three times. One implementation, one suite.

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


_write = _util.write


def _incompressible(n):
    """`n` bytes xz cannot shrink, reproducibly: high entropy reaches the
    binary leg without depending on one lzma version's ratio."""
    out = bytearray()
    seed = b"session-archivers"
    while len(out) < n:
        seed = hashlib.sha256(seed).digest()
        out += seed
    return bytes(out[:n])


# --- content-type classification ---------------------------------------------

def test_is_text_accepts_the_suffixes_the_archivers_upload_and_no_other(tmp):
    """Suffix-anchored: `.jsonl` inside a name is not a `.jsonl` file."""
    for key in ("sessions/a/wire.jsonl", "x.txt", "y.json", "z.js"):
        assert _STORE.is_text(key), key
    for key in ("archive.tar.gz", "image.png", "wire.jsonl.xz", "noext",
                "a.jsonl.bak", "notes-about-json"):
        assert not _STORE.is_text(key), key


# --- machine identity ----------------------------------------------------------

def test_machine_hash_is_stable_and_opaque(tmp):
    """Opaque because the manifest object name must not leak the hostname;
    per machine, so N machines write N manifests and never clobber each other."""
    first = _STORE.machine_hash()
    assert first == _STORE.machine_hash()
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)
    assert socket.gethostname() not in first
    assert _STORE.manifest_key() == f"{_STORE.MANIFEST_PREFIX}/{first}.json"


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
    """Falling back to the Cloudflare account: the R2 bucket lives in that
    account, so one value, not two that drift."""
    with _Deployment(tmp, R2_ACCOUNT_ID="acct-1", R2_ACCESS_KEY_ID="ak",
                     R2_SECRET_ACCESS_KEY="sk"):
        assert _STORE.r2_config() == ("acct-1", "ak", "sk")
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
    # Nothing in Store is module-level, so two can coexist in one process.
    claude_client = _util.FakeS3("claude")
    _STORE.Store(claude_client, "claude", [].append).upload_bytes(b"a", "k")
    dest.upload_bytes(b"bb", "k")
    assert (claude_client.objects, client.objects["k"]) == ({"k": b"a"}, b"bb")


# --- manifest round-trip --------------------------------------------------------

def test_manifest_saves_and_loads_back_the_same_entries(tmp):
    """The manifest is how a stored object is recognised as up to date. A
    reader fetches it with get_object and json.loads, nothing else."""
    dest, client, _ = _store()
    dest.manifest = {"a/b.jsonl.xz": [1234567890, 42]}
    dest.save_manifest()
    assert json.loads(client.objects[_STORE.manifest_key()]) == {
        "a/b.jsonl.xz": [1234567890, 42]}
    assert dest.load_manifest() == {"a/b.jsonl.xz": [1234567890, 42]}


def test_an_absent_corrupt_or_wrong_shape_manifest_reads_as_empty(tmp):
    """None yet on a new machine; a truncated object costs one re-upload, not
    the whole run; a JSON list parses and would break every .get()."""
    for body in (None, b"{not json", b'["a", "b"]'):
        dest, client, _ = _store()
        if body is not None:
            client.objects[_STORE.manifest_key()] = body
        assert dest.load_manifest() == {}, body


# --- inventory -------------------------------------------------------------------

def test_inventory_returns_every_key_with_its_stored_size_from_every_page(tmp):
    """One page holds 1000 keys on R2; a real bucket is many pages."""
    assert _store()[0].inventory() == {}
    dest, client, _ = _store(_util.FakeS3(_BUCKET, page_size=3))
    client.objects = {f"k{i:03d}": b"x" * (i + 1) for i in range(10)}
    expected = {f"k{i:03d}": i + 1 for i in range(10)}
    assert dest.inventory() == expected
    assert dest.remote == expected


def _sig(path):
    st = path.stat()
    return [st.st_mtime_ns, st.st_size]


def test_holds_recognises_every_storage_form_of_the_current_bytes(tmp):
    dest, client, _ = _store()
    text = _write(Path(tmp) / "text.jsonl", "x\n" * 100)
    plain = _write(Path(tmp) / "plain.bin", _incompressible(4096))
    packed = _write(Path(tmp) / "old.jsonl.xz", lzma.compress(b"squeezed"))
    for src, key in ((text, "a/text.jsonl"), (plain, "a/plain.bin"),
                     (packed, "a/old.jsonl.xz")):
        assert not dest.holds(src, key), key
        assert dest.upload_file(src, key)
        assert dest.holds(src, key), key
    assert sorted(client.objects) == ["a/old.jsonl.xz", "a/plain.bin",
                                      "a/text.jsonl.xz"]
    client.fail_puts.add("a/failed.bin")
    failed = _write(Path(tmp) / "failed.bin", b"NEW!")
    try:
        dest.upload_file(failed, "a/failed.bin")
    except RuntimeError as e:
        assert "put refused: a/failed.bin" in str(e)
    else:
        raise AssertionError("the injected put failure never fired")
    assert not dest.holds(failed, "a/failed.bin"), "a failed upload is never held"


def test_holds_needs_the_object_and_the_signature_of_the_current_bytes(tmp):
    """Same size is not same bytes; a manifest entry alone is no proof (the
    object may since have been deleted remotely); a vanished file has
    nothing to compare against."""
    dest, _, _ = _store()
    plain = _write(Path(tmp) / "plain.bin", b"NEW!", days_old=1)
    text = _write(Path(tmp) / "text.jsonl", "{}\n", days_old=1)
    dest.remote = {"a/plain.bin": 4, "a/text.jsonl.xz": 3}
    assert not dest.holds(plain, "a/plain.bin"), "same size, no signature"
    assert not dest.holds(text, "a/text.jsonl"), "same size, no signature"
    dest.manifest = {"a/plain.bin": [_sig(plain)[0] - 1, 4],
                     "a/text.jsonl.xz": [_sig(text)[0] - 1, 3]}
    assert not dest.holds(plain, "a/plain.bin"), "signature of older bytes"
    assert not dest.holds(text, "a/text.jsonl"), "signature of older bytes"
    dest.remote = {}
    dest.manifest = {"a/plain.bin": _sig(plain), "a/plain.bin.xz": _sig(plain)}
    assert not dest.holds(plain, "a/plain.bin"), "signature, no object"
    dest.remote = {"a/plain.bin": 4}
    assert dest.holds(plain, "a/plain.bin")
    plain.unlink()
    assert not dest.holds(plain, "a/plain.bin"), "vanished"


def test_holds_dir_is_true_only_while_every_file_is_stored_as_it_is(tmp):
    """Keyed exactly as upload_dir keys them, nested paths included."""
    dest, _, _ = _store()
    root = Path(tmp) / "data"
    _write(root / "top.json", '{"a": 1}', days_old=2)
    blob = _write(root / "nested" / "deep" / "blob.bin", _incompressible(4096),
                  days_old=2)
    assert not dest.holds_dir(root, "p/u/data")
    assert dest.upload_dir(root, "p/u/data") == 2
    assert dest.holds_dir(root, "p/u/data")
    assert not dest.holds_dir(root, "p/u/other")
    _write(blob, _incompressible(4096)[::-1], days_old=1)  # same size
    assert not dest.holds_dir(root, "p/u/data"), "one file changed since upload"
    dest.upload_dir(root, "p/u/data")
    _write(root / "late.json", "{}")
    assert not dest.holds_dir(root, "p/u/data"), "one file never uploaded"


# --- compression policy ------------------------------------------------------------

def test_text_is_stored_xz_signed_skipped_unchanged_and_redone_changed(tmp):
    """Readers lzma-decompress the object (stdlib, no dependency). Its size
    cannot be compared to the file's, so the signature makes the next run
    free. No twin, so no delete: one per upload would double the requests."""
    dest, client, _ = _store()
    src = _write(Path(tmp) / "wire.jsonl", '{"a": 1}\n' * 200)
    assert dest.upload_file(src, "k/wire.jsonl")
    assert lzma.decompress(client.objects["k/wire.jsonl.xz"]) == src.read_bytes()
    assert "k/wire.jsonl" not in client.objects and client.deletes == []
    assert dest.manifest["k/wire.jsonl.xz"] == _sig(src)
    assert dest.remote["k/wire.jsonl.xz"] == len(client.objects["k/wire.jsonl.xz"])
    assert not dest.upload_file(src, "k/wire.jsonl")
    # A live session directory changes under the archiver mid-run.
    assert not dest.upload_file(Path(tmp) / "gone.jsonl", "k/gone.jsonl")
    assert client.puts == ["k/wire.jsonl.xz"]
    _write(src, "x\n" * 400)
    assert dest.upload_file(src, "k/wire.jsonl")
    assert lzma.decompress(client.objects["k/wire.jsonl.xz"]) == src.read_bytes()


def test_an_incompressible_binary_is_stored_plain_a_compressible_one_xz(tmp):
    """Storing the first xz would make the object bigger than the file."""
    dest, client, _ = _store()
    blob = _incompressible(4096)
    src = _write(Path(tmp) / "blob.bin", blob)
    zeros = _write(Path(tmp) / "zeros.bin", b"\0" * 8192)
    assert dest.upload_file(src, "k/blob.bin")
    assert dest.upload_file(zeros, "k/zeros.bin")
    assert client.objects["k/blob.bin"] == blob
    assert "k/blob.bin.xz" not in client.objects
    assert dest.remote["k/blob.bin"] == len(blob)
    assert lzma.decompress(client.objects["k/zeros.bin.xz"]) == b"\0" * 8192
    assert dest.manifest == {"k/blob.bin": _sig(src), "k/zeros.bin.xz": _sig(zeros)}


def test_a_plain_binary_is_skipped_unchanged_and_uploaded_at_the_same_size(tmp):
    dest, client, _ = _store()
    blob = _incompressible(4096)
    src = _write(Path(tmp) / "blob.bin", blob, days_old=2)
    dest.upload_file(src, "k/blob.bin")
    assert dest.manifest == {"k/blob.bin": _sig(src)}
    assert not dest.upload_file(src, "k/blob.bin")
    assert client.puts == ["k/blob.bin"]
    _write(src, blob[::-1], days_old=1)
    assert dest.upload_file(src, "k/blob.bin")
    assert client.objects["k/blob.bin"] == blob[::-1]
    assert client.puts == ["k/blob.bin", "k/blob.bin"]
    assert dest.manifest == {"k/blob.bin": _sig(src)}


def test_a_changed_size_under_the_recorded_mtime_is_not_held(tmp):
    """The signature is both fields, so this is not the stored bytes."""
    dest, client, _ = _store()
    src = _write(Path(tmp) / "wire.jsonl", "x\n" * 100, days_old=2)
    stat = src.stat()
    assert dest.upload_file(src, "k/wire.jsonl")
    src.write_bytes(b"y\n" * 50)
    os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert src.stat().st_mtime_ns == stat.st_mtime_ns
    assert not dest.holds(src, "k/wire.jsonl")
    assert dest.upload_file(src, "k/wire.jsonl")
    assert lzma.decompress(client.objects["k/wire.jsonl.xz"]) == b"y\n" * 50


def test_a_same_size_object_with_no_recorded_signature_is_replaced_once(tmp):
    """Stored before plain and verbatim uploads were signed: the size
    matches, so nothing proves its bytes are these. One re-upload, then
    signed; a stale signature does not skip either."""
    for key in ("k/blob.bin", "k/old.jsonl.xz"):
        dest, client, _ = _store()
        src = _write(Path(tmp) / key, b"NEW!")
        client.objects = {key: b"OLD!"}
        dest.inventory()
        assert dest.upload_file(src, key), key
        assert client.objects[key] == b"NEW!", key
        assert not dest.upload_file(src, key), key
        dest.manifest = {key: [_sig(src)[0] - 1, 4]}
        assert dest.upload_file(src, key), key
        assert client.puts == [key, key], key


def test_a_manifest_written_before_plain_signatures_is_honoured(tmp):
    dest, client, _ = _store()
    src = _write(Path(tmp) / "wire.jsonl", "x\n" * 100)
    client.objects = {
        "k/wire.jsonl.xz": lzma.compress(src.read_bytes()),
        _STORE.manifest_key(): json.dumps(
            {"k/wire.jsonl.xz": _sig(src)}).encode("utf-8"),
    }
    dest.inventory()
    dest.load_manifest()
    assert dest.holds(src, "k/wire.jsonl")
    assert not dest.upload_file(src, "k/wire.jsonl")
    assert client.puts == []


def test_an_already_compressed_input_is_stored_verbatim(tmp):
    """Re-compressing a .xz input would double-wrap it."""
    dest, client, _ = _store()
    payload = lzma.compress(b"already squeezed")
    src = _write(Path(tmp) / "old.jsonl.xz", payload)
    assert dest.upload_file(src, "k/old.jsonl.xz")
    assert client.objects["k/old.jsonl.xz"] == payload
    assert "k/old.jsonl.xz.xz" not in client.objects
    assert dest.manifest == {"k/old.jsonl.xz": _sig(src)}
    assert not dest.upload_file(src, "k/old.jsonl.xz")  # matching signature
    assert client.puts == ["k/old.jsonl.xz"]


def test_the_manifest_object_is_never_compressed_on_the_upload_path(tmp):
    """It is read back with json.loads; an xz'd manifest is unreadable.
    Nor does it record a signature for itself, so it is always written."""
    dest, client, _ = _store()
    key = _STORE.manifest_key()
    src = _write(Path(tmp) / "manifest.json", b'{"a": [1, 2]}')
    assert dest.upload_file(src, key)
    assert json.loads(client.objects[key]) == {"a": [1, 2]}
    assert dest.manifest == {}
    assert dest.upload_file(src, key)
    assert client.puts == [key, key]


def test_a_dry_run_puts_nothing_and_names_what_a_real_run_stores(tmp):
    """A text key always lands at `<key>.xz`; a binary's form is decided by
    compressing it, which a preview skips, so the size stated is the input's.
    What it would upload counts as held: it previews a real run's deletions."""
    dest, client, said = _store(dry_run=True)
    text = _write(Path(tmp) / "wire.jsonl", "x\n" * 1000)
    root = Path(tmp) / "data"
    _write(root / "blob.bin", _incompressible(4096))
    packed = _write(root / "old.jsonl.xz", lzma.compress(b"x"))
    original = _STORE.lzma.compress

    def _refuse(*_a, **_kw):
        raise AssertionError("a dry run compressed its input")

    _STORE.lzma.compress = _refuse
    try:
        assert dest.upload_file(text, "k/wire.jsonl")
        assert dest.upload_dir(root, "p") == 2
    finally:
        _STORE.lzma.compress = original
    assert dest.upload_bytes(b"12345", "k")
    dest.save_manifest()
    assert (client.puts, dest.manifest) == ([], {})
    assert sorted(said) == sorted([
        "  DRY upload k/wire.jsonl.xz "
        "(2,000 B uncompressed; compressed size not computed)",
        "  DRY upload p/blob.bin or p/blob.bin.xz "
        "(4,096 B uncompressed; stored form undetermined until upload)",
        f"  DRY upload p/old.jsonl.xz ({packed.stat().st_size:,} B)",
        "  DRY upload k (5 B)", f"  DRY put manifest {_STORE.manifest_key()} (0 entries)"])
    assert dest.holds(text, "k/wire.jsonl")
    assert dest.holds_dir(root, "p")
    other = _write(Path(tmp) / "other.jsonl", "y\n")
    assert not dest.holds(other, "k/other.jsonl")  # never planned


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
    assert dest.manifest == {"k/blob.bin.xz": _sig(src)}


def test_flipping_to_plain_deletes_the_compressed_twin_and_its_signature(tmp):
    dest, client, _ = _store()
    src = _write(Path(tmp) / "blob.bin", b"\0" * 8192)
    dest.upload_file(src, "k/blob.bin")
    assert "k/blob.bin.xz" in dest.manifest
    _write(src, _incompressible(4096))
    assert dest.upload_file(src, "k/blob.bin")
    assert client.deletes == ["k/blob.bin.xz"]
    assert dest.manifest == {"k/blob.bin": _sig(src)}
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
    # The twin outlived its delete, but not its signature: its bytes are old.
    assert dest.manifest == {"k/blob.bin.xz": _sig(src)}


# --- upload_bytes --------------------------------------------------------------------

def test_upload_bytes_stores_the_body_and_records_what_it_stored(tmp):
    """Without the record the same marker uploads again later in the same
    run -- the Kimi copy omitted it, the defect the shared version fixes."""
    dest, client, _ = _store()
    assert dest.upload_bytes(b"12345", "sessions/h/project.json")
    assert client.objects["sessions/h/project.json"] == b"12345"
    assert dest.remote["sessions/h/project.json"] == 5
    assert not dest.upload_bytes(b"12345", "sessions/h/project.json")
    assert client.puts == ["sessions/h/project.json"]


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
    assert dest.upload_dir(root, "p/u/data") == 0  # objects uploaded, not visited
    (Path(tmp) / "empty").mkdir()
    assert dest.upload_dir(Path(tmp) / "empty", "p") == 0
    assert len(client.objects) == 2


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(dict(globals()))))
