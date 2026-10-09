"""tests_bugfix/test_claim_vote_acceptance_213.py — bugs 213 and 214: `claim_vote.tally` accepts a claim some voters did not commit to.

The runbook's acceptance rule (docs/claim-check/RUNBOOK.md): "accept a verdict only
when ALL of the models commit to it"; the code says "every voter committed, all to
one verdict". `unanimous` (the `*` that means *accept*) broke it twice:

213 — a model's own verdict is the majority of its runs, and `Counter.most_common`
      settles a tie by whichever verdict it saw first. A model that said TRUE in
      one run and FALSE in the other has no majority, yet counted as a TRUE voter:
      with two such runs per model (`--runs 2`) a claim one voter contradicted
      itself on came out `unanimous`.
214 — `len(model_major)` counted only the models that voted on the claim. A voter
      that returned nothing for it (a dead model, a lost batch) was not in the
      count at all, so with four voters and one dead the three that answered were
      "every voter". The runbook calls such a model "a model to drop from the
      roster": it must not be able to pass for one that agreed.
A model without a majority is an abstainer, as an UNSURE one is.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402

CLAIMS = [{"claim": "A plain world fact here."}]


def _run(model, run, vote):
    return {"model": model, "run": run, "votes": {} if vote is None else {0: vote}}


def _tally(results):
    return cv.tally(CLAIMS, results, set())[0]


def test_a_model_that_contradicts_itself_has_no_verdict_of_its_own():
    out = _tally([_run("a/m1", 0, "TRUE"), _run("a/m1", 1, "FALSE"),
                  _run("b/m2", 0, "TRUE"), _run("b/m2", 1, "TRUE"),
                  _run("c/m3", 0, "TRUE"), _run("c/m3", 1, "TRUE")])
    assert out["by_model"]["a/m1"] == "UNSURE"
    assert out["unanimous"] is False, "a voter that said both TRUE and FALSE did not commit"
    assert out["verdict"] == "UNSURE", "two committed voters are under the quorum of three"


def test_the_order_of_the_runs_does_not_decide_a_tie():
    first_true = _tally([_run("a/m1", 0, "TRUE"), _run("a/m1", 1, "FALSE")])
    first_false = _tally([_run("a/m1", 0, "FALSE"), _run("a/m1", 1, "TRUE")])
    assert first_true["by_model"] == first_false["by_model"] == {"a/m1": "UNSURE"}


def test_a_real_majority_of_a_models_runs_still_stands():
    out = _tally([_run("a/m1", 0, "TRUE"), _run("a/m1", 1, "FALSE"), _run("a/m1", 2, "TRUE"),
                  _run("b/m2", 0, "TRUE"), _run("c/m3", 0, "TRUE")])
    assert out["by_model"]["a/m1"] == "TRUE"
    assert out["unanimous"] is True and out["verdict"] == "TRUE"


def test_an_unsure_run_among_decided_ones_does_not_hide_the_majority():
    out = _tally([_run("a/m1", 0, "TRUE"), _run("a/m1", 1, "TRUE"), _run("a/m1", 2, "UNSURE"),
                  _run("b/m2", 0, "TRUE"), _run("c/m3", 0, "TRUE")])
    assert out["by_model"]["a/m1"] == "TRUE" and out["unanimous"] is True


def test_a_voter_with_no_votes_on_the_claim_spoils_unanimity():
    out = _tally([_run("a/m1", 0, "TRUE"), _run("b/m2", 0, "TRUE"), _run("c/m3", 0, "TRUE"),
                  _run("d/m4", 0, None)])
    assert out["verdict"] == "TRUE", "three committed voters still reach the quorum"
    assert out["unanimous"] is False, "the fourth voter never committed"


def test_every_voter_present_and_agreeing_is_still_unanimous():
    out = _tally([_run(f"x/m{i}", 0, "FALSE") for i in range(4)])
    assert out["unanimous"] is True and out["verdict"] == "FALSE"


def test_a_voter_that_missed_only_this_claim_spoils_only_this_claim():
    claims = [{"claim": "A plain world fact one."}, {"claim": "A plain world fact two."}]
    results = [{"model": m, "run": 0, "votes": {0: "TRUE", 1: "TRUE"}} for m in ("a/m1", "b/m2", "c/m3")]
    results.append({"model": "d/m4", "run": 0, "votes": {0: "TRUE"}})       # lost the batch holding claim 1
    out = cv.tally(claims, results, set())
    assert [t["unanimous"] for t in out] == [True, False]
