"""tests/test_gate1_vote_review.py — the Gate 1 vote (CC-10) after the review of 2026-10-12: families
read off the models, a voter with no vote under `all`, a file the task will create, the skipped
counter, `vote_confirm`, voters of contest.local.ini, the deadline of a voter given up on, one view
a run, `presence_deadline_sec` of the single reader.

The voters answer in live shapes (a clean list, separate objects with prose, a quote over two lines,
HTTP 404) through the real parser, quote check, tally and families rule; the HTTP tests use a real
local socket that accepts and never answers.
"""

from __future__ import annotations

import configparser
import json
import re
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto import gate1_vote as gv
from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter
from tools.claimcheck import anchors as cc_anchors
from tools.claimcheck import judge as cc_judge
from tools.claimcheck.anchors import PathRepoView
import tools.llm_stream as llm_stream

THREE = ("kenary/step-5-preview", "kenary/muse-spark-1-3", "kenary/nemotron-3-ultra")
FIVE = THREE + ("kenary/agnes-3-0-flash", "kenary/hy3")


@pytest.fixture(autouse=True)
def _no_local_ini(tmp_path, monkeypatch):
    """The developer's own contest.local.ini is never read by these tests."""
    empty = tmp_path / "no-local-ini"
    empty.mkdir()
    monkeypatch.setattr(gv, "LOCAL_ROOT", empty)


def _config(sections: dict = None, **gate1) -> configparser.ConfigParser:
    keys = {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false", "llm_call_retry_wait_sec": "0"}
    keys.update(gate1)
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"api": {"active": "local", "verify_ssl": "false"},
                   "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test",
                                 "model": "test-model", "api_format": "openai"},
                   "gate1": keys, "loop": {"timeout_seconds": "10"}, **(sections or {})})
    return cfg


def _on(profiles=THREE, sections=None, **extra):
    return _config(sections, claim_vote="on", vote_profiles=", ".join(profiles), **extra)


def _first_chunk(prompt: str) -> tuple:
    m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)", prompt)
    return m.group(1), m.group(2).rstrip()


def _two_lines(prompt: str) -> tuple:
    m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)\n\s*\d+\|\s?([^\n]+)", prompt)
    return m.group(1), m.group(2).rstrip() + "\n" + m.group(3).rstrip()


def _reply(shape: str, verdict: str, prompt: str) -> str:
    if shape == "dead":
        raise RuntimeError("HTTP 404 from https://example.invalid/v1: model not found")
    if shape == "garbage":
        return "I think so, yes."
    cid, line = _first_chunk(prompt)
    if shape == "list":
        return json.dumps([{"id": 1, "verdict": verdict, "chunk": cid, "quote": line}])
    if shape == "objects":
        return (f'Here is my answer, the "claim" is judged:\n'
                f'{json.dumps({"id": 1, "verdict": verdict, "chunk": cid, "quote": line})}\nDone.')
    if shape == "newline":
        cid, text = _two_lines(prompt)
        return ('[{"id": 1, "verdict": "%s", "chunk": "%s", "quote": "%s"}]'
                % (verdict, cid, text.replace("\\", "\\\\").replace('"', '\\"')))
    raise AssertionError(shape)


def _voters(plan: dict):
    """An `ask_fn`: *plan* maps a ref to (shape, verdict); calls are recorded."""
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


def _repo(tmp_path: Path, n: int = 1, instruction="the file is opened and never closed") -> list:
    (tmp_path / "pkg").mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        (tmp_path / "pkg" / f"m{i}.py").write_text(textwrap.dedent(f"""\
            def load_{i}(path):
                data = open(path).read()
                return data
        """), encoding="utf-8")
        out.append(CandidateTask(
            title=f"close the file in load_{i}", instruction=instruction,
            target_files=[f"pkg/m{i}.py"], acceptance_check="true",
            cited_location=CitedLocation(file=f"pkg/m{i}.py", symbol=f"load_{i}"), cluster="pkg"))
    return out


def _filter(cfg, ask=None, reader=("rejected", "the reader read it and disagrees")):
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


