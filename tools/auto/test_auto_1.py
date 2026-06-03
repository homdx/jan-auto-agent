"""tests/test_auto_a1.py — Smoke tests for AUTO-A1 (entry point).

Covers both ACs from the story:
  AC1: `python main.py --auto "improve current code" --base .` starts a run
       and exits cleanly (exit code 0).
  AC2: `python main.py` (no args) does NOT enter autonomous mode — the
       interactive prompt is reached as today (we verify by checking that
       --auto is NOT required and that normal arg parsing still works).
  AC3: Existing --once one-shot mode is unaffected (no regression).
  AC4: /auto with no goal prints usage, does not crash (tested via the
       controller directly).
"""

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# Resolve the project root (one level above tests/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(*args, input_text: str | None = None, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run main.py with the given extra args and return the CompletedProcess."""
    cmd = [sys.executable, str(PROJECT_ROOT / "main.py"), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        input=input_text,
        cwd=str(PROJECT_ROOT),
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — --auto exits cleanly (exit code 0) and prints the start banner
# ─────────────────────────────────────────────────────────────────────────────

def test_auto_flag_exits_zero():
    """--auto with a valid goal and an existing --base must exit 0."""
    with tempfile.TemporaryDirectory() as tmp:
        result = _run("--auto", "improve current code", "--base", tmp)
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_auto_flag_prints_banner():
    """--auto must print the goal and base_dir in the start banner."""
    with tempfile.TemporaryDirectory() as tmp:
        result = _run("--auto", "improve current code", "--base", tmp)
    assert "improve current code" in result.stdout, "Goal not echoed in banner"
    assert tmp in result.stdout, "base_dir not echoed in banner"


def test_auto_creates_agent_dir():
    """--auto must create .agent/ inside the base directory."""
    with tempfile.TemporaryDirectory() as tmp:
        _run("--auto", "improve current code", "--base", tmp)
        assert (Path(tmp) / ".agent").is_dir(), ".agent/ directory was not created"


def test_auto_empty_goal_exits_nonzero():
    """--auto with an empty string must exit non-zero."""
    with tempfile.TemporaryDirectory() as tmp:
        result = _run("--auto", "", "--base", tmp)
    assert result.returncode != 0, "Empty goal should not exit 0"


def test_auto_missing_base_exits_nonzero():
    """--auto pointing at a non-existent directory must exit non-zero."""
    result = _run("--auto", "improve current code", "--base", "/nonexistent/path/xyz")
    assert result.returncode != 0, "Non-existent base_dir should not exit 0"


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — existing modes are untouched (no regressions)
# ─────────────────────────────────────────────────────────────────────────────

def test_help_flag_unaffected():
    """--help must still list --auto without breaking."""
    result = _run("--help")
    # argparse exits 0 for --help
    assert result.returncode == 0
    assert "--auto" in result.stdout, "--auto should appear in --help output"


def test_once_mode_unaffected():
    """--once with an empty/missing query still exits 1 (existing behaviour)."""
    result = _run("--once", "")
    assert result.returncode == 1, "--once '' should still exit 1"


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — AutoController unit tests (no subprocess needed)
# ─────────────────────────────────────────────────────────────────────────────

def test_controller_raises_on_empty_goal():
    import importlib, sys
    # Ensure tools/ is importable from project root
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from tools.auto.controller import AutoController
    import pytest
    with pytest.raises(ValueError, match="non-empty goal"):
        AutoController(goal="", base_dir="/tmp")


def test_run_auto_returns_int():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from tools.auto.controller import run_auto
    with tempfile.TemporaryDirectory() as tmp:
        code = run_auto(goal="test goal", base_dir=tmp)
    assert isinstance(code, int)
    assert code == 0


if __name__ == "__main__":
    # Quick self-check without pytest
    import traceback

    tests = [
        test_auto_flag_exits_zero,
        test_auto_flag_prints_banner,
        test_auto_creates_agent_dir,
        test_auto_empty_goal_exits_nonzero,
        test_auto_missing_base_exits_nonzero,
        test_help_flag_unaffected,
        test_once_mode_unaffected,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✔  {t.__name__}")
            passed += 1
        except Exception:
            print(f"  ✖  {t.__name__}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)