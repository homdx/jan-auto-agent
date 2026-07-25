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

    @pytest.mark.parametrize("content,label", [
        ("def привет(:\n    return 1\n", "syntax error"),
        ("def привет():\n\treturn 1\n  return 2\n", "indentation error"),
        ("\x00def привет(): pass\n", "null byte"),
    ])
    def test_unanalysable_content_is_accepted(self, content, label):
        """Empty set means the caller passes the file — fail-open."""
        from tools.auto.coder import _find_non_ascii_identifiers
        assert _find_non_ascii_identifiers(content) == set(), label


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
