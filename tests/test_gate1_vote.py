"""tests/test_gate1_vote.py — CC-10: Gate 1's presence check asks three voters with the code and a
quote before it asks the single reader (`tools/auto/gate1_vote.py`, ticket 292).

The voters here are fakes, but they answer in the live reply shapes (a clean list, separate
objects with prose around them, a quote over two code lines with a bare newline) and go through
the real parser, the real quote check, the real tally and the real families rule.
"""

from __future__ import annotations

import configparser
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto import gate1_vote as gv
from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter
from tools.claimcheck import judge as cc_judge
from tools.claimcheck.anchors import PathRepoView

VOTERS = ("kenary/step-5-preview", "kenary/muse-spark-1-3", "kenary/nemotron-3-ultra")


# ── helpers ──────────────────────────────────────────────────────────────────────────────────

def _config(**gate1) -> configparser.ConfigParser:
    keys = {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false",
            "llm_call_retry_wait_sec": "0"}
    keys.update(gate1)
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test",
                      "model": "test-model", "api_format": "openai"},
        "gate1": keys, "loop": {"timeout_seconds": "10"}})
    return cfg


def _on(**extra) -> configparser.ConfigParser:
    return _config(claim_vote="on", vote_profiles=", ".join(VOTERS), **extra)


def _filter(cfg, ask=None, reader=("confirmed", "the reader says so")):
    filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1", api_key="test",
                       model="test-model", api_format="openai", verify_ssl=False)
    if ask is not None and filt._vote is not None:
        filt._vote._ask_fn = ask
    filt.reader_calls = 0

    def fake_reader(candidate, block, **kw):
        filt.reader_calls += 1
        return (reader[0] == "confirmed", reader[1], reader[0])

    filt._check_presence = fake_reader
    return filt


def _repo(tmp_path: Path, n: int = 1) -> list:
    (tmp_path / "pkg").mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        (tmp_path / "pkg" / f"m{i}.py").write_text(textwrap.dedent(f"""\
            def load_{i}(path):
                data = open(path).read()
                return data
        """), encoding="utf-8")
        out.append(CandidateTask(
            title=f"close the file in load_{i}", instruction="the file is opened and never closed",
            target_files=[f"pkg/m{i}.py"], acceptance_check="true",
            cited_location=CitedLocation(file=f"pkg/m{i}.py", symbol=f"load_{i}"), cluster="pkg"))
    return out


def _first_chunk(prompt: str) -> tuple:
    """(chunk id, the first code line of it) of the first claim of *prompt*."""
    m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)", prompt)
    return m.group(1), m.group(2).rstrip()


def _two_lines(prompt: str) -> tuple:
    m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)\n\s*\d+\|\s?([^\n]+)", prompt)
    return m.group(1), m.group(2).rstrip() + "\n" + m.group(3).rstrip()


def _reply(shape: str, verdict: str, prompt: str) -> str:
    if shape == "dead":
        raise RuntimeError("HTTP 404")
    if shape == "unsure":
        return json.dumps([{"id": 1, "verdict": "UNSURE", "chunk": "", "quote": ""}])
    if shape == "fabricated":
        cid, _ = _first_chunk(prompt)
        return json.dumps([{"id": 1, "verdict": verdict, "chunk": cid,
                            "quote": "this text is nowhere in the code"}])
    if shape == "list":
        cid, line = _first_chunk(prompt)
        return json.dumps([{"id": 1, "verdict": verdict, "chunk": cid, "quote": line}])
    if shape == "objects":     # no list, prose around, a "stray" quote
        cid, line = _first_chunk(prompt)
        return (f'Here is my answer, the "claim" is judged:\n'
                f'{json.dumps({"id": 1, "verdict": verdict, "chunk": cid, "quote": line})}\nDone.')
    if shape == "newline":     # a quote over two code lines carries a bare newline
        cid, text = _two_lines(prompt)
        return ('[{"id": 1, "verdict": "%s", "chunk": "%s", "quote": "%s"}]'
                % (verdict, cid, text.replace("\\", "\\\\").replace('"', '\\"')))
    raise AssertionError(shape)


