"""Behaviour suite for the three archivers.

The existing suite (test_package_contract.py) reads the package as TEXT and
parses its ASTs — it asserts no credential literal ships and that the wheel
promises what it promises, and it deliberately never imports the code. That
is a real check, but it means nothing here has ever been executed, so line
coverage sat at 1%.

This one executes. It stays off the network entirely: every function tested
below is either pure, or filesystem-only against a temp dir. Nothing here
constructs an R2 client.

The two areas are chosen rather than swept:

  * KEY CONSTRUCTION decides where a session is filed in object storage,
    permanently. ingest reads the project and the thread id straight back
    out of the key, so a wrong key is wrong for the life of the object.

  * DELETION decides which local files get removed. `is_old` is the entire
    predicate behind `cleanup_local`, and an off-by-one there deletes
    someone's session history.
"""
# pylint: disable=protected-access
# These archivers are scripts, not a library: their helpers are
# module-private because nothing outside imports them, not because
# they are implementation detail behind a public API. `_is_text`
# picks a Content-Type and `is_old` decides what gets deleted —
# testing them through some public wrapper would mean testing an
# R2 upload to test a suffix check.
import json
import os
import time
from pathlib import Path

import _util

# Imported as a PACKAGE, not by path: these modules use relative imports
# (`from .settings import ...`), which have no parent package when a file
# is loaded standalone. See _util.load_package_module.
_CLAUDE = _util.load_package_module("claude")
_CODEX = _util.load_package_module("codex")
_KIMI = _util.load_package_module("kimi")
_SETTINGS = _util.load_package_module("settings")

_MODULES = (("claude", _CLAUDE), ("codex", _CODEX), ("kimi", _KIMI))


# --- content-type classification -------------------------------------------
# _is_text picks the Content-Type an object is uploaded with. Getting it
# wrong makes a transcript download as a binary blob.

def test_is_text_accepts_the_suffixes_the_archivers_upload(tmp):
    for name, mod in _MODULES:
        for key in ("sessions/a/wire.jsonl", "x.txt", "y.json", "z.js"):
            assert mod._is_text(key), f"{name}: {key} should be text"


def test_is_text_rejects_everything_else(tmp):
    for name, mod in _MODULES:
        for key in ("archive.tar.gz", "image.png", "wire.jsonl.xz", "noext"):
            assert not mod._is_text(key), f"{name}: {key} should not be text"


def test_is_text_is_suffix_anchored_not_substring(tmp):
    """`.jsonl` inside a name is not a `.jsonl` file."""
    for name, mod in _MODULES:
        assert not mod._is_text("a.jsonl.bak"), name
        assert not mod._is_text("notes-about-json"), name


# --- project hashing --------------------------------------------------------

def test_project_hash_is_stable_and_short(tmp):
    first = _CODEX.project_hash("/home/u/proj")
    assert first == _CODEX.project_hash("/home/u/proj")
    assert len(first) == 12
    assert all(c in "0123456789abcdef" for c in first)


def test_project_hash_separates_different_projects(tmp):
    assert _CODEX.project_hash("/home/u/a") != _CODEX.project_hash("/home/u/b")


def test_project_hash_tolerates_an_absent_cwd(tmp):
    """`rollout_key` guards against this, but the hash must not explode."""
    assert len(_CODEX.project_hash(None)) == 12
    assert len(_CODEX.project_hash("")) == 12


# --- rollout uuid extraction -------------------------------------------------

def test_rollout_uuid_takes_the_five_uuid_groups(tmp):
    name = "rollout-2026-08-21T10-30-00-0195f2a1-b2c3-4d5e-8f90-1a2b3c4d5e6f.jsonl"
    assert _CODEX.rollout_uuid(name) == "0195f2a1-b2c3-4d5e-8f90-1a2b3c4d5e6f"


def test_rollout_uuid_ignores_the_directory(tmp):
    path = os.path.join("a", "b", "rollout-2026-01-01T00-00-00-1-2-3-4-5.jsonl")
    assert _CODEX.rollout_uuid(path) == "1-2-3-4-5"


