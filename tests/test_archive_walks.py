"""What each archiver does with its own tree, and what its main() drives.

The destination is tested once in test_store.py and the host scaffolding once
in test_runtime.py. What is left, and what genuinely differs between the
three, is the WALK: which files an archiver finds, which key it files each one
under, what it counts, and what it is willing to delete afterwards.

Off the network throughout. Every test builds a Store around `_util.FakeS3`;
the two-per-archiver main() tests replace `store.client` so the real one is
never called.

Each test rebinds the module-level directory constants it needs onto the
temp dir and restores them -- see `sandbox`. A test that forgot would archive,
and then delete, the session history of whoever ran the suite.
"""
# pylint: disable=protected-access
# The archivers are scripts: `_retire_local` and `_workdir_from_state` are
# private because nothing imports them, not because there is an API in front.
import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import _util

_CLAUDE = _util.load_package_module("claude")
_CODEX = _util.load_package_module("codex")
_KIMI = _util.load_package_module("kimi")
_STORE = _util.load_package_module("store")

_BUCKET = "test-bucket"


@contextlib.contextmanager
def sandbox(mod, **attrs):
    """Point one archiver's module-level constants at a temp tree."""
    saved = {k: getattr(mod, k) for k in attrs}
    for key, value in attrs.items():
        setattr(mod, key, value)
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(mod, key, value)


def _dest(dry_run=False):
    """A Store over the stub, plus the client and the lines it logged."""
    client = _util.FakeS3(_BUCKET)
    said = []
    return _STORE.Store(client, _BUCKET, said.append, dry_run), client, said


def _write(path, data, days_old=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    if days_old is not None:
        _age(path, days_old)
    return path


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _cutoff(days=3):
    return time.time() - days * 86400


def _run_main(mod, tmp, argv, client, **attrs):
    """Drive an archiver's main() with the stub client and a sandboxed tree."""
    saved_argv = sys.argv
    saved_client = _STORE.client
    attrs.setdefault("BUCKET", _BUCKET)
    attrs.setdefault("LOG_FILE", Path(tmp) / "archive.log")
    attrs.setdefault("LOCK_FILE", Path(tmp) / "run.lock")
    try:
        sys.argv = argv
        _STORE.client = lambda: client
        with sandbox(mod, **attrs):
            code = mod.main()
        log_path = attrs["LOG_FILE"]
        written = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        return code, written
    finally:
        sys.argv = saved_argv
        _STORE.client = saved_client


# --- claude: archive_projects --------------------------------------------------

def test_claude_archives_a_transcript_and_its_data_dir(tmp):
    projects = Path(tmp) / "projects"
    uuid = "1111-2222"
    _write(projects / "-root-proj" / f"{uuid}.jsonl", '{"type": "user"}\n')
    _write(projects / "-root-proj" / uuid / "tasks.json", "{}")
    dest, client, _ = _dest()
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff(1)) == (2, 0, 0)
    assert sorted(client.objects) == [
        f"-root-proj/{uuid}/{uuid}.jsonl.xz",
        f"-root-proj/{uuid}/data/tasks.json.xz",
    ]


def test_claude_deletes_a_stale_transcript_and_its_data_dir(tmp):
    projects = Path(tmp) / "projects"
    uuid = "aaaa-bbbb"
    jsonl = _write(projects / "p" / f"{uuid}.jsonl", '{"x": 1}\n', days_old=10)
    data = _write(projects / "p" / uuid / "blob.json", "{}")
    dest, client, _ = _dest()
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff()) == (2, 1, 0)
    assert not jsonl.exists()
    assert not data.parent.exists()
    assert f"p/{uuid}/{uuid}.jsonl.xz" in client.objects


def test_claude_keeps_a_fresh_transcript(tmp):
    projects = Path(tmp) / "projects"
    jsonl = _write(projects / "p" / "fresh.jsonl", "{}\n")
    dest, _, _ = _dest()
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff()) == (1, 0, 0)
    assert jsonl.exists()


def test_claude_dry_run_uploads_nothing_and_deletes_nothing(tmp):
    projects = Path(tmp) / "projects"
    uuid = "iiii-jjjj"
    jsonl = _write(projects / "p" / f"{uuid}.jsonl", "{}\n", days_old=10)
    data = _write(projects / "p" / uuid / "blob.json", "{}")
    dest, client, said = _dest(dry_run=True)
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff()) == (2, 1, 0)
    assert client.puts == []
    assert jsonl.exists() and data.exists()
    assert any("DRY rm " in line for line in said)
    assert any("DRY rmtree" in line for line in said)


def test_claude_archives_an_orphan_uuid_dir(tmp):
    """A data dir whose transcript is already gone still holds artifacts."""
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "cccc-dddd" / "out.txt", "left behind")
    dest, client, _ = _dest()
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff(1)) == (1, 0, 0)
    assert "p/cccc-dddd/data/out.txt.xz" in client.objects