def _voters(*spec):
    """An `ask_fn` for the three voters: *spec* is one (shape, verdict) per voter, in VOTERS order.
    The reply is read by the real parser and the real quote check (`ask_with_packs`)."""
    plan = dict(zip(VOTERS, spec))
    calls = []

    def ask(ref, items, run, seed, timeout):
        shape, verdict = plan[ref]
        calls.append(ref)
        got = cc_judge.ask_with_packs(items, run, seed, lambda p: _reply(shape, verdict, p),
                                      batch=1, code_batch=1)
        row = {"model": ref, "run": run, "votes": got["votes"], "accepted": got["accepted"],
               "rejected": got["rejected"]}
        if got["errors"]:
            row["error"] = got["errors"][0]
        return row

    ask.calls = calls
    return ask


def _run(tmp_path, filt, n=1):
    cands = _repo(tmp_path, n)
    counters: dict = {}
    accepted, rejected = filt.filter(cands, tmp_path, counters=counters)
    return accepted, rejected, counters


# ── the verdicts ─────────────────────────────────────────────────────────────────────────────

def test_unanimous_true_confirms_without_the_reader(tmp_path):
    """Three families, three verified quotes, all TRUE (a clean list, separate objects, a quote over two lines)."""
    ask = _voters(("list", "TRUE"), ("objects", "TRUE"), ("newline", "TRUE"))
    filt = _filter(_on(), ask)
    accepted, rejected, counters = _run(tmp_path, filt)
    assert len(accepted) == 1 and not rejected
    assert filt.reader_calls == 0
    assert sorted(ask.calls) == sorted(VOTERS)          # three requests, one candidate
    assert (counters["votes_asked"], counters["votes_decided"], counters["votes_overridden"]) == (1, 1, 0)
    assert counters["votes_requests"] == 3


def test_unanimous_false_is_rejected_when_the_reader_does_not_confirm(tmp_path):
    """The veto is on: the reader is asked, says rejected, and both reasons are recorded."""
    filt = _filter(_on(), _voters(("list", "FALSE"), ("list", "FALSE"), ("list", "FALSE")),
                   reader=("rejected", "the file is closed by the caller"))
    accepted, rejected, counters = _run(tmp_path, filt)
    assert not accepted and len(rejected) == 1
    assert rejected[0].stage == "presence"
    assert "claim vote: rejected" in rejected[0].reason and "single reader" in rejected[0].reason
    assert filt.reader_calls == 1
    assert counters["votes_decided"] == 1 and counters["votes_overridden"] == 0


def test_unanimous_false_is_overridden_by_a_reader_that_confirms(tmp_path):
    """A reader that read the code and says confirmed keeps the candidate; the override is counted."""
    filt = _filter(_on(), _voters(("list", "FALSE"), ("list", "FALSE"), ("list", "FALSE")))
    accepted, rejected, counters = _run(tmp_path, filt)
    assert len(accepted) == 1 and not rejected
    assert counters["votes_overridden"] == 1 and counters["votes_decided"] == 0


def test_unanimous_false_with_the_veto_off_rejects_without_the_reader(tmp_path):
    filt = _filter(_on(vote_veto_by_reader="off"), _voters(("list", "FALSE"), ("list", "FALSE"), ("list", "FALSE")))
    accepted, rejected, _ = _run(tmp_path, filt)
    assert not accepted and "claim vote: rejected" in rejected[0].reason
    assert filt.reader_calls == 0


def test_a_reader_with_no_answer_does_not_save_a_unanimous_false(tmp_path):
    """`unknown` is not `confirmed`: the vote's rejection stands, and it says nothing about the reader."""
    filt = _filter(_on(), _voters(("list", "FALSE"), ("list", "FALSE"), ("list", "FALSE")),
                   reader=("unknown", "presence unknown — empty (transport)"))
    accepted, rejected, _ = _run(tmp_path, filt)
    assert not accepted
    assert rejected[0].reason.startswith("claim vote: rejected") and "single reader" not in rejected[0].reason


def test_a_split_falls_back_to_the_single_reader(tmp_path):
    filt = _filter(_on(), _voters(("list", "TRUE"), ("list", "FALSE"), ("list", "TRUE")),
                   reader=("rejected", "reader decides"))
    accepted, rejected, counters = _run(tmp_path, filt)
    assert not accepted and rejected[0].reason == "reader decides"
    assert filt.reader_calls == 1
    assert counters["votes_decided"] == 0 and counters["votes_overridden"] == 0