def test_rollout_uuid_falls_back_to_the_stem_when_unparseable(tmp):
    assert _CODEX.rollout_uuid("odd.jsonl") == "odd"


# --- rollout key construction -----------------------------------------------

def _meta(**over):
    meta = {"session_id": "thread-1", "cwd": "/home/u/proj"}
    meta.update(over)
    return meta


def test_rollout_key_places_a_main_rollout(tmp):
    key = _CODEX.rollout_key("rollout-x.jsonl", _meta())

    assert key == f"sessions/{_CODEX.project_hash('/home/u/proj')}/thread-1/wire.jsonl"


def test_rollout_key_keys_a_subagent_per_file(tmp):
    """A subagent shares its parent's session_id, so the key must not.

    Two subagent rollouts of the same parent would otherwise land on the
    same object and the second would overwrite the first.
    """
    name_a = "rollout-2026-01-01T00-00-00-aaaa-bbbb-cccc-dddd-eeee.jsonl"
    name_b = "rollout-2026-01-01T00-00-00-1111-2222-3333-4444-5555.jsonl"

    key_a = _CODEX.rollout_key(name_a, _meta(agent_path="/agents/x"))
    key_b = _CODEX.rollout_key(name_b, _meta(agent_path="/agents/x"))

    assert key_a != key_b
    assert "/subagents/" in key_a and "/subagents/" in key_b


def test_parent_thread_id_also_marks_a_subagent(tmp):
    key = _CODEX.rollout_key("rollout-a-b-c-d-e.jsonl",
                             _meta(parent_thread_id="thread-0"))

    assert "/subagents/" in key


def test_rollout_key_refuses_to_guess(tmp):
    """No identity means skipped, not filed under a guess.

    ingest reads the project and thread id straight back out of the key, so
    a guessed key is wrong for the life of the object.
    """
    assert _CODEX.rollout_key("r.jsonl", None) is None
    assert _CODEX.rollout_key("r.jsonl", {}) is None
    assert _CODEX.rollout_key("r.jsonl", {"session_id": "t"}) is None
    assert _CODEX.rollout_key("r.jsonl", {"cwd": "/p"}) is None


# --- session_meta reading ---------------------------------------------------

def _rollout(tmp, *records):
    path = Path(tmp) / "rollout-2026-01-01T00-00-00-a-b-c-d-e.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def test_read_session_meta_finds_the_opening_record(tmp):
    path = _rollout(tmp, {"type": "session_meta",
                          "payload": {"session_id": "t1", "cwd": "/p"}})

    assert _CODEX.read_session_meta(path) == {"session_id": "t1", "cwd": "/p"}


def test_read_session_meta_skips_records_before_it(tmp):
    path = _rollout(tmp,
                    {"type": "other"},
                    {"type": "session_meta", "payload": {"session_id": "t2"}})

    assert _CODEX.read_session_meta(path) == {"session_id": "t2"}


def test_read_session_meta_survives_a_malformed_line(tmp):
    path = Path(tmp) / "r.jsonl"
    path.write_text(
        '{"session_meta": broken\n'
        + json.dumps({"type": "session_meta", "payload": {"session_id": "t3"}})
        + "\n",
        encoding="utf-8")

    assert _CODEX.read_session_meta(path) == {"session_id": "t3"}


def test_read_session_meta_returns_none_for_a_non_dict_payload(tmp):
    path = _rollout(tmp, {"type": "session_meta", "payload": "nope"})

    assert _CODEX.read_session_meta(path) is None


def test_read_session_meta_returns_none_when_absent(tmp):
    path = _rollout(tmp, {"type": "other"}, {"type": "another"})

    assert _CODEX.read_session_meta(path) is None


def test_read_session_meta_returns_none_for_a_missing_file(tmp):
    assert _CODEX.read_session_meta(Path(tmp) / "nope.jsonl") is None


def test_read_session_meta_only_reads_the_head(tmp):
    """A rollout can be 20MB; the meta is the opening record.

    Pinned because the bound is the reason this is safe to call on every
    file in a directory.
    """
    records = [{"type": "filler", "i": i} for i in range(200)]
    records.append({"type": "session_meta", "payload": {"session_id": "late"}})
    path = _rollout(tmp, *records)

    assert _CODEX.read_session_meta(path) is None


