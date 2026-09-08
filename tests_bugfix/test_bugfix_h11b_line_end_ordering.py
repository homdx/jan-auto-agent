"""H11b — a null line_end must not invent (or hide) a task dependency.

``BacklogPrioritiser._same_file_upstream`` decides whether task A's edit
finishes above where task B's begins; a True adds a dependency edge, and
dependencies are hard blockers in the controller (a task whose dep is not
DONE is marked BLOCKED and never run).

``a_end`` used to fall back to ``line_start`` when ``line_end`` was null, so
a symbol-anchored citation at line 1 was judged to *end* at line 1 and every
later task in the same file became its dependent — an ordering the citation
never claimed, serialising work that could have run in any order.

FIX-2 #15 (2c7ad71) removed that fallback. This module pins the whole
predicate in the bugfix suite, including the two directions candidate fixes
got wrong: requiring B's ``line_end`` as well (which drops real edges — B's
extent has no bearing on whether A finishes before B starts), and accepting
an A citation with an end but no start (which is not a range either, and the
docstring already promises "numeric line anchors").
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation  # noqa: E402
from tools.auto.backlog_prioritiser import build_backlog  # noqa: E402

FILE = "tools/m.py"


def _cand(
    *,
    title: str,
    symbol: str,
    line_start: int | None,
    line_end: int | None,
) -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction="Do something useful.",
        target_files=[FILE],
        acceptance_check="python -m pytest tests/ -q",
        cited_location=CitedLocation(
            file=FILE, symbol=symbol, line_start=line_start, line_end=line_end,
        ),
        cluster="agents",
    )


def _deps(a_bounds, b_bounds) -> set[str]:
    """B's dependencies after prioritising A then B."""
    a = _cand(title="Task A", symbol="fn_a",
              line_start=a_bounds[0], line_end=a_bounds[1])
    b = _cand(title="Task B", symbol="fn_b",
              line_start=b_bounds[0], line_end=b_bounds[1])
    backlog = build_backlog([a, b])
    by_title = {t.title: t for t in backlog.auto_tasks}
    return set(by_title["Task B"].dependencies), by_title["Task A"].task_id


@pytest.mark.parametrize(
    "a_bounds,b_bounds",
    [
        ((1, None), (200, 220)),   # the reported bug: A's extent unknown
        ((None, 5), (200, 220)),   # an end with no start is not a range
        ((None, None), (200, 220)),  # symbol-only citation, no anchors at all
        ((1, 5), (None, 220)),     # B's start unknown — nothing to compare to
        ((1, 5), (None, None)),
    ],
)
def test_incomplete_anchors_create_no_dependency(a_bounds, b_bounds) -> None:
    """With the range unknown, infer nothing — never guess an ordering."""
    deps, a_id = _deps(a_bounds, b_bounds)
    assert a_id not in deps


def test_known_disjoint_ranges_still_create_the_dependency() -> None:
    """The heuristic must still fire when both anchors really are known —
    dropping the fallback must not turn into dropping the feature."""
    deps, a_id = _deps((1, 5), (200, 220))
    assert a_id in deps


def test_b_without_a_line_end_still_gets_the_dependency() -> None:
    """B's own extent is irrelevant: A ending at 5 is above B starting at
    200 whether or not B says where it stops. One candidate fix demanded
    loc_b.line_end too, which silently drops edges like this one."""
    deps, a_id = _deps((1, 5), (200, None))
    assert a_id in deps


def test_overlapping_ranges_create_no_dependency() -> None:
    """A ends below where B starts — they overlap, so neither is upstream."""
    deps, a_id = _deps((1, 250), (200, 220))
    assert a_id not in deps


def test_adjacent_ranges_are_ordered_but_touching_ones_are_not() -> None:
    """The comparison is strict: A ending exactly where B starts is a shared
    line, not a clean hand-off."""
    deps_touching, a_id = _deps((1, 200), (200, 220))
    assert a_id not in deps_touching

    deps_adjacent, a_id = _deps((1, 199), (200, 220))
    assert a_id in deps_adjacent
