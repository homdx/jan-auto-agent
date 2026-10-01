"""FL-7: `pytest -q` against this repo's `pytest.ini` still prints its `N passed` line."""
from __future__ import annotations

import configparser
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTEST_INI = REPO_ROOT / "pytest.ini"


def _addopts() -> list:
    parser = configparser.ConfigParser()
    parser.read(PYTEST_INI, encoding="utf-8")
    return shlex.split(parser.get("pytest", "addopts", fallback=""))


def test_addopts_carry_no_quiet_flag():
    """An ini `-q` plus the `-q` everyone types is `-qq`: no summary line."""
    quiet = [opt for opt in _addopts()
             if opt == "--quiet" or re.fullmatch(r"-q+", opt)]
    assert quiet == [], f"pytest.ini addopts carry {quiet}"


def test_pytest_q_prints_the_passed_line(tmp_path):
    """The command an agent runs: the repo's ini, a `-q` of its own, a summary.

    `-n 0` keeps the child in-process (xdist's own switch; it overrides the
    ini's `-n auto`), so the check costs one tiny pytest run, not a pool.
    """
    (tmp_path / "test_one.py").write_text("def test_one():\n    assert True\n",
                                          encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(PYTEST_INI), "--rootdir", str(tmp_path),
         "-p", "no:cacheprovider", "-n", "0", "-q", str(tmp_path / "test_one.py")],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120)

    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^1 passed\b", result.stdout, re.MULTILINE), result.stdout
