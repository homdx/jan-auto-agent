"""tests/test_collect_loader_absent.py — COLLECT-21.

No `.collect/` at all -> loader returns "no model" (`status="absent"`),
and every query method on it degrades to empty/None/unknown rather than
raising, so a caller that forgets to check `.available` still gets
today's behavior (no collect data) instead of a crash.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.collect import loader as loader_mod


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def test_absent_when_never_run(mini_repo):
    model = loader_mod.load(mini_repo)
    assert model.status == loader_mod.STATUS_ABSENT
    assert model.available is False
    assert not (mini_repo / ".collect").exists()


def test_absent_model_query_methods_degrade_gracefully(mini_repo):
    model = loader_mod.load(mini_repo)

    assert model.module("pkg/a.py") is None
    assert model.contracts_for("pkg/a.py") == []
    assert model.fail_open_for("pkg/a.py") == []
    assert model.zero_coverage() == []
    assert model.thin_coverage() == []
    assert model.gates_for() == []
    assert model.risk_for("pkg/a.py") is None
    assert model.config_map_for() == []

    answer = model.is_safe("pkg/a.py:1")
    assert answer.safe is False
    assert answer.reason == "unknown"


def test_absent_never_creates_collect_dir(mini_repo):
    loader_mod.load(mini_repo)
    assert not (mini_repo / ".collect").exists()
