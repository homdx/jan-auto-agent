"""335: the Architect's parallel requests over real HTTP, and what the ticket left unsaid.

`tests/test_architect_concurrency.py` drives a fake `_review_one_cluster`; these send the
real one to a local provider, and pin three things no entry of the round had together:
Ctrl-C does not run the queue first, concurrent tokens do not reach the console, and one
request at a time is still today's behaviour line for line.
"""

from __future__ import annotations

import configparser
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster


class _Provider:
    """An OpenAI-format provider: holds each reply until `hold` requests are in flight."""

    def __init__(self, hold=0):
        self.hold = hold
        self.cond = threading.Condition()
        self.inflight = self.peak = 0
        self.prompts = []
        self.timed_out = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                owner.answer(self, json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def answer(self, handler, body):
        text = body["messages"][-1]["content"]
        name = re.findall(r"\b(f\d+\.py)\b", text)[-1]
        with self.cond:
            self.prompts.append(text)
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            self.cond.notify_all()
            if self.hold and not self.cond.wait_for(lambda: self.peak >= self.hold, timeout=10):
                self.timed_out = True
        reply = json.dumps([{"title": f"T {name}", "instruction": "Do it.", "target_files": [name],
                             "acceptance_check": "pytest",
                             "cited_location": {"file": name, "symbol": "fn", "line_start": 1, "line_end": 1}}])
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.end_headers()
        for i in range(0, len(reply), 16):
            handler.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": reply[i:i + 16]}}]})
                                 + "\n\n").encode())
            handler.wfile.flush()
        handler.wfile.write(b"data: [DONE]\n\n")
        with self.cond:
            self.inflight -= 1


@pytest.fixture(autouse=True)
def _close_providers(monkeypatch):
    """Every provider a test opened is shut with it, its serving thread and its port."""
    opened = []
    real = _Provider.__init__

    def tracked(self, *args, **kwargs):
        real(self, *args, **kwargs)
        opened.append(self)
    monkeypatch.setattr(_Provider, "__init__", tracked)
    yield
    for provider in opened:
        provider.server.shutdown()
        provider.server.server_close()


def _reviewer(url, limit=None):
    cfg = configparser.ConfigParser()
    architect = {"temperature": "0.2", "max_tokens": "512", "max_files_per_review": "1"}
    if limit is not None:
        architect["max_parallel_requests"] = str(limit)
    cfg.read_dict({"api": {"active": "local"}, "architect": architect, "loop": {"timeout_seconds": "20"},
                   "api_local": {"base_url": url, "api_key": "k", "model": "m", "api_format": "openai"}})
    return ClusterReviewer(config=cfg, base_url=url, api_key="k", model="m", api_format="openai", verify_ssl=False)


def _clusters(tmp_path, count=6):
    for i in range(count):
        (tmp_path / f"f{i}.py").write_text(f"def fn():\n    return {i}\n", encoding="utf-8")
    return [RepoCluster(name=f"c{i}", patterns=[], files=[f"f{i}.py"]) for i in range(count)]


def _plain(candidates):
    return [(c.title, c.instruction, c.cluster, c.target_files) for c in candidates]


def test_three_held_requests_complete_only_because_they_overlap(tmp_path):
    provider = _Provider(hold=3)
    out = _reviewer(provider.url, 3).review_clusters(_clusters(tmp_path), tmp_path, "goal")
    assert provider.peak == 3 and not provider.timed_out
    assert [c.cluster for c in out] == [f"c{i}" for i in range(6)]


def test_the_plan_and_the_prompts_are_the_same_either_way(tmp_path):
    serial, parallel = _Provider(), _Provider(hold=3)
    plan_serial = _reviewer(serial.url).review_clusters(_clusters(tmp_path), tmp_path, "goal")
    plan_parallel = _reviewer(parallel.url, 3).review_clusters(_clusters(tmp_path), tmp_path, "goal")
    assert serial.peak == 1 and parallel.peak == 3
    assert _plain(plan_serial) == _plain(plan_parallel)
    assert sorted(serial.prompts) == sorted(parallel.prompts)


def test_concurrent_tokens_do_not_reach_the_console(tmp_path, capsys):
    _reviewer(_Provider(hold=3).url, 3).review_clusters(_clusters(tmp_path), tmp_path, "goal")
    out = capsys.readouterr().out
    assert '"title"' not in out and "LIVE ARCHITECT" not in out
    # what replaces them: one line a batch, one named total a cluster
    assert all(f"Reviewed 'c{i}': 1 candidate(s)" in out and f"[c{i}] 1 grounded" in out for i in range(6))


def test_one_request_at_a_time_still_streams_in_todays_order(tmp_path, capsys):
    _reviewer(_Provider().url).review_clusters(_clusters(tmp_path, 2), tmp_path, "goal")
    out = capsys.readouterr().out
    assert "LIVE ARCHITECT" in out and '"title"' in out and "Reviewed '" not in out
    assert out.index("grounded candidate") < out.index("[c1]")


def test_a_non_positive_setting_is_malformed_and_says_so(tmp_path, caplog):
    for bad in ("0", "-3", "2.5", "abc", ""):
        caplog.clear()
        assert _reviewer("http://127.0.0.1:9/v1", bad)._max_parallel_requests == 1
        assert "max_parallel_requests is malformed" in caplog.text


def test_ctrl_c_does_not_run_the_queue_first(tmp_path):
    (tmp_path / "x.py").write_text("x\n", encoding="utf-8")
    reviewer = _reviewer("http://127.0.0.1:9/v1", 2)
    calls = []

    def review(cluster, base_dir, goal, all_files=None):
        calls.append(cluster.name)
        if len(calls) == 1:
            raise KeyboardInterrupt  # the pool hands it to the waiting thread, as a signal would land there
        time.sleep(0.2)
        return []
    reviewer._review_one_cluster = review
    clusters = [RepoCluster(name=f"c{i}", patterns=[], files=["x.py"]) for i in range(30)]
    with pytest.raises(KeyboardInterrupt):
        reviewer.review_clusters(clusters, tmp_path, "goal")
    assert len(calls) < 10


def test_a_built_probe_runs_the_whole_plan_one_request_at_a_time(tmp_path, caplog):
    class Probe:
        usable = False  # `_review_one_cluster` writes to any probe it was given, usable or not

        def __getattr__(self, name):
            return lambda *a, **k: ""

    provider = _Provider()
    reviewer = _reviewer(provider.url, 3)
    reviewer._probe_enabled = True
    reviewer._get_probe = lambda base_dir: Probe()
    assert len(reviewer.review_clusters(_clusters(tmp_path), tmp_path, "goal")) == 6
    assert provider.peak == 1 and "concurrency limited to 1" in caplog.text
