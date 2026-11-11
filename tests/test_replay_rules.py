"""tests/test_replay_rules.py — CC-11's offline replay (T143).

`replay` is the oracle the claim-vote tally is checked against, so its two
acceptance rules are pinned directly: three voters that agree settle one
claim, a wrong accepted verdict is counted wrong, and a falsy truth (0) is
no claim at all. The truths are signed ints — -1, not `False` — because a
bool is 0 to `if not row["truth"]` and would hide the truth row it names.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "contest-bench" / "cc"))
import replay_rules  # noqa: E402

VOTERS = ("v1", "v2", "v3")


def _data(truth: int, vote: int) -> dict:
    """One claim with *truth*, judged *vote* by each of the three voters."""
    return {"claims": [{"truth": truth}], "voters": {v: [vote] for v in VOTERS}}


@pytest.mark.parametrize("rule", ["all", "committed"])
@pytest.mark.parametrize("truth,vote,want", [
    (-1, 1, (1, 1)),    # accepted +1 against truth -1: settled and wrong
    (-1, -1, (1, 0)),   # accepted -1 against truth -1: settled, not wrong
    (1, -1, (1, 1)),    # accepted -1 against truth +1: settled and wrong
    (0, 1, (0, 0)),     # no truth: replay counts nothing
])
def test_replay_under_both_rules_with_three_voters(rule, truth, vote, want):
    assert replay_rules.replay(list(VOTERS), rule, data=_data(truth, vote)) == want


def test_signed_truths_are_not_booleans():
    """-1 is a real FALSE verdict; `False` would be 0 and be skipped as no truth."""
    assert replay_rules.accept([-1, -1, -1], "all") == -1
    assert replay_rules.accept([1, 1, 1], "all") == 1
    assert replay_rules.replay(list(VOTERS), "all", data=_data(-1, -1)) == (1, 0)

@pytest.mark.parametrize("rule", ["all", "committed"])
@pytest.mark.parametrize("votes", [
    [True, True, True], [False, False, False], [True, 1, 1], [1.0, 1.0, 1.0],
    ["1", "1", "1"], [None, None, None], [2, 2, 2], [-2, 1, 1],
])
def test_only_real_ints_from_the_set_are_votes(rule, votes):
    """`True == 1` in Python, so a bool (or a float) must be refused by type, not by value."""
    with pytest.raises(ValueError):
        replay_rules.accept(votes, rule)


def test_real_int_votes_are_still_accepted():
    assert replay_rules.accept([1, 1, 1], "all") == 1
    assert replay_rules.accept([-1, -1, -1, 0], "committed") == -1
    assert replay_rules.accept([0, 0, 0], "all") == 0
