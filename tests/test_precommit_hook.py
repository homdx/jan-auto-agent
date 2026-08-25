"""TIERS-1 — the pre-commit hook does what Tests.MD promises.

The hook guards failures the suite structurally cannot catch (a missing
test does not fail; a dangling symlink is skipped silently), so the hook
itself needs covering — an executable-bit slip or a stale path inside it
would disable the guard without any visible symptom.

Each test runs the hook against a throwaway git repository built in
``tmp_path``, so nothing here touches the real working tree or its index.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "githooks" / "pre-commit"
SYNC = REPO_ROOT / "scripts" / "sync_test_tiers.py"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="needs git and bash",
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A miniature repo with the same tier layout as the real one."""
    root = tmp_path / "repo"
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "githooks").mkdir()

    shutil.copy(SYNC, root / "scripts" / "sync_test_tiers.py")
    shutil.copy(HOOK, root / "githooks" / "pre-commit")
    os.chmod(root / "githooks" / "pre-commit", 0o755)

    (root / "tests" / "_pass_a_stub.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_fast.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (root / "tests" / "test_heavy.py").write_text("def test_b(): pass\n", encoding="utf-8")
    (root / "tests" / "SLOW_TESTS.txt").write_text("test_heavy.py\n", encoding="utf-8")

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "core.hooksPath", "githooks")

    subprocess.run(
        [sys.executable, "scripts/sync_test_tiers.py"],
        cwd=str(root), capture_output=True, text=True, check=True,
    )
    return root


def _run_hook(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "githooks/pre-commit"],
        cwd=str(root), capture_output=True, text=True,
    )


# ── the hook file itself ─────────────────────────────────────────────────────

def test_hook_exists_and_is_executable():
    """A lost +x bit disables the guard with no other symptom."""
    assert HOOK.is_file()
    assert os.access(HOOK, os.X_OK), "githooks/pre-commit is not executable"


def test_hook_is_documented():
    text = (REPO_ROOT / "Tests.MD").read_text(encoding="utf-8")
    assert "core.hooksPath githooks" in text
    assert "--no-verify" in text


def test_hook_passes_on_the_real_repository():
    """The committed tree must satisfy its own hook."""
    result = subprocess.run(
        ["bash", str(HOOK)], cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ── behaviour ────────────────────────────────────────────────────────────────

def test_clean_sandbox_passes(sandbox):
    result = _run_hook(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_shared_link_is_caught(sandbox):
    """Exactly the .smoke_tests/fixtures loss that motivated the hook."""
    (sandbox / ".smoke_tests" / "fixtures").unlink()
    result = _run_hook(sandbox)
    assert result.returncode == 1
    assert "fixtures" in result.stdout


def test_new_test_without_a_symlink_is_caught(sandbox):
    (sandbox / "tests" / "test_new.py").write_text("def test_c(): pass\n", encoding="utf-8")
    result = _run_hook(sandbox)
    assert result.returncode == 1
    assert "test_new.py" in result.stdout


def test_absolute_symlink_is_caught(sandbox):
    """The original bug: a link into one developer's home directory."""
    link = sandbox / ".regression_tests" / "test_heavy.py"
    link.unlink()
    link.symlink_to("/home/someone/elsewhere/tests/test_heavy.py")
    result = _run_hook(sandbox)
    assert result.returncode == 1
    assert "/home/someone" in result.stdout


def test_orphan_link_for_deleted_test_is_caught(sandbox):
    (sandbox / "tests" / "test_fast.py").unlink()
    result = _run_hook(sandbox)
    assert result.returncode == 1
    assert "test_fast.py" in result.stdout


def test_stray_root_python_file_is_caught(sandbox):
    (sandbox / "test_stray.py").write_text("x = 1\n", encoding="utf-8")
    _git(sandbox, "add", "test_stray.py")
    result = _run_hook(sandbox)
    assert result.returncode == 1
    assert "test_stray.py" in result.stdout


def test_allowed_root_script_is_not_flagged(sandbox):
    (sandbox / "main.py").write_text("x = 1\n", encoding="utf-8")
    _git(sandbox, "add", "main.py")
    result = _run_hook(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr


def test_unstaged_root_file_is_not_flagged(sandbox):
    """Only files ADDED by this commit count — no blocking unrelated work."""
    (sandbox / "scratch.py").write_text("x = 1\n", encoding="utf-8")
    result = _run_hook(sandbox)
    assert result.returncode == 0, result.stdout + result.stderr


def test_hook_does_not_modify_the_tree(sandbox):
    """Read-only: it reports the fix command, it does not apply it."""
    (sandbox / ".smoke_tests" / "fixtures").unlink()
    before = sorted(p.name for p in (sandbox / ".smoke_tests").iterdir())

    _run_hook(sandbox)

    after = sorted(p.name for p in (sandbox / ".smoke_tests").iterdir())
    assert before == after


def test_failure_message_names_the_fix_command(sandbox):
    (sandbox / ".smoke_tests" / "fixtures").unlink()
    result = _run_hook(sandbox)
    assert "sync_test_tiers.py" in result.stdout
    assert "--no-verify" in result.stdout


def test_hook_blocks_a_real_commit(sandbox):
    """End-to-end through git, not just by invoking the script."""
    (sandbox / "tests" / "test_new.py").write_text("def test_c(): pass\n", encoding="utf-8")
    _git(sandbox, "add", "-A")
    result = _git(sandbox, "commit", "-m", "should be blocked")
    assert result.returncode != 0
    assert "test_new.py" in result.stdout + result.stderr


def test_no_verify_bypasses_the_hook(sandbox):
    (sandbox / "tests" / "test_new.py").write_text("def test_c(): pass\n", encoding="utf-8")
    _git(sandbox, "add", "-A")
    result = _git(sandbox, "commit", "--no-verify", "-m", "bypassed")
    assert result.returncode == 0, result.stdout + result.stderr
