#!/usr/bin/env python3
"""What the wheel promises: three archivers, no credentials, no bundle needed.

These ran in the agent-harness-bundle on whichever machine last exercised them
by hand. The reason to extract them is that CI runs this on every push, across
the operating systems the archivers actually run on.
"""
import ast
import re
import sys
from pathlib import Path

import _util

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "session_archivers"
ARCHIVERS = ("claude", "kimi", "codex")

# A literal key in the source travels with the code to every machine that
# installs it, and this repository is public.
CREDENTIAL_PATTERNS = (
    (r"^ACCESS_KEY\s*=\s*['\"][0-9a-f]{16,}", "access key"),
    (r"^SECRET_KEY\s*=\s*['\"][0-9a-f]{16,}", "secret key"),
    (r"^ACCOUNT_ID\s*=\s*['\"][0-9a-f]{16,}", "account id"),
    (r"[0-9a-f]{32,}", "a long hex literal that could be a key"),
)


def test_no_archiver_carries_a_credential(_tmp):
    for name in ARCHIVERS + ("settings",):
        source = (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
        for pattern, what in CREDENTIAL_PATTERNS[:3]:
            assert not re.search(pattern, source, re.MULTILINE), \
                f"{name}.py hardcodes the R2 {what}"


def test_no_long_hex_literal_slipped_in(_tmp):
    """The catch-all the named patterns would miss under a different variable."""
    offenders = []
    for path in sorted(PACKAGE.glob("*.py")):
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"['\"][0-9a-f]{32,}['\"]", line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        "a long hex literal looks like a key and this repo is public: "
        + ", ".join(offenders))


def _import(name):
    """Import a module as part of the package.

    Not `_util.load`, which loads by path: these modules use relative imports
    for the settings resolver, and a path load has no parent package to resolve
    them against. The trade is that a broken __init__ surfaces here rather than
    in the module under test, which the failure message makes obvious anyway.
    """
    import importlib
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module(f"session_archivers.{name}")


def test_every_archiver_exposes_main(_tmp):
    for name in ARCHIVERS:
        module = _import(name)
        assert callable(getattr(module, "main", None)), \
            f"{name}.py has no main() for its console script"


def test_importing_never_demands_credentials(_tmp):
    """Import must not touch R2 — tests and `--help` import this module.

    Resolving credentials at import time would make the module unimportable on
    any machine that has not configured R2, which includes CI.
    """
    for name in ARCHIVERS:
        _import(name)


def test_the_settings_seam_is_package_relative(_tmp):
    """The bundle bootstrapped `_settings` off a sys.path insert.

    That seam is what tied these to an installed agent-harness-bundle. The
    wheel has to stand alone, so the resolver travels with it.
    """
    for name in ARCHIVERS:
        source = (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
        assert "from .settings import" in source, \
            f"{name}.py does not use the vendored settings resolver"
        assert ".agent-bundle' / 'scripts'" not in source, \
            f"{name}.py still bootstraps the bundle's scripts directory"


def test_each_archiver_targets_its_own_bucket_and_home(_tmp):
    """Three harnesses, three destinations — a shared default would collide."""
    expected = {
        "claude": ("R2_BUCKET_CLAUDE", ".claude"),
        "kimi": ("R2_BUCKET_KIMI", ".kimi-code"),
        "codex": ("R2_BUCKET_CODEX", ".codex"),
    }
    for name, (bucket_key, home) in expected.items():
        source = (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
        assert bucket_key in source, f"{name}.py does not read {bucket_key}"
        assert home in source, f"{name}.py does not reference {home}"


def test_the_version_is_a_single_source_of_truth(_tmp):
    """pyproject reads __version__; a second literal could disagree with it."""
    init = (PACKAGE / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(init)
    version = next(
        (node.value.value for node in tree.body
         if isinstance(node, ast.Assign)
         and getattr(node.targets[0], "id", None) == "__version__"),
        None)
    assert isinstance(version, str) and version.count(".") == 2, version
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = { attr = "session_archivers.__version__" }' in pyproject
    assert f'"{version}"' not in pyproject, (
        "the version is repeated in pyproject and can drift from the package")


def main():
    return _util.runner(_util.collect(globals()), "archivercontract_")


if __name__ == "__main__":
    sys.exit(main())