def test_claude_deletes_a_stale_orphan_dir(tmp):
    projects = Path(tmp) / "projects"
    orphan = projects / "p" / "eeee-ffff"
    _write(orphan / "out.txt", "left behind")
    _age(orphan, 10)
    dest, _, _ = _dest()
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff()) == (1, 1, 0)
    assert not orphan.exists()


def test_claude_dry_run_keeps_a_stale_orphan_dir(tmp):
    projects = Path(tmp) / "projects"
    orphan = projects / "p" / "mmmm-nnnn"
    _write(orphan / "out.txt", "x")
    _age(orphan, 10)
    dest, _, said = _dest(dry_run=True)
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff()) == (1, 1, 0)
    assert orphan.exists()
    assert any("DRY rmtree" in line for line in said)


def test_claude_never_archives_the_memory_and_tasks_directories(tmp):
    """They are working state, not session history, and memory/ is private."""
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "memory" / "note.md", "private")
    _write(projects / "p" / "tasks" / "t.output", "scratch")
    dest, client, _ = _dest()
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff(1)) == (0, 0, 0)
    assert client.objects == {}


def test_claude_ignores_a_loose_file_where_a_project_should_be(tmp):
    projects = Path(tmp) / "projects"
    _write(projects / "stray.txt", "not a project")
    dest, client, _ = _dest()
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff(1)) == (0, 0, 0)
    assert client.objects == {}


def test_claude_does_not_delete_a_transcript_whose_upload_failed(tmp):
    """A failed upload must never take the only copy with it."""
    projects = Path(tmp) / "projects"
    jsonl = _write(projects / "p" / "doomed.jsonl", "{}\n", days_old=10)
    dest, client, said = _dest()
    client.fail_puts.add("p/doomed/doomed.jsonl.xz")
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff()) == (0, 0, 1)
    assert jsonl.exists()
    assert any("upload failed" in line for line in said)


def test_claude_does_not_delete_when_the_data_dir_upload_failed(tmp):
    projects = Path(tmp) / "projects"
    uuid = "gggg-hhhh"
    jsonl = _write(projects / "p" / f"{uuid}.jsonl", "{}\n", days_old=10)
    _write(projects / "p" / uuid / "blob.json", "{}")
    dest, client, said = _dest()
    client.fail_puts.add(f"p/{uuid}/data/blob.json.xz")
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff()) == (1, 0, 1)
    assert jsonl.exists()
    assert any("not deleting either" in line for line in said)


def test_claude_counts_a_failed_orphan_upload_and_keeps_the_dir(tmp):
    projects = Path(tmp) / "projects"
    orphan = projects / "p" / "kkkk-llll"
    _write(orphan / "out.txt", "left behind")
    _age(orphan, 10)
    dest, client, said = _dest()
    client.fail_puts.add("p/kkkk-llll/data/out.txt.xz")
    with sandbox(_CLAUDE, PROJECTS_DIR=projects):
        assert _CLAUDE.archive_projects(dest, _cutoff()) == (0, 0, 1)
    assert orphan.exists()
    assert any("orphan uuid_dir" in line for line in said)


def test_claude_on_a_machine_with_no_projects_dir(tmp):
    dest, _, _ = _dest()
    with sandbox(_CLAUDE, PROJECTS_DIR=Path(tmp) / "absent"):
        assert _CLAUDE.archive_projects(dest, 0) == (0, 0, 0)


def test_claude_cleanup_sweeps_all_three_scratch_dirs(tmp):
    root = Path(tmp) / "claude-home"
    stale_debug = _write(root / "debug" / "old.log", "x", days_old=10)
    fresh_debug = _write(root / "debug" / "new.log", "x")
    stale_tel = _write(root / "telemetry" / "old.jsonl", "x", days_old=10)
    hist = root / "file-history" / "session-1"
    _write(hist / "a.txt", "x")
    _age(hist, 10)
    with sandbox(_CLAUDE, DEBUG_DIR=root / "debug",
                 FILE_HISTORY_DIR=root / "file-history",
                 TELEMETRY_DIR=root / "telemetry"):
        _CLAUDE.cleanup_local(_cutoff(), False, print)
    assert not stale_debug.exists()
    assert fresh_debug.exists()
    assert not stale_tel.exists()
    assert not hist.exists()


# --- claude: main ----------------------------------------------------------------

