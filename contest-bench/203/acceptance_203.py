"""Round 203 black box: scripts/claim_vote.py, written from the ticket alone.

One test per behaviour the ticket names (bugs 17-22). Each fails on the base code.
"""
from __future__ import annotations

import configparser
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claim_vote as cv  # noqa: E402

LIST = '[{"id":1,"verdict":"TRUE"},{"id":2,"verdict":"FALSE"}]'
WANT = {0: "TRUE", 1: "FALSE"}


# ---- 17: a bracket outside the JSON list ------------------------------------
def test_17_plain_list():
    assert cv.parse_votes(LIST, [0, 1]) == WANT


@pytest.mark.parametrize("text", [
    "Judging [3 claims]:\n" + LIST,
    LIST + "\n[Note: see above]",
    "See [1].\n" + LIST,
    "Judging [3 claims]: " + LIST + " [end]",
])
def test_17_bracket_outside_the_list(text):
    assert cv.parse_votes(text, [0, 1]) == WANT


def test_17_two_lists_first_with_vote_rows_wins():
    other = '[{"id":1,"verdict":"FALSE"},{"id":2,"verdict":"TRUE"}]'
    assert cv.parse_votes("[1, 2] " + LIST + " then " + other, [0, 1]) == WANT


def test_17_no_vote_rows_is_empty():
    assert cv.parse_votes("Judging [3 claims] and [x]. nothing else", [0, 1]) == {}
    assert cv.parse_votes("", [0, 1]) == {}
    assert cv.parse_votes('[{"a": 1}]', [0, 1]) == {}


def test_17_order_mapping_kept():
    assert cv.parse_votes("See [1]. " + LIST, [1, 0]) == {1: "TRUE", 0: "FALSE"}


# ---- tally helpers ----------------------------------------------------------
CLAIM = [{"claim": "Water boils at 100 degrees Celsius at sea level."}]


def res(model, run, verdict):
    votes = {} if verdict is None else {0: verdict}
    return {"model": model, "run": run, "votes": votes}


def tally(results):
    return cv.tally(CLAIM, results, set())[0]


# ---- 18: a model that contradicts itself ------------------------------------
def test_18_self_contradiction_is_not_a_committed_voter():
    r = [res("a", 0, "TRUE"), res("a", 1, "FALSE"),
         res("b", 0, "TRUE"), res("b", 1, "TRUE"),
         res("c", 0, "TRUE"), res("c", 1, "TRUE")]
    t = tally(r)
    assert t["unanimous"] is False
    assert t["by_model"]["a"] == "UNSURE"


def test_18_order_of_runs_does_not_matter():
    r = [res("a", 0, "FALSE"), res("a", 1, "TRUE"),
         res("b", 0, "TRUE"), res("b", 1, "TRUE"),
         res("c", 0, "TRUE"), res("c", 1, "TRUE")]
    t = tally(r)
    assert t["unanimous"] is False and t["by_model"]["a"] == "UNSURE"
    t2 = tally(list(reversed(r)))
    assert t2["by_model"] == t["by_model"] and t2["unanimous"] is False


def test_18_three_different_answers_is_a_tie():
    r = [res("a", 0, "TRUE"), res("a", 1, "FALSE"), res("a", 2, "UNSURE")] + \
        [res(m, i, "TRUE") for m in "bc" for i in range(3)]
    t = tally(r)
    assert t["by_model"]["a"] == "UNSURE" and t["unanimous"] is False


def test_18_two_of_three_majority_still_stands():
    r = [res("a", 0, "TRUE"), res("a", 1, "FALSE"), res("a", 2, "TRUE")] + \
        [res(m, i, "TRUE") for m in "bc" for i in range(3)]
    t = tally(r)
    assert t["by_model"]["a"] == "TRUE" and t["unanimous"] is True and t["verdict"] == "TRUE"


def test_18_consistent_models_are_unanimous():
    r = [res(m, i, "TRUE") for m in "abc" for i in range(2)]
    t = tally(r)
    assert t["unanimous"] is True and t["verdict"] == "TRUE"


