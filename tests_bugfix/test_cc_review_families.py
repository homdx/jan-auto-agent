"""Review of cc-epic-docs: a truth row needs three families behind it, and its evidence is the final verdict's."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claim_vote as cv  # noqa: E402
from tools.claimcheck import judge, lenz_hint  # noqa: E402
from tools.claimcheck import truth as cc_truth  # noqa: E402
from tools.claimcheck.model import Chunk, Pack  # noqa: E402

AGNES = ["kenary/agnes-3-0-flash:free", "kenary/agnes-2-flash:free", "other/agnes-4:free"]
THREE = ["kenary/agnes-3-0-flash:free", "kenary/mimo-v2-6-flash:free", "kenary/nemotron-3-ultra:free"]


def _votes(voters, verdict="TRUE", kind="code"):
    results = [{"model": m, "run": 0, "votes": {"0": verdict}} for m in voters]
    claims = cv.tally([{"claim": "`f` in a.py returns 1"}], results,
                      meta=[{"kind": kind, "dangling": False, "sha": "abc1234"}])
    return {"results": results, "claims": claims}


def test_one_family_unanimous_writes_no_row_and_goes_to_pending():
    votes = _votes(AGNES)
    assert votes["claims"][0]["unanimous"] is True          # tally still counts models
    assert cc_truth.to_truth_rows(votes, checked_by="c") == []
    pend = cc_truth.pending_rows(votes)
    assert [i for i, _c in pend] == [0]
    md = cc_truth.pending_markdown(votes)
    assert "fewer than 3 model families" in md and md.count("`f` in a.py returns 1") == 1


def test_three_families_write_the_row():
    rows = cc_truth.to_truth_rows(_votes(THREE), checked_by="c")
    assert [r["truth"] for r in rows] == ["REAL"]


def test_profile_names_count_by_the_model_they_name():
    profiles = ["vote_a", "vote_b", "vote_c"]
    votes = _votes(profiles)
    same = dict(zip(profiles, AGNES))
    differ = dict(zip(profiles, THREE))
    assert cc_truth.to_truth_rows(votes, checked_by="c", models=same) == []
    assert len(cc_truth.to_truth_rows(votes, checked_by="c", models=differ)) == 1


def test_a_one_family_world_claim_is_worth_a_lenz_credit():
    votes = _votes(AGNES, kind="world")
    assert lenz_hint.lenz_group(votes["claims"][0]) == "DISSENT"
    assert "too few families" in lenz_hint._ours(votes["claims"][0])
    assert lenz_hint.lenz_group(_votes(THREE, kind="world")["claims"][0]) is None


def test_qwen_versions_are_one_family():
    votes = _votes(["qwen/qwen3-235b:free", "qwen/qwen-2.5-72b:free", "kenary/mimo-v2:free"])
    assert cc_truth.to_truth_rows(votes, checked_by="c") == []


def test_the_rows_evidence_is_the_final_verdicts():
    """A model voting FALSE, TRUE, TRUE: the REAL row cites only the TRUE runs."""
    acc = lambda v, q: {0: {"chunk": "src:a.py:1-2", "quote": q, "verdict": v}}   # noqa: E731
    res = [{"model": THREE[0], "run": 0, "votes": {0: "FALSE"}, "accepted": acc("FALSE", "the FALSE run's quote")},
           {"model": THREE[0], "run": 1, "votes": {0: "TRUE"}, "accepted": acc("TRUE", "a TRUE quote")},
           {"model": THREE[0], "run": 2, "votes": {0: "TRUE"}, "accepted": acc("TRUE", "a TRUE quote")},
           {"model": THREE[1], "run": 0, "votes": {0: "TRUE"}, "accepted": acc("TRUE", "another TRUE quote")},
           {"model": THREE[2], "run": 0, "votes": {0: "TRUE"}, "accepted": acc("TRUE", "a TRUE quote")}]
    row = cv.tally([{"claim": "c"}], res, meta=[{"kind": "code", "dangling": False, "sha": "s"}])[0]
    assert row["verdict"] == "TRUE" and row["unanimous"]
    assert row["quotes"] == ["a TRUE quote", "another TRUE quote"]
    assert "FALSE run" not in cc_truth.how_of(row)
    # an old votes.json (accepted without a verdict) keeps every entry, as before
    old = [{**r, "accepted": {0: {k: v for k, v in r["accepted"][0].items() if k != "verdict"}}} for r in res]
    row = cv.tally([{"claim": "c"}], old, meta=[{"kind": "code", "dangling": False, "sha": "s"}])[0]
    assert row["quotes"][0] == "the FALSE run's quote"


def test_accepted_entries_carry_the_verdict():
    chunk = Chunk("src:a.py:1-2", "source", "a.py", 1, 2, " 1| def f():\n 2|     return 1", ())
    pack = Pack("c", "abc1234", (chunk,), False, 0, 0)
    reply = '[{"id": 1, "verdict": "TRUE", "chunk": "src:a.py:1-2", "quote": "def f(): return 1"}]'
    got = judge.ask_with_packs([("c", pack)], 0, 1, lambda _p: reply)
    assert got["accepted"][0]["verdict"] == "TRUE"
