"""FIX-2 #15 (tie-breaking half) — ties were broken wave-locally, not globally.

``_topological_sort`` runs Kahn's algorithm and breaks ties by
``original_index`` to preserve the Architect's ordering. The ready set used to
be a FIFO deque to which each pop appended its own newly-freed tasks, sorted
only *within that batch*. A task freed by an early pop therefore jumped ahead
of a lower-index task freed later, so the emitted order depended on traversal
history rather than on the graph.

The canonical shape, with indices in brackets:

    A[0] → D[3]        pop A, D becomes ready and is appended
    B[1] → C[2]        pop B, C becomes ready and is appended after D

The deque then held [D, C] and emitted ``A B D C`` — D ahead of C at the same
depth purely because A happened to be popped first. Re-sorting the whole ready
set after every pop makes the next emission the global minimum, giving
``A B C D``.

``ReadyTask.dependencies`` is persisted into plan.json and consumed by the
Coder loop, so this is the order work is actually attempted in.

Also pinned here: determinism when two tasks share an ``original_index``. Five
of the eight candidate patches replaced the deque with a ``heapq`` keyed on
``(original_index, task)``; a duplicate index makes that tuple comparison fall
through to comparing ``ReadyTask`` objects, which raises ``TypeError`` because
the dataclass is not ordered. The shipped ``sorted()`` is stable and cannot,
so ``test_duplicate_original_index_is_stable_not_a_crash`` keeps that
regression out if the sort is ever "optimised" into a heap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation  # noqa: E402
from tools.auto.backlog_prioritiser import ReadyTask, _topological_sort  # noqa: E402


def _task(task_id: str, index: int, deps: list[str] | None = None) -> ReadyTask:
    return ReadyTask(
        task_id=task_id,
        candidate=CandidateTask(
            title=task_id,
            instruction="i",
            target_files=["a.py"],
            acceptance_check="true",
            cited_location=CitedLocation(file="a.py"),
            cluster="c",
        ),
        dependencies=list(deps or []),
        original_index=index,
    )


def _ids(tasks: list[ReadyTask]) -> list[str]:
    return [t.task_id for t in tasks]


class TestGlobalTieBreak:
    def test_two_independent_chains_interleave_by_original_index(self) -> None:
        """The canonical case. Wave-local ordering yields A B D C."""
        a = _task("A", 0)
        b = _task("B", 1)
        c = _task("C", 2, deps=["B"])
        d = _task("D", 3, deps=["A"])

        assert _ids(_topological_sort([a, b, c, d])) == ["A", "B", "C", "D"]

    def test_input_order_does_not_change_the_result(self) -> None:
        """Ordering must be a function of the graph, not of list order."""
        a = _task("A", 0)
        b = _task("B", 1)
        c = _task("C", 2, deps=["B"])
        d = _task("D", 3, deps=["A"])

        assert _ids(_topological_sort([d, c, b, a])) == ["A", "B", "C", "D"]

    def test_deep_chain_does_not_starve_a_lower_index_sibling(self) -> None:
        """A[0] frees a long chain; E[1] is ready from the start and must not
        be pushed behind the chain's later-index members."""
        a = _task("A", 0)
        e = _task("E", 1)
        b = _task("B", 2, deps=["A"])
        c = _task("C", 3, deps=["B"])

        assert _ids(_topological_sort([a, e, b, c])) == ["A", "E", "B", "C"]

    def test_dependencies_are_still_respected(self) -> None:
        """Global tie-breaking must never reorder across a real edge."""
        first = _task("FIRST", 9)
        second = _task("SECOND", 0, deps=["FIRST"])

        order = _ids(_topological_sort([first, second]))
        assert order.index("FIRST") < order.index("SECOND")


class TestDeterminism:
    def test_duplicate_original_index_is_stable_not_a_crash(self) -> None:
        """A heap keyed on (original_index, ReadyTask) raises TypeError here."""
        a = _task("A", 0)
        b = _task("B", 0)
        c = _task("C", 0)

        assert _ids(_topological_sort([a, b, c])) == ["A", "B", "C"]

    def test_repeated_runs_agree(self) -> None:
        def build() -> list[ReadyTask]:
            return [
                _task("A", 0),
                _task("B", 1),
                _task("C", 2, deps=["B"]),
                _task("D", 3, deps=["A"]),
            ]

        assert _ids(_topological_sort(build())) == _ids(_topological_sort(build()))


class TestUnchangedBehaviour:
    def test_empty_input(self) -> None:
        assert _topological_sort([]) == []

    def test_all_independent_tasks_keep_original_order(self) -> None:
        tasks = [_task("C", 2), _task("A", 0), _task("B", 1)]
        assert _ids(_topological_sort(tasks)) == ["A", "B", "C"]

    def test_unknown_dependency_is_ignored(self) -> None:
        a = _task("A", 0, deps=["GHOST"])
        b = _task("B", 1)
        assert set(_ids(_topological_sort([a, b]))) == {"A", "B"}

    def test_cycle_falls_back_to_original_order(self) -> None:
        a = _task("A", 0, deps=["B"])
        b = _task("B", 1, deps=["A"])
        assert _ids(_topological_sort([a, b])) == ["A", "B"]

    def test_no_task_is_lost_or_duplicated(self) -> None:
        tasks = [
            _task("A", 0),
            _task("B", 1),
            _task("C", 2, deps=["B"]),
            _task("D", 3, deps=["A"]),
            _task("E", 4, deps=["C", "D"]),
        ]
        out = _ids(_topological_sort(tasks))
        assert sorted(out) == ["A", "B", "C", "D", "E"]
        assert len(out) == len(set(out))