def test_18_single_run_models_unchanged():
    r = [res(m, 0, "FALSE") for m in "abc"]
    t = tally(r)
    assert t["unanimous"] is True and t["verdict"] == "FALSE"


# ---- 19: a voter that returned nothing --------------------------------------
def test_19_silent_voter_blocks_unanimity_but_not_the_verdict():
    r = [res("a", 0, "TRUE"), res("b", 0, "TRUE"), res("c", 0, "TRUE"), res("d", 0, None)]
    t = tally(r)
    assert t["unanimous"] is False
    assert t["verdict"] == "TRUE"
    assert set(t["by_model"]) == {"a", "b", "c"}


def test_19_silent_in_every_run_of_this_claim_but_voting_on_another():
    claims = [{"claim": "Water boils at 100 degrees Celsius at sea level."},
              {"claim": "The sky above a clear noon is blue."}]
    r = [{"model": m, "run": 0, "votes": {0: "TRUE", 1: "TRUE"}} for m in "abc"]
    r.append({"model": "d", "run": 0, "votes": {1: "TRUE"}})
    out = cv.tally(claims, r, set())
    assert out[0]["unanimous"] is False
    assert out[1]["unanimous"] is True


def test_19_all_voters_present_is_unanimous():
    r = [res(m, 0, "TRUE") for m in "abcd"]
    assert tally(r)["unanimous"] is True


def test_19_fewer_than_three_voters_never_accepts():
    r = [res("a", 0, "TRUE"), res("b", 0, "TRUE")]
    t = tally(r)
    assert t["unanimous"] is False


def test_19_voter_with_error_and_no_votes_counts():
    r = [res("a", 0, "TRUE"), res("b", 0, "TRUE"), res("c", 0, "TRUE"),
         {"model": "d", "run": 0, "votes": {}, "error": "boom"}]
    assert tally(r)["unanimous"] is False


# ---- 20: shared parser between two voters -----------------------------------
def make_parser(*names):
    p = configparser.ConfigParser(interpolation=None)
    p.add_section("claim_vote")
    for n in names:
        p.add_section(n)
        p.set(n, "base_url", f"http://{n}.invalid/v1")
        p.set(n, "api_key", f"key-{n}")
        p.set(n, "model", f"model-{n}")
    return p


def test_20_two_threads_each_get_their_own_settings(monkeypatch):
    parser = make_parser("prof_a", "prof_b")
    real = cv.resolve_llm_profile
    gate = threading.Barrier(2, timeout=10)

    def slow(*a, **k):
        # the window between "pick written" and "pick read", widened
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return real(*a, **k)

    monkeypatch.setattr(cv, "resolve_llm_profile", slow)
    got = {}

    def work(name):
        got[name] = cv.voter_settings(name, parser)

    ts = [threading.Thread(target=work, args=(n,)) for n in ("prof_a", "prof_b")]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert got["prof_a"].model == "model-prof_a"
    assert got["prof_b"].model == "model-prof_b"


def test_20_many_rounds_no_cross(monkeypatch):
    parser = make_parser("prof_a", "prof_b")
    bad = []

    def work(name):
        for _ in range(300):
            if cv.voter_settings(name, parser).model != f"model-{name}":
                bad.append(name)

    ts = [threading.Thread(target=work, args=(n,)) for n in ("prof_a", "prof_b")]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert not bad


# ---- 21: a voter that cannot be resolved ------------------------------------
def err(row):
    return row.get("errors") or row.get("error")


def test_21_misspelt_profile_is_a_row_with_errors():
    parser = make_parser("good")
    row = cv.ask("p_typo", ["a claim"], 0, 1, parser, 5)
    assert row["model"] == "p_typo" and row["votes"] == {} and err(row)


def test_21_missing_kilo_file_is_a_row_with_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(cv, "KILO_CONFIG", tmp_path / "nope.jsonc")
    monkeypatch.setattr(cv, "KILO_AUTH", tmp_path / "nope.json")
    row = cv.ask("someprov/some-model", ["a claim"], 0, 1, make_parser(), 5)
    assert row["votes"] == {} and err(row)


