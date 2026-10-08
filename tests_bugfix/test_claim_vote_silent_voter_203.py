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


def test_a_silent_voter_spoils_unanimity_but_not_the_verdict():
    results = [{"model": m, "run": 0, "votes": {0: "TRUE"}} for m in ("a", "b", "c")]
    results.append({"model": "d", "run": 0, "votes": {}})   # d answered nothing
    out = cv.tally(CLAIMS, results, set())
    t = out[0]
    assert t["verdict"] == "TRUE"            # the three that voted still decide
    assert t["unanimous"] is False           # ... but d was asked, so no star
    assert set(t["by_model"]) == {"a", "b", "c"}


def test_a_silent_voter_with_an_error_row_spoils_unanimity():
    results = [{"model": m, "run": 0, "votes": {0: "FALSE"}} for m in ("a", "b", "c")]
    results.append({"model": "d", "run": 0, "votes": {}, "error": "boom"})
    out = cv.tally(CLAIMS, results, set())
    assert out[0]["verdict"] == "FALSE"
    assert out[0]["unanimous"] is False


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