def _run(tmp_path, filt, cands):
    counters: dict = {}
    accepted, rejected = filt.filter(cands, tmp_path, counters=counters)
    return accepted, rejected, counters


AGNES = {"voter_a": {"model": "kenary/agnes-3-0-flash:free"},
         "voter_b": {"model": "kenary/agnes-3-5-flash:free"},
         "voter_c": {"model": "kenary/agnes-2-5-pro:free"}}
VENDORS = {"llm1": {"model": "kenary/agnes-3-0-flash:free"},
           "llm2": {"model": "openai/gpt-5-mini"},
           "llm3": {"model": "meta/llama-4-scout"}}


# ── №1: the families are the models', not the section names' ─────────────────────────────────

def test_three_sections_of_one_family_are_refused():
    cfg = gv.VoteConfig.from_config(_on(tuple(AGNES), AGNES))
    assert cfg.enabled is False and "families" in cfg.why


def test_add_profiles_names_of_three_qwen_models_are_one_family():
    """The names `claim_vote.py --add-profiles` writes, all three qwen: one family (6.txt)."""
    secs = {"claim_vote_llm.kenary_qwen3_235b_free": {"model": "kenary/qwen3-235b:free"},
            "claim_vote_llm.kenary_qwen_2_5_72b": {"model": "kenary/qwen-2.5-72b"},
            "claim_vote_llm.kenary_qwen3_coder": {"model": "kenary/qwen3-coder"}}
    assert gv.VoteConfig.from_config(_on(tuple(secs), secs)).enabled is False


def test_numbered_sections_of_three_vendors_are_three_families():
    cfg = gv.VoteConfig.from_config(_on(tuple(VENDORS), VENDORS))
    assert cfg.enabled and cfg.model_map["llm2"] == "openai/gpt-5-mini"


def test_a_unanimous_true_of_one_family_named_three_ways_does_not_decide(tmp_path):
    """Past the config check (a test builds the vote by hand): the tally's families rule reads the
    same model map, so three agnes sections still give no decision."""
    cfg = gv.VoteConfig(True, tuple(AGNES), models=tuple((k, v["model"]) for k, v in AGNES.items()),
                        confirm=True)
    vote = gv.Gate1Vote(cfg, ask_fn=_voters({r: ("list", "TRUE") for r in AGNES}))
    out = vote.presence_by_vote(_repo(tmp_path)[0], PathRepoView(tmp_path))
    assert out.verdict is None and out.requests == 3


def test_numbered_sections_of_three_vendors_decide(tmp_path):
    filt = _filter(_on(tuple(VENDORS), VENDORS, vote_confirm="on"),
                   _voters({"llm1": ("list", "TRUE"), "llm2": ("objects", "TRUE"), "llm3": ("newline", "TRUE")}))
    accepted, _, counters = _run(tmp_path, filt, _repo(tmp_path))
    assert len(accepted) == 1 and filt.reader_calls == 0 and counters["votes_decided"] == 1


# ── №4: voters of contest.local.ini, and a voter that cannot start ───────────────────────────

