"""tests/test_gates_fail_mode_truth.py — GATES fail_mode must match reality.

build_gates_map citation-checks two of the three claims in each _GATE_SEED
entry: `module` must be a path Pass A actually scanned, and `parser` must
resolve to a real `def` in the repo — a citation that no longer resolves is a
hard failure, not a warning.

`fail_mode` is the claim it cannot check that way. How a caller treats an
unparseable verdict is not recoverable by resolving a name, so the field was
hand-asserted and unverified — and it was wrong for `language`:

    parseable, non-ASCII idents : {'привет'} -> gate REJECTS
    syntax error, same idents   : set()      -> gate PASSES (empty set)

_find_non_ascii_identifiers catches TokenError/IndentationError/SyntaxError/
ValueError and returns an empty set, which coder._write_files reads as "no bad
identifiers" and accepts. That is fail-OPEN, while the seed said "closed", so
a consumer of the gates map would have believed unanalysable content was
rejected.

These tests exercise the real implementations, so the table cannot drift from
the behaviour it documents.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect.gates import FAIL_MODES, _GATE_SEED


class TestFailModeVocabulary:
    def test_every_seed_uses_a_known_fail_mode(self):
        for name, spec in _GATE_SEED.items():
            assert spec["fail_mode"] in FAIL_MODES, name


class TestLanguageGateIsFailOpen:
    """The gate this table got wrong."""

    def test_seed_records_open(self):
        assert _GATE_SEED["language"]["fail_mode"] == "open"

    def test_parseable_non_ascii_is_caught(self):
        from tools.auto.coder import _find_non_ascii_identifiers
        assert _find_non_ascii_identifiers("def привет():\n    return 1\n")

    def test_syntax_error_is_still_caught_on_this_interpreter(self):
        """A sanity check, not the contract test: at least one real-world
        broken-syntax input goes through the except branch on whatever
        interpreter is running this suite right now. Not parametrized and
        not exhaustive — see test_exception_branch_always_returns_empty_set
        for the version-independent version of this guarantee.
        """
        from tools.auto.coder import _find_non_ascii_identifiers
        assert _find_non_ascii_identifiers("def привет(:\n    return 1\n") == set()

    @pytest.mark.parametrize("exc_type", [
        __import__("tokenize").TokenError,
        IndentationError,
        SyntaxError,
        ValueError,
    ])
    def test_exception_branch_always_returns_empty_set(self, exc_type, monkeypatch):
        """The actual contract, tested without depending on WHEN a real
        tokenizer decides to raise.

        BUGFIX: this test used to hand-pick "broken" source strings (a
        syntax error, an indentation error, an unterminated string, at one
        point a leading null byte) and assert tokenize raises on all of
        them. That is not a stable property to test BY EXAMPLE: CPython's
        tokenizer has repeatedly changed exactly when and whether it raises
        for a given malformed input across 3.10/3.11/3.12/3.13 (unterminated
        strings, null bytes, and others have all changed behavior across
        these versions — see cpython#117212, cpython#105390, cpython#84357).
        Worse, _find_non_ascii_identifiers accumulates `bad` incrementally
        INSIDE the loop and only discards it in the except clause — so for
        an input where the real tokenizer raises AFTER already yielding the
        NAME token, the same literal source passes on one interpreter and
        fails on another. That is exactly what broke: "unterminated string"
        passed on 3.12 (raises before consuous NAME) and failed on 3.10
        (tokenizes through to привет first).

        The property this module actually needs to guarantee — the language
        gate's fail_mode really is "open" — is about the EXCEPT BRANCH's
        return value, not about which real-world strings happen to trigger
        it on any one interpreter. Mocking tokenize.generate_tokens to raise
        each caught exception type directly tests that branch, independent
        of tokenizer version quirks, and independent of whether a NAME token
        was consumed before the raise.
        """
        import tokenize
        from unittest.mock import patch
        from tools.auto.coder import _find_non_ascii_identifiers

        def boom(readline):
            yield tokenize.TokenInfo(tokenize.NAME, "привет", (1, 0), (1, 6), "")
            raise exc_type("synthetic failure")

        with patch("tokenize.generate_tokens", side_effect=lambda rl: boom(rl)):
            result = _find_non_ascii_identifiers("irrelevant — generate_tokens is mocked")
        assert result == set(), (
            f"{exc_type.__name__} raised after a NAME token was already "
            f"yielded must still discard everything accumulated so far"
        )


class TestSoftVerdictGatesAreFailOpen:
    """The four gates sharing _parse_verdict_soft all claim open."""

    @pytest.mark.parametrize("gate", ["verdict", "continuity", "theme", "fact"])
    def test_seed_records_open(self, gate):
        assert _GATE_SEED[gate]["fail_mode"] == "open"

    def test_parser_flags_unparseable_rather_than_rejecting(self):
        from tools.auto.inner_loop import _parse_verdict_soft
        approved, _reason, unparseable = _parse_verdict_soft("total gibberish")
        assert unparseable is True
        assert approved is True, "fail-open means an unreadable verdict passes"


class TestGate1IsFailClosed:
    def test_seed_records_closed(self):
        assert _GATE_SEED["gate1"]["fail_mode"] == "closed"

    def test_unparseable_presence_reply_is_not_confirmed(self):
        from tools.auto.gate1_filter import Gate1Filter
        confirmed, _reason, unparseable = Gate1Filter._parse_presence_response(
            Gate1Filter.__new__(Gate1Filter), "total gibberish", "some title"
        )
        assert unparseable is True
        assert confirmed is False, "fail-closed means an unreadable reply rejects"
