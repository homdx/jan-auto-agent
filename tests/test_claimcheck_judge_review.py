"""tests/test_claimcheck_judge_review.py — the vote parser after the review of 2026-10-12: an answer after
a long code-heavy reasoning (hundreds of `[` and `{`) is read, and a long unterminated nesting is
bounded work, not seconds per reply.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from tools.claimcheck import judge

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import claim_vote as cv  # noqa: E402

ROW = {"id": 1, "verdict": "TRUE", "chunk": "[[src:a.py:1-2]]", "quote": "def f():"}
REASONING = "x = a[i] + b[j] {k: v} "        # one line of code-heavy reasoning: a `[`, a `{`


@pytest.mark.parametrize("before", [REASONING * 700 + "\n", "a[1] " * 3000, "[" * 5000],
                         ids=["reasoning-700", "brackets-3000", "deep-5000"])
def test_an_answer_after_hundreds_of_brackets_is_read(before):
    text = before + json.dumps([ROW])
    assert judge.parse_votes_v2(text, [0])[0].verdict == "TRUE"
    assert cv.parse_votes(text, [0]) == {0: "TRUE"}


def test_rows_as_objects_after_thousands_of_objects_are_read():
    text = '{"a": 1} ' * 3000 + json.dumps(ROW)
    assert judge.parse_votes_v2(text, [0])[0].verdict == "TRUE"


def test_the_first_list_with_rows_still_wins():
    """Head and tail are tried in text order: an early answer is not overridden by a late one."""
    early = json.dumps([{**ROW, "verdict": "FALSE"}])
    text = early + REASONING * 900 + json.dumps([ROW])
    assert judge.parse_votes_v2(text, [0])[0].verdict == "FALSE"
    assert cv.parse_votes(text, [0]) == {0: "FALSE"}


@pytest.mark.parametrize("text", [
    ("[" + "1," * 1000) * 800,                      # 1.6 MB of nested unterminated lists: 19 s before
    "[" * 20000,
    '[{"id": 1, "quote": "' + "a[" * 100000,
    '{"id": 1, "verdict": "TRUE", "quote": "' + "x" * 200000,
], ids=["nested-1.6MB", "deep-20000", "string-of-brackets", "unterminated-object"])
def test_a_hostile_reply_is_bounded_work(text):
    started = time.perf_counter()
    assert judge.parse_votes_v2(text, [0]) == {}
    assert cv.parse_votes(text, [0]) == {}
    assert time.perf_counter() - started < 5.0


def test_starts_are_all_of_them_or_the_head_and_the_tail():
    assert judge._starts("[" * 10, "[") == list(range(10))
    n = judge.MAX_STARTS
    got = judge._starts("[" * (3 * n), "[")
    assert got == list(range(n)) + list(range(2 * n, 3 * n))
