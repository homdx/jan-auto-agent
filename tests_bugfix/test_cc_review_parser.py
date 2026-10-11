"""Review of cc-epic-docs: the vote parsers survive any reply — deep nesting, unclosed brackets, a stray quote — in bounded time."""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claim_vote as cv  # noqa: E402
from tools.claimcheck import judge  # noqa: E402
from tools.claimcheck.model import Chunk, Pack  # noqa: E402

HOSTILE = {"deep 20,000": "[" * 20000 + "]" * 20000, "deep 5,000": "[" * 5000 + "]" * 5000,
           "unclosed lists": "[1, " * 30000 + "]", "unclosed objects": "{" * 30000}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_hostile_reply_is_no_votes_never_an_exception(name):
    started = time.monotonic()
    assert judge.parse_votes_v2(HOSTILE[name], [0]) == {}
    assert cv.parse_votes(HOSTILE[name], [0]) == {}          # the CLAIM-1 path too
    assert time.monotonic() - started < 30                    # was seconds per 5,000 levels, quadratic


def test_votes_before_a_hostile_tail_still_count():
    assert judge.parse_votes_v2('[{"id": 1, "verdict": "TRUE"}] ' + "[" * 20000, [0])[0].verdict == "TRUE"


@pytest.mark.parametrize("reply", [
    'He said "hi.\n[{"id": 1, "verdict": "TRUE",}]',
    'a 5" screen\n[{"id": 1, "verdict": "TRUE"},]',
    'a 5" screen\n{"id": 1, "verdict": "TRUE",}',
])
def test_a_stray_quote_in_the_prose_does_not_lose_the_vote(reply):
    assert judge.parse_votes_v2(reply, [0])[0].verdict == "TRUE"


def test_a_parse_error_is_that_voters_error_not_the_runs(monkeypatch):
    chunk = Chunk("src:a.py:1-1", "source", "a.py", 1, 1, " 1| x = 1", ())
    items = [("c", Pack("c", "s", (chunk,), False, 0, 0)), ("w", None)]

    def boom(_text, _order):
        raise RecursionError("deep")

    monkeypatch.setattr(judge, "parse_votes_v2", boom)
    got = judge.ask_with_packs(items, 0, 1, lambda _p: "[[[", batch=1, code_batch=1)
    assert got["votes"] == {} and got["errors"] and "unreadable reply" in got["errors"][0]
