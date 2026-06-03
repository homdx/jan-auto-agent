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
from pathlib import Path

import pytest

# ── 1. sys.path bootstrap ──────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
        result = subprocess.run(
            [sys.executable, str(self.fspath)],
            capture_output=True,
            text=True,
        )
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
