"""Bug 19 (round 203): a voter that returned nothing for a claim made unanimity.

tally compared ``committed`` against ``len(model_major)`` — the models that
VOTED on this claim — so a dead model, a lost batch, or a batch whose reply
parse_votes could not read was invisible in the denominator.  Voters ``a``,
``b``, ``c`` all TRUE and ``d`` silent gave ``unanimous True`` with three
models listed, and the claim was accepted.  The rule is "every voter", and the
voters are the ones asked, not the ones that answered.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402

CLAIMS = [{"claim": "A plain world fact one."}]


CLAIMS2 = [{"claim": "A plain world fact one."}, {"claim": "A plain world fact two."}]


def test_a_voter_silent_on_one_claim_but_alive_elsewhere_spoils_unanimity():
    """The bug itself: a lost batch or an unreadable reply for *this* claim, from a model that
    answered others, is a voter that did not commit here -- no star."""
    results = [{"model": m, "run": 0, "votes": {0: "TRUE", 1: "TRUE"}} for m in ("a", "b", "c")]
    results.append({"model": "d", "run": 0, "votes": {0: "TRUE"}})   # d lost the batch with claim 1
    out = cv.tally(CLAIMS2, results, set())
    assert out[1]["verdict"] == "TRUE"       # the three that voted still decide
    assert out[1]["unanimous"] is False      # ... but d was asked and is alive, so no star
    assert out[0]["unanimous"] is True


def test_a_model_with_no_vote_at_all_is_not_a_voter():
    """CC-6 (round 280, live): a model out of its free plan answers nothing for any claim; counting it
    made every claim non-unanimous (0 of 80 decided).  It is dead, not a dissenter; the quorum of
    three committed voters still holds, and the reply says who was dead."""
    results = [{"model": m, "run": 0, "votes": {0: "TRUE"}} for m in ("a", "b", "c")]
    results.append({"model": "d", "run": 0, "votes": {}})   # d answered nothing at all
    t = cv.tally(CLAIMS, results, set())[0]
    assert t["verdict"] == "TRUE" and t["unanimous"] is True
    assert set(t["by_model"]) == {"a", "b", "c"}


def test_a_dead_voter_with_an_error_row_is_not_a_voter_either():
    results = [{"model": m, "run": 0, "votes": {0: "FALSE"}} for m in ("a", "b", "c")]
    results.append({"model": "d", "run": 0, "votes": {}, "error": "boom"})
    out = cv.tally(CLAIMS, results, set())
    assert out[0]["verdict"] == "FALSE"
    assert out[0]["unanimous"] is True


def test_two_live_voters_and_a_dead_one_are_still_below_quorum():
    results = [{"model": m, "run": 0, "votes": {0: "TRUE"}} for m in ("a", "b")]
    results.append({"model": "d", "run": 0, "votes": {}})
    t = cv.tally(CLAIMS, results, set())[0]
    assert t["verdict"] == "UNSURE" and t["unanimous"] is False


def test_every_asked_voter_committing_is_still_unanimous():
    results = [{"model": m, "run": 0, "votes": {0: "TRUE"}} for m in ("a", "b", "c")]
    out = cv.tally(CLAIMS, results, set())
    assert out[0]["verdict"] == "TRUE"
    assert out[0]["unanimous"] is True


def test_quorum_still_holds_with_fewer_than_three_voters():
    results = [{"model": m, "run": 0, "votes": {0: "TRUE"}} for m in ("a", "b")]
    out = cv.tally(CLAIMS, results, set())
    assert out[0]["verdict"] == "UNSURE"
    assert out[0]["unanimous"] is False
