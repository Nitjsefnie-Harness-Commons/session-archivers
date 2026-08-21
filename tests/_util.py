"""Shared helpers for the archiver suites.

Not a suite itself -- run_tests.py only loads `test_*.py`.

Stdlib only and OS-agnostic: these suites run on Linux, macOS and Windows in
CI, and a POSIX-only assumption here would be a platform failure reported as a
test failure.
"""
import glob
import importlib
import importlib.util
import os
import sys
import tempfile
import traceback
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = str(_PROJECT_ROOT / "session_archivers")
"""The package directory. The suites load an archiver by path rather than by
import so that a broken `__init__` cannot mask an archiver defect as an
ImportError."""

CLAUDE = os.path.expanduser("~/.claude")


def script(name):
    return str(_PROJECT_ROOT / "session_archivers" / name)


def load(path, name=None):
    """Import an archiver by path without running its __main__ block."""
    name = name or ("mod_" + os.path.splitext(os.path.basename(path))[0]
                    .replace("-", "_"))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_package_module(name):
    """Import `session_archivers.<name>` as part of its package.

    `load()` above deliberately imports by PATH, so a broken __init__ cannot
    disguise a parser defect as an ImportError. That is not available here:
    these modules do `from .settings import ...`, and a relative import has
    no parent package when the file is loaded standalone.

    So this one goes through the package, with the repo root on sys.path so
    it resolves from the checkout rather than from whatever happens to be
    installed.
    """
    root = str(_PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(f"session_archivers.{name}")


def transcripts(limit=None, min_size=0):
    """Real Claude session transcripts on this box, smallest first.

    Empty on a machine that has none -- CI runners included -- so a suite that
    wants one must skip rather than fail when the list comes back empty.
    """
    found = [p for p in glob.glob(os.path.join(CLAUDE, "projects", "**", "*.jsonl"),
                                  recursive=True)
             if os.path.getsize(p) >= min_size]
    found.sort(key=os.path.getsize)
    return found[-limit:] if limit else found


# pylint: disable=too-few-public-methods
# The stubs below stand in for boto3 objects that genuinely have one
# method each -- a response Body you read(), a paginator you paginate().
# A second method would mean stubbing an operation nothing calls.
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
            # for an empty bucket, which is the branch inventory() guards.
            yield {}
            return
        for start in range(0, len(keys), self._page_size):
            chunk = keys[start:start + self._page_size]
            yield {"Contents": [{"Key": k, "Size": len(self._client.objects[k])}
                                for k in chunk]}


class FakeS3:
    """In-memory stand-in for the boto3 S3 client `store.client()` builds.

    Deliberately only the four operations the archivers call. A stub that
    accepted anything would let a call to a method R2 does not have pass in
    the suite and fail in production. It also asserts on the bucket it is
    handed, so a test that expected one archiver's bucket and got another's
    fails here rather than passing quietly.

    `fail_puts` / `fail_deletes` hold keys whose operation raises, so the
    error branches -- which log and count a failure -- are reachable.
    """

    def __init__(self, bucket="test-bucket", page_size=2):
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
            # R2 raises a generic ClientError here, not S3's NoSuchKey -- the
            # code only ever catches Exception, so the type is free.
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


class Skipped(Exception):
    """Raised by a test that cannot hold on this platform."""


def skip(reason):
    """End the running test as skipped, with a reason the log will show."""
    raise Skipped(reason)


def _assertion_site(exc):
    """`file:line: source` of the assert that failed, for bare asserts."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return ""
    last = frames[-1]
    return f"{os.path.basename(last.filename)}:{last.lineno}: {last.line}"


def runner(tests, tmp_prefix="archivertests_"):
    """Shared main(): run every callable, print PASS/FAIL, return exit code.

    Each test takes one argument: an isolated temp dir, handed over fully
    resolved -- macOS resolves /var to /private/var, and a suite comparing a
    path it was given against a path the code produced would otherwise see two
    spellings of the same directory and call them different.
    """
    failed = []
    skipped = []
    with tempfile.TemporaryDirectory(prefix=tmp_prefix) as tmp:
        tmp = os.path.realpath(tmp)
        for t in tests:
            d = os.path.join(tmp, t.__name__)
            os.makedirs(d, exist_ok=True)
            try:
                t(d)
                print(f"  PASS  {t.__name__}")
            except Skipped as e:
                skipped.append(t.__name__)
                print(f"  SKIP  {t.__name__}: {e}")
            except AssertionError as e:
                failed.append(t.__name__)
                # A bare `assert x == y` carries no message, which on a
                # platform you cannot run locally leaves nothing to go on.
                detail = str(e) or _assertion_site(e)
                print(f"  FAIL  {t.__name__}: {detail}")
            except Exception as e:  # noqa: BLE001
                failed.append(t.__name__)
                print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    passed = len(tests) - len(failed) - len(skipped)
    summary = f"\n{passed}/{len(tests)} passed"
    if skipped:
        summary += f", {len(skipped)} skipped"
    print(summary)
    return 1 if failed else 0


def collect(namespace):
    return [v for k, v in sorted(namespace.items())
            if k.startswith("test_") and callable(v)]