def test_a_voter_section_of_contest_local_ini_is_read(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    # a BOM (Notepad) and the sections only there, not in the ini Gate 1 runs with
    (root / "contest.local.ini").write_text(
        "\ufeff[claim_vote_llm.a]\nmodel = kenary/agnes-3-0-flash:free\n"
        "[claim_vote_llm.b]\nmodel = openai/gpt-5-mini\n[claim_vote_llm.c]\nmodel = meta/llama-4\n",
        encoding="utf-8")
    monkeypatch.setattr(gv, "LOCAL_ROOT", root)
    refs = ("claim_vote_llm.a", "claim_vote_llm.b", "claim_vote_llm.c")
    cfg = gv.VoteConfig.from_config(_on(refs))
    assert cfg.enabled and cfg.model_map["claim_vote_llm.b"] == "openai/gpt-5-mini"
    # and the same three, all agnes there, are one family
    (root / "contest.local.ini").write_text(
        "".join(f"[claim_vote_llm.{x}]\nmodel = kenary/agnes-{i}:free\n" for i, x in enumerate("abc")),
        encoding="utf-8")
    assert gv.VoteConfig.from_config(_on(refs)).enabled is False


def test_the_ini_gate1_runs_with_wins_over_contest_local_ini(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "contest.local.ini").write_text("[llm1]\nmodel = kenary/agnes-x\n", encoding="utf-8")
    monkeypatch.setattr(gv, "LOCAL_ROOT", root)
    cfg = gv.VoteConfig.from_config(_on(tuple(VENDORS), VENDORS))
    assert cfg.model_map["llm1"] == VENDORS["llm1"]["model"]


def test_a_voter_that_cannot_be_resolved_is_named_and_left_out(tmp_path, caplog):
    """The real path (no ask_fn): a section with no base_url is named once in the log, before the
    first vote, and under `all` the vote stops (it could never decide) — no cap slot, no 'request'."""
    secs = {"llm1": {"model": "kenary/agnes-3-0-flash:free", "base_url": "http://127.0.0.1:9/v1", "api_key": "k"},
            "llm2": {"model": "openai/gpt-5-mini", "base_url": "http://127.0.0.1:9/v1", "api_key": "k"},
            "llm3": {"model": "meta/llama-4-scout"}}                 # no base_url, no key
    vote = gv.build(_on(tuple(secs), secs))
    with caplog.at_level("WARNING"):
        outs = [vote.presence_by_vote(c, PathRepoView(tmp_path)) for c in _repo(tmp_path, 2)]
    assert all(o.verdict is None and o.requests == 0 for o in outs)
    assert caplog.text.count("voter llm3 cannot be used") == 1
    assert "vote_accept = all needs every voter" in vote.stopped
    assert vote.counters["votes_asked"] == 0 and vote.counters["votes_skipped"] == 2
    assert vote._voted == 0                                          # no cap slot spent


def test_under_committed_an_unresolvable_voter_is_dropped_and_the_rest_vote():
    secs = {f"v{i}": {"model": m, "base_url": "http://127.0.0.1:9/v1", "api_key": "k"}
            for i, m in enumerate(("kenary/agnes-3", "openai/gpt-5", "meta/llama-4", "zai/glm-5"))}
    secs["v4"] = {"model": "qwen/qwen3"}                             # cannot be resolved
    vote = gv.build(_on(tuple(secs), secs, vote_accept="committed"))
    assert vote.resolve_voters() == "" and vote._voters == ["v0", "v1", "v2", "v3"]


def test_a_voter_error_is_logged_once_a_run(tmp_path, caplog):
    plan = {r: ("list", "TRUE") for r in THREE}
    plan[THREE[2]] = ("dead", "TRUE")
    filt = _filter(_on(), _voters(plan))
    with caplog.at_level("WARNING"):
        _run(tmp_path, filt, _repo(tmp_path, 3))
    assert caplog.text.count(f"voter {THREE[2]}: HTTP 404") == 1


# ── №2: under `all` a voter with no vote on the candidate blocks it ──────────────────────────

@pytest.mark.parametrize("profiles, dead", [(THREE + ("kenary/agnes-3-0-flash",), 1), (FIVE, 1), (FIVE, 2)])
def test_under_all_a_voter_with_no_vote_gives_no_decision(tmp_path, profiles, dead):
    plan = {r: ("list", "TRUE") for r in profiles}
    for r in profiles[-dead:]:
        plan[r] = ("dead", "TRUE") if dead == 1 else ("garbage", "TRUE")
    filt = _filter(_on(profiles, vote_confirm="on"), _voters(plan))
    accepted, _, counters = _run(tmp_path, filt, _repo(tmp_path))
    assert filt.reader_calls == 1 and counters["votes_decided"] == 0
    assert counters["votes_requests"] == len(profiles)


def test_under_committed_a_voter_with_no_vote_abstains_by_the_rule(tmp_path):
    """CC-11: three committed of five, agreeing, settle it; a dead one is an abstainer."""
    plan = {r: ("list", "TRUE") for r in FIVE}
    plan[FIVE[3]], plan[FIVE[4]] = ("dead", "TRUE"), ("garbage", "TRUE")
    filt = _filter(_on(FIVE, vote_accept="committed", vote_confirm="on"), _voters(plan))
    accepted, _, counters = _run(tmp_path, filt, _repo(tmp_path))
    assert len(accepted) == 1 and filt.reader_calls == 0


# ── №3, №38 and the skipped counter ───────────────────────────────────────────────────────────

def test_an_instruction_that_names_the_file_it_will_create_is_voted_on(tmp_path):
    cand = _repo(tmp_path, instruction="the file is never closed; add a test in tests/test_load_close.py")[0]
    ask = _voters({r: ("list", "FALSE") for r in THREE})
    filt = _filter(_on(), ask)
    accepted, rejected, counters = _run(tmp_path, filt, [cand])
    assert counters["votes_asked"] == 1 and counters["votes_skipped"] == 0
    assert not accepted and rejected[0].reason.startswith("claim vote: rejected")


def test_a_cited_file_not_in_the_tree_is_skipped_and_counted(tmp_path):
    _repo(tmp_path)
    cand = CandidateTask("t", "it leaks", ["pkg/gone.py"], "true", CitedLocation(file="pkg/gone.py", symbol="load_0"))
    ask = _voters({r: ("list", "TRUE") for r in THREE})
    vote = gv.build(_on(), ask_fn=ask)
    assert vote.presence_by_vote(cand, PathRepoView(tmp_path)).verdict is None
    assert ask.calls == [] and vote.counters["votes_skipped"] == 1 and vote.counters["votes_asked"] == 0


def test_an_empty_instruction_is_not_voted_on(tmp_path):
    """--validate-plan reads a hand-edited plan.json: an instruction can be empty (5.txt, Bug 3)."""
    cand = _repo(tmp_path, instruction="  ")[0]
    ask = _voters({r: ("list", "TRUE") for r in THREE})
    vote = gv.build(_on(), ask_fn=ask)
    out = vote.presence_by_vote(cand, PathRepoView(tmp_path))
    assert out.verdict is None and out.requests == 0 and ask.calls == []
    assert vote.counters["votes_skipped"] == 1


def test_the_cap_counts_as_skipped_and_the_line_says_it(tmp_path, capsys):
    filt = _filter(_on(vote_max_per_run="1"), _voters({r: ("list", "FALSE") for r in THREE}))
    _, _, counters = _run(tmp_path, filt, _repo(tmp_path, 3))
    assert counters["votes_asked"] == 1 and counters["votes_skipped"] == 2
    assert "requests 3, skipped 2" in capsys.readouterr().out


# ── №6: vote_confirm ─────────────────────────────────────────────────────────────────────────

def test_veto_only_hands_a_unanimous_true_to_the_reader_and_logs_it(tmp_path, caplog):
    filt = _filter(_on(vote_confirm="off"), _voters({r: ("list", "TRUE") for r in THREE}),
                   reader=("rejected", "the docstring says this is a deliberate example"))
    with caplog.at_level("INFO"):
        accepted, rejected, counters = _run(tmp_path, filt, _repo(tmp_path))
    assert not accepted and rejected[0].reason == "the docstring says this is a deliberate example"
    assert filt.reader_calls == 1 and counters["votes_decided"] == 0
    assert "claim vote: TRUE by" in caplog.text and "vote_confirm = off" in caplog.text


def test_veto_only_a_unanimous_false_still_rejects(tmp_path):
    filt = _filter(_on(vote_confirm="off"), _voters({r: ("list", "FALSE") for r in THREE}))
    accepted, rejected, counters = _run(tmp_path, filt, _repo(tmp_path))
    assert not accepted and counters["votes_decided"] == 1


@pytest.mark.parametrize("value, confirm", [("on", True), ("off", False), ("", True), ("maybe", True)])
def test_vote_confirm_is_read(value, confirm):
    assert gv.VoteConfig.from_config(_on(vote_confirm=value)).confirm is confirm


def test_the_shipped_ini_keeps_confirm_on_and_bounds_the_reader():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(PROJECT_ROOT / "agents.ini", encoding="utf-8")
    parser.set("gate1", "claim_vote", "on")
    parser.set("gate1", "vote_profiles", ", ".join(THREE))
    assert gv.VoteConfig.from_config(parser).confirm is True
    assert parser.get("gate1", "presence_deadline_sec") == "1800"


# ── №7, №9: a voter given up on stops, its thread never holds the process ────────────────────

def test_a_voter_that_never_answers_is_dropped_after_two_misses_and_the_vote_stops(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gv, "VOTE_SLACK_SEC", 0.2)
    release = threading.Event()
    plan = {r: ("list", "TRUE") for r in THREE}
    ask_ok = _voters(plan)

    def ask(ref, items, run, seed, timeout):
        if ref == THREE[2]:
            release.wait(30)            # a provider that never answers
            return {"votes": {}}
        return ask_ok(ref, items, run, seed, timeout)

    cfg = gv.VoteConfig(True, THREE, timeout_sec=0.1, confirm=True, models=tuple((r, r) for r in THREE))
    vote = gv.Gate1Vote(cfg, ask_fn=ask)
    cands = _repo(tmp_path, 4)
    view = PathRepoView(tmp_path)
    try:
        with caplog.at_level("WARNING"):
            started = time.monotonic()
            outs = [vote.presence_by_vote(c, view) for c in cands]
            took = time.monotonic() - started
        stuck = [t for t in threading.enumerate() if t.name == f"gate1-vote-{THREE[2]}"]
        assert stuck and all(t.daemon for t in stuck)       # never holds the interpreter at exit
    finally:
        release.set()
    assert all(o.verdict is None for o in outs)
    assert vote.stopped and "vote_accept = all needs every voter" in vote.stopped
    assert vote.counters["votes_asked"] == 2 and vote.counters["votes_skipped"] == 2
    assert took < 20          # the stuck voter waits 30 s: the vote never waited for it (a margin for load)
    assert "is not asked again this run" in caplog.text


def test_the_real_ask_passes_the_timeout_as_set_and_a_deadline(monkeypatch):
    cv = gv._load_claim_vote()
    seen = {}

    def fake_ask_v2(ref, items, run, seed, parser, timeout, batch, code_batch, deadline=None, budget=None):
        seen.update(timeout=timeout, deadline=deadline, budget=budget)
        return {"model": ref, "votes": {}}

    monkeypatch.setattr(cv, "ask_v2", fake_ask_v2)
    vote = gv.Gate1Vote(gv.VoteConfig(True, THREE, timeout_sec=1.9))
    vote._run_voters([THREE[0]], [])
    assert seen["timeout"] == 1.9 and seen["budget"] == 3.8      # the request's own time, from its slot
    assert 0 < seen["deadline"] - time.monotonic() <= 3.8


def _silent_server():
    """A socket that accepts and never answers (bound here on port 0: no xdist group needed)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held = []
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                pass

    threading.Thread(target=loop, daemon=True).start()

    def close():
        stop.set()
        for c in held:
            c.close()
        srv.close()
    return srv.getsockname()[1], close


def test_request_completion_stops_at_its_deadline_on_a_silent_provider():
    port, close = _silent_server()
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="deadline"):
            llm_stream.request_completion(
                f"http://127.0.0.1:{port}/v1/chat/completions", {}, {"model": "m", "messages": []},
                timeout=1.0, error_retries=60, error_retry_wait_sec=0.5,
                deadline=time.monotonic() + 2.5)
        took = time.monotonic() - started
    finally:
        close()
    assert took < 30, took         # 61 attempts of 1 s + 60 waits of 0.5 s (91 s) without it; a margin for load


def test_request_completion_without_a_deadline_is_as_before(monkeypatch):
    """No deadline: the retry budget alone, every wait made (the injected sleep counts them)."""
    waits = []

    def refused(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(llm_stream.urllib.request, "urlopen", refused)
    with pytest.raises(RuntimeError, match="after 3 retries"):
        llm_stream.request_completion("http://127.0.0.1:9/v1/chat/completions", {}, {"model": "m", "messages": []},
                                      timeout=1.0, error_retries=3, error_retry_wait_sec=10.0,
                                      _sleep_fn=waits.append)
    assert waits == [10.0, 10.0, 10.0]


def test_ask_v2_on_a_silent_provider_returns_at_the_deadline(tmp_path):
    port, close = _silent_server()
    cv = gv._load_claim_vote()
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict({"claim_vote": {"error_retries": "60", "error_retry_wait_sec": "0.5"},
                      "silent": {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": "k",
                                 "model": "agnes-1", "api_format": "openai"}})
    _repo(tmp_path)
    claim = "`load_0` in `pkg/m0.py` has the problem: the file is never closed"
    view = PathRepoView(tmp_path)
    resolved = cc_anchors.resolve_anchors(cc_anchors.extract_anchors(claim), view)
    from tools.claimcheck.pack import build_pack
    items = [(claim, build_pack(claim, resolved, view))]
    try:
        started = time.monotonic()
        row = cv.ask_v2("silent", items, 0, 0, parser, 1.0, batch=1, code_batch=1,
                        deadline=time.monotonic() + 2.5)
        took = time.monotonic() - started
    finally:
        close()
    assert row["votes"] == {} and took < 30, took   # 91 s without the deadline; a margin for load


# ── №8: one view of the target for the run ───────────────────────────────────────────────────

def test_the_target_is_indexed_once_a_run(tmp_path, monkeypatch):
    builds = []
    real = cc_anchors._Index

    class Counting(real):
        def __init__(self, view):
            builds.append(view)
            super().__init__(view)

    monkeypatch.setattr(cc_anchors, "_Index", Counting)
    filt = _filter(_on(presence_workers="4"), _voters({r: ("list", "FALSE") for r in THREE}))
    _, rejected, counters = _run(tmp_path, filt, _repo(tmp_path, 6))
    assert counters["votes_asked"] == 6 and len(rejected) == 6
    assert len(builds) == 1


# ── №28: the single reader's whole check has a deadline ──────────────────────────────────────

def _reader_filter(**gate1):
    return Gate1Filter(config=_config(**gate1), base_url="http://localhost:1337/v1", api_key="test",
                       model="test-model", api_format="openai", verify_ssl=False)


@pytest.mark.parametrize("value, expected", [(None, 1800.0), ("600", 600.0), ("0", 0.0), ("soon", 1800.0),
                                             ("-5", 1800.0)])
def test_presence_deadline_sec_is_read(value, expected):
    keys = {} if value is None else {"presence_deadline_sec": value}
    assert _reader_filter(**keys)._presence_deadline_sec == expected


def test_the_reader_passes_its_deadline_and_stops_retrying_past_it(monkeypatch):
    filt = _reader_filter(presence_deadline_sec="0.3", llm_call_retry_max="50")
    calls = []

    def provider(*a, **kw):
        calls.append(kw.get("deadline"))
        time.sleep(0.2)
        raise RuntimeError("HTTP 503 from x: busy")

    monkeypatch.setattr(llm_stream, "request_completion", provider)
    cand = CandidateTask("t", "it leaks", ["a.py"], "true", CitedLocation(file="a.py", symbol="f"))
    ok, reason, verdict = filt._check_presence(cand, "def f():\n    pass\n", _sleep_fn=lambda s: None)
    assert verdict == "unknown" and not ok
    assert calls and all(d is not None for d in calls)
    assert len(calls) <= 3                 # 50 retries allowed, the deadline ended it


def test_with_no_deadline_the_reader_passes_none(monkeypatch):
    filt = _reader_filter(presence_deadline_sec="0", llm_call_retry_max="0")
    seen = []

    def provider(*a, **kw):
        seen.append("deadline" in kw)
        raise RuntimeError("HTTP 503 from x: busy")

    monkeypatch.setattr(llm_stream, "request_completion", provider)
    cand = CandidateTask("t", "it leaks", ["a.py"], "true", CitedLocation(file="a.py", symbol="f"))
    filt._check_presence(cand, "def f():\n    pass\n", _sleep_fn=lambda s: None)
    assert seen and not any(seen)
