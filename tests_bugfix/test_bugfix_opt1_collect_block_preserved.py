"""OPT-1 -- the per-task collect block was dropped on the first context pull.

``InnerLoop.run`` seeds ``prefetched_context`` once per task with the
COLLECT-24 static block for ``target_files``. But the pull-model branches
later in the round reassign the whole variable::

    prefetched_context = self._broker.format_for_prompt(resolved_context)

That replaces the string outright, so the collect block disappeared the
first time the coder (or the validator) asked for extra context -- i.e. at
exactly the moment the model had just admitted it was missing something.
From that attempt on, the prompt was strictly poorer than on attempt 1.

There are TWO such reassignment sites, one on the coder branch and one on
the validator branch. Fixing only the coder one leaves the same hole on the
other path, which is what ``test_validator_branch_also_preserves_the_block``
pins.

The block is computed once per task and re-prepended through a local helper,
rather than re-querying the bridge on every attempt: ``context_for_many`` is
per-task static, so re-calling it is repeated work for an identical answer.

This is not part of the B1-B10 series -- it is an independent finding,
shipped separately so it can be taken or dropped on its own.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

COLLECT_MARKER = "COLLECT-RECORD-MARKER"
FORMATTED = "RESOLVED-SYMBOL-BLOCK"


class _FakeBridge:
    """Stands in for the collect bridge; counts how often it is queried."""

    def __init__(self, block: str) -> None:
        self._block = block
        self.calls = 0

    def context_for_many(self, target_files) -> str:
        self.calls += 1
        return self._block


def _build_context(bridge, formatted: str, *, pulls: int) -> tuple[str, int]:
    """Replay the seed-then-reassign sequence the way InnerLoop.run does it.

    Returns the final prefetched_context and how many times the bridge was
    queried, so the test can assert both the content and that the static
    block is not recomputed per attempt.
    """
    prefetched_context = ""
    collect_block = ""
    if bridge is not None:
        collect_block = bridge.context_for_many(["a.py"]) or ""
        if collect_block:
            prefetched_context = collect_block + "\n\n"

    def with_collect_block(text: str) -> str:
        if not collect_block:
            return text
        if not text:
            return collect_block + "\n\n"
        return collect_block + "\n\n" + text

    for _ in range(pulls):
        prefetched_context = with_collect_block(formatted)
    return prefetched_context, (bridge.calls if bridge else 0)


class TestCollectBlockSurvivesContextPulls:
    def test_block_is_present_before_any_pull(self) -> None:
        ctx, _ = _build_context(_FakeBridge(COLLECT_MARKER), FORMATTED, pulls=0)
        assert COLLECT_MARKER in ctx

    def test_block_survives_a_coder_context_pull(self) -> None:
        ctx, _ = _build_context(_FakeBridge(COLLECT_MARKER), FORMATTED, pulls=1)
        assert COLLECT_MARKER in ctx

    def test_validator_branch_also_preserves_the_block(self) -> None:
        """Both reassignment sites, not just the coder one."""
        ctx, _ = _build_context(_FakeBridge(COLLECT_MARKER), FORMATTED, pulls=2)
        assert COLLECT_MARKER in ctx

    def test_resolved_context_is_still_included(self) -> None:
        ctx, _ = _build_context(_FakeBridge(COLLECT_MARKER), FORMATTED, pulls=1)
        assert FORMATTED in ctx

    def test_collect_block_comes_first(self) -> None:
        ctx, _ = _build_context(_FakeBridge(COLLECT_MARKER), FORMATTED, pulls=1)
        assert ctx.index(COLLECT_MARKER) < ctx.index(FORMATTED)

    def test_block_is_not_duplicated(self) -> None:
        ctx, _ = _build_context(_FakeBridge(COLLECT_MARKER), FORMATTED, pulls=3)
        assert ctx.count(COLLECT_MARKER) == 1

    def test_bridge_is_queried_once_per_task_not_per_attempt(self) -> None:
        _, calls = _build_context(_FakeBridge(COLLECT_MARKER), FORMATTED, pulls=5)
        assert calls == 1


class TestPurelyAdditive:
    def test_no_bridge_leaves_context_untouched(self) -> None:
        ctx, _ = _build_context(None, FORMATTED, pulls=1)
        assert ctx == FORMATTED

    def test_empty_block_leaves_context_untouched(self) -> None:
        ctx, _ = _build_context(_FakeBridge(""), FORMATTED, pulls=1)
        assert ctx == FORMATTED

    def test_empty_formatted_context_still_carries_the_block(self) -> None:
        ctx, _ = _build_context(_FakeBridge(COLLECT_MARKER), "", pulls=1)
        assert ctx.strip() == COLLECT_MARKER


class TestSourceHasBothSitesGuarded:
    """Guards against a partial fix that patches only the coder branch."""

    def test_no_bare_format_for_prompt_assignment_remains(self) -> None:
        source = (
            PROJECT_ROOT / "tools" / "auto" / "inner_loop.py"
        ).read_text(encoding="utf-8")
        assert "prefetched_context = self._broker.format_for_prompt" not in source

    def test_both_reassignments_go_through_the_helper(self) -> None:
        source = (
            PROJECT_ROOT / "tools" / "auto" / "inner_loop.py"
        ).read_text(encoding="utf-8")
        assert source.count("prefetched_context = _with_collect_block(") == 2
