"""What each archiver decides on its own, before anything is uploaded.

The shared halves are tested where they live: the destination in
test_store.py, the host scaffolding in test_runtime.py, the per-archiver
walks in test_archive_walks.py. What is left here is the reasoning no other
archiver shares.

  * KEY CONSTRUCTION decides where a session is filed in object storage,
    permanently. Ingest reads the project and the thread id straight back out
    of the key, so a wrong key is wrong for the life of the object. Codex is
    the interesting case: its on-disk layout carries no project at all, so
    the archiver derives one.

  * SETTINGS RESOLUTION decides which account and which bucket an archiver
    talks to, and refuses to run rather than guess. Every archiver reads its
    own R2_BUCKET_* through this.

Nothing here touches the network, and nothing here needs a client.
"""
# pylint: disable=protected-access
# `_file_env` is private because it is a cache, not because the resolution
# order it implements is an implementation detail -- that order IS the
# contract, and it is what these tests pin.
import json
import os
from pathlib import Path

import _util

# Imported as a PACKAGE, not by path: these modules use relative imports
# (`from .settings import ...`), which have no parent package when a file
# is loaded standalone. See _util.load_package_module.
_CODEX = _util.load_package_module("codex")
_PROVIDER = _util.load_package_module("provider")
_SETTINGS = _util.load_package_module("settings")


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


# --- transcript provider classification --------------------------------------

def _assistant(model):
    return {"type": "assistant",
            "message": {"role": "assistant", "model": model}}


def _jsonl_bytes(*entries):
    return ("\n".join(json.dumps(e) for e in entries) + "\n").encode("utf-8")


def test_a_glm_model_classifies_as_zai(tmp):
    path = Path(tmp) / "s.jsonl"
    path.write_bytes(_jsonl_bytes(
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        _assistant("glm-5.3-flash")))
    assert _PROVIDER.of_file(path) == ({"zai"}, set())


def test_a_bonsai_model_classifies_as_llama(tmp):
    """bonsai-2-27b is what a llama.cpp server reports for that GGUF."""
    path = Path(tmp) / "s.jsonl"
    path.write_bytes(_jsonl_bytes(_assistant("bonsai-2-27b")))
    assert _PROVIDER.of_file(path) == ({"llama"}, set())


def test_a_llama_model_classifies_as_llama(tmp):
    path = Path(tmp) / "s.jsonl"
    path.write_bytes(_jsonl_bytes(_assistant("llama-3.3-70b")))
    assert _PROVIDER.of_file(path) == ({"llama"}, set())


def test_a_claude_model_classifies_as_claude(tmp):
    path = Path(tmp) / "s.jsonl"
    path.write_bytes(_jsonl_bytes(_assistant("claude-opus-5")))
    assert _PROVIDER.of_file(path) == ({"claude"}, set())


def test_a_fable_model_also_classifies_as_claude(tmp):
    path = Path(tmp) / "s.jsonl"
    path.write_bytes(_jsonl_bytes(_assistant("claude-fable-5")))
    assert _PROVIDER.of_file(path) == ({"claude"}, set())


def test_every_family_present_is_collected_not_just_the_first(tmp):
    """A session can switch harnesses mid-stream, so the scan reads every
    assistant entry and returns EVERY family it names — the first no longer
    decides, and file order does not matter."""
    glm_first = Path(tmp) / "glm-first.jsonl"
    glm_first.write_bytes(_jsonl_bytes(_assistant("glm-5.3-flash"),
                                       _assistant("claude-opus-5")))
    assert _PROVIDER.of_file(glm_first) == ({"zai", "claude"}, set())

    claude_first = Path(tmp) / "claude-first.jsonl"
    claude_first.write_bytes(_jsonl_bytes(_assistant("claude-opus-5"),
                                          _assistant("glm-5.3-flash")))
    assert _PROVIDER.of_file(claude_first) == ({"zai", "claude"}, set())


def test_a_model_mention_in_user_text_never_classifies(tmp):
    """Only `type: assistant` entries carry a model the archiver trusts."""
    path = Path(tmp) / "s.jsonl"
    path.write_bytes(_jsonl_bytes(
        {"type": "user", "message": {"role": "user",
         "content": 'run this on "model":"claude-opus-5" please'}}))
    assert _PROVIDER.of_file(path) == (set(), set())


def test_unknown_models_are_collected_and_synthetic_is_ignored(tmp):
    """Unknown ids come back so the caller can log them; `<synthetic>` is
    Claude Code's placeholder for locally generated messages and counts as
    neither known nor unknown."""
    mixed = Path(tmp) / "s.jsonl"
    mixed.write_bytes(_jsonl_bytes(_assistant("glm-5.3-flash"),
                                   _assistant("stealth/space-bunny-alpha")))
    assert _PROVIDER.of_file(mixed) == ({"zai"}, {"stealth/space-bunny-alpha"})

    only_unknown = Path(tmp) / "unknown.jsonl"
    only_unknown.write_bytes(
        _jsonl_bytes(_assistant("nvidia/nemotron-3-ultra-550b-a55b:free")))
    assert _PROVIDER.of_file(only_unknown) == (
        set(), {"nvidia/nemotron-3-ultra-550b-a55b:free"})

    synthetic = Path(tmp) / "synthetic.jsonl"
    synthetic.write_bytes(_jsonl_bytes(_assistant("<synthetic>")))
    assert _PROVIDER.of_file(synthetic) == (set(), set())

    synthetic_and_glm = Path(tmp) / "synthetic-glm.jsonl"
    synthetic_and_glm.write_bytes(_jsonl_bytes(_assistant("<synthetic>"),
                                               _assistant("glm-5.3-flash")))
    assert _PROVIDER.of_file(synthetic_and_glm) == ({"zai"}, set())


def test_a_truncated_final_line_still_classifies_from_what_precedes(tmp):
    body = _jsonl_bytes(_assistant("claude-opus-5")) + b'{"type": "as'
    path = Path(tmp) / "s.jsonl"
    path.write_bytes(body)
    assert _PROVIDER.of_file(path) == ({"claude"}, set())


def test_an_absent_transcript_classifies_as_none(tmp):
    assert _PROVIDER.of_file(Path(tmp) / "nope.jsonl") == (set(), set())


def test_transcript_in_takes_the_first_jsonl(tmp):
    directory = Path(tmp) / "uuid-dir"
    _write_jsonl(directory / "b.jsonl", _assistant("claude-opus-5"))
    _write_jsonl(directory / "a.jsonl", _assistant("glm-5.3-flash"))
    assert _PROVIDER.transcript_in(directory) == directory / "a.jsonl"


def test_transcript_in_is_none_without_a_jsonl(tmp):
    directory = Path(tmp) / "uuid-dir"
    directory.mkdir()
    assert _PROVIDER.transcript_in(directory) is None
    assert _PROVIDER.transcript_in(Path(tmp) / "absent") is None


def _write_jsonl(path, *entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_jsonl_bytes(*entries))


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(dict(globals()))))