def test_claude_main_archives_uploads_and_saves_the_manifest(tmp):
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s1.jsonl", '{"type": "user"}\n')
    client = _util.FakeS3(_BUCKET)
    code, out = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions", "--days", "3"], client,
        PROJECTS_DIR=projects, DEBUG_DIR=Path(tmp) / "absent",
        FILE_HISTORY_DIR=Path(tmp) / "absent", TELEMETRY_DIR=Path(tmp) / "absent")
    assert code == 0
    assert "p/s1/s1.jsonl.xz" in client.objects
    saved = json.loads(client.objects[_STORE.manifest_key()])
    assert saved["p/s1/s1.jsonl.xz"][1] == len('{"type": "user"}\n')
    assert "done — uploaded=1 deleted=0 failures=0" in out


def test_claude_main_uses_the_bucket_its_own_setting_names(tmp):
    """Each archiver has its own R2_BUCKET_*; the stub refuses any other."""
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s.jsonl", "{}\n")
    client = _util.FakeS3("claude-only")
    code, _ = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions"], client,
        BUCKET="claude-only", PROJECTS_DIR=projects,
        DEBUG_DIR=Path(tmp) / "absent", FILE_HISTORY_DIR=Path(tmp) / "absent",
        TELEMETRY_DIR=Path(tmp) / "absent")
    assert code == 0
    assert "p/s/s.jsonl.xz" in client.objects


def test_claude_main_returns_one_when_an_upload_failed(tmp):
    """The exit code is what cron reports; a silent 0 hides data loss."""
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s2.jsonl", "{}\n")
    client = _util.FakeS3(_BUCKET)
    client.fail_puts.add("p/s2/s2.jsonl.xz")
    code, out = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions"], client,
        PROJECTS_DIR=projects, DEBUG_DIR=Path(tmp) / "absent",
        FILE_HISTORY_DIR=Path(tmp) / "absent", TELEMETRY_DIR=Path(tmp) / "absent")
    assert code == 1
    assert "failures=1" in out


def test_claude_main_logs_a_failed_manifest_save_without_failing_the_run(tmp):
    """The transcripts are already in the bucket; only the next run pays."""
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s4.jsonl", "{}\n")
    client = _util.FakeS3(_BUCKET)
    client.fail_puts.add(_STORE.manifest_key())
    code, out = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions"], client,
        PROJECTS_DIR=projects, DEBUG_DIR=Path(tmp) / "absent",
        FILE_HISTORY_DIR=Path(tmp) / "absent", TELEMETRY_DIR=Path(tmp) / "absent")
    assert code == 0
    assert "manifest save failed" in out
    assert "p/s4/s4.jsonl.xz" in client.objects


def test_claude_main_dry_run_writes_nothing_to_the_bucket(tmp):
    projects = Path(tmp) / "projects"
    _write(projects / "p" / "s3.jsonl", "{}\n")
    client = _util.FakeS3(_BUCKET)
    code, _ = _run_main(
        _CLAUDE, tmp, ["archive-claude-sessions", "--dry-run"], client,
        PROJECTS_DIR=projects, DEBUG_DIR=Path(tmp) / "absent",
        FILE_HISTORY_DIR=Path(tmp) / "absent", TELEMETRY_DIR=Path(tmp) / "absent")
    assert code == 0
    assert client.puts == []


def test_claude_main_exits_quietly_when_another_run_holds_the_lock(tmp):
    lock = Path(tmp) / "run.lock"
    runtime = _util.load_package_module("runtime")
    holder = runtime.acquire_lock(lock)
    if holder is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        client = _util.FakeS3(_BUCKET)
        code, out = _run_main(
            _CLAUDE, tmp, ["archive-claude-sessions"], client, LOCK_FILE=lock,
            PROJECTS_DIR=Path(tmp) / "absent", DEBUG_DIR=Path(tmp) / "absent",
            FILE_HISTORY_DIR=Path(tmp) / "absent",
            TELEMETRY_DIR=Path(tmp) / "absent")
    finally:
        holder.close()
    assert code == 0
    assert client.puts == []
    assert "previous run still holds the lock" in out


# --- codex ---------------------------------------------------------------------------

def _rollout(sessions, day, uuid, meta, days_old=None):
    name = f"rollout-2026-08-{day}T10-00-00-{uuid}.jsonl"
    body = json.dumps({"type": "session_meta", "payload": meta}) + "\n"
    body += '{"type": "event_msg"}\n'
    return _write(sessions / "2026" / "08" / day / name, body, days_old=days_old)


