"""tests/test_harden30_a_executor_workspace_pythonpath.py — HARDEN-30 A.

An acceptance check run by :meth:`Executor.run` imports the project from the
workspace it runs in: the workspace comes first on the child's ``PYTHONPATH``,
never the source repository, and an inherited ``PYTHONPATH`` entry that points
into the source repository cannot take precedence. :meth:`Executor.run_raw`
keeps its base_dir behaviour.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from tools.auto.executor import Executor


def _write_tree(repo: Path) -> None:
    (repo / "app.py").write_text(
        textwrap.dedent(
            """\
            from pathlib import Path
            def check() -> None:
                Path(__file__).with_name("side_effect.txt").write_text(__file__)
            """
        ),
        encoding="utf-8",
    )
    (repo / "checks").mkdir()
    (repo / "checks" / "check.py").write_text(
        "import app\nprint(app.__file__)\napp.check()\n",
        encoding="utf-8",
    )


def _task() -> dict:
    return {
        "id": "T1",
        "target_files": ["app.py", "checks/check.py"],
        "acceptance_check": "python checks/check.py",
    }


def test_run_imports_workspace_copy_not_the_live_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_tree(repo)
    ex = Executor(base_dir=repo, workspace_root=tmp_path / "ws", timeout_sec=5)
    result = ex.run(_task())
    assert result.passed, result.stderr
    # The source repository is never written into.
    assert not (repo / "side_effect.txt").exists()
    # The workspace copy ran and left its side effect inside the workspace.
    workspace = tmp_path / "ws" / "T1"
    assert (workspace / "side_effect.txt").exists()
    # The imported module's __file__ is the workspace copy.
    assert f"{workspace / 'app.py'}" in result.stdout


def test_run_from_workspace_root_still_imports_project_modules(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_tree(repo)
    ex = Executor(base_dir=repo, workspace_root=tmp_path / "ws", timeout_sec=5)
    result = ex.run(_task())
    assert result.passed, result.stderr
    assert f"{tmp_path / 'ws' / 'T1' / 'app.py'}" in result.stdout


def test_inherited_repo_pythonpath_does_not_shadow_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_tree(repo)
    # An inherited PYTHONPATH entry pointing into the source repository.
    monkeypatch.setenv("PYTHONPATH", str(repo))
    ex = Executor(base_dir=repo, workspace_root=tmp_path / "ws", timeout_sec=5)
    result = ex.run(_task())
    assert result.passed, result.stderr
    assert not (repo / "side_effect.txt").exists()
    assert (tmp_path / "ws" / "T1" / "side_effect.txt").exists()
    assert f"{tmp_path / 'ws' / 'T1' / 'app.py'}" in result.stdout


def test_run_raw_still_runs_in_base_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mark.py").write_text(
        "from pathlib import Path\nPath(__file__).with_name('raw_marker.txt').write_text('raw')\n",
        encoding="utf-8",
    )
    ex = Executor(base_dir=repo, workspace_root=tmp_path / "ws", timeout_sec=60)  # not what is pinned
    result = ex.run_raw(f"{sys.executable} mark.py")
    assert result.passed, result.stderr
    # run_raw runs in base_dir: the marker lands next to mark.py in the repo.
    assert (repo / "raw_marker.txt").exists()
    assert result.task_id == ""