def test_a_fabricated_quote_is_downgraded_and_falls_back(tmp_path):
    """One voter quotes text that is not in the pack: its vote is UNSURE, two committed are below the quorum."""
    filt = _filter(_on(), _voters(("list", "TRUE"), ("list", "TRUE"), ("fabricated", "TRUE")))
    accepted, rejected, counters = _run(tmp_path, filt)
    assert filt.reader_calls == 1 and counters["votes_decided"] == 0


def test_a_voter_that_abstains_falls_back(tmp_path):
    filt = _filter(_on(), _voters(("list", "TRUE"), ("list", "TRUE"), ("unsure", "TRUE")))
    _run(tmp_path, filt)
    assert filt.reader_calls == 1


def test_a_dead_voter_is_a_result_not_a_crash(tmp_path):
    """HTTP 404 from one voter: no decision, the single reader answers, `votes_overridden` stays 0."""
    filt = _filter(_on(), _voters(("list", "TRUE"), ("list", "TRUE"), ("dead", "TRUE")))
    accepted, rejected, counters = _run(tmp_path, filt)
    assert len(accepted) == 1 and filt.reader_calls == 1
    assert counters["votes_overridden"] == 0 and counters["votes_decided"] == 0


def test_a_voter_that_raises_outright_is_a_missing_vote(tmp_path):
    def ask(ref, items, run, seed, timeout):
        if ref == VOTERS[2]:
            raise ConnectionError("boom")
        return _voters(("list", "TRUE"), ("list", "TRUE"), ("list", "TRUE"))(ref, items, run, seed, timeout)
    filt = _filter(_on(), ask)
    accepted, _, _ = _run(tmp_path, filt)
    assert len(accepted) == 1 and filt.reader_calls == 1


def test_three_voters_of_two_families_do_not_decide(tmp_path):
    """The families rule (CC-8) holds here too: a `ask_fn` that names two agnes-* and one muse voter."""
    cfg = _config(claim_vote="on", vote_profiles="kenary/agnes-3-0-flash, kenary/agnes-2-5, kenary/muse-spark-1-3")
    assert gv.VoteConfig.from_config(cfg).enabled is False       # refused up front, not at the tally
    filt = _filter(cfg)
    assert filt._vote is None


# ── off, and what it must not change ─────────────────────────────────────────────────────────

def test_off_builds_nothing_and_changes_nothing(tmp_path):
    filt = _filter(_config())                       # no claim_vote key at all
    assert filt._vote is None
    accepted, rejected, counters = _run(tmp_path, filt)
    assert len(accepted) == 1 and filt.reader_calls == 1
    assert not any(k.startswith("votes_") for k in counters)


def test_explicit_off_is_off(tmp_path):
    assert _filter(_config(claim_vote="off", vote_profiles=", ".join(VOTERS)))._vote is None


def test_on_with_no_voters_is_the_single_reader_byte_for_byte(tmp_path):
    """`on` with no profiles: nothing is asked of anyone, the reader's reason is the reason."""
    filt = _filter(_config(claim_vote="on"), reader=("confirmed", "reader reason"))
    assert filt._vote is None
    accepted, rejected, counters = _run(tmp_path, filt)
    assert len(accepted) == 1 and filt.reader_calls == 1
    assert not any(k.startswith("votes_") for k in counters)


# ── the settings ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("keys, why", [
    ({"claim_vote": "maybe", "vote_profiles": ", ".join(VOTERS)}, "not on/off"),
    ({"claim_vote": "on", "vote_profiles": "a/x-1, b/y-1"}, "needs 3"),
    ({"claim_vote": "on", "vote_profiles": ", ".join(VOTERS), "vote_quorum": "1"}, "only 3"),
    ({"claim_vote": "on", "vote_profiles": "a/hy3, b/hy3-2, c/hy3:free"}, "families"),
])
def test_a_setting_that_makes_the_vote_unsafe_turns_it_off(keys, why):
    cfg = gv.VoteConfig.from_config(_config(**keys))
    assert cfg.enabled is False and why in cfg.why


def test_defaults_and_malformed_numbers():
    cfg = gv.VoteConfig.from_config(_on())
    assert (cfg.enabled, cfg.max_per_run, cfg.timeout_sec, cfg.veto_by_reader) == (True, 200, 60.0, True)
    bad = gv.VoteConfig.from_config(_on(vote_max_per_run="many", vote_timeout_sec="-3"))
    assert bad.enabled and bad.max_per_run == 200 and bad.timeout_sec == 60.0


