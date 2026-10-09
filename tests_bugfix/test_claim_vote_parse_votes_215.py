"""tests_bugfix/test_claim_vote_parse_votes_215.py — bug 215: a bracket outside the JSON list loses every vote of the reply.

Bug: `parse_votes` took `re.search(r"\\[.*\\]", text, re.S)` — from the FIRST `[` to the
LAST `]` of the whole reply. A reply with a reference after the list
(`[...] Note: see [1]`) or a bracket before it (`Claims [1-3]: [...]`) made one
span that is no JSON, `json.loads` failed, and the reply counted as garbage: every
verdict in it lost without a word (the run showed `votes=0/N`, which the runbook
reads as "a model to drop from the roster"). The list is now the first `[` from
which a JSON array decodes.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402

ORDER = [2, 0, 1]


@pytest.mark.parametrize("text", [
    '[{"id":1,"verdict":"TRUE"}]\n\nNote: see [1] for details',
    'Claims [1-3]: [{"id":1,"verdict":"TRUE"}]',
    'Here you go [sic]: [{"id":1,"verdict":"TRUE"}] (items [a] and [b])',
    '```json\n[{"id":1,"verdict":"TRUE"}]\n```\nDone [1].',
])
def test_a_list_with_brackets_around_it_is_read(text):
    assert cv.parse_votes(text, ORDER) == {2: "TRUE"}


def test_what_worked_still_works():
    assert cv.parse_votes('x [{"id":1,"verdict":"true"},{"id":3,"verdict":"UNSURE"}] y', ORDER) == {2: "TRUE", 1: "UNSURE"}
    assert cv.parse_votes("no json", ORDER) == {}
    assert cv.parse_votes("", ORDER) == {} and cv.parse_votes(None, ORDER) == {}
    assert cv.parse_votes('[{"id":9,"verdict":"TRUE"}]', ORDER) == {}
    assert cv.parse_votes("[{broken", ORDER) == {}
    assert cv.parse_votes('{"id":1,"verdict":"TRUE"}', ORDER) == {}
