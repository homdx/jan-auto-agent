"""
conftest.py — project root

Handles two things:
  1. sys.path bootstrap so `from tools.x import Y` works in every test file.
  2. Custom collector for standalone script-style test files (test_story_*.py).
     These files run all their checks at import time and call sys.exit(1) on
     failure, which crashes pytest's collection phase with INTERNALERROR.

     Fix: two cooperating hooks:
       - pytest_collect_file     → adds a ScriptTestFile node (runs the script
                                   as a subprocess, maps exit code to PASS/FAIL)
       - pytest_pycollect_makemodule → intercepts the built-in Python Module
                                   collector for the same files and returns an
                                   empty shell so the file is never imported.
"""

import subprocess
import sys


import pytest
# conftest.py  (project root)

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
# ── 1. sys.path bootstrap ──────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── 1b. Cache scan_repo(REPO_ROOT) across the test session ─────────────────────
#
# ~30 tests across tests/test_collect_*.py call scan_repo(REPO_ROOT) (this
# repo's own source tree) with no config= override, each re-walking and
# re-parsing every file from scratch -- 2-8s per call, ~100s+ of the
# suite's wall time in aggregate. That specific call is safe to cache:
# this checked-out repo doesn't change mid-run, and scan_repo has no side
# effects (ModuleRecord is a frozen dataclass of tuples; verified no test
# or library call in this suite mutates the returned list in place).
#
# The cache is scoped tightly on purpose -- a blanket
# functools.lru_cache(scan_repo) was tried and reverted, because it broke
# two other things: (1) ConfigParser isn't hashable, so any call passing
# config=<a real ConfigParser> (e.g. tools/collect/loader.py's staleness
# path) raised TypeError; (2) several tests scan a tmp_path repo, mutate
# a file, and scan the *same* tmp_path again expecting the change to show
# up -- caching that call would silently return stale results and defeat
# the test. So this only ever caches root == this repo's own ROOT with
# config is None; every other call (tmp_path roots, explicit configs)
# goes straight to the real, uncached scan_repo, unchanged.
#
# Only this test session's imported reference is wrapped -- the on-disk
# tools/collect/scanner.py is untouched, so `collect` CLI runs are unaffected.
from tools.collect import scanner as _scanner

_real_scan_repo = _scanner.scan_repo
_repo_scan_cache: dict = {}


def _cached_scan_repo(root, *, config=None):
    resolved = Path(root).resolve()
    if config is not None or resolved != ROOT:
        return _real_scan_repo(root, config=config)
    if resolved not in _repo_scan_cache:
        _repo_scan_cache[resolved] = _real_scan_repo(root, config=config)
    return _repo_scan_cache[resolved]


_scanner.scan_repo = _cached_scan_repo


# ── 2a. Custom collector ───────────────────────────────────────────────────────

def _is_script_test(p: Path) -> bool:
    return p.suffix == ".py" and p.name.startswith("test_story_")


def pytest_collect_file(parent, file_path):
    """Claim every test_story_*.py file and run it as a subprocess."""
    if _is_script_test(file_path):
        return ScriptTestFile.from_parent(parent, path=file_path)


# ── 2b. Block the built-in Python Module collector for the same files ──────────

class _EmptyModule(pytest.Module):
    """Placeholder Module that collects nothing — never imports the file."""
    def collect(self):
        yield from []


def pytest_pycollect_makemodule(module_path, parent):
    """
    pytest_pycollect_makemodule is firstresult=True.
    Returning a non-None value here stops the built-in Module from being
    created, which prevents the file from being imported (and hitting sys.exit).
    """
    if _is_script_test(module_path):
        return _EmptyModule.from_parent(parent, path=module_path)


# ── 3. ScriptTestFile / ScriptTestItem ────────────────────────────────────────

class ScriptTestFile(pytest.File):
    """Represents one standalone test script as a single collectible node."""

    def collect(self):
        yield ScriptTestItem.from_parent(self, name=self.path.stem)


class ScriptTestItem(pytest.Item):
    """Runs the script in a subprocess; maps exit code to PASSED / FAILED."""

    def runtest(self):
        # AUTO-FIX (medium-priority audit, DeepSeek-plan finding): no
        # timeout= was passed to subprocess.run — a hung test_story_*.py
        # script (an infinite loop, a stuck network call inside the
        # script) blocked the entire test suite indefinitely with no way
        # to recover short of killing the process manually. 300s covers
        # every legitimate script test currently in the suite with room to
        # spare; TimeoutExpired is reported the same way a non-zero exit
        # already is, via ScriptTestFailed, so repr_failure's existing
        # stdout/stderr/exit-code formatting handles it without changes.
        try:
            result = subprocess.run(
                [sys.executable, str(self.fspath)],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired as exc:
            # BUGFIX: with text=True, TimeoutExpired.stdout/stderr are
            # documented as str — but can still be None (no output captured
            # before the kill) or, on Python builds older than 3.7.10, bytes
            # on a partial-read boundary. Decode defensively so a slow
            # script is reported as a clean timeout instead of crashing the
            # whole pytest run with an uncaught TypeError (str + bytes).
            def _as_text(b):
                if isinstance(b, (bytes, bytearray)):
                    return b.decode("utf-8", "replace")
                return b or ""
            self._stdout = _as_text(exc.stdout)
            self._stderr = _as_text(exc.stderr) + "\n[TIMEOUT] script exceeded 300s"
            self._returncode = -1
            raise ScriptTestFailed(-1, self._stdout, self._stderr) from exc

        self._stdout = result.stdout
        self._stderr = result.stderr
        self._returncode = result.returncode

        if result.returncode != 0:
            raise ScriptTestFailed(result.returncode, result.stdout, result.stderr)

    def repr_failure(self, excinfo):
        exc = excinfo.value
        lines = []
        if exc.stdout:
            lines.append("--- stdout ---")
            lines.extend(exc.stdout.rstrip().splitlines())
        if exc.stderr:
            lines.append("--- stderr ---")
            lines.extend(exc.stderr.rstrip().splitlines())
        lines.append(f"--- exit code: {exc.returncode} ---")
        return "\n".join(lines)

    def reportinfo(self):
        return self.fspath, None, f"script: {self.fspath.basename}"

    def teardown(self):
        # Write captured stdout so it appears with pytest -s
        if getattr(self, "_stdout", None):
            sys.stdout.write(self._stdout)


class ScriptTestFailed(Exception):
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
