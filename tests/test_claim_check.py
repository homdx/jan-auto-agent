"""claim_extract / claim_vote: sectioning, the no-repeat-credit cache, prompt variants, vote tally (offline)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402
import lenz_claim_filter as lf  # noqa: E402

REPORT = ("# Title\n\nintro line that is long enough to matter " + "x" * 120 + "\n\n"
          "## One\n\n" + "a " * 80 + "\n\n### Two\n\n" + "b " * 80 + "\n\n### tiny\n\nshort\n")


def test_sections_split_at_headings_and_drop_the_tiny_ones():
    secs = lf.split_sections(REPORT)
    assert [s.splitlines()[0] for s in secs] == ["# Title", "## One", "### Two"]


def test_extract_cache_hit_sends_nothing(tmp_path, monkeypatch):
    cache = lf.Cache(tmp_path / "c.json")
    calls = []
    monkeypatch.setattr(lf, "call", lambda *a, **k: calls.append(a) or {"claim": "A fact.", "identified_claims": ["B fact."]})
    assert lf.extract_claims("sec", "k", cache) == ["A fact.", "B fact."]
    assert len(calls) == 1
    monkeypatch.setattr(lf, "call", lambda *a, **k: pytest.fail("a cached section went to the network"))
    assert lf.extract_claims("sec", "k", lf.Cache(tmp_path / "c.json")) == ["A fact.", "B fact."]


def test_assess_never_pays_twice_and_never_caches_errors(tmp_path, monkeypatch):
    cache = lf.Cache(tmp_path / "c.json")
    rows = {"claims": [{"claim": "ok", "verdict": "True"}, {"claim": "bad", "verdict": "Error"}]}
    monkeypatch.setattr(lf, "call", lambda *a, **k: rows)
    first = lf.assess(["ok", "bad"], "k", cache)
    assert [r["cached"] for r in first] == [False, False]
    sent = []
    monkeypatch.setattr(lf, "call", lambda p, path, key, body=None, **k: sent.append(body) or rows)
    lf.assess(["ok", "bad"], "k", lf.Cache(tmp_path / "c.json"))
    assert sent == [{"claims": ["bad"]}]          # the paid one stays cached, the error is retried


def test_prompt_variants_differ_at_the_first_sentence_and_in_order():
    claims = [f"claim number {i}" for i in range(8)]
    texts = [cv.build_prompt(claims, run, 1)[0] for run in range(3)]
    assert len({t.split(".")[0] for t in texts}) == 3
    assert len({t.split("\n\n", 1)[1] for t in texts}) > 1


def test_parse_votes_maps_shuffled_ids_back_and_survives_garbage():
    order = [2, 0, 1]
    assert cv.parse_votes('x [{"id":1,"verdict":"true"},{"id":3,"verdict":"UNSURE"}] y', order) == {2: "TRUE", 1: "UNSURE"}
    assert cv.parse_votes("no json", order) == {}
    assert cv.parse_votes('[{"id":9,"verdict":"TRUE"}]', order) == {}


def _res(model, votes):
    return {"model": model, "run": 0, "votes": votes}


def test_tally_unsure_abstains_quorum_and_code_claims():
    claims = [{"claim": "Pytest exits with code 4 on a usage error."},
              {"claim": "One brave vote."},
              {"claim": "`_pytest()` in `gates.py` passes the flag."}]
    results = [_res("a/m1", {"0": "TRUE", "1": "TRUE", "2": "TRUE"}),
               _res("b/m2", {"0": "TRUE", "1": "UNSURE", "2": "TRUE"}),
               _res("c/m3", {"0": "TRUE", "1": "UNSURE", "2": "TRUE"}),
               _res("d/m4", {"0": "FALSE", "1": "UNSURE", "2": "TRUE"})]
    out = cv.tally(claims, results, set())          # string keys, as read back from a file
    assert [t["verdict"] for t in out] == ["TRUE", "UNSURE", "CODE-CHECK"]
    assert out[2]["needs_code"] is True


def test_unanimous_needs_every_voter_to_commit_to_one_verdict():
    claims = [{"claim": "A plain world fact one."}, {"claim": "A plain world fact two."},
              {"claim": "A plain world fact three."}]
    results = [_res("a/m1", {0: "TRUE", 1: "TRUE", 2: "TRUE"}),
               _res("b/m2", {0: "TRUE", 1: "TRUE", 2: "TRUE"}),
               _res("c/m3", {0: "TRUE", 1: "UNSURE", 2: "FALSE"})]
    out = cv.tally(claims, results, set())
    assert [t["unanimous"] for t in out] == [True, False, False]   # an abstainer or a dissenter spoils it


def test_tally_tie_is_split():
    results = [_res(f"p/m{i}", {0: v}) for i, v in enumerate(["TRUE", "TRUE", "FALSE", "FALSE"])]
    assert cv.tally([{"claim": "A plain world fact here."}], results, set())[0]["verdict"] == "SPLIT"
