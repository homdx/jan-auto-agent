"""tests_bugfix/test_bugfix_345_recovery_paths.py — AGNES-6 recovery paths.

Pins three recovery paths the suite only passed through before:

* T13 — ``check_improvements.parse_improvements`` on a ``Path.read_text`` that
  raises ``PermissionError`` (an ``OSError``): it prints a WARNING and returns
  ``[]``, it does not raise.
* T50 — ``CommitOnSuccess.commit_result`` when the post-commit memory commit
  (``GitManager.commit_paths``) fails: the task's own commit already happened,
  so the outcome stays committed with the primary SHA and the task was already
  written DONE. The failure is raised at ``commit_paths`` itself, not by
  replacing ``_commit_memory_files``.
* T62 — the repo walk at ``max_depth=2`` over a depth 0..3 tree: both the
  plain walk and the collect-skip reporting walk include depths 0, 1 and 2 and
  leave out only depth 3.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import check_improvements  # noqa: E402
from tools.auto.commit_on_success import (  # noqa: E402
    COMMIT_OK, CommitOnSuccess,
)
from tools.auto.git_manager import GitError  # noqa: E402
from tools.auto.repo_ingest import RepoIngestor  # noqa: E402
from tools.auto.state import STATUS_DONE  # noqa: E402


# ── T13: parse_improvements fail-open on an unreadable file ───────────────────

def test_parse_improvements_permission_error_returns_empty(tmp_path, monkeypatch, capsys):
    target = tmp_path / "IMPROVEMENTS.md"

    def _deny(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "read_text", _deny)

    result = check_improvements.parse_improvements(target)

    out = capsys.readouterr().out
    assert result == []
    assert "WARNING" in out
    assert "IMPROVEMENTS.md" in out


# ── T50: a failing memory commit does not undo the task's own commit ──────────

class _RecordingState:
    def __init__(self):
        self.statuses: dict[str, str] = {}
        self.logs: list[str] = []

    def set_task_status(self, task_id, status, **kwargs):
        self.statuses[task_id] = status

    def log(self, message):
        self.logs.append(message)


class _MemoryCommittingGit:
    """A git manager whose task commit succeeds and whose memory commit fails."""

    def __init__(self, exc: Exception, state=None):
        self._exc = exc
        self._state = state
        self.memory_calls: list[tuple[str, list]] = []
        self.status_at_failure = None

    def commit_task(self, task_id, title):  # noqa: D401 — test double
        return "primary-sha-0123456789"

    def commit_paths(self, message, paths):
        self.memory_calls.append((message, list(paths)))
        if self._state is not None:
            self.status_at_failure = self._state.statuses.get("CH1")
        raise self._exc


@pytest.mark.parametrize("exc", [GitError("index.lock"), OSError("disk full")])
def test_memory_commit_failure_keeps_the_primary_commit(tmp_path, exc):
    synopsis = tmp_path / "synopsis.md"
    synopsis.write_text("- chapter one happened\n", encoding="utf-8")
    summary = SimpleNamespace(_synopsis_path=synopsis)

    # A no-op update: this test is about the memory COMMIT failing, not the rewrite.
    summary.update = lambda chapter_file, base_dir=None: None

    state = _RecordingState()
    git = _MemoryCommittingGit(exc, state)
    cos = CommitOnSuccess(
        git, state, summary_memory=summary, task_mode="creative",
        base_dir=tmp_path,
    )
    task = {"id": "CH1", "title": "one", "target_files": ["CH1.py"],
            "acceptance_check": "true"}

    result = cos.commit_result(task)

    assert result.outcome == COMMIT_OK
    assert result.sha == "primary-sha-0123456789"
    assert git.status_at_failure == STATUS_DONE
    assert state.statuses["CH1"] == STATUS_DONE
    assert git.memory_calls and git.memory_calls[0][1] == [synopsis]


# ── T62: the depth limit includes max_depth itself, not only shallower files ──

def _depth(rel: str) -> int:
    return len(Path(rel).parts) - 1


def _depth_tree(base: Path) -> None:
    for parts in (
        ("root.txt",),
        ("d1", "one.txt"),
        ("d2", "sub", "two.txt"),
        ("d3", "sub", "sub2", "three.txt"),
    ):
        p = base.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n", encoding="utf-8")


@pytest.mark.parametrize("collect_skips", [False, True])
def test_walk_at_max_depth_includes_it_and_skips_only_deeper(tmp_path, collect_skips):
    base = tmp_path / "repo"
    _depth_tree(base)

    ingestor = RepoIngestor(base, max_depth=2, collect_skips=collect_skips)
    walked = list(ingestor.walk())

    assert {_depth(p) for p in walked} == {0, 1, 2}, walked
    assert all(_depth(p) <= 2 for p in walked), walked


def test_walk_reports_the_depth_three_file_only_when_collecting_skips(tmp_path):
    base = tmp_path / "repo"
    _depth_tree(base)

    reporting = RepoIngestor(base, max_depth=2, collect_skips=True)
    reporting_walked = list(reporting.walk())
    assert not any(_depth(p) == 3 for p in reporting_walked)
    assert ("d3/sub/sub2/three.txt", "too deep") in reporting.skipped_files

    plain = RepoIngestor(base, max_depth=2, collect_skips=False)
    plain_walked = list(plain.walk())
    assert {_depth(p) for p in plain_walked} == {0, 1, 2}
    assert plain.skipped_files == []
