"""Host-side scaffolding: the logger, the lock, retention, the local sweeps.

These used to be three copies each, and the suite tested them three times
over. They are one implementation now, so this suite tests them once —
against `runtime`, not through whichever archiver happened to import it.

Nothing here touches the network or the machine's own ~/.claude, ~/.kimi or
~/.codex tree: every path handed to the code under test is inside the temp
directory the runner supplies.
"""
import os
import time
from pathlib import Path

import _util

_RUNTIME = _util.load_package_module("runtime")


def _write(path, data="x", days_old=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    if days_old is not None:
        old = time.time() - days_old * 86400
        os.utime(path, (old, old))
    return path


def _age(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


# --- the logger --------------------------------------------------------------

def test_a_log_line_reaches_both_stdout_and_the_file(tmp):
    """stdout is what cron mails; the file is what you read afterwards."""
    path = Path(tmp) / "archive.log"
    log = _RUNTIME.Logger(path)
    log("first")
    log("second")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" first")
    assert lines[1].endswith(" second")


def test_each_log_line_carries_a_timestamp(tmp):
    """A log with no time on it cannot answer 'what did the 04:00 run do'."""
    path = Path(tmp) / "archive.log"
    _RUNTIME.Logger(path)("hello")
    line = path.read_text(encoding="utf-8").strip()
    stamp, _, message = line.partition(" ")
    assert message == "hello"
    time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")


def test_the_logger_appends_rather_than_truncating(tmp):
    path = Path(tmp) / "archive.log"
    path.write_text("earlier run\n", encoding="utf-8")
    _RUNTIME.Logger(path)("later run")
    assert path.read_text(encoding="utf-8").startswith("earlier run\n")


def test_an_unwritable_log_does_not_stop_the_run(tmp):
    """These run from cron; a bad log path must not cost the archive."""
    log = _RUNTIME.Logger(Path(tmp) / "no-such-dir" / "archive.log")
    log("still archiving")  # must not raise


def test_two_archivers_get_two_separate_logs(tmp):
    """The path is per instance -- a module global holding it is the shape
    that let a test append to a production log."""
    first = _RUNTIME.Logger(Path(tmp) / "a.log")
    second = _RUNTIME.Logger(Path(tmp) / "b.log")
    first("only a")
    second("only b")
    assert "only a" in (Path(tmp) / "a.log").read_text(encoding="utf-8")
    assert "only b" not in (Path(tmp) / "a.log").read_text(encoding="utf-8")


# --- the single-instance lock ------------------------------------------------

def test_acquire_lock_is_exclusive(tmp):
    """Two archivers on one machine must not upload concurrently."""
    lock = Path(tmp) / "archiver.lock"
    first = _RUNTIME.acquire_lock(lock)
    if first is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        assert _RUNTIME.acquire_lock(lock) is None, \
            "a second holder acquired the same lock"
    finally:
        first.close()


def test_the_lock_is_released_when_the_handle_closes(tmp):
    """The next hourly run has to be able to take it."""
    lock = Path(tmp) / "archiver.lock"
    first = _RUNTIME.acquire_lock(lock)
    if first is None:
        _util.skip("advisory locking unavailable on this platform")
    first.close()
    second = _RUNTIME.acquire_lock(lock)
    assert second is not None, "the lock outlived its handle"
    second.close()


def test_acquire_lock_creates_the_directory_it_needs(tmp):
    """First run on a fresh machine: ~/.kimi-code does not exist yet."""
    lock = Path(tmp) / "brand" / "new" / "archiver.lock"
    handle = _RUNTIME.acquire_lock(lock)
    if handle is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        assert lock.is_file()
    finally:
        handle.close()


def test_the_lockfile_is_never_empty(tmp):
    """Windows cannot lock a byte range of a zero-length file."""
    lock = Path(tmp) / "archiver.lock"
    handle = _RUNTIME.acquire_lock(lock)
    if handle is None:
        _util.skip("advisory locking unavailable on this platform")
    try:
        assert lock.stat().st_size >= 1
    finally:
        handle.close()


# --- the retention predicate --------------------------------------------------

def test_is_old_is_true_only_past_the_cutoff(tmp):
    path = _write(Path(tmp) / "f.txt")
    now = time.time()
    _age(path, 10)
    assert _RUNTIME.is_old(path, now - 3 * 86400)
    assert not _RUNTIME.is_old(path, now - 30 * 86400)


def test_is_old_says_no_for_a_missing_path(tmp):
    """A path that vanished mid-walk is not old -- returning True would send
    the caller on to unlink something it never saw."""
    assert not _RUNTIME.is_old(Path(tmp) / "gone.txt", time.time())


# --- the local sweeps ---------------------------------------------------------

def test_sweep_files_removes_only_what_is_past_the_cutoff(tmp):
    root = Path(tmp) / "debug"
    stale = _write(root / "old.log", days_old=10)
    fresh = _write(root / "new.log")
    _RUNTIME.sweep_files(root, time.time() - 3 * 86400, False, print)
    assert not stale.exists()
    assert fresh.exists()


def test_sweep_files_leaves_subdirectories_alone(tmp):
    """It sweeps files; a directory here is somebody else's business."""
    root = Path(tmp) / "debug"
    nested = _write(root / "sub" / "keep.log", days_old=10)
    _RUNTIME.sweep_files(root, time.time() - 3 * 86400, False, print)
    assert nested.exists()


def test_sweep_files_dry_run_only_reports(tmp):
    root = Path(tmp) / "debug"
    stale = _write(root / "old.log", days_old=10)
    said = []
    _RUNTIME.sweep_files(root, time.time() - 3 * 86400, True, said.append)
    assert stale.exists()
    assert said == [f"  DRY rm {stale}"]


def test_sweep_dirs_removes_stale_directories_whole(tmp):
    root = Path(tmp) / "file-history"
    stale = root / "session-1"
    _write(stale / "a.txt")
    _age(stale, 10)
    fresh = root / "session-2"
    _write(fresh / "a.txt")
    _RUNTIME.sweep_dirs(root, time.time() - 3 * 86400, False, print)
    assert not stale.exists()
    assert fresh.exists()


def test_sweep_dirs_leaves_loose_files_alone(tmp):
    root = Path(tmp) / "file-history"
    loose = _write(root / "stray.txt", days_old=10)
    _RUNTIME.sweep_dirs(root, time.time() - 3 * 86400, False, print)
    assert loose.exists()


def test_sweep_dirs_dry_run_only_reports(tmp):
    root = Path(tmp) / "file-history"
    stale = root / "session-1"
    _write(stale / "a.txt")
    _age(stale, 10)
    said = []
    _RUNTIME.sweep_dirs(root, time.time() - 3 * 86400, True, said.append)
    assert stale.exists()
    assert said == [f"  DRY rmtree {stale}"]


def test_sweeping_a_directory_that_does_not_exist_is_not_an_error(tmp):
    """A machine that never wrote telemetry has no telemetry directory."""
    absent = Path(tmp) / "absent"
    _RUNTIME.sweep_files(absent, time.time(), False, print)
    _RUNTIME.sweep_dirs(absent, time.time(), False, print)


# --- stream reconfiguration ---------------------------------------------------

def test_reconfigure_streams_tolerates_a_stream_without_it(tmp):
    """A redirected or piped stream may genuinely have no reconfigure()."""
    import sys
    saved = sys.stdout
    try:
        sys.stdout = object()  # type: ignore[assignment]
        _RUNTIME.reconfigure_streams()  # must not raise
    finally:
        sys.stdout = saved


if __name__ == "__main__":
    raise SystemExit(_util.runner(_util.collect(dict(globals()))))
