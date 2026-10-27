"""tests/test_gate1_vote_queue_review.py — the Gate 1 vote after the review of 2026-10-12 (n*.txt): the vote waits
as long as the claim_vote pacer's queue needs (voters of one host were cut as dead past a flat 2 x timeout + 5),
each request's time starts when it has its slot, and vote_skip_tests uses the project's test-path rule.

The queue test is the real path: voters resolved from ini sections, the real pacer (scaled down), the real
ask_v2 and request_completion against a local HTTP server that answers in the live shape.
"""

from __future__ import annotations

import configparser
import json
import re
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
from tools.claimcheck.anchors import PathRepoView

MODELS = ["agnes-3", "gpt-5", "llama-4", "glm-5", "qwen-3", "mimo-2"]


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
            prompt = body["messages"][-1]["content"]
            m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)", prompt)
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


def _candidate(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def load(path):\n    data = open(path).read()\n    return data\n",
                                           encoding="utf-8")
    return CandidateTask("close the file", "the file is opened and never closed", ["pkg/m.py"], "true",
                         CitedLocation(file="pkg/m.py", symbol="load"), "pkg")


def test_voters_queued_behind_the_pacer_are_not_cut_as_dead(tmp_path, monkeypatch):
    """Six voters on one host, two requests in flight: three waves of 0.8 s end at 2.4 s, past the flat
    2 x timeout (1 s) + slack = 2.1 s, which cut the third wave every candidate (its request's own deadline
    ran from the vote's start too); the queue-aware bound waits for it."""
    cv = gv._load_claim_vote()
    monkeypatch.setattr(cv, "PACER", cv.Pacer(width=2, interval=0.05))
    monkeypatch.setattr(gv, "VOTE_SLACK_SEC", 0.1)
    srv = _server(0.8)
    try:
        port = srv.server_address[1]
        refs = [f"v{i}" for i in range(len(MODELS))]
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read_dict({"gate1": {"claim_vote": "on", "vote_profiles": ", ".join(refs), "vote_timeout_sec": "1",
                                 "vote_accept": "all"},
                       "claim_vote": {"error_retries": "0"},
                       **{r: {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": "k", "model": m,
                              "api_format": "openai"} for r, m in zip(refs, MODELS)}})
        vote = gv.build(cfg)
        cand = _candidate(tmp_path)
        started = time.monotonic()
        out = vote.presence_by_vote(cand, PathRepoView(tmp_path))
        took = time.monotonic() - started
    finally:
        srv.shutdown()
    assert out.verdict == "confirmed" and out.requests == 6, (out, took)
    assert vote._voters == refs and not vote.stopped


def test_the_bound_follows_the_queue_per_host(monkeypatch):
    cv = gv._load_claim_vote()
    monkeypatch.setattr(cv, "PACER", cv.Pacer(width=2, interval=4.0))
    vote = gv.Gate1Vote(gv.VoteConfig(True, tuple(f"v{i}" for i in range(9)), timeout_sec=60.0))
    vote._hosts = {f"v{i}": "kenari.id" for i in range(9)}
    assert vote._bound(list(vote._hosts)) == 5 * 120 + 8 * 4 + gv.VOTE_SLACK_SEC
    vote._hosts = {"v0": "a", "v1": "b", "v2": "c"}               # one voter a host: no queue
    assert vote._bound(["v0", "v1", "v2"]) == 120 + gv.VOTE_SLACK_SEC


def test_a_fake_voter_has_no_queue():
    vote = gv.Gate1Vote(gv.VoteConfig(True, ("a", "b", "c"), timeout_sec=2.0), ask_fn=lambda *a: {})
    assert vote._bound(["a", "b", "c"]) == 4.0 + gv.VOTE_SLACK_SEC


def test_a_request_has_its_budget_from_its_slot(monkeypatch):
    """ask_v2's deadline bounds the queue; the request itself gets `budget` seconds once it starts."""
    cv = gv._load_claim_vote()
    seen = []
    monkeypatch.setattr(cv, "request_completion", lambda *a, **k: seen.append(k.get("deadline")) or "[]")
    monkeypatch.setattr(cv, "voter_settings", lambda ref, parser: type("S", (), dict(
        base_url="http://h/v1", api_key="k", model="m", api_format="openai", temperature=0, max_tokens=10,
        num_ctx=None, think=None))())
    parser = configparser.ConfigParser()
    parser.add_section(cv.SECTION)
    far = time.monotonic() + 1000
    cv.ask_v2("x", [("c", None)], 0, 0, parser, 1.0, batch=1, code_batch=1, deadline=far, budget=2.0)
    assert seen and seen[0] < far and seen[0] - time.monotonic() <= 2.0


# ── №44: the test-path rule ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path, test", [
    ("tests/test_a.py", True), ("tests_slow/test_big.py", True), ("stub-test/test_s.py", True),
    ("conftest.py", True), ("pkg/conftest.py", True), ("app/tests.py", True),
    ("web/a.test.js", True), ("web/a.spec.ts", True), ("ui/__tests__/a.js", True), ("src/test/java/A.java", True),
    ("a/b_test.go", True),
    ("tools/auto/a.py", False), ("tools/testing.py", False), ("", False), (None, False),
])
def test_which_paths_are_test_code(path, test):
    assert gv._is_test_path(path) is test


def test_a_pytest_shaped_name_is_a_test_only_when_it_holds_one(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "test_map.py").write_text("def build_test_map(root):\n    return {}\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "test_x.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "my_test.py").write_text("class TestThing:\n    pass\n", encoding="utf-8")
    view = PathRepoView(tmp_path)
    assert gv._is_test_path("tools/test_map.py", view) is False          # shipped code, it lost its vote
    assert gv._is_test_path("pkg/test_x.py", view) is True
    assert gv._is_test_path("pkg/my_test.py", view) is True
    assert gv._is_test_path("pkg/test_x.py") is True                     # no view: the name decides
