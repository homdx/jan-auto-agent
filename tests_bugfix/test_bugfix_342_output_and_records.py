"""tests_bugfix/test_bugfix_342_output_and_records.py — AGNES-3.

Printing and reading back a record are diagnostics. Each of the seven cases
below used to turn one into a failure of the real work: a closed stdout, an
ASCII console, or one damaged record (a BOM, a malformed metrics entry, a
null checkpoint field).

  1. AUTO-T45  AgentTracer console echo on a closed stdout raised ValueError.
  2. AUTO-T46  BacklogPrioritiser.build printed an emoji on an ASCII stdout.
  3. AUTO-T70  ValidatorAgent.validate's banner print killed the LLM call.
  4. AUTO-T92  flow_json dropped the detail flow_line shows.
  5. AUTO-T66  load_events lost the first event of a BOM-prefixed JSONL.
  6. AUTO-T94  summarize_failures crashed on a non-numeric iterations_used.
  7. AUTO-T95  a null search_result in resume state crashed run_pipeline.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from tools.agent_trace import AgentTracer
from tools.arena.output import flow_json
from tools.auto.backlog_prioritiser import BacklogPrioritiser
from tools.auto.view_trace import load_events
from tools.metrics_collector import MetricsCollector
from tools.validator_agent import ValidatorAgent

REPO_ROOT = Path(__file__).resolve().parent.parent


# ── shared fakes ─────────────────────────────────────────────────────────────

class _ClosedStdout:
    """A stdout whose every write/flush raises ValueError — a closed stream."""

    encoding = "utf-8"

    def write(self, s: str) -> int:
        raise ValueError("I/O operation on closed file")

    def flush(self) -> None:
        raise ValueError("I/O operation on closed file")


def _metrics_record(**overrides):
    r = {
        "timestamp": "2026-01-01 00:00:00",
        "intent": "improve",
        "prompt_version": "v1",
        "iterations_used": 3,
        "validator_status": "rejected",
        "validator_feedback": "bad",
        "improvement_json_ok": True,
        "elapsed_seconds": 1.0,
    }
    r.update(overrides)
    return r


# ── Output: closed stdout / ASCII console ────────────────────────────────────

def test_tracer_closed_stdout_console_echo_still_records(tmp_path):
    """AUTO-T45: event() on a closed stdout must not raise, and the event must
    still reach the trace file (only the diagnostic console echo is dropped)."""
    t = AgentTracer()
    t.configure(enabled=True, path=str(tmp_path / "trace.jsonl"), console_echo=True)
    real = sys.stdout
    sys.stdout = _ClosedStdout()
    try:
        t.event("orchestrator", "validator_agent", "result", content="x")
    finally:
        sys.stdout = real
    evts = load_events(tmp_path / "trace.jsonl")
    assert len(evts) == 1
    assert evts[0]["kind"] == "result"
    assert evts[0]["content"] == "x"


def test_backlog_ascii_stdout_completes(monkeypatch):
    """AUTO-T46: build() on an ASCII stdout must not die on its emoji banner."""
    buf = io.BytesIO()
    ascii_out = io.TextIOWrapper(buf, encoding="ascii")
    monkeypatch.setattr(sys, "stdout", ascii_out)
    backlog = BacklogPrioritiser().build([])
    assert backlog.auto_tasks == []
    assert backlog.manual_suggestions == []


def test_validator_stream_closed_stdout_still_calls_llm(monkeypatch):
    """AUTO-T70: a closed stdout must not turn the banner print into an
    _api_error that skips the LLM call — the call is made and the verdict
    returned; _api_error stays for real API failures."""
    import tools.validator_agent as va

    calls = []

    def fake_completion(url, headers, payload, timeout, **kw):
        calls.append(kw.get("stream"))
        return '{"status": "approved", "feedback": "ok", "suggested_searches": []}'

    monkeypatch.setattr(va, "request_completion", fake_completion)
    real = sys.stdout
    sys.stdout = _ClosedStdout()
    try:
        v = ValidatorAgent(stream=True)
        result = v.validate({
            "task": "improve handler in app.py",
            "target_block": "def handler():\n    pass\n",
            "imports": [],
            "related_code": {},
            "missing_refs": [],
            "iteration": 1,
        })
    finally:
        sys.stdout = real
    assert calls == [True], "the banner print must not swallow the LLM call"
    assert result["status"] == "approved"
    assert "_api_error" not in result


def test_flow_json_keeps_detail():
    """AUTO-T92: flow_json must carry the detail flow_line shows, and a
    two-element step must not get a detail key."""
    assert flow_json([("parse", "ok", "found 12")]) == [
        {"step": "parse", "state": "ok", "detail": "found 12"},
    ]
    two = flow_json([("a", "ok")])
    assert two == [{"step": "a", "state": "ok"}]
    assert "detail" not in two[0]


# ── Records: one damaged entry must not break the batch ──────────────────────

def test_load_events_utf8_bom(tmp_path):
    """AUTO-T66: a JSONL starting with a UTF-8 BOM must not lose its first
    event — both events load."""
    f = tmp_path / "trace.jsonl"
    f.write_bytes(b'\xef\xbb\xbf{"seq": 1}\n{"seq": 2}\n')
    evts = load_events(f)
    assert [e["seq"] for e in evts] == [1, 2]


def test_summarize_failures_damaged_iterations(tmp_path):
    """AUTO-T94: a "three" or null iterations_used must not crash the summary;
    such a record stays in total_runs but is left out of avg_iterations and of
    worst_intent (the average divides by the count of valid values)."""
    mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
    mc.metrics_path.write_text(json.dumps([
        _metrics_record(),
        _metrics_record(iterations_used="three"),
        _metrics_record(iterations_used=None),
    ]), encoding="utf-8")
    summary = mc.summarize_failures(n=10)
    assert summary["total_runs"] == 3
    assert summary["avg_iterations"] == 3.0
    assert summary["worst_intent"] == "improve"


def test_summarize_failures_no_valid_iterations(tmp_path):
    """AUTO-T94: when no record carries a usable iterations_used, the average
    is 0 and there is no worst intent — the summary still completes."""
    mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
    mc.metrics_path.write_text(json.dumps([
        _metrics_record(iterations_used="three", intent="improve"),
        _metrics_record(iterations_used=None, intent="fix"),
    ]), encoding="utf-8")
    summary = mc.summarize_failures(n=10)
    assert summary["total_runs"] == 2
    assert summary["avg_iterations"] == 0.0
    assert summary["worst_intent"] is None


def test_run_pipeline_null_search_result_resume(tmp_path, monkeypatch):
    """AUTO-T95: a null search_result in saved resume state must not crash
    run_pipeline — it is replaced by the default empty result where the state
    is read, before the formatter (or the improvement context) sees it."""
    import main as main_mod
    from main import Orchestrator

    orc = Orchestrator(config_path=str(REPO_ROOT / "agents_stub.ini"))
    orc.prompt_store.store_path = tmp_path / "prompts.json"

    class _FakeSearch:
        def run(self, **kw):
            return {"found": {}, "not_found": [], "searched_files": []}

    class _FakeValidator:
        max_hints = 3

        def validate(self, payload):
            return {"status": "approved", "feedback": "ok",
                    "suggested_searches": []}

    class _FakeImprovement:
        def process(self, intent, ctx):
            return {"explanation": "", "issues": [],
                    "improved_code": "", "changes": []}

    monkeypatch.setattr(orc, "search_agent", _FakeSearch())
    monkeypatch.setattr(orc, "validator_agent", _FakeValidator())
    monkeypatch.setattr(orc, "improvement_agent", _FakeImprovement())
    monkeypatch.setattr(orc.metrics_collector, "record", lambda *a, **k: None)
    monkeypatch.setattr(orc.metrics_collector, "summarize_failures",
                        lambda n=10: {
                            "total_runs": 0, "avg_iterations": 0.0,
                            "json_parse_failure_rate": 0.0,
                            "common_feedback": [], "worst_intent": None,
                        })

    captured = {}

    def fake_render(**kw):
        captured.update(kw)

    monkeypatch.setattr(main_mod.OutputFormatter, "render", fake_render)

    target = tmp_path / "app.py"
    target.write_text("def handler():\n    return 1\n", encoding="utf-8")

    # iteration > max_iterations skips the validation loop, so the null
    # search_result reaches the improvement context / formatter untouched.
    resume = {
        "loop": "run_pipeline", "iteration": 99, "refs": [],
        "already_searched": [], "search_result": None,
    }
    orc.run_pipeline("improve handler in app.py", str(tmp_path),
                     resume_state=resume)
    assert captured.get("search_result") == {
        "found": {}, "not_found": [], "searched_files": [],
    }


# ── What a narrow fix breaks: real failures stay failures, and normal output stays ───

class _Server:
    """A local chat endpoint: *status* for every request, and a count of them."""

    def __init__(self, status=200):
        import http.server
        import threading
        owner = self
        self.calls = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                owner.calls += 1
                body = json.dumps({"choices": [{"message": {"content": '{"status":"approved","feedback":""}'}}]}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"


_PAYLOAD = {"iteration": 1, "task": "t", "target_block": "c", "imports": [], "related_code": {}, "missing_refs": []}


def test_a_real_api_failure_on_a_closed_stdout_is_still_an_api_error(monkeypatch):
    import tools.validator_agent as module
    real = module.request_completion
    monkeypatch.setattr(module, "request_completion", lambda *a, **k: real(*a, **{**k, "error_retries": 0}))
    server = _Server(status=500)
    stream = io.StringIO()
    stream.close()
    monkeypatch.setattr(sys, "stdout", stream)
    result = ValidatorAgent(base_url=server.url, timeout=5).validate(dict(_PAYLOAD))
    assert server.calls == 1
    assert result["_api_error"] is True


def test_the_validator_banner_is_still_printed_on_an_open_stdout(monkeypatch):
    server = _Server()
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    result = ValidatorAgent(base_url=server.url, timeout=5).validate(dict(_PAYLOAD))
    assert result["status"] == "approved" and "validator_agent" in stream.getvalue()


def test_the_backlog_keeps_its_emoji_on_a_utf8_stdout(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    BacklogPrioritiser().build([])
    assert "📋" in stream.getvalue()


def test_a_stop_during_the_trace_echo_is_not_swallowed(tmp_path):
    tracer = AgentTracer()
    tracer.configure(True, str(tmp_path / "t.jsonl"), console_echo=True)

    def stop(*_a, **_k):
        raise KeyboardInterrupt()
    tracer._console_line = stop
    with pytest.raises(KeyboardInterrupt):
        tracer.event("a", "b", "result", content="x")


def test_a_secret_in_a_flow_detail_is_masked_and_folded():
    entry = flow_json([("a", "fail", "url https://u:s3cret@h/v1?api_key=s3cret\nnext")])[0]
    assert "s3cret" not in entry["detail"] and "\n" not in entry["detail"]


def test_an_empty_flow_detail_has_no_key_as_the_text_shows_none():
    assert flow_json([("a", "ok", "")]) == [{"step": "a", "state": "ok"}]


def test_a_truncated_multibyte_tail_after_a_bom_still_loads(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_bytes(b'\xef\xbb\xbf{"seq":1}\n{"seq":2,"c":"\xd0')
    assert [e["seq"] for e in load_events(path)] == [1]


def _summary(records):
    collector = MetricsCollector.__new__(MetricsCollector)
    collector._load_all = lambda: records
    return collector.summarize_failures(len(records))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "3", None])
def test_only_a_finite_number_counts_towards_the_average(bad):
    summary = _summary([{"iterations_used": bad, "intent": "a"}, {"iterations_used": 4, "intent": "b"}])
    assert summary["total_runs"] == 2 and summary["avg_iterations"] == 4.0 and summary["worst_intent"] == "b"


def test_a_record_with_no_iterations_key_still_counts_as_zero():
    assert _summary([{"intent": "a"}, {"iterations_used": 4, "intent": "a"}])["avg_iterations"] == 2.0