def test_21_missing_provider_key_in_kilo_files_is_a_row(monkeypatch, tmp_path):
    cfg, auth = tmp_path / "k.jsonc", tmp_path / "a.json"
    cfg.write_text('{"provider": {"other": {"options": {"baseURL": "http://x/v1"}}}}')
    auth.write_text("{}")
    monkeypatch.setattr(cv, "KILO_CONFIG", cfg)
    monkeypatch.setattr(cv, "KILO_AUTH", auth)
    row = cv.ask("someprov/some-model", ["a claim"], 0, 1, make_parser(), 5)
    assert row["votes"] == {} and err(row)


def test_21_main_the_others_still_vote(monkeypatch, tmp_path, capsys):
    ini = tmp_path / "contest.ini"
    ini.write_text("[claim_vote]\n" + "".join(
        f"\n[{n}]\nbase_url = http://{n}.invalid/v1\napi_key = k\nmodel = m-{n}\n"
        for n in ("g1", "g2")))
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps(["Water boils at 100 degrees Celsius at sea level."]))
    monkeypatch.setattr(cv, "request_completion",
                        lambda *a, **k: '[{"id": 1, "verdict": "TRUE"}]')
    out = tmp_path / "out.json"
    rc = cv.main([str(claims), "--profiles", "g1", "p_typo", "g2", "--runs", "1",
                  "--repo-root", str(tmp_path), "--interval", "0.001",
                  "--parallel", "3", "--out", str(out)])
    assert rc == 0
    rep = json.loads(out.read_text())
    by = {r["model"]: r for r in rep["results"]}
    assert set(by) == {"g1", "g2", "p_typo"}
    assert by["g1"]["votes"] and by["g2"]["votes"]
    assert by["p_typo"]["votes"] == {} and err(by["p_typo"])
    text = capsys.readouterr().out
    assert "p_typo" in text and "ERROR" in text


# ---- 22: add_profiles file mode ---------------------------------------------
@pytest.fixture
def kilo(monkeypatch, tmp_path):
    cfg, auth = tmp_path / "kilo.jsonc", tmp_path / "auth.json"
    cfg.write_text(json.dumps({"provider": {"prov": {"options": {"baseURL": "http://p.invalid/v1"}}}}))
    auth.write_text(json.dumps({"prov": {"key": "SECRET-KEY"}}))
    monkeypatch.setattr(cv, "KILO_CONFIG", cfg)
    monkeypatch.setattr(cv, "KILO_AUTH", auth)
    return tmp_path


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_22_new_file_is_0600_even_with_a_loose_umask(kilo):
    root = kilo / "root"
    root.mkdir()
    old = os.umask(0o022)
    try:
        names = cv.add_profiles(["prov/model-x"], root)
    finally:
        os.umask(old)
    f = root / cv.roster.LOCAL_FILENAME
    assert "SECRET-KEY" in f.read_text() and names
    assert mode(f) == 0o600


def test_22_new_file_is_0600_with_umask_0(kilo):
    root = kilo / "root2"
    root.mkdir()
    old = os.umask(0)
    try:
        cv.add_profiles(["prov/model-x"], root)
    finally:
        os.umask(old)
    assert mode(root / cv.roster.LOCAL_FILENAME) == 0o600


def test_22_existing_file_keeps_its_mode(kilo):
    root = kilo / "root3"
    root.mkdir()
    f = root / cv.roster.LOCAL_FILENAME
    f.write_text("[x]\na = 1\n")
    os.chmod(f, 0o640)
    cv.add_profiles(["prov/model-x"], root)
    assert "SECRET-KEY" in f.read_text() and "[x]" in f.read_text()
    assert mode(f) == 0o640


def test_22_existing_profile_is_kept_and_nothing_rewritten(kilo):
    root = kilo / "root4"
    root.mkdir()
    cv.add_profiles(["prov/model-x"], root)
    f = root / cv.roster.LOCAL_FILENAME
    before = f.read_text()
    cv.add_profiles(["prov/model-x"], root)
    assert f.read_text() == before and mode(f) == 0o600