# --- deletion predicate ------------------------------------------------------
# is_old is the entire predicate behind cleanup_local. Everything it returns
# True for is deleted.

def test_is_old_is_true_only_past_the_cutoff(tmp):
    path = Path(tmp) / "f"
    path.write_text("x", encoding="utf-8")
    old = time.time() - 10_000
    os.utime(path, (old, old))

    for name, mod in _MODULES:
        assert mod.is_old(path, time.time()) is True, name
        assert mod.is_old(path, old - 1) is False, name


def test_is_old_says_no_for_a_missing_path(tmp):
    """A file that vanished between listing and checking is not 'old'.

    Returning True there would send `cleanup_local` on to unlink a path it
    has already lost track of.
    """
    for name, mod in _MODULES:
        assert mod.is_old(Path(tmp) / "gone", time.time()) is False, name


def test_cleanup_local_dry_run_deletes_nothing(tmp):
    """The flag that makes this safe to run for a look."""
    debug = Path(tmp) / "debug"
    debug.mkdir()
    victim = debug / "old.log"
    victim.write_text("x", encoding="utf-8")
    old = time.time() - 10_000
    os.utime(victim, (old, old))

    original = _CLAUDE.DEBUG_DIR
    try:
        _CLAUDE.DEBUG_DIR = debug
        _CLAUDE.cleanup_local(time.time(), dry_run=True)
        assert victim.exists(), "dry run deleted a file"

        _CLAUDE.cleanup_local(time.time(), dry_run=False)
        assert not victim.exists(), "non-dry run left the file"
    finally:
        _CLAUDE.DEBUG_DIR = original


def test_cleanup_local_spares_recent_files(tmp):
    debug = Path(tmp) / "debug"
    debug.mkdir()
    keep = debug / "fresh.log"
    keep.write_text("x", encoding="utf-8")

    original = _CLAUDE.DEBUG_DIR
    try:
        _CLAUDE.DEBUG_DIR = debug
        _CLAUDE.cleanup_local(time.time() - 10_000, dry_run=False)
        assert keep.exists(), "deleted a file newer than the cutoff"
    finally:
        _CLAUDE.DEBUG_DIR = original


# --- machine identity --------------------------------------------------------

def test_machine_hash_is_stable_and_opaque(tmp):
    for name, mod in _MODULES:
        first = mod._machine_hash()
        assert first == mod._machine_hash(), name
        assert len(first) == 16, name
        # It is a hash of the machine id, so the id must not be readable
        # from it — this is why manifests can be named by it in a shared
        # bucket.
        assert all(c in "0123456789abcdef" for c in first), name


def test_manifest_key_is_per_machine(tmp):
    for name, mod in _MODULES:
        key = mod.manifest_key()
        assert key.startswith("manifests/"), name
        assert key.endswith(".json"), name
        assert mod._machine_hash() in key, name


# --- settings resolution -----------------------------------------------------

def test_setting_prefers_the_environment(tmp, ):
    os.environ["ARCHIVER_TEST_KEY"] = "from-env"
    try:
        assert _SETTINGS.setting("ARCHIVER_TEST_KEY") == "from-env"
    finally:
        del os.environ["ARCHIVER_TEST_KEY"]


def test_an_empty_environment_variable_counts_as_unset(tmp):
    """An exported-but-blank value is what a shell accident looks like.

    Adopting "" would take a deployment value down to nothing silently.
    """
    os.environ["ARCHIVER_TEST_KEY"] = ""
    try:
        assert _SETTINGS.setting("ARCHIVER_TEST_KEY", "fallback") == "fallback"
    finally:
        del os.environ["ARCHIVER_TEST_KEY"]


def test_setting_returns_the_default_when_absent(tmp):
    os.environ.pop("ARCHIVER_ABSENT_KEY", None)
    assert _SETTINGS.setting("ARCHIVER_ABSENT_KEY", "d") == "d"
    assert _SETTINGS.setting("ARCHIVER_ABSENT_KEY") is None


