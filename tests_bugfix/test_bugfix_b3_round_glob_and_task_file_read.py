"""B3 -- two sibling robustness holes on the round/deadline read path.

``utils.highest_completed_round`` globbed ``feedback_round_*.md`` and matched
names without checking that the entry is a file. A directory with that name
counted as a completed round, and the value is load-bearing:
``controller._reset_resettable_blocked_tasks`` compares it against
``max_rounds`` to decide whether a BLOCKED task may be reset, so a phantom
round parked the task in BLOCKED permanently.

``StateStore.read_task_file`` used ``read_text(...) if exists() else None``,
which raised in three cases the contract promises None for (a directory,
invalid UTF-8, a TOCTOU disappearance) and, separately, *created* the
per-task directory as a side effect of ``task_dir()`` on what is supposed to
be a pure read.

Without the fix: the directory-round test, all three read_task_file failure
tests, and ``test_read_does_not_create_the_task_directory`` fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore  # noqa: E402
from tools.auto.utils import highest_completed_round  # noqa: E402


class TestHighestCompletedRound:
    def test_real_feedback_files_are_counted(self, tmp_path: Path) -> None:
        (tmp_path / "feedback_round_1.md").write_text("x", encoding="utf-8")
        (tmp_path / "feedback_round_4.md").write_text("x", encoding="utf-8")
        assert highest_completed_round(tmp_path) == 4

    def test_a_directory_is_not_a_completed_round(self, tmp_path: Path) -> None:
        (tmp_path / "feedback_round_2.md").write_text("x", encoding="utf-8")
        (tmp_path / "feedback_round_9.md").mkdir()
        assert highest_completed_round(tmp_path) == 2

    def test_only_directories_means_no_rounds(self, tmp_path: Path) -> None:
        (tmp_path / "feedback_round_7.md").mkdir()
        assert highest_completed_round(tmp_path) == 0


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / ".agent")
    s.initialise("goal", tmp_path)
    return s


class TestReadTaskFile:
    def test_reads_an_ordinary_file(self, store: StateStore) -> None:
        (store.task_dir("T-1") / "note.txt").write_text("hi", encoding="utf-8")
        assert store.read_task_file("T-1", "note.txt") == "hi"

    def test_absent_file_returns_none(self, store: StateStore) -> None:
        assert store.read_task_file("T-1", "nope.txt") is None

    def test_directory_in_place_of_file_returns_none(
        self, store: StateStore
    ) -> None:
        (store.task_dir("T-1") / "note.txt").mkdir()
        assert store.read_task_file("T-1", "note.txt") is None

    def test_non_utf8_file_returns_none(self, store: StateStore) -> None:
        (store.task_dir("T-1") / "blob.txt").write_bytes(b"\xff\xfe\x00bad")
        assert store.read_task_file("T-1", "blob.txt") is None

    def test_read_does_not_create_the_task_directory(
        self, store: StateStore
    ) -> None:
        """A read must not mutate the filesystem.

        task_dir() mkdir()s as a side effect, so the old body created the
        per-task directory just to discover the file was not there.
        """
        task_dir = store._tasks_dir / store._safe_task_id("T-NEVER-SEEN")
        assert not task_dir.exists()
        assert store.read_task_file("T-NEVER-SEEN", "deadline.txt") is None
        assert not task_dir.exists()