def test_the_ini_ships_with_the_vote_off():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(PROJECT_ROOT / "agents.ini", encoding="utf-8")
    assert gv.VoteConfig.from_config(parser).enabled is False


# ── the cap, and the claims with nothing to quote ────────────────────────────────────────────

def test_past_the_cap_the_single_reader_answers(tmp_path):
    ask = _voters(("list", "TRUE"), ("list", "TRUE"), ("list", "TRUE"))
    filt = _filter(_on(vote_max_per_run="1"), ask)
    accepted, _, counters = _run(tmp_path, filt, n=2)
    assert len(accepted) == 2
    assert counters["votes_asked"] == 1 and filt.reader_calls == 1
    assert len(ask.calls) == 3


def test_a_candidate_with_no_anchor_in_the_tree_is_not_voted_on(tmp_path):
    ask = _voters(("list", "TRUE"), ("list", "TRUE"), ("list", "TRUE"))
    vote = gv.build(_on(), ask_fn=ask)
    cand = CandidateTask("t", "do it", ["gone.py"], "true", CitedLocation(file="gone.py", symbol="nope"))
    out = vote.presence_by_vote(cand, PathRepoView(tmp_path))
    assert out.verdict is None and out.requests == 0 and ask.calls == []


def test_a_vote_that_crashes_is_no_decision(tmp_path):
    def ask(*a):
        raise AssertionError("never reached")
    vote = gv.build(_on(), ask_fn=ask)
    vote._tally_fn = lambda *a, **k: 1 / 0
    cand = _repo(tmp_path)[0]
    assert vote.presence_by_vote(cand, PathRepoView(tmp_path)).verdict is None


def test_the_claim_is_the_symbol_the_file_and_the_instruction():
    cand = CandidateTask("t", "  the file is\nnever closed ", ["a.py"], "true",
                         CitedLocation(file="pkg/a.py", symbol="load"))
    assert gv.claim_of(cand) == "`load` in `pkg/a.py` has the problem: the file is never closed"
    bare = CandidateTask("t", "x", ["a.py"], "true", CitedLocation(file="pkg/a.py"))
    assert gv.claim_of(bare) == "`pkg/a.py` has the problem: x"


# ── the fixture chain ────────────────────────────────────────────────────────────────────────

FIXTURE = PROJECT_ROOT / "contest-bench" / "cc"


def _fixture_tasks() -> list:
    data = json.loads((FIXTURE / "claims_fixture.json").read_text(encoding="utf-8"))
    return [c for c in data["claims"] if c["kind"] == "code" and len(c["anchors"]) == 2]


def test_the_fixture_chain_unanimous_readers_give_zero_wrong():
    """The fixture's code claims as tasks, at the head tree: three fake pack-readers that answer each
    claim's known answer. Every decided verdict is the known one; nothing is rejected that is true."""
    root = FIXTURE / "fixture" / "head"
    tasks = _fixture_tasks()
    assert len(tasks) >= 40
    wrong = decided = 0
    for c in tasks:
        file, symbol = c["anchors"]
        verdict = "TRUE" if c["truth_head"] else "FALSE"
        vote = gv.build(_on(), ask_fn=_voters(("list", verdict), ("objects", verdict), ("newline", verdict)))
        cand = CandidateTask(c["id"], c["claim"], [file], "true", CitedLocation(file=file, symbol=symbol))
        out = vote.presence_by_vote(cand, PathRepoView(root))
        if out.verdict is None:
            continue
        decided += 1
        wrong += (out.verdict == "confirmed") != bool(c["truth_head"])
    assert wrong == 0
    assert decided >= len(tasks) - 5      # 48 of 50 today; the two others name a symbol the head tree lacks


def test_the_fixture_chain_with_a_dead_third_voter_decides_nothing():
    root = FIXTURE / "fixture" / "head"
    for c in _fixture_tasks()[:10]:
        file, symbol = c["anchors"]
        vote = gv.build(_on(), ask_fn=_voters(("list", "TRUE"), ("list", "TRUE"), ("dead", "TRUE")))
        cand = CandidateTask(c["id"], c["claim"], [file], "true", CitedLocation(file=file, symbol=symbol))
        assert vote.presence_by_vote(cand, PathRepoView(root)).verdict is None
        assert vote.counters["votes_overridden"] == 0