def test_codex_finds_every_rollout_under_the_date_buckets(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t1", "cwd": "/a"})
    _rollout(sessions, "20", "6-7-8-9-0", {"session_id": "t2", "cwd": "/b"})
    with sandbox(_CODEX, SESSIONS_DIR=sessions):
        found = _CODEX.find_rollouts()
    assert [p.parent.name for p in found] == ["20", "21"]


def test_codex_find_rollouts_on_a_machine_with_no_sessions_dir(tmp):
    with sandbox(_CODEX, SESSIONS_DIR=Path(tmp) / "absent"):
        assert _CODEX.find_rollouts() == []


def test_codex_archives_a_main_rollout_under_its_thread_id(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "thread-a", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    dest, client, _ = _dest()
    with sandbox(_CODEX, SESSIONS_DIR=sessions):
        assert _CODEX.archive_rollouts(dest, _cutoff()) == (2, 0, 0, 0)
    assert f"sessions/{phash}/thread-a/wire.jsonl.xz" in client.objects
    assert json.loads(client.objects[f"sessions/{phash}/project.json"]) == {
        "path": "/proj"}


def test_codex_keys_a_subagent_rollout_per_file(tmp):
    """Subagent threads reuse the parent's session_id, so the uuid keys them."""
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "aaaa-bb-cc-dd-eeee",
             {"session_id": "thread-a", "cwd": "/proj",
              "parent_thread_id": "thread-a"})
    phash = _CODEX.project_hash("/proj")
    dest, client, _ = _dest()
    with sandbox(_CODEX, SESSIONS_DIR=sessions):
        uploaded, _, failed, _ = _CODEX.archive_rollouts(dest, _cutoff())
    assert (uploaded, failed) == (2, 0)
    assert (f"sessions/{phash}/thread-a/subagents/aaaa-bb-cc-dd-eeee/wire.jsonl.xz"
            in client.objects)


def test_codex_skips_a_rollout_with_no_session_meta(tmp):
    """It has no thread identity; filing it under a guess is permanent."""
    sessions = Path(tmp) / "sessions"
    _write(sessions / "2026" / "08" / "21"
           / "rollout-2026-08-21T10-00-00-1-2-3-4-5.jsonl",
           '{"type": "event_msg"}\n')
    dest, client, _ = _dest()
    with sandbox(_CODEX, SESSIONS_DIR=sessions):
        assert _CODEX.archive_rollouts(dest, _cutoff()) == (0, 0, 0, 1)
    assert client.objects == {}


def test_codex_deletes_a_stale_rollout_only_once_it_is_in_the_bucket(tmp):
    sessions = Path(tmp) / "sessions"
    path = _rollout(sessions, "21", "1-2-3-4-5",
                    {"session_id": "t", "cwd": "/proj"}, days_old=10)
    dest, _, _ = _dest()
    with sandbox(_CODEX, SESSIONS_DIR=sessions):
        assert _CODEX.archive_rollouts(dest, _cutoff()) == (2, 1, 0, 0)
    assert not path.exists()


def test_codex_keeps_a_stale_rollout_the_bucket_does_not_have(tmp):
    """`_retire_local` refuses without proof the object is stored."""
    path = _write(Path(tmp) / "rollout.jsonl", "{}\n", days_old=10)
    dest, _, _ = _dest()
    assert not _CODEX._retire_local(path, "sessions/h/t/wire.jsonl", dest, _cutoff())
    assert path.exists()


def test_codex_keeps_a_fresh_rollout_that_is_in_the_bucket(tmp):
    path = _write(Path(tmp) / "rollout.jsonl", "{}\n")
    dest, _, _ = _dest()
    dest.remote = {"sessions/h/t/wire.jsonl.xz": 40}
    assert not _CODEX._retire_local(path, "sessions/h/t/wire.jsonl", dest, _cutoff())
    assert path.exists()


def test_codex_dry_run_keeps_a_stale_rollout(tmp):
    path = _write(Path(tmp) / "rollout.jsonl", "{}\n", days_old=10)
    dest, _, said = _dest(dry_run=True)
    dest.remote = {"sessions/h/t/wire.jsonl.xz": 40}
    assert not _CODEX._retire_local(path, "sessions/h/t/wire.jsonl", dest, _cutoff())
    assert path.exists()
    assert any("DRY rm " in line for line in said)


def test_codex_counts_a_failed_rollout_upload_and_keeps_the_file(tmp):
    sessions = Path(tmp) / "sessions"
    path = _rollout(sessions, "21", "1-2-3-4-5",
                    {"session_id": "t", "cwd": "/proj"}, days_old=10)
    phash = _CODEX.project_hash("/proj")
    dest, client, said = _dest()
    client.fail_puts.add(f"sessions/{phash}/t/wire.jsonl.xz")
    with sandbox(_CODEX, SESSIONS_DIR=sessions):
        uploaded, deleted, failed, _ = _CODEX.archive_rollouts(dest, _cutoff())
    assert (uploaded, deleted, failed) == (1, 0, 1)
    assert path.exists()
    assert any("upload failed" in line for line in said)


def test_codex_counts_a_failed_marker_upload(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    dest, client, said = _dest()
    client.fail_puts.add(f"sessions/{phash}/project.json")
    with sandbox(_CODEX, SESSIONS_DIR=sessions):
        uploaded, _, failed, _ = _CODEX.archive_rollouts(dest, _cutoff())
    assert (uploaded, failed) == (1, 1)
    assert any("marker upload failed" in line for line in said)


def test_codex_publishes_one_marker_per_project_not_per_rollout(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t1", "cwd": "/proj"})
    _rollout(sessions, "20", "6-7-8-9-0", {"session_id": "t2", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    dest, client, _ = _dest()
    with sandbox(_CODEX, SESSIONS_DIR=sessions):
        uploaded, _, failed, _ = _CODEX.archive_rollouts(dest, _cutoff())
    assert (uploaded, failed) == (3, 0)
    assert [k for k in client.objects if k.endswith("project.json")] == [
        f"sessions/{phash}/project.json"]


def test_codex_main_archives_and_saves_the_manifest(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    client = _util.FakeS3(_BUCKET)
    code, out = _run_main(_CODEX, tmp, ["archive-codex-sessions", "--days", "3"],
                          client, SESSIONS_DIR=sessions)
    assert code == 0
    assert f"sessions/{phash}/t/wire.jsonl.xz" in client.objects
    assert _STORE.manifest_key() in client.objects
    assert "uploaded=2 deleted=0 failed=0 skipped=0" in out


def test_codex_main_returns_one_when_an_upload_failed(tmp):
    sessions = Path(tmp) / "sessions"
    _rollout(sessions, "21", "1-2-3-4-5", {"session_id": "t", "cwd": "/proj"})
    phash = _CODEX.project_hash("/proj")
    client = _util.FakeS3(_BUCKET)
    client.fail_puts.add(f"sessions/{phash}/t/wire.jsonl.xz")
    code, out = _run_main(_CODEX, tmp, ["archive-codex-sessions"], client,
                          SESSIONS_DIR=sessions)
    assert code == 1
    assert "failed=1" in out


def test_codex_main_exits_quietly_when_another_run_holds_the_lock(tmp):
    lock = Path(tmp) / "run.lock"
    runtime = _util.load_package_module("runtime")
    holder = runtime.acquire_lock(lock)
    if holder is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        client = _util.FakeS3(_BUCKET)
        code, _ = _run_main(_CODEX, tmp, ["archive-codex-sessions"], client,
                            SESSIONS_DIR=Path(tmp) / "absent", LOCK_FILE=lock)
    finally:
        holder.close()
    assert code == 0
    assert client.puts == []


# --- kimi ------------------------------------------------------------------------------

def test_kimi_project_map_reads_kimi_json(tmp):
    kimi_dir = Path(tmp) / "kimi"
    _write(kimi_dir / "kimi.json",
           json.dumps({"work_dirs": [{"path": "/p/one"}, {"path": "/p/two"},
                                     {"no_path": True}]}))
    with sandbox(_KIMI, KIMI_DIR=kimi_dir):
        mapping = _KIMI.load_project_map()
    assert mapping[hashlib.md5(b"/p/one").hexdigest()] == "/p/one"
    assert len(mapping) == 2


def test_kimi_project_map_is_empty_without_or_with_a_corrupt_kimi_json(tmp):
    with sandbox(_KIMI, KIMI_DIR=Path(tmp) / "absent"):
        assert _KIMI.load_project_map() == {}
    kimi_dir = Path(tmp) / "kimi"
    _write(kimi_dir / "kimi.json", "{not json")
    with sandbox(_KIMI, KIMI_DIR=kimi_dir):
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
    with sandbox(_KIMI, KIMI_CODE_DIR=kc):
        assert _KIMI.load_kimi_code_workdirs() == {"0123456789ab": "/h/proj"}


def test_kimi_code_workdirs_is_empty_without_an_index(tmp):
    with sandbox(_KIMI, KIMI_CODE_DIR=Path(tmp) / "absent"):
        assert _KIMI.load_kimi_code_workdirs() == {}


def test_kimi_workdir_falls_back_to_the_session_state(tmp):
    """Without session_index.jsonl the homedir in state.json still has it."""
    session = Path(tmp) / "ses_1"
    _write(session / "state.json", json.dumps({"agents": {"main": {
        "homedir": "/h/proj/sessions/wd_p_0123456789ab/ses_1/agents/main"}}}))
    assert _KIMI._workdir_from_state(session) == "/h/proj"


def test_kimi_workdir_from_state_returns_none_when_it_cannot_tell(tmp):
    assert _KIMI._workdir_from_state(Path(tmp) / "no-session") is None

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
    dest, client, _ = _dest()
    with sandbox(_KIMI, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions):
        assert _KIMI.archive_sessions(dest, _cutoff()) == (2, 0, 0)
    assert f"sessions/{phash}/uuid-1/wire.jsonl.xz" in client.objects
    assert json.loads(client.objects[f"sessions/{phash}/project.json"]) == {
        "path": "/p/one"}


def test_kimi_deletes_a_stale_legacy_session_and_prunes_the_hash_dir(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    uuid_dir = sessions / "hash1" / "uuid-1"
    _write(uuid_dir / "wire.jsonl", '{"a": 1}\n', days_old=10)
    dest, _, _ = _dest()
    with sandbox(_KIMI, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions):
        assert _KIMI.archive_sessions(dest, _cutoff()) == (1, 1, 0)
    assert not uuid_dir.exists()
    assert not (sessions / "hash1").exists()


def test_kimi_legacy_dry_run_keeps_the_stale_session(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    uuid_dir = sessions / "hash1" / "uuid-1"
    _write(uuid_dir / "wire.jsonl", "{}\n", days_old=10)
    dest, client, said = _dest(dry_run=True)
    with sandbox(_KIMI, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions):
        assert _KIMI.archive_sessions(dest, _cutoff()) == (1, 1, 0)
    assert client.puts == []
    assert uuid_dir.exists()
    assert any("DRY rmtree" in line for line in said)


def test_kimi_counts_a_failed_legacy_marker_upload(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    phash = hashlib.md5(b"/p/one").hexdigest()
    _write(kimi_dir / "kimi.json", json.dumps({"work_dirs": [{"path": "/p/one"}]}))
    _write(sessions / phash / "uuid-1" / "wire.jsonl", "{}\n")
    dest, client, said = _dest()
    client.fail_puts.add(f"sessions/{phash}/project.json")
    with sandbox(_KIMI, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions):
        uploaded, _, failed = _KIMI.archive_sessions(dest, _cutoff())
    assert (uploaded, failed) == (1, 1)
    assert any("marker upload failed" in line for line in said)


def test_kimi_counts_a_failed_legacy_session_upload_and_keeps_it(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    uuid_dir = sessions / "hash1" / "uuid-1"
    _write(uuid_dir / "wire.jsonl", "{}\n", days_old=10)
    dest, client, said = _dest()
    client.fail_puts.add("sessions/hash1/uuid-1/wire.jsonl.xz")
    with sandbox(_KIMI, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions):
        assert _KIMI.archive_sessions(dest, _cutoff()) == (0, 0, 1)
    assert uuid_dir.exists()
    assert any("uuid_dir" in line for line in said)


def test_kimi_archive_sessions_on_a_machine_with_no_sessions_dir(tmp):
    dest, _, _ = _dest()
    with sandbox(_KIMI, KIMI_DIR=Path(tmp) / "absent",
                 SESSIONS_DIR=Path(tmp) / "absent"):
        assert _KIMI.archive_sessions(dest, 0) == (0, 0, 0)


def test_kimi_archives_user_history_and_deletes_the_stale_files(tmp):
    hist = Path(tmp) / "user-history"
    fresh = _write(hist / "hash-fresh.jsonl", '{"q": 1}\n')
    stale = _write(hist / "hash-stale.jsonl", '{"q": 2}\n', days_old=10)
    _write(hist / "notes.txt", "ignored")
    dest, client, _ = _dest()
    with sandbox(_KIMI, USER_HISTORY_DIR=hist):
        assert _KIMI.archive_user_history(dest, _cutoff()) == (2, 1, 0)
    assert sorted(client.objects) == ["user-history/hash-fresh.jsonl.xz",
                                      "user-history/hash-stale.jsonl.xz"]
    assert fresh.exists()
    assert not stale.exists()


def test_kimi_user_history_upload_failure_keeps_the_local_file(tmp):
    hist = Path(tmp) / "user-history"
    stale = _write(hist / "h.jsonl", "{}\n", days_old=10)
    dest, client, said = _dest()
    client.fail_puts.add("user-history/h.jsonl.xz")
    with sandbox(_KIMI, USER_HISTORY_DIR=hist):
        assert _KIMI.archive_user_history(dest, _cutoff()) == (0, 0, 1)
    assert stale.exists()
    assert any("upload failed" in line for line in said)


def test_kimi_user_history_dry_run_keeps_the_stale_file(tmp):
    hist = Path(tmp) / "user-history"
    stale = _write(hist / "h.jsonl", "{}\n", days_old=10)
    dest, client, said = _dest(dry_run=True)
    with sandbox(_KIMI, USER_HISTORY_DIR=hist):
        assert _KIMI.archive_user_history(dest, _cutoff()) == (1, 1, 0)
    assert client.puts == []
    assert stale.exists()
    assert any("DRY rm " in line for line in said)


def test_kimi_archive_user_history_on_a_machine_with_no_such_dir(tmp):
    dest, _, _ = _dest()
    with sandbox(_KIMI, USER_HISTORY_DIR=Path(tmp) / "absent"):
        assert _KIMI.archive_user_history(dest, 0) == (0, 0, 0)


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
    dest, client, _ = _dest()
    with sandbox(_KIMI, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        assert _KIMI.archive_kimi_code_sessions(dest, _cutoff()) == (5, 0, 0)
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
    dest, client, _ = _dest()
    with sandbox(_KIMI, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        _KIMI.archive_kimi_code_sessions(dest, _cutoff())
    assert json.loads(client.objects["sessions/0123456789ab/project.json"]) == {
        "path": "/from/index"}


def test_kimi_code_skips_a_bucket_with_no_hash_suffix(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_not_a_hash", "ses_1")
    dest, client, _ = _dest()
    with sandbox(_KIMI, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        assert _KIMI.archive_kimi_code_sessions(dest, _cutoff()) == (0, 0, 0)
    assert client.objects == {}


def test_kimi_ignores_loose_files_where_directories_should_be(tmp):
    kimi_dir = Path(tmp) / "kimi"
    sessions = kimi_dir / "sessions"
    _write(sessions / "stray.txt", "x")
    _write(sessions / "hash1" / "stray.txt", "x")
    kc = Path(tmp) / "kimi-code"
    _write(kc / "sessions" / "stray.txt", "x")
    _write(kc / "sessions" / "wd_p_0123456789ab" / "stray.txt", "x")
    dest, client, _ = _dest()
    with sandbox(_KIMI, KIMI_DIR=kimi_dir, SESSIONS_DIR=sessions,
                 KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        assert _KIMI.archive_sessions(dest, _cutoff(1)) == (0, 0, 0)
        assert _KIMI.archive_kimi_code_sessions(dest, _cutoff(1)) == (0, 0, 0)
    assert client.objects == {}


def test_kimi_code_deletes_a_stale_session_and_prunes_the_bucket(tmp):
    kc = Path(tmp) / "kimi-code"
    sess = _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    for wire in sess.rglob("wire.jsonl"):
        _age(wire, 10)
    dest, _, _ = _dest()
    with sandbox(_KIMI, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        assert _KIMI.archive_kimi_code_sessions(dest, _cutoff()) == (4, 1, 0)
    assert not sess.exists()
    assert not (kc / "sessions" / "wd_p_0123456789ab").exists()


def test_kimi_code_dry_run_keeps_the_session(tmp):
    kc = Path(tmp) / "kimi-code"
    sess = _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    for wire in sess.rglob("wire.jsonl"):
        _age(wire, 10)
    dest, client, said = _dest(dry_run=True)
    with sandbox(_KIMI, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        assert _KIMI.archive_kimi_code_sessions(dest, _cutoff()) == (4, 1, 0)
    assert client.puts == []
    assert sess.exists()
    assert any("DRY rmtree" in line for line in said)


def test_kimi_code_counts_a_failed_marker_upload(tmp):
    kc = Path(tmp) / "kimi-code"
    _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1", workdir="/h/proj")
    dest, client, said = _dest()
    client.fail_puts.add("sessions/0123456789ab/project.json")
    with sandbox(_KIMI, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
        uploaded, _, failed = _KIMI.archive_kimi_code_sessions(dest, _cutoff(1))
    assert (uploaded, failed) == (4, 1)
    assert any("marker upload failed" in line for line in said)


def test_kimi_code_counts_a_failed_upload_of_each_kind(tmp):
    """Main wire, subagent wire, blobs and state each get their own leg."""
    cases = (
        ("sessions/0123456789ab/ses_1/wire.jsonl.xz", "main_wire"),
        ("sessions/0123456789ab/ses_1/subagents/agent-1/wire.jsonl.xz",
         "subagent_wire"),
        ("sessions/0123456789ab/ses_1/subagents/agent-1/blobs/shot.json.xz",
         "blobs="),
        ("sessions/0123456789ab/ses_1/state.json.xz", "state="),
    )
    for index, (key, marker) in enumerate(cases):
        kc = Path(tmp) / f"kimi-code-{index}"
        _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
        dest, client, said = _dest()
        client.fail_puts.add(key)
        with sandbox(_KIMI, KIMI_CODE_DIR=kc, KC_SESSIONS_DIR=kc / "sessions"):
            uploaded, _, failed = _KIMI.archive_kimi_code_sessions(dest, _cutoff(1))
        assert (uploaded, failed) == (3, 1), marker
        assert any(marker in line for line in said), marker


def test_kimi_archive_kimi_code_sessions_on_a_machine_without_them(tmp):
    dest, _, _ = _dest()
    with sandbox(_KIMI, KIMI_CODE_DIR=Path(tmp) / "absent",
                 KC_SESSIONS_DIR=Path(tmp) / "absent"):
        assert _KIMI.archive_kimi_code_sessions(dest, 0) == (0, 0, 0)


def test_kimi_cleanup_sweeps_logs_and_telemetry(tmp):
    root = Path(tmp) / "kimi"
    stale_log = _write(root / "logs" / "old.log", "x", days_old=10)
    fresh_log = _write(root / "logs" / "new.log", "x")
    stale_tel = _write(root / "telemetry" / "old.jsonl", "x", days_old=10)
    with sandbox(_KIMI, LOGS_DIR=root / "logs", TELEMETRY_DIR=root / "telemetry"):
        _KIMI.cleanup_local(_cutoff(), False, print)
    assert not stale_log.exists()
    assert fresh_log.exists()
    assert not stale_tel.exists()


def _kimi_main_dirs(tmp, kimi_dir, kc):
    return {
        "KIMI_DIR": kimi_dir,
        "SESSIONS_DIR": kimi_dir / "sessions",
        "USER_HISTORY_DIR": kimi_dir / "user-history",
        "LOGS_DIR": kimi_dir / "logs",
        "TELEMETRY_DIR": kimi_dir / "telemetry",
        "KIMI_CODE_DIR": kc,
        "KC_SESSIONS_DIR": kc / "sessions",
    }


def test_kimi_main_archives_all_three_sources(tmp):
    kimi_dir = Path(tmp) / "kimi"
    kc = Path(tmp) / "kimi-code"
    _write(kimi_dir / "sessions" / "hash1" / "uuid-1" / "wire.jsonl", '{"a": 1}\n')
    _write(kimi_dir / "user-history" / "hash1.jsonl", '{"q": 1}\n')
    _kimi_code_session(kc, "wd_p_0123456789ab", "ses_1")
    client = _util.FakeS3(_BUCKET)
    code, out = _run_main(_KIMI, tmp, ["archive-kimi-sessions", "--days", "3"],
                          client, **_kimi_main_dirs(tmp, kimi_dir, kc))
    assert code == 0
    assert "sessions/hash1/uuid-1/wire.jsonl.xz" in client.objects
    assert "user-history/hash1.jsonl.xz" in client.objects
    assert "sessions/0123456789ab/ses_1/wire.jsonl.xz" in client.objects
    assert _STORE.manifest_key() in client.objects
    assert "done — uploaded=6 deleted=0 failures=0" in out


def test_kimi_main_returns_one_when_an_upload_failed(tmp):
    kimi_dir = Path(tmp) / "kimi"
    kc = Path(tmp) / "absent-kc"
    _write(kimi_dir / "user-history" / "hash1.jsonl", "{}\n")
    client = _util.FakeS3(_BUCKET)
    client.fail_puts.add("user-history/hash1.jsonl.xz")
    code, out = _run_main(_KIMI, tmp, ["archive-kimi-sessions"], client,
                          **_kimi_main_dirs(tmp, kimi_dir, kc))
    assert code == 1
    assert "failures=1" in out


def test_kimi_main_logs_a_failed_manifest_save_without_failing_the_run(tmp):
    kimi_dir = Path(tmp) / "kimi"
    kc = Path(tmp) / "absent-kc"
    _write(kimi_dir / "user-history" / "hash1.jsonl", "{}\n")
    client = _util.FakeS3(_BUCKET)
    client.fail_puts.add(_STORE.manifest_key())
    code, out = _run_main(_KIMI, tmp, ["archive-kimi-sessions"], client,
                          **_kimi_main_dirs(tmp, kimi_dir, kc))
    assert code == 0
    assert "manifest save failed" in out
    assert "user-history/hash1.jsonl.xz" in client.objects


def test_kimi_main_dry_run_writes_nothing_to_the_bucket(tmp):
    kimi_dir = Path(tmp) / "kimi"
    kc = Path(tmp) / "absent-kc"
    _write(kimi_dir / "user-history" / "hash1.jsonl", "{}\n")
    client = _util.FakeS3(_BUCKET)
    code, out = _run_main(_KIMI, tmp, ["archive-kimi-sessions", "--dry-run"],
                          client, **_kimi_main_dirs(tmp, kimi_dir, kc))
    assert code == 0
    assert client.puts == []
    assert "DRY upload" in out


def test_kimi_main_exits_quietly_when_another_run_holds_the_lock(tmp):
    lock = Path(tmp) / "run.lock"
    runtime = _util.load_package_module("runtime")
    holder = runtime.acquire_lock(lock)
    if holder is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        client = _util.FakeS3(_BUCKET)
        code, out = _run_main(
            _KIMI, tmp, ["archive-kimi-sessions"], client, LOCK_FILE=lock,
            **_kimi_main_dirs(tmp, Path(tmp) / "absent", Path(tmp) / "absent"))
    finally:
        holder.close()
    assert code == 0
    assert client.puts == []
    assert "previous run still holds the lock" in out


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(dict(globals()))))
