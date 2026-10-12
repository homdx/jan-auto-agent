"""tests/test_gate1_vote_inflight_review.py — the Gate 1 vote after the self-review of 2026-10-12: votes of several
presence workers share the pacer's queue, a voter that never replies is dropped, a number that is no number is refused.

The queue and the dropped-voter tests are the real path: voters resolved from ini sections, the real pacer (scaled
down), the real ask_v2 and request_completion against local sockets bound on port 0 (no xdist group needed).
"""

from __future__ import annotations

import configparser
import json
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto import gate1_vote as gv
from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter
from tools.claimcheck.anchors import PathRepoView

MODELS = ["agnes-3", "gpt-5", "llama-4", "glm-5", "qwen-3"]


@pytest.fixture(autouse=True)
def _no_local_ini(tmp_path, monkeypatch):
    empty = tmp_path / "no-local-ini"
    empty.mkdir()
    monkeypatch.setattr(gv, "LOCAL_ROOT", empty)


def _server(delay: float):
    """Chat completions that answer after *delay* with one TRUE row quoting the pack's first code line."""
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)", body["messages"][-1]["content"])
            content = json.dumps([{"id": 1, "verdict": "TRUE", "chunk": m.group(1), "quote": m.group(2).rstrip()}])
            time.sleep(delay)
            out = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _silent_server():
    """A socket that accepts and never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    held, stop = [], threading.Event()

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


def _vote(ports: list, accept: str):
    refs = [f"v{i}" for i in range(len(ports))]
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"gate1": {"claim_vote": "on", "vote_profiles": ", ".join(refs), "vote_timeout_sec": "1",
                             "vote_accept": accept},
                   "claim_vote": {"error_retries": "0"},
                   **{r: {"base_url": f"http://127.0.0.1:{p}/v1", "api_key": "k", "model": m, "api_format": "openai"}
                      for r, m, p in zip(refs, MODELS, ports)}})
    return gv.build(cfg), refs


def _candidate(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def load(path):\n    data = open(path).read()\n    return data\n",
                                           encoding="utf-8")
    return CandidateTask("close the file", "the file is opened and never closed", ["pkg/m.py"], "true",
                         CitedLocation(file="pkg/m.py", symbol="load"), "pkg")


# ── №54: the votes of several presence workers queue on one pacer ────────────────────────────

def test_votes_of_eight_workers_wait_for_the_whole_queue(tmp_path, monkeypatch):
    """Eight candidates voted on at once (presence_workers = 8), three voters each on one host, two requests in
    flight: 24 requests in 12 waves. Each vote's bound counted its own three requests (2 x timeout + pacing),
    the requests of the votes ahead of it were cut at that deadline, and 5 of 16 votes decided; the bound now
    counts the requests in flight on the host, and they reach the pacer in the order their votes were queued:
    the eight start at once, and under load a vote's request arrived after a later vote's ("no free slot before
    the deadline", 1 round in 14 with only the count)."""
    cv = gv._load_claim_vote()
    monkeypatch.setattr(cv, "PACER", cv.Pacer(width=2, interval=0.05))
    monkeypatch.setattr(gv, "VOTE_SLACK_SEC", 0.1)
    srv = _server(0.4)
    try:
        vote, refs = _vote([srv.server_address[1]] * 3, "all")
        cand, view = _candidate(tmp_path), PathRepoView(tmp_path)
        outs = []

        def one():
            outs.append(vote.presence_by_vote(cand, view))

        workers = [threading.Thread(target=one) for _ in range(8)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(60)
    finally:
        srv.shutdown()
    assert [o.verdict for o in outs] == ["confirmed"] * 8, [o.reason or o.requests for o in outs]
    assert vote._voters == refs and not vote.stopped and set(vote._misses.values()) == {0}
    assert sum(vote._inflight.values()) == 0          # every request left the count when it returned


def test_the_bound_counts_the_requests_in_flight_on_its_host(monkeypatch):
    cv = gv._load_claim_vote()
    monkeypatch.setattr(cv, "PACER", cv.Pacer(width=2, interval=4.0))
    vote = gv.Gate1Vote(gv.VoteConfig(True, ("a", "b", "c"), timeout_sec=60.0))
    vote._hosts = {"a": "kenari.id", "b": "kenari.id", "c": "vercel"}
    alone = vote._bound(["a", "b", "c"])
    assert alone == 120 + 4 + gv.VOTE_SLACK_SEC                     # two on kenari: one wave, one interval
    from collections import Counter
    # six requests of other votes ahead on kenari.id: eight in four waves; vercel's are not in kenari's queue
    assert vote._bound(["a", "b", "c"], Counter({"kenari.id": 6, "elsewhere": 9})) == 4 * 120 + 7 * 4 + gv.VOTE_SLACK_SEC


def test_a_place_given_up_is_passed_over_and_the_order_kept():
    """Places 0..3 of one host: 3 asks before 1 and 2, 1 gives up, 2 never asks (its request failed before):
    3 still waits for 0, and gets its turn once 0 is done — not before, and not stuck behind 1 and 2."""
    turns = gv._Turns()
    places = [turns.take("h") for _ in range(4)]
    assert places == [0, 1, 2, 3]
    got = []
    t3 = threading.Thread(target=lambda: got.append(turns.wait("h", 3, time.monotonic() + 30)))
    t3.start()
    assert not turns.wait("h", 1, time.monotonic() + 0.05)          # 0 has not had its turn: 1 gives up
    gv._Turn(turns, "h", 2, None).close()                           # never asked
    t3.join(0.3)
    assert t3.is_alive() and not got                                 # 0 is first
    assert turns.wait("h", 0, time.monotonic() + 1)
    turns.done("h", 0)
    t3.join(5)
    assert got == [True]


# ── found while fixing №54: a voter that never replies was never dropped ─────────────────────

def test_a_voter_that_never_replies_is_dropped_after_two_votes(tmp_path, monkeypatch):
    """Its request ends at its own deadline with an error, before the vote's bound: that row counted as an
    answer, the misses went back to 0, and a dead provider was asked for every candidate, each vote waiting
    out its 2 x timeout. Five voters under `committed`: the four left still decide."""
    cv = gv._load_claim_vote()
    monkeypatch.setattr(cv, "PACER", cv.Pacer(width=2, interval=0.05))
    monkeypatch.setattr(gv, "VOTE_SLACK_SEC", 0.1)
    srv = _server(0.05)
    port, close = _silent_server()
    try:
        vote, refs = _vote([srv.server_address[1]] * 4 + [port], "committed")
        cand, view = _candidate(tmp_path), PathRepoView(tmp_path)
        outs = [vote.presence_by_vote(cand, view) for _ in range(3)]
    finally:
        srv.shutdown()
        close()
    assert [o.verdict for o in outs] == ["confirmed"] * 3
    assert [o.requests for o in outs] == [5, 5, 4]
    assert vote._voters == refs[:4] and not vote.stopped


def test_what_counts_as_no_reply():
    vote = gv.Gate1Vote(gv.VoteConfig(True, ("a", "b", "c", "d", "e"), accept="committed",
                                      models=tuple((r, f"family{r}/m") for r in "abcde")))
    vote._note_voter("a", {"votes": {}, "error": "timed out", "requests": 1, "replies": 0})
    vote._note_voter("b", {"votes": {}, "error": "no free slot for h before the deadline", "requests": 0,
                           "replies": 0})                     # the queue's, not the voter's
    vote._note_voter("c", {"votes": {}, "requests": 1, "replies": 1})      # a reply nothing could read
    vote._note_voter("d", {"votes": {}, "error": "boom"})                  # a row with no counts (a fake)
    vote._note_voter("e", None)                                            # nothing by the bound
    assert vote._misses == {"a": 1, "b": 0, "c": 0, "d": 0, "e": 1}
    vote._note_voter("a", {"votes": {0: "TRUE"}, "requests": 1, "replies": 1})
    assert vote._misses["a"] == 0


# ── №57: a number that is no number ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["inf", "nan", "-inf", "Infinity"])
def test_vote_timeout_sec_must_be_a_finite_number(value):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"gate1": {"claim_vote": "on", "vote_profiles": "a/x-1, b/y-1, c/z-1",
                             "vote_timeout_sec": value, "vote_max_per_run": value}})
    got = gv.VoteConfig.from_config(cfg)
    assert got.enabled and got.timeout_sec == gv.DEFAULT_TIMEOUT_SEC and got.max_per_run == gv.DEFAULT_MAX_PER_RUN


@pytest.mark.parametrize("value", ["inf", "nan"])
def test_presence_deadline_sec_must_be_a_finite_number(value):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_dict({"api": {"active": "local", "verify_ssl": "false"},
                   "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test", "model": "m",
                                 "api_format": "openai"},
                   "gate1": {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false",
                             "presence_deadline_sec": value},
                   "loop": {"timeout_seconds": "10"}})
    filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1", api_key="test", model="m",
                       api_format="openai", verify_ssl=False)
    assert filt._presence_deadline_sec == 1800.0
