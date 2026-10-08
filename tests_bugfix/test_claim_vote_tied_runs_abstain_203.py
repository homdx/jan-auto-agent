"""Bug 18 (round 203): a model that contradicts itself counted as a committed voter.

claim_vote.tally settled each model's own verdict with
``Counter(v).most_common(1)[0][0]``, and ``most_common`` breaks a tie by insertion
order -- so the model's *first* run decided.

    model a: TRUE then FALSE ; model b and c: TRUE twice each
    -> verdict TRUE, unanimous True

With ``--runs 2`` (or any even number, or three different answers) one model's own
disagreement was invisible and the claim was accepted. The rule the check exists for
is "a verdict is accepted only when all voters commit to it"; a model that cannot
settle itself has not committed to anything.

Fix: a model's verdict is its *strict* plurality, and a tie is UNSURE -- an
abstention, which the runbook already defines as "not a vote". Acceptance is
stricter on purpose, and the answer no longer depends on the order of the runs.

Ticket 203; probed on arena @ 00355fd.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402


CLAIMS = [{"claim": "A plain world fact that needs no code to judge."}]


def _res(model, verdict, run=0):
    return {"model": model, "run": run, "votes": {0: verdict}}


def test_tied_runs_are_an_abstention_not_a_commitment():
    # a changed its mind once; b and c never did.
    results = [_res("a/m1", "TRUE", 0), _res("a/m1", "FALSE", 1),
               _res("b/m2", "TRUE", 0), _res("b/m2", "TRUE", 1),
               _res("c/m3", "TRUE", 0), _res("c/m3", "TRUE", 1)]
    out = cv.tally(CLAIMS, results, set())[0]
    assert out["by_model"] == {"a/m1": "UNSURE", "b/m2": "TRUE", "c/m3": "TRUE"}
    assert out["unanimous"] is False
    # only two models committed, below the quorum of three
    assert out["verdict"] == "UNSURE"


def test_a_tie_does_not_depend_on_the_order_of_the_runs():
    results = [_res("a/m1", "TRUE", 0), _res("a/m1", "FALSE", 1),
               _res("b/m2", "TRUE", 0), _res("c/m3", "TRUE", 0)]
    shuffled = [_res("a/m1", "FALSE", 1), _res("b/m2", "TRUE", 0),
                _res("c/m3", "TRUE", 0), _res("a/m1", "TRUE", 0)]
    a = cv.tally(CLAIMS, results, set())[0]
    b = cv.tally(CLAIMS, shuffled, set())[0]
    assert a["verdict"] == b["verdict"] == "UNSURE"
    assert a["unanimous"] is False and b["unanimous"] is False
    assert a["by_model"]["a/m1"] == b["by_model"]["a/m1"] == "UNSURE"


def test_three_way_tie_is_an_abstention():
    results = [_res("a/m1", "TRUE", 0), _res("a/m1", "FALSE", 1),
               _res("a/m1", "UNSURE", 2), _res("b/m2", "TRUE", 0),
               _res("c/m3", "TRUE", 0)]
    assert cv.tally(CLAIMS, results, set())[0]["by_model"]["a/m1"] == "UNSURE"


def test_a_two_of_three_majority_of_one_model_still_stands():
    results = [_res("a/m1", "TRUE", 0), _res("a/m1", "TRUE", 1),
               _res("a/m1", "FALSE", 2), _res("b/m2", "TRUE", 0),
               _res("c/m3", "TRUE", 0)]
    out = cv.tally(CLAIMS, results, set())[0]
    assert out["by_model"]["a/m1"] == "TRUE"
    assert out["verdict"] == "TRUE"


def test_three_consistent_models_are_still_unanimous():
    results = [_res("a/m1", "TRUE", 0), _res("b/m2", "TRUE", 0),
               _res("c/m3", "TRUE", 0)]
    out = cv.tally(CLAIMS, results, set())[0]
    assert out["verdict"] == "TRUE"
    assert out["unanimous"] is True


def test_a_cross_model_tie_still_splits():
    results = [_res(f"p/m{i}", v, 0) for i, v in enumerate(["TRUE", "TRUE", "FALSE", "FALSE"])]
    assert cv.tally(CLAIMS, results, set())[0]["verdict"] == "SPLIT"
