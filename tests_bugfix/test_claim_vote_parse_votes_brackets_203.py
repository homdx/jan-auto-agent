"""Bug 17 (round 203): a bracket outside the JSON list lost every vote of a reply.

claim_vote.parse_votes cut the reply from its *first* ``[`` to its *last* ``]`` and
parsed that one slice as a JSON value. A model that wrapped its votes in prose, or
trailing prose, lost all of them:

    parse_votes("[{\"id\":1,\"verdict\":\"TRUE\"},{\"id\":2,\"verdict\":\"FALSE\"}]", [0, 1])
        -> {0: 'TRUE', 1: 'FALSE'}
    parse_votes("Judging [3 claims]:\n" + the same list, [0, 1])
        -> {}
    parse_votes(the same list + "\n[Note: see above]", [0, 1])
        -> {}
    parse_votes("See [1].\n" + the same list, [0, 1])
        -> {}

The greedy ``\\[.*\\]`` match swallows the stray bracket into the slice, the slice is
no longer a JSON value, and the whole reply reads as nothing. A model that answered
every claim counted as a model that gave nothing: ``votes=0/N``, which the runbook
reads as "a model to drop from the roster", and a claim loses voters below the
quorum of three.

Fix: try every ``[`` as the start of a JSON array with
``json.JSONDecoder().raw_decode`` and take the first list that holds vote rows
(``id`` and ``verdict``) -- a stray ``[1]`` or an empty list is skipped.

Ticket 203; probed on arena @ 00355fd.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402


ROWS = '[{"id":1,"verdict":"TRUE"},{"id":2,"verdict":"FALSE"}]'
ORDER = [0, 1]
VOTES = {0: "TRUE", 1: "FALSE"}


def test_bare_list_still_parses():
    assert cv.parse_votes(ROWS, ORDER) == VOTES


def test_bracket_inside_prose_before_the_list():
    # "[3 claims]" is not a JSON value; the vote list after it is the answer.
    assert cv.parse_votes("Judging [3 claims]:\n" + ROWS, ORDER) == VOTES


def test_bracket_inside_prose_after_the_list():
    assert cv.parse_votes(ROWS + "\n[Note: see above]", ORDER) == VOTES


def test_stray_numeric_bracket_before_the_list():
    # "[1]" IS a JSON value, but it holds no vote row, so it is skipped.
    assert cv.parse_votes("See [1].\n" + ROWS, ORDER) == VOTES


def test_two_lists_the_first_one_with_vote_rows_wins():
    assert cv.parse_votes(ROWS + '\n[{"id":1,"verdict":"UNSURE"}]', ORDER) == VOTES


def test_brackets_without_vote_rows_still_give_nothing():
    assert cv.parse_votes('[1, 2, 3]', ORDER) == {}
    assert cv.parse_votes('[{"n": 1, "v": "TRUE"}]', ORDER) == {}
    assert cv.parse_votes("no json here, just prose", ORDER) == {}
    assert cv.parse_votes("Judging [3 claims]: nothing to see", ORDER) == {}


def test_out_of_range_and_unknown_verdicts_are_dropped():
    assert cv.parse_votes('{"id":1,"verdict":"TRUE"}' + " " + ROWS, ORDER) == VOTES
    assert cv.parse_votes('May be: [{"id":9,"verdict":"TRUE"},{"id":2,"verdict":"MAYBE"}]', ORDER) == {}
