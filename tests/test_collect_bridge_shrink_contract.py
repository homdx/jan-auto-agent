"""tests/test_collect_bridge_shrink_contract.py — PLAN-v2 V6.

The characterization guard on `CollectBridge._shrink`. V6 wires the budget
into `build_collect_context_block` so the pack's static row-shrinking runs
*before* `_shrink` ever gets a chance to pay an LLM call; `_shrink` itself is
**unchanged** by V6 (acceptance: "git diff touches no line inside _shrink").

This file pins `_shrink`'s current behaviour directly, bypassing `context_for`,
so no later ticket can drift it: the overshoot tolerance, the truncation
notice, `shrink_calls` accounting, and the fail-open-to-truncation guarantee.
The test must pass both before and after V6 — it tests the method V6 explicitly
does not touch.

Pinned behaviours (from the V6 plan):
  * summarizer within budget*1.15 -> returned verbatim
  * beyond 1.15 -> hard truncation with `[+N chars truncated by CollectBridge]`,
    overshoot logged
  * summarizer raises -> hard truncation, failure logged
  * summarizer_call is None -> hard truncation, shrink_calls stays 0
  * shrink_calls increments once per attempt, including failed ones
"""
from __future__ import annotations

import logging

from tools.auto.collect_bridge import _SHRINK_SYSTEM_PROMPT, CollectBridge

# A budget large enough that __init__'s max(200, ...) floor never interferes,
# and a raw string comfortably larger than it.
_BUDGET = 400
_RAW = "R" * 800
_TRUNC_NOTICE = "chars truncated by CollectBridge"


def _bare_bridge(summarizer_call=None) -> CollectBridge:
    """A bridge whose only purpose is to exercise `_shrink` in isolation.

    `model=None` is fine: `_shrink` never reads `self._model` — it consults
    only `self._summarizer_call`, `self._max_context_chars` and
    `self.shrink_calls`.
    """
    return CollectBridge(None, max_context_chars=_BUDGET, summarizer_call=summarizer_call)


# ── within tolerance: verbatim ──────────────────────────────────────────────


def test_shrink_summarizer_within_1_15_tolerance_returns_verbatim(caplog):
    """A summarizer reply that lands inside budget*1.15 is trusted and
    returned as-is (stripped), no hard truncation, shrink_calls == 1."""
    shrunk = "S" * int(_BUDGET * 1.15)  # exactly at the tolerance boundary

    def _summ(system, user):
        return shrunk

    bridge = _bare_bridge(_summ)
    with caplog.at_level(logging.WARNING):
        result = bridge._shrink(_RAW)

    assert result == shrunk
    assert bridge.shrink_calls == 1
    # No truncation notice, no overshoot warning.
    assert _TRUNC_NOTICE not in result
    assert not any("overshot" in r.message for r in caplog.records)


def test_shrink_summarizer_just_under_tolerance_returns_verbatim():
    """One character below the ceiling is still verbatim — the bound is
    inclusive (<=), not strict."""
    shrunk = "S" * (int(_BUDGET * 1.15) - 1)

    def _summ(system, user):
        return shrunk

    bridge = _bare_bridge(_summ)
    assert bridge._shrink(_RAW) == shrunk
    assert bridge.shrink_calls == 1


# ── overshoot: hard truncation ──────────────────────────────────────────────


def test_shrink_summarizer_overshoot_hard_truncates_and_logs(caplog):
    """Beyond budget*1.15 the LLM output is not trusted: hard truncation with
    the `[+N chars truncated]` notice, and an overshoot warning."""
    overshoot = "X" * (int(_BUDGET * 1.15) + 1)

    def _summ(system, user):
        return overshoot

    bridge = _bare_bridge(_summ)
    with caplog.at_level(logging.WARNING):
        result = bridge._shrink(_RAW)

    excess = len(_RAW) - _BUDGET
    assert result == _RAW[:_BUDGET] + f"\n… [+{excess} {_TRUNC_NOTICE}]\n"
    assert len(result) < len(overshoot)
    assert bridge.shrink_calls == 1
    assert any("overshot" in r.message for r in caplog.records)


def test_shrink_summarizer_returns_empty_string_hard_truncates():
    """An empty/blank summarizer reply is treated as a failure to shrink —
    hard truncation instead of a blank block."""
    def _summ(system, user):
        return "   "

    bridge = _bare_bridge(_summ)
    result = bridge._shrink(_RAW)

    excess = len(_RAW) - _BUDGET
    assert result == _RAW[:_BUDGET] + f"\n… [+{excess} {_TRUNC_NOTICE}]\n"
    assert bridge.shrink_calls == 1


# ── summarizer raises: hard truncation ──────────────────────────────────────


def test_shrink_summarizer_exception_hard_truncates_and_logs(caplog):
    """A summarizer that raises is caught: hard truncation with the notice,
    and the failure is logged — never propagates into the caller."""
    def _boom(system, user):
        raise RuntimeError("provider down")

    bridge = _bare_bridge(_boom)
    with caplog.at_level(logging.WARNING):
        result = bridge._shrink(_RAW)

    excess = len(_RAW) - _BUDGET
    assert result == _RAW[:_BUDGET] + f"\n… [+{excess} {_TRUNC_NOTICE}]\n"
    assert bridge.shrink_calls == 1
    assert any("shrink call failed" in r.message for r in caplog.records)


# ── no summarizer: hard truncation, zero LLM calls ─────────────────────────


def test_shrink_without_summarizer_hard_truncates_shrink_calls_zero():
    """summarizer_call=None: no LLM attempt at all, shrink_calls stays 0,
    pure hard truncation."""
    bridge = _bare_bridge(None)
    result = bridge._shrink(_RAW)

    excess = len(_RAW) - _BUDGET
    assert result == _RAW[:_BUDGET] + f"\n… [+{excess} {_TRUNC_NOTICE}]\n"
    assert bridge.shrink_calls == 0


# ── shrink_calls accounting ────────────────────────────────────────────────


def test_shrink_calls_increments_per_attempt_including_failed_ones():
    """Each _shrink invocation that reaches the summarizer bumps
    shrink_calls exactly once — even when that attempt raises, which falls
    through to hard truncation rather than propagating."""
    attempts = []

    def _summ(system, user):
        attempts.append(1)
        raise RuntimeError("transient")

    bridge = _bare_bridge(_summ)
    bridge._shrink(_RAW)  # failed attempt
    bridge._shrink(_RAW)  # second failed attempt

    assert bridge.shrink_calls == 2
    assert len(attempts) == 2  # summarizer was actually invoked both times


def test_shrink_system_prompt_and_user_budget_are_wired():
    """The summarizer receives the fixed system prompt and a user message
    that names the exact character budget — pinned so V6's 'don't touch
    _shrink' line is enforceable."""
    captured = {}

    def _summ(system, user):
        captured["system"] = system
        captured["user"] = user
        return "x" * _BUDGET

    bridge = _bare_bridge(_summ)
    bridge._shrink(_RAW)

    assert captured["system"] == _SHRINK_SYSTEM_PROMPT
    assert "at most" in captured["user"]
    assert str(_BUDGET) in captured["user"]
    assert _RAW in captured["user"]