def test_required_names_the_key_and_the_file_it_belongs_in(tmp):
    os.environ.pop("ARCHIVER_ABSENT_KEY", None)
    try:
        _SETTINGS.required("ARCHIVER_ABSENT_KEY", hint="the bucket")
    except SystemExit as exc:
        message = str(exc)
        assert "ARCHIVER_ABSENT_KEY" in message
        assert "settings.json" in message
        assert "the bucket" in message
    else:
        raise AssertionError("required() accepted a missing value")


def test_required_returns_a_present_value(tmp):
    os.environ["ARCHIVER_TEST_KEY"] = "v"
    try:
        assert _SETTINGS.required("ARCHIVER_TEST_KEY") == "v"
    finally:
        del os.environ["ARCHIVER_TEST_KEY"]


def test_settings_files_are_read_and_merged(tmp):
    """settings.local.json wins, matching the order Claude Code uses."""
    base = Path(tmp) / "base"
    base.mkdir()
    (base / "settings.json").write_text(
        json.dumps({"env": {"K1": "a", "K2": "b"}}), encoding="utf-8")
    (base / "settings.local.json").write_text(
        json.dumps({"env": {"K2": "override"}}), encoding="utf-8")

    orig_dir, orig_legacy = _SETTINGS.SETTINGS_DIR, _SETTINGS.LEGACY_SETTINGS_DIR
    try:
        _SETTINGS.SETTINGS_DIR = base
        _SETTINGS.LEGACY_SETTINGS_DIR = Path(tmp) / "absent"
        merged = _SETTINGS._file_env(force=True)
        assert merged["K1"] == "a"
        assert merged["K2"] == "override"
    finally:
        _SETTINGS.SETTINGS_DIR, _SETTINGS.LEGACY_SETTINGS_DIR = orig_dir, orig_legacy
        _SETTINGS._file_env(force=True)


def test_a_malformed_settings_file_is_skipped_not_fatal(tmp):
    base = Path(tmp) / "base"
    base.mkdir()
    (base / "settings.json").write_text("{not json", encoding="utf-8")

    orig_dir, orig_legacy = _SETTINGS.SETTINGS_DIR, _SETTINGS.LEGACY_SETTINGS_DIR
    try:
        _SETTINGS.SETTINGS_DIR = base
        _SETTINGS.LEGACY_SETTINGS_DIR = Path(tmp) / "absent"
        assert _SETTINGS._file_env(force=True) == {}
    finally:
        _SETTINGS.SETTINGS_DIR, _SETTINGS.LEGACY_SETTINGS_DIR = orig_dir, orig_legacy
        _SETTINGS._file_env(force=True)


def test_non_string_settings_values_are_ignored(tmp):
    """These become environment variables, which are strings."""
    base = Path(tmp) / "base"
    base.mkdir()
    (base / "settings.json").write_text(
        json.dumps({"env": {"OK": "s", "BAD": 5, "ALSO_BAD": {"x": 1}}}),
        encoding="utf-8")

    orig_dir, orig_legacy = _SETTINGS.SETTINGS_DIR, _SETTINGS.LEGACY_SETTINGS_DIR
    try:
        _SETTINGS.SETTINGS_DIR = base
        _SETTINGS.LEGACY_SETTINGS_DIR = Path(tmp) / "absent"
        assert _SETTINGS._file_env(force=True) == {"OK": "s"}
    finally:
        _SETTINGS.SETTINGS_DIR, _SETTINGS.LEGACY_SETTINGS_DIR = orig_dir, orig_legacy
        _SETTINGS._file_env(force=True)


# --- locking -----------------------------------------------------------------

def test_acquire_lock_is_exclusive(tmp):
    """Two archivers on one machine must not upload concurrently."""
    lock = Path(tmp) / "archiver.lock"
    original = _CLAUDE.LOCK_FILE
    try:
        _CLAUDE.LOCK_FILE = lock
        first = _CLAUDE.acquire_lock()
        if first is None:
            _util.skip("advisory locking unavailable on this platform")
        second = _CLAUDE.acquire_lock()
        assert second is None, "a second holder acquired the same lock"
        first.close()
    finally:
        _CLAUDE.LOCK_FILE = original


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(dict(globals()))))
