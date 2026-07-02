"""tests/test_cr19_chapter_deps.py — AUTO-CR-19-3: filename-derived chapter dependencies.

BacklogPrioritiser must infer ordering edges from chapter filenames so that a
failed/exhausted chapter N blocks chapter N+1 rather than leaving a silent
narrative gap.

Spec (from CREATIVE_AUTO_EPIC_CR19.md):
  - test_edges_from_filenames
  - test_numeric_not_lexicographic
  - test_gap_tolerated
  - test_failed_dependency_blocks_successor
"""

from __future__ import annotations


from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.backlog_prioritiser import (
    BacklogPrioritiser,
    ReadyTask,
    _chapter_dependencies,
    _chapter_number,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_candidate(title: str, target_files: list[str]) -> CandidateTask:
    """Minimal CandidateTask for dependency-inference tests."""
    return CandidateTask(
        title=title,
        instruction=f"Write {title}",
        target_files=target_files,
        acceptance_check=f"grep -q '{title}' output.txt",
        cited_location=CitedLocation(file=target_files[0] if target_files else "story.md"),
        cluster="creative",
    )


def _make_ready(task_id: str, target_files: list[str], original_index: int = 0) -> ReadyTask:
    """ReadyTask with a given task_id and target_files."""
    return ReadyTask(
        task_id=task_id,
        candidate=_make_candidate(task_id, target_files),
        original_index=original_index,
    )


def _ids(tasks: list[ReadyTask]) -> list[str]:
    return [t.task_id for t in tasks]


def _deps_map(tasks: list[ReadyTask]) -> dict[str, list[str]]:
    return {t.task_id: list(t.dependencies) for t in tasks}


# ─────────────────────────────────────────────────────────────────────────────
# Unit: _chapter_number helper
# ─────────────────────────────────────────────────────────────────────────────

class TestChapterNumber:

    def test_simple_underscore(self):
        t = _make_ready("T1", ["chapter_3.md"])
        assert _chapter_number(t) == 3

    def test_hyphen_separator(self):
        t = _make_ready("T1", ["chapter-7.txt"])
        assert _chapter_number(t) == 7

    def test_space_separator(self):
        t = _make_ready("T1", ["chapter 2.md"])
        assert _chapter_number(t) == 2

    def test_no_separator(self):
        t = _make_ready("T1", ["chapter10.md"])
        assert _chapter_number(t) == 10

    def test_uppercase(self):
        t = _make_ready("T1", ["Chapter_05.md"])
        assert _chapter_number(t) == 5

    def test_no_chapter_file(self):
        t = _make_ready("T1", ["src/main.py", "README.md"])
        assert _chapter_number(t) is None

    def test_returns_max_when_multiple_chapters_in_target_files(self):
        """A task touching chapter_3 and chapter_4 → highest is 4."""
        t = _make_ready("T1", ["chapter_3.md", "chapter_4.md"])
        assert _chapter_number(t) == 4

    def test_leading_zeros_parsed_as_decimal(self):
        t = _make_ready("T1", ["chapter_07.md"])
        assert _chapter_number(t) == 7


# ─────────────────────────────────────────────────────────────────────────────
# Unit: _chapter_dependencies function
# ─────────────────────────────────────────────────────────────────────────────

class TestChapterDependencies:

    def test_edges_from_filenames(self):
        """tasks targeting chapter_1/2/3 → deps {t2:[t1], t3:[t2]}."""
        t1 = _make_ready("T1", ["chapter_1.md"], original_index=0)
        t2 = _make_ready("T2", ["chapter_2.md"], original_index=1)
        t3 = _make_ready("T3", ["chapter_3.md"], original_index=2)

        edges = _chapter_dependencies([t1, t2, t3])

        assert edges.get("T2") == ["T1"]
        assert edges.get("T3") == ["T2"]
        assert "T1" not in edges  # chapter_1 has no predecessor

    def test_numeric_not_lexicographic(self):
        """chapter_10 depends on chapter_2 (or nearest below), NOT on chapter_1
        by string ordering.  With chapters 2 and 10, chapter_10 → chapter_2."""
        t2 = _make_ready("T2", ["chapter_2.md"], original_index=0)
        t10 = _make_ready("T10", ["chapter_10.md"], original_index=1)

        edges = _chapter_dependencies([t2, t10])

        assert edges.get("T10") == ["T2"]
        # Ensure chapter_1 would NOT be the predecessor if it were present
        # (tested separately via three-task variant below)

    def test_numeric_three_tasks_10_depends_on_9_not_1(self):
        """With chapters 1, 9, 10 → chapter_10 depends on chapter_9."""
        t1 = _make_ready("T1", ["chapter_1.md"], original_index=0)
        t9 = _make_ready("T9", ["chapter_9.md"], original_index=1)
        t10 = _make_ready("T10", ["chapter_10.md"], original_index=2)

        edges = _chapter_dependencies([t1, t9, t10])

        # chapter_10 must depend on chapter_9, not chapter_1
        assert edges.get("T10") == ["T9"]
        # chapter_9 must depend on chapter_1
        assert edges.get("T9") == ["T1"]

    def test_gap_tolerated(self):
        """Chapters 1 and 3 present (no 2) → chapter_3 depends on chapter_1."""
        t1 = _make_ready("T1", ["chapter_1.md"], original_index=0)
        t3 = _make_ready("T3", ["chapter_3.md"], original_index=1)

        edges = _chapter_dependencies([t1, t3])

        assert edges.get("T3") == ["T1"]
        assert "T1" not in edges

    def test_single_chapter_no_edges(self):
        t1 = _make_ready("T1", ["chapter_1.md"])
        edges = _chapter_dependencies([t1])
        assert edges == {}

    def test_no_chapter_tasks_no_edges(self):
        t1 = _make_ready("T1", ["src/main.py"])
        t2 = _make_ready("T2", ["src/utils.py"])
        edges = _chapter_dependencies([t1, t2])
        assert edges == {}

    def test_mixed_chapter_and_code_tasks(self):
        """Code tasks alongside chapter tasks don't get spurious edges."""
        tc = _make_ready("TC", ["src/utils.py"], original_index=0)
        t1 = _make_ready("T1", ["chapter_1.md"], original_index=1)
        t2 = _make_ready("T2", ["chapter_2.md"], original_index=2)

        edges = _chapter_dependencies([tc, t1, t2])

        assert "TC" not in edges
        assert edges.get("T2") == ["T1"]
        assert "T1" not in edges

    def test_tasks_in_any_input_order(self):
        """_chapter_dependencies handles tasks given in non-ascending order."""
        t3 = _make_ready("T3", ["chapter_3.md"], original_index=2)
        t1 = _make_ready("T1", ["chapter_1.md"], original_index=0)
        t2 = _make_ready("T2", ["chapter_2.md"], original_index=1)

        edges = _chapter_dependencies([t3, t1, t2])

        assert edges.get("T2") == ["T1"]
        assert edges.get("T3") == ["T2"]


# ─────────────────────────────────────────────────────────────────────────────
# Integration: BacklogPrioritiser.build() wires chapter edges end-to-end
# ─────────────────────────────────────────────────────────────────────────────

class TestBacklogPrioritiserChapterIntegration:

    def _build(self, *file_lists: list[str]) -> list[ReadyTask]:
        """Build a backlog from candidates whose target_files are *file_lists*."""
        candidates = [
            _make_candidate(f"Task {i+1}", files)
            for i, files in enumerate(file_lists)
        ]
        bp = BacklogPrioritiser(task_id_prefix="AUTO-T")
        backlog = bp.build(candidates)
        return backlog.auto_tasks

    def test_build_adds_chapter_edges(self):
        tasks = self._build(
            ["chapter_1.md"],
            ["chapter_2.md"],
            ["chapter_3.md"],
        )
        dm = _deps_map(tasks)
        # Find the task_ids assigned to each chapter (ordered T1→T3 since build
        # assigns IDs before sorting, then topo-sort preserves chapter order).
        id_ch1 = next(t.task_id for t in tasks if "chapter_1" in t.target_files[0])
        id_ch2 = next(t.task_id for t in tasks if "chapter_2" in t.target_files[0])
        id_ch3 = next(t.task_id for t in tasks if "chapter_3" in t.target_files[0])

        assert id_ch1 in dm[id_ch2], "chapter_2 must depend on chapter_1"
        assert id_ch2 in dm[id_ch3], "chapter_3 must depend on chapter_2"
        assert dm[id_ch1] == [], "chapter_1 has no chapter predecessor"

    def test_topological_order_chapter_1_before_2_before_3(self):
        tasks = self._build(
            ["chapter_3.md"],  # deliberately given in reverse order
            ["chapter_1.md"],
            ["chapter_2.md"],
        )
        positions = {t.task_id: i for i, t in enumerate(tasks)}
        id_ch1 = next(t.task_id for t in tasks if "chapter_1" in t.target_files[0])
        id_ch2 = next(t.task_id for t in tasks if "chapter_2" in t.target_files[0])
        id_ch3 = next(t.task_id for t in tasks if "chapter_3" in t.target_files[0])

        assert positions[id_ch1] < positions[id_ch2], "chapter_1 before chapter_2"
        assert positions[id_ch2] < positions[id_ch3], "chapter_2 before chapter_3"


# ─────────────────────────────────────────────────────────────────────────────
# Blocked-successor behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestFailedDependencyBlocksSuccessor:
    """Simulate the controller's dependency-gate logic.

    The controller in ``_run_task_loop`` marks a task BLOCKED when any
    dependency is not STATUS_DONE.  Exhausted tasks are set to STATUS_BLOCKED
    by ExhaustionHandler — so ``dep["status"] != "done"`` is True and the
    successor is blocked.

    This test verifies that the dependency edges produced by
    ``_chapter_dependencies`` would cause the controller to skip chapter N+1
    when chapter N is in a non-DONE state.
    """

    def _simulate_controller_gate(
        self,
        tasks: list[ReadyTask],
        statuses: dict[str, str],
    ) -> dict[str, str]:
        """Return {task_id: "run" | "blocked"} mirroring controller logic."""
        result: dict[str, str] = {}
        for task in tasks:
            failed_deps = [
                dep_id
                for dep_id in task.dependencies
                if statuses.get(dep_id, "todo") != "done"
            ]
            result[task.task_id] = "blocked" if failed_deps else "run"
        return result

    def test_failed_dependency_blocks_successor(self):
        """chapter_2 task FAILED (blocked) → chapter_3 task is blocked, not run."""
        t1 = _make_ready("T1", ["chapter_1.md"], original_index=0)
        t2 = _make_ready("T2", ["chapter_2.md"], original_index=1)
        t3 = _make_ready("T3", ["chapter_3.md"], original_index=2)

        edges = _chapter_dependencies([t1, t2, t3])
        for task_id, dep_ids in edges.items():
            task = next(t for t in [t1, t2, t3] if t.task_id == task_id)
            task.dependencies = dep_ids

        # chapter_1 done, chapter_2 exhausted → blocked
        statuses = {"T1": "done", "T2": "blocked", "T3": "todo"}
        outcome = self._simulate_controller_gate([t1, t2, t3], statuses)

        assert outcome["T1"] == "run"    # no deps, runs fine
        assert outcome["T2"] == "run"    # dep T1 is done
        assert outcome["T3"] == "blocked"  # dep T2 is NOT done

    def test_done_dependency_allows_successor(self):
        """When chapter_2 is done, chapter_3 is not blocked."""
        t1 = _make_ready("T1", ["chapter_1.md"], original_index=0)
        t2 = _make_ready("T2", ["chapter_2.md"], original_index=1)
        t3 = _make_ready("T3", ["chapter_3.md"], original_index=2)

        edges = _chapter_dependencies([t1, t2, t3])
        for task_id, dep_ids in edges.items():
            task = next(t for t in [t1, t2, t3] if t.task_id == task_id)
            task.dependencies = dep_ids

        statuses = {"T1": "done", "T2": "done", "T3": "todo"}
        outcome = self._simulate_controller_gate([t1, t2, t3], statuses)

        assert outcome["T3"] == "run"

    def test_chapter_1_never_blocked_by_chapter_deps(self):
        """The first chapter has no predecessor → never blocked by chapter edges."""
        t1 = _make_ready("T1", ["chapter_1.md"], original_index=0)
        t2 = _make_ready("T2", ["chapter_2.md"], original_index=1)

        edges = _chapter_dependencies([t1, t2])
        for task_id, dep_ids in edges.items():
            task = next(t for t in [t1, t2] if t.task_id == task_id)
            task.dependencies = dep_ids

        statuses = {"T1": "todo", "T2": "todo"}
        outcome = self._simulate_controller_gate([t1, t2], statuses)

        assert outcome["T1"] == "run"
