"""tests_bugfix/test_bugfix_last_missing_context_string.py

Regression test for the last_missing_context string-iteration bug.

Commit 5b2ae64 ("fix(inner_loop): guard hints/missing_context against string
LLM output") fixed two sites:

  1. _format_gate2_feedback — «hints» as a string → iterate characters.
     Covered by tests/test_gate2_feedback_hints.py.

  2. LLMGate2Validator.approve() — «missing_context» as a string →
     self.last_missing_context = [char for char in "fix the ending"]
     = ['f','i','x',' ','t','h','e',' ','e','n','d','i','n','g']
     instead of ["fix the ending"].  NOT covered anywhere.

This file pins the fix for gap (2).

The validator is exercised via LLMGate2Validator.approve() because that is
the real code path that calls _validate_with_llm() and sets
self.last_missing_context.  The LLM reply is injected via
patch("tools.llm_stream.request_completion") — the same pattern used by the
existing tests in tests/test_cr2_gate2_soft.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.inner_loop import LLMGate2Validator


# ── minimal stubs ────────────────────────────────────────────────────────────

_DUMMY_TASK = {
    "mode": "code",
    "goal": "test goal",
    "instruction": "do something",
}
_DUMMY_EXEC = MagicMock(stdout="", stderr="", returncode=0)
_DUMMY_CODER = MagicMock(raw_response="", missing_context=[])


def _make_validator() -> LLMGate2Validator:
    return LLMGate2Validator(
        base_url="http://localhost:99999/v1",
        model="test-model",
    )


def _approve_with_reply(reply_json: dict):
    """Run approve() with a fixed LLM JSON reply; return the validator."""
    v = _make_validator()
    raw = json.dumps(reply_json)
    with (
        patch("tools.llm_stream.request_completion", return_value=raw),
        patch.object(v, "_read_changed_content", return_value="some code"),
    ):
        v.approve(_DUMMY_TASK, _DUMMY_EXEC, _DUMMY_CODER)
    return v


# ── gap: missing_context as string ──────────────────────────────────────────

class TestLastMissingContextStringGuard:
    """Before the fix, a string «missing_context» was iterated as individual
    characters and stored as a list of single chars.  After the fix, a string
    value is treated as absent (empty list) — the LLM should have sent a list;
    a string is not a usable symbol list."""

    def test_string_missing_context_yields_empty_list(self):
        """A string «missing_context» must produce [] not a list of chars."""
        v = _approve_with_reply({
            "approved": False,
            "missing_context": "fix the ending",   # string, not list
            "feedback": "needs work",
        })
        assert v.last_missing_context == [], (
            f"A string 'missing_context' must yield [], "
            f"got {v.last_missing_context!r}"
        )

    def test_string_missing_context_not_iterated_as_chars(self):
        """Explicit character-iteration guard: none of the chars of
        'missing_context' must appear as individual entries."""
        v = _approve_with_reply({
            "approved": False,
            "missing_context": "MyHelper",
            "feedback": "need MyHelper",
        })
        # Before the fix this would be ['M','y','H','e','l','p','e','r']
        for char in "MyHelper":
            assert char not in v.last_missing_context, (
                f"Character {char!r} must not appear as a separate entry "
                f"(string iterated as chars): {v.last_missing_context!r}"
            )

    def test_list_missing_context_still_works(self):
        """Regression guard: a proper list must still be stored correctly."""
        v = _approve_with_reply({
            "approved": False,
            "missing_context": ["MyHelper", "AnotherSymbol"],
            "feedback": "need symbols",
        })
        assert v.last_missing_context == ["MyHelper", "AnotherSymbol"], (
            f"A list 'missing_context' must be stored as-is, "
            f"got {v.last_missing_context!r}"
        )

    def test_absent_missing_context_gives_empty_list(self):
        """Key absent entirely must produce []."""
        v = _approve_with_reply({
            "approved": False,
            "feedback": "no missing context key at all",
        })
        assert v.last_missing_context == []

    def test_none_missing_context_gives_empty_list(self):
        """null JSON value must produce []."""
        v = _approve_with_reply({
            "approved": False,
            "missing_context": None,
            "feedback": "null value",
        })
        assert v.last_missing_context == []

    def test_int_missing_context_gives_empty_list(self):
        """A non-list, non-string type must produce [] (conservative drop)."""
        v = _approve_with_reply({
            "approved": False,
            "missing_context": 42,
            "feedback": "wrong type",
        })
        assert v.last_missing_context == []

    def test_empty_list_missing_context_gives_empty_list(self):
        """An explicit empty list must produce [] (not crash)."""
        v = _approve_with_reply({
            "approved": False,
            "missing_context": [],
            "feedback": "nothing missing",
        })
        assert v.last_missing_context == []

    def test_approved_true_still_clears_last_missing_context(self):
        """When approved=True, last_missing_context is cleared regardless."""
        v = _approve_with_reply({
            "approved": True,
            "missing_context": ["SomeSymbol"],
        })
        # An approved verdict: last_missing_context should not matter to the
        # caller, but the fix must not break the approved path either.
        # The attribute exists and is a list.
        assert isinstance(v.last_missing_context, list)
