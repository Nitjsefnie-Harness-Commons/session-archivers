"""Running as a scheduled job on a machine: logging, locking, retention.

Everything here is about the *host*, not the destination — where the log
line goes, how a second copy of the archiver is kept from starting, and what
counts as stale enough to delete. `store.py` is the other half.

Split out of the three archivers, which each carried their own copy. That
duplication was load-bearing while they were standalone scripts copied onto a
machine one file at a time; they install as one package now, so the copies are
just three places for a fix to land in two of.

Stdlib only and OS-agnostic: these run from cron and Task Scheduler, where a
dependency is one more thing that can be missing at 3am, and the single-
instance lock has to work on both.
"""

import shutil
import sys
import time
from pathlib import Path

DAYS = 3
"""Default retention threshold, in days, for the delete gate."""


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


class Logger:  # pylint: disable=too-few-public-methods
    """One archiver's log: every line to stdout AND to its own log file.

    stdout is what cron mails and what CI shows; the file is what you read
    afterwards to find out what the 04:00 run did. A callable rather than a
    module function because the path differs per archiver, and a module
    global holding it is exactly the shape that made a test append to a
    production log.
    """

    def __init__(self, path):
        self.path = Path(path)

    def __call__(self, msg):
        line = f'{stamp()} {msg}'
        print(line, flush=True)
        try:
            with self.path.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        except OSError:
            # An unwritable log must not cost the archive run. There is
            # nowhere left to report this to except stdout, which already
            # has the line.
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


def acquire_lock(path):
    '''Open `path` and try a non-blocking exclusive lock.

    Returns the open handle on success — the caller keeps it alive until
    exit, and the OS drops the lock when the process ends or the handle
    closes — or None when another run already holds it.
    '''
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # NOT a `with`: the handle IS the lock. Closing it on the way out of this
    # function releases the lock immediately and the caller gets a handle to
    # nothing, which is the bug this comment exists to prevent.
    fh = path.open('a+b')  # pylint: disable=consider-using-with
    try:
        # Windows needs at least one byte in the file to lock a range of it.
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
    '''True when `path` was last modified before `cutoff`.

    A path that vanished between the walk and this call is NOT old: returning
    True there would send the caller on to unlink something it never saw.
    '''
    try:
        return path.stat().st_mtime < cutoff
    except FileNotFoundError:
        return False


def sweep_files(directory, cutoff, dry_run, log):
    '''Delete the files directly under `directory` that are older than
    `cutoff`. Missing directory: nothing to do.'''
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if not entry.is_file() or not is_old(entry, cutoff):
            continue
        if dry_run:
            log(f'  DRY rm {entry}')
            continue
        try:
            entry.unlink()
        except OSError as e:
            log(f'  rm failed: {entry}: {e}')


def sweep_dirs(directory, cutoff, dry_run, log):
    '''The same for the subdirectories of `directory`, removed whole.'''
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if not entry.is_dir() or not is_old(entry, cutoff):
            continue
        if dry_run:
            log(f'  DRY rmtree {entry}')
        else:
            shutil.rmtree(entry, ignore_errors=True)


def reconfigure_streams():
    '''Put stdout and stderr into UTF-8, replacing what will not encode.

    A session path can hold any character the filesystem allows, and the
    Windows console default encoding cannot print all of them — the archiver
    would die formatting its own log line. TextIO does not declare
    `reconfigure`, and a redirected or piped stream may genuinely not have
    it, so this is a lookup rather than a call inside a bare except: it says
    the same thing to a reader and to a type checker.
    '''
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding='utf-8', errors='replace')
        except (ValueError, OSError):
            pass  # not a reconfigurable stream (redirected/piped)
