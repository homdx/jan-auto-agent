"""tests/test_coder_transport_failure.py — RUN-8: a transport failure mid-stream
is charged to the coder, not to the task.

``request_completion`` retries a stream that dies BEFORE the first token, but
once tokens have been emitted it raises
``RuntimeError("TimeoutError reading stream from …: … (after 0 retries, tokens
already emitted)")`` — a stream cannot be resumed. The coder caught that like
any other error and came back ``succeeded=False``, so the inner loop charged
the attempt, put "attempt N: coder failed" in the feedback (the next prompt
told the model *it* failed), traced the stage ``REJECTED`` (the snapshot read
that as a cut-off and pointed the RUN-4 ladder at a budget that was never the
problem), and burned the wall-clock spent waiting on the dead socket out of the
task's ``max_task_seconds`` — one 80-minute silence (23:37 → 00:58 on
../testtext2, run baa9da87a2ab) ended AUTO-T5 after a single round with a
knowledge file that said the coder failed, when the coder had produced nothing
at all.

ACs (ticket 36):
  * every call a transport failure → ``run_task`` ends ``unavailable=True``,
    ``attempts_used == 0``, the coder is called ``1 + coder_transport_retries``
    times, no "coder failed" line in the feedback, no ``REJECTED`` coder event,
    ``TRANSPORT`` events present with the budget the call went out at
  * one failure, then a valid reply → the task passes on attempt 1 and the
    retry went out at the same ``max_tokens`` (no spurious ladder climb)
  * NO-JSON is still ``error_kind == "parse"``, still consumes the attempt, and
    the RUN-4 ladder still climbs (``tests/test_run4_coder_budget_ladder.py``
    is untouched)
  * deadline credit: 8 s inside a transport call moves the deadline 8 s; the
    same 8 s inside a reply that came back does not
  * outer loop: the task goes back to ``todo`` with no feedback file, and the
    shared ``_task_deadline`` gets the seconds back too
  * controller: no knowledge note, no ticket, ``validator_status`` =
    unavailable (no verdict was reached)

Fully offline: the coder is the real ``Coder`` over a stubbed
``request_completion``, or a fake object — no provider is ever contacted.
"""
from __future__ import annotations

import configparser
import importlib.util
import json
import sys
import time as _real_time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.auto.inner_loop as inner_loop_mod
import tools.auto.outer_loop as outer_loop_mod
import tools.llm_stream as llm_stream
from tools.agent_trace import tracer
from tools.auto.coder import Coder, CoderResult
from tools.auto.controller import AutoController, RunLimits
from tools.auto.inner_loop import InnerLoop, InnerLoopResult, make_inner_loop
from tools.auto.outer_loop import OuterLoop, OuterLoopResult
from tools.auto.state import StateStore, make_task, STATUS_DONE, STATUS_TODO


# ── the reply shapes the live run produced ───────────────────────────────────

PATH = "pkg/mod.py"
TRANSPORT_ERROR = (
    "TimeoutError reading stream from https://token.sensenova.ai/v1/chat/completions: "
    "The read operation timed out (after 0 retries, tokens already emitted)"
)
COMPLETE_JSON = json.dumps(
    {"files": [{"path": PATH, "content": "def f():\n    return 1\n"}]}
)
NO_JSON = "Sorry, I cannot produce that file — could you clarify the acceptance check?"
# Unterminated string, no closing brace: the decode error is "Unterminated
# string" and the text does not end with "}", so _parse_response calls it a
# truncation — the shape that climbs the RUN-4 ladder.
CUT_OFF_JSON = '{"files": [{"path": "pkg/mod.py", "content": "def f():\\n    return '

TASK = {"id": "AUTO-T5", "title": "module", "instruction": "rewrite pkg/mod.py",
        "target_files": [PATH]}


def _config(max_tokens: int = 4096) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url": "http://localhost:1337/v1",
                "api_key": "test",
                "model": "test-model",
                "api_format": "openai",
            },
            "loop": {"timeout_seconds": "300"},
            "coder": {"temperature": "0.2", "max_tokens": str(max_tokens)},
        }
    )
    return cfg


def _make_coder(max_tokens: int = 4096) -> Coder:
    return Coder(config=_config(max_tokens), base_url="http://localhost:1337/v1",
                 api_key="test", model="test-model", api_format="openai",
                 verify_ssl=False)


# ── fakes ────────────────────────────────────────────────────────────────────

@dataclass
class FakeCoderResult:
    succeeded: bool = True
    files_written: list = field(default_factory=lambda: [PATH])
    files_skipped: list = field(default_factory=list)
    error: str = ""
    raw_response: str = ""
    missing_context: list = field(default_factory=list)
    context_satisfied: bool = True
    max_tokens: int = 4096
    budget_raised: bool = False
    error_kind: str = ""


def _transport(err: str = f"LLM call failed: {TRANSPORT_ERROR}",
               budget: int = 4096) -> FakeCoderResult:
    return FakeCoderResult(succeeded=False, files_written=[], error=err,
                           max_tokens=budget, error_kind="transport")


def _parse_fail(err: str = f"LLM call failed: {NO_JSON}",
                budget: int = 4096) -> FakeCoderResult:
    return FakeCoderResult(succeeded=False, files_written=[], error=err,
                           max_tokens=budget, error_kind="parse")


class FakeCoder:
    """Scripted coder. Each item is a result or an Exception to raise, or a
    ``(result, seconds)`` pair that also advances the fake clock inside the
    call. Records every ``prior_feedback`` list it was handed."""
    def __init__(self, script):
        self._script = list(script)
        self.prior_feedback_seen = []
        self.calls = 0
        self.clock = None            # FakeClock advanced inside generate()

    def _next(self):
        if not self._script:
            return FakeCoderResult(), 0.0
        item = self._script.pop(0)
        if isinstance(item, tuple):
            return item[0], float(item[1])
        return item, 0.0

    def generate(self, task, base_dir, prior_feedback=None, **kw):
        self.calls += 1
        self.prior_feedback_seen.append(list(prior_feedback or []))
        result, seconds = self._next()
        if self.clock is not None and seconds > 0:
            self.clock.advance(seconds)
        if isinstance(result, BaseException):
            raise result
        return result


class FailingExecutor:
    def run(self, task):
        return SimpleNamespace(passed=False, exit_code=1, stdout="", stderr="boom",
                               traceback="boom", command="python3 -m pytest",
                               timed_out=False)


class PassingExecutor:
    def run(self, task):
        return SimpleNamespace(passed=True, exit_code=0, stdout="", stderr="",
                               traceback="", command="python3 -m pytest",
                               timed_out=False)


class ApprovingValidator:
    def approve(self, task, exec_result, coder_result, *, base_dir=None,
                prior_critique=""):
        return True, ""


class ScriptedCoderProvider:
    """``request_completion`` stand-in for the real coder. Each script item is
    an Exception to raise or a reply string. Records the budget every call went
    out at, so a retry is provably at the same tier."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.max_tokens_seen = []
        self.user_messages = []

    def __call__(self, url, headers, payload, timeout, stream=True, on_token=None,
                 api_format="openai", ssl_context=None, **kw):
        self.calls += 1
        self.max_tokens_seen.append(payload.get("max_tokens"))
        self.user_messages.append(payload["messages"][-1]["content"])
        item = self.script.pop(0) if self.script else COMPLETE_JSON
        if isinstance(item, BaseException):
            raise item
        return item


class FakeClock:
    """A monotonic() a test can advance."""
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ClockShim:
    """Stands in for the ``time`` module in one module only: ``monotonic`` and
    ``sleep`` are the fake, everything else is the real module. Patching
    ``time.monotonic`` globally would also move the clock pytest itself reads."""
    def __init__(self, real, clock: FakeClock, sleep=lambda _s: None):
        self._real = real
        self.monotonic = clock
        self.sleep = sleep

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    # Autouse: [collect] error_retry_wait_sec defaults to 60 s and every
    # transport retry sleeps it — with the real sleep this file took ~3
    # minutes under xdist and a full minute per test standalone.
    slept = []
    monkeypatch.setattr(inner_loop_mod.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def traced(monkeypatch):
    """Capture every stage-decision event; params stay in their native types
    (the real tracer stringifies them)."""
    events = []

    def _capture(source, target, kind, params=None, content=None, **kw):
        events.append({"source": source, "target": target, "kind": kind,
                       "content": content, "params": dict(params or {})})

    monkeypatch.setattr(tracer, "event", _capture)
    return events


def _coder_decisions(events):
    return [e for e in events
            if e["kind"] == "decision" and e["source"] == "coder"
            and e["target"] == "inner_loop"]


# ── 1. the classification in Coder.generate ─────────────────────────────────

class TestClassification:
    def test_stream_timeout_is_transport(self, tmp_path):
        coder = _make_coder()
        with patch("tools.llm_stream.request_completion",
                   side_effect=RuntimeError(TRANSPORT_ERROR)):
            result = coder.generate(TASK, tmp_path)
        assert result.error_kind == "transport"
        assert result.succeeded is False
        assert result.error == f"LLM call failed: {TRANSPORT_ERROR}"
        assert result.files_written == []
        assert result.max_tokens == 4096
        assert result.budget_raised is False

    @pytest.mark.parametrize("exc", [
        TimeoutError("The read operation timed out"),
        RuntimeError("TimeoutError reading stream from http://x: boom"),
        urllib.error.URLError("connection refused"),
        urllib.error.HTTPError("http://x", 502, "Bad Gateway", {}, None),
    ])
    def test_every_exception_out_of_the_call_is_transport(self, tmp_path, exc):
        coder = _make_coder()
        with patch("tools.llm_stream.request_completion", side_effect=exc):
            result = coder.generate(TASK, tmp_path)
        assert result.error_kind == "transport", type(exc).__name__
        assert result.succeeded is False

    def test_no_json_is_parse_and_does_not_climb(self, tmp_path):
        coder = _make_coder(max_tokens=3000)
        provider = ScriptedCoderProvider([NO_JSON, NO_JSON])
        with patch("tools.llm_stream.request_completion", side_effect=provider):
            results = [coder.generate(TASK, tmp_path) for _ in range(2)]
        assert all(r.error_kind == "parse" for r in results)
        assert all(r.budget_raised is False for r in results)
        assert provider.max_tokens_seen == [3000, 3000]

    def test_cut_off_is_parse_and_still_climbs_the_ladder(self, tmp_path):
        coder = _make_coder(max_tokens=3000)
        provider = ScriptedCoderProvider([CUT_OFF_JSON, COMPLETE_JSON])
        with patch("tools.llm_stream.request_completion", side_effect=provider):
            first = coder.generate(TASK, tmp_path)
            second = coder.generate(TASK, tmp_path)
        assert first.error_kind == "parse"
        assert first.budget_raised is True
        assert first.max_tokens == 3000
        assert provider.max_tokens_seen == [3000, 6000], "the ladder still climbs"
        assert second.succeeded is True and second.error_kind == ""

    def test_malformed_json_is_parse(self, tmp_path):
        coder = _make_coder()
        with patch("tools.llm_stream.request_completion",
                   return_value='{"files": [}'):
            result = coder.generate(TASK, tmp_path)
        assert result.error_kind == "parse"
        assert result.budget_raised is False

    def test_a_successful_reply_has_no_kind(self, tmp_path):
        coder = _make_coder()
        with patch("tools.llm_stream.request_completion", return_value=COMPLETE_JSON):
            result = coder.generate(TASK, tmp_path)
        assert result.succeeded is True
        assert result.error_kind == ""
        assert (tmp_path / PATH).exists()

    def test_a_hand_built_result_has_no_kind(self):
        assert CoderResult(task_id="T", files_written=[], error="need ctx").error_kind == ""
        assert CoderResult().error_kind == ""


# ── 2. the inner loop: a transport failure is not an attempt ────────────────

def _loop(script, *, max_attempts=5, **kw):
    coder = FakeCoder(script)
    loop = InnerLoop(coder, FailingExecutor(), ApprovingValidator(),
                     max_attempts=max_attempts, **kw)
    return loop, coder


class TestTransportIsNotAnAttempt:
    def test_every_call_transport_leaves_the_round_unreviewed(self, tmp_path, traced):
        loop, coder = _loop([_transport()] * 4, coder_transport_retries=2)
        r = loop.run_task(TASK, tmp_path)

        assert r.unavailable is True and r.passed is False
        assert r.attempts_used == 0, "an attempt that produced nothing is not charged"
        assert r.records == []
        assert r.last_feedback == ""
        assert r.unavailable_stage == "coder"
        assert r.unavailable_reason == f"LLM call failed: {TRANSPORT_ERROR}"
        assert coder.calls == 3, "one call + coder_transport_retries (2)"

        fb = r.last_feedback + "\n".join(rec.feedback for rec in r.records)
        assert "coder failed" not in fb and "coder error" not in fb
        decisions = _coder_decisions(traced)
        assert [d["content"] for d in decisions] == ["TRANSPORT"] * 3
        for d in decisions:
            assert d["params"]["stage"] == "coder"
            assert d["params"]["attempt"] == 1
            assert d["params"]["max_tokens"] == 4096
            assert d["params"]["budget_raised"] is False
            assert d["params"]["calls"] == 3
            assert TRANSPORT_ERROR in d["params"]["error"]

    def test_a_real_coder_classifies_its_own_outage(self, tmp_path, monkeypatch,
                                                    no_sleep, traced):
        provider = ScriptedCoderProvider([RuntimeError(TRANSPORT_ERROR)] * 5)
        monkeypatch.setattr(llm_stream, "request_completion", provider)
        coder = _make_coder()
        loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=5)
        r = loop.run_task(TASK, tmp_path)

        assert r.unavailable is True and r.attempts_used == 0 and r.records == []
        assert provider.calls == 3
        decisions = _coder_decisions(traced)
        assert [d["content"] for d in decisions] == ["TRANSPORT"] * 3
        assert all(d["content"] != "REJECTED" for d in decisions)

    def test_one_failure_then_a_valid_reply_passes_on_attempt_one(
            self, tmp_path, monkeypatch, no_sleep, traced):
        provider = ScriptedCoderProvider([RuntimeError(TRANSPORT_ERROR), COMPLETE_JSON])
        monkeypatch.setattr(llm_stream, "request_completion", provider)
        coder = _make_coder(max_tokens=3000)
        loop = InnerLoop(coder, PassingExecutor(), ApprovingValidator(), max_attempts=5)
        r = loop.run_task(TASK, tmp_path)

        assert r.passed is True and r.unavailable is False and r.attempts_used == 1
        assert r.records[0].passed is True
        assert provider.calls == 2
        assert provider.max_tokens_seen == [3000, 3000], \
            "the retry went out at the same tier — no spurious ladder climb"
        (decision,) = _coder_decisions(traced)
        assert decision["content"] == "TRANSPORT"
        assert decision["params"]["max_tokens"] == 3000
        assert decision["params"]["budget_raised"] is False

    def test_zero_retries_is_one_call_and_still_no_charge(self, tmp_path, traced, no_sleep):
        loop, coder = _loop([_transport()], coder_transport_retries=0)
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is True and r.attempts_used == 0
        assert coder.calls == 1
        assert [d["content"] for d in _coder_decisions(traced)] == ["TRANSPORT"]

    def test_configured_wait_is_honoured(self, tmp_path, no_sleep):
        loop, coder = _loop([_transport()] * 3, coder_transport_wait_sec=2.5)
        loop.run_task(TASK, tmp_path)
        assert no_sleep == [2.5, 2.5]

    def test_no_feedback_line_and_prior_feedback_untouched(self, tmp_path):
        loop, coder = _loop([_transport()] * 3)
        r = loop.run_task(TASK, tmp_path,
                          prior_feedback=["attempt 1: exec failed (exit 1)"])
        assert len(coder.prior_feedback_seen) == 3
        for seen in coder.prior_feedback_seen:
            assert seen == ["attempt 1: exec failed (exit 1)"], \
                "a transport retry reuses the same prompt, unchanged"
            assert "coder failed" not in " ".join(seen)
        assert r.unavailable is True

    def test_attempts_that_reached_a_reply_are_still_counted(self, tmp_path, traced,
                                                              no_sleep):
        """attempt 1: a NO-JSON reply (a real failure); attempt 2: the socket
        dies on every re-run."""
        loop, coder = _loop([_parse_fail(), _transport(), _transport(), _transport()])
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is True and r.passed is False
        assert r.attempts_used == 1
        assert [rec.attempt_num for rec in r.records] == [1]
        assert "coder failed" in r.records[0].feedback
        assert r.last_feedback == "", "the outage itself wrote no line"
        contents = [d["content"] for d in _coder_decisions(traced)]
        assert contents.count("REJECTED") == 1 and contents.count("TRANSPORT") == 3

    def test_legacy_coder_without_the_field_is_a_charged_attempt(self, tmp_path, traced):
        class LegacyCoder:                   # older fakes: no error_kind at all
            def generate(self, task, base_dir, prior_feedback=None, **kwargs):
                return SimpleNamespace(succeeded=False, error="no files written")

        loop = InnerLoop(LegacyCoder(), FailingExecutor(), ApprovingValidator(),
                         max_attempts=3)
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is False and r.passed is False
        assert r.attempts_used == 3
        assert _coder_decisions(traced)[0]["content"] == "REJECTED"

    def test_a_coder_that_raises_is_still_a_charged_error_attempt(self, tmp_path, traced):
        loop, coder = _loop([RuntimeError("programming error, not an outage")] * 5)
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is False and r.passed is False
        assert r.attempts_used == 5, "a raising coder is charged, one attempt per call"
        assert "coder error" in r.records[0].feedback
        assert _coder_decisions(traced)[0]["content"] == "ERROR"

    def test_a_malformed_budget_on_a_transport_result_does_not_raise(
            self, tmp_path, traced):
        loop, coder = _loop([_transport(budget="n/a"), _transport(budget=None),
                             _transport(budget=4096)])
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is True and r.attempts_used == 0 and coder.calls == 3
        decisions = _coder_decisions(traced)
        assert [d["content"] for d in decisions] == ["TRANSPORT"] * 3
        assert "max_tokens" not in decisions[0]["params"]
        assert "max_tokens" not in decisions[1]["params"]
        assert decisions[2]["params"]["max_tokens"] == 4096


# ── 3. the wait is not the task's: deadline credit ───────────────────────────

class TestDeadlineCredit:
    def test_transport_seconds_are_credited_back_to_the_deadline(self, monkeypatch):
        clock = FakeClock(0.0)
        monkeypatch.setattr(inner_loop_mod, "time", ClockShim(_real_time, clock))

        loop = InnerLoop(FakeCoder([(_transport(), 8.0), (_transport(), 8.0),
                                    _parse_fail(), _parse_fail(), _parse_fail(),
                                    _parse_fail(), _parse_fail()]),
                         FailingExecutor(), ApprovingValidator(),
                         max_attempts=5, max_task_seconds=10,
                         coder_transport_retries=2, coder_transport_wait_sec=0)
        loop.coder.clock = clock
        r = loop.run_task(TASK, Path("."))

        assert clock.now == 16.0, "16 s were spent inside the failing calls"
        assert clock.now > 10, "the raw budget is gone — only the credit kept it alive"
        assert r.unavailable is False and r.passed is False
        assert r.attempts_used == 5, "all five attempts ran: the deadline moved 16 s"
        assert r.deadline_credit_s == 16.0

    def test_seconds_inside_a_reply_that_came_back_are_still_charged(self, monkeypatch):
        clock = FakeClock(0.0)
        monkeypatch.setattr(inner_loop_mod, "time", ClockShim(_real_time, clock))

        loop = InnerLoop(FakeCoder([(_parse_fail(), 8.0), (_parse_fail(), 8.0),
                                    _parse_fail(), _parse_fail(), _parse_fail()]),
                         FailingExecutor(), ApprovingValidator(),
                         max_attempts=5, max_task_seconds=10,
                         coder_transport_retries=2, coder_transport_wait_sec=0)
        loop.coder.clock = clock
        r = loop.run_task(TASK, Path("."))

        assert clock.now == 16.0
        assert r.deadline_credit_s == 0.0, "a reply was produced — the wait is the task's"
        assert r.unavailable is False and r.attempts_used == 2, \
            "the wall-clock guard stops the round once the raw budget is spent"

    def test_no_deadline_means_no_credit_to_record(self, tmp_path, monkeypatch):
        clock = FakeClock(0.0)
        monkeypatch.setattr(inner_loop_mod, "time", ClockShim(_real_time, clock))
        loop, coder = _loop([(_transport(), 8.0)] * 3, max_task_seconds=0)
        loop.coder.clock = clock
        r = loop.run_task(TASK, tmp_path)
        assert r.unavailable is True and r.attempts_used == 0
        assert r.deadline_credit_s == 0.0
        assert clock.now == 24.0

    def test_credit_is_reported_when_the_round_passes(self, monkeypatch):
        clock = FakeClock(0.0)
        monkeypatch.setattr(inner_loop_mod, "time", ClockShim(_real_time, clock))
        ok = FakeCoderResult(succeeded=True, files_written=[PATH])
        loop = InnerLoop(FakeCoder([(_transport(), 8.0), ok]), PassingExecutor(),
                         ApprovingValidator(), max_attempts=2, max_task_seconds=10)
        loop.coder.clock = clock
        r = loop.run_task(TASK, Path("."))
        assert r.passed is True and r.attempts_used == 1
        assert r.deadline_credit_s == 8.0


# ── 4. outer loop: left todo, nothing written, the budget is credited ────────

def _unavailable_result(**kw):
    base = dict(task_id="AUTO-T5", passed=False, attempts_used=0, last_feedback="",
                records=[], unavailable=True, unavailable_stage="coder",
                unavailable_reason=f"LLM call failed: {TRANSPORT_ERROR}")
    base.update(kw)
    return InnerLoopResult(**base)


def _failed_result(text, **kw):
    base = dict(task_id="AUTO-T5", passed=False, attempts_used=1, last_feedback=text,
                records=[])
    base.update(kw)
    return InnerLoopResult(**base)


class FakeInnerLoop:
    def __init__(self, results, max_task_seconds=0, clock=None, seconds_per_call=0.0):
        self._results = list(results)
        self.max_task_seconds = max_task_seconds
        self.clock = clock
        self.seconds_per_call = seconds_per_call
        self.calls = 0

    def run_task(self, task, base_dir, prior_feedback=None,
                 prior_implementations=None, deadline=None):
        self.calls += 1
        if self.clock is not None and self.seconds_per_call > 0:
            self.clock.advance(self.seconds_per_call)
        return self._results.pop(0)


def _state(tmp_path) -> StateStore:
    st = StateStore(tmp_path / ".agent")
    st.initialise("goal", tmp_path)
    st.upsert_task(make_task(id="AUTO-T5", title="t", instruction="x",
                             target_files=[PATH]))
    return st


class TestOuterLoopLeavesTodo:
    def test_fresh_task_goes_back_to_todo_with_nothing_written(self, tmp_path):
        st = _state(tmp_path)
        rewriter = MagicMock()
        res = OuterLoop(FakeInnerLoop([_unavailable_result()]), st, max_rounds=10,
                        task_rewriter=rewriter).run_task(TASK, tmp_path)

        assert res.unavailable is True and res.passed is False and res.exhausted is False
        assert res.unavailable_stage == "coder"
        assert res.rounds_used == 0 and res.feedback_files == []
        assert res.impl_versions_used == []
        assert "coder transport" in res.summary()
        t = st.get_task("AUTO-T5")
        assert t["status"] == STATUS_TODO
        assert t["round"] == 0 and t["impl_version"] == 1
        assert not list(st.task_dir("AUTO-T5").glob("feedback_round_*.md"))
        assert not list(st.task_dir("AUTO-T5").glob("rewrite_round_*.md"))
        assert not (st.task_dir("AUTO-T5") / "knowledge.md").exists()
        rewriter.rewrite.assert_not_called()

    def test_resumed_task_keeps_its_completed_rounds_and_resumes_there(self, tmp_path):
        st = _state(tmp_path)
        inner = FakeInnerLoop([_failed_result("err A"), _failed_result("err B"),
                               _unavailable_result()])
        res = OuterLoop(inner, st, max_rounds=10).run_task(TASK, tmp_path)
        assert res.unavailable is True and res.rounds_used == 2
        assert sorted(p.name for p in st.task_dir("AUTO-T5").glob("feedback_round_*.md")) == \
            ["feedback_round_1.md", "feedback_round_2.md"]
        t = st.get_task("AUTO-T5")
        assert t["status"] == STATUS_TODO and t["round"] == 2

        again = FakeInnerLoop([InnerLoopResult(task_id="AUTO-T5", passed=True,
                                               attempts_used=1)])
        res2 = OuterLoop(again, st, max_rounds=10).run_task(TASK, tmp_path)
        assert res2.passed and res2.rounds_used == 3

    def test_the_shared_deadline_gets_the_seconds_back(self, tmp_path, monkeypatch):
        clock = FakeClock(0.0)
        monkeypatch.setattr(outer_loop_mod, "time", ClockShim(_real_time, clock))
        # Round 1 spends 12 s inside a dead coder call and gives them back;
        # round 2 then runs. Without the credit the 12 s would have hit the
        # 10 s budget and blocked the task before round 2.
        inner = FakeInnerLoop(
            [_failed_result("boom", deadline_credit_s=12.0),
             InnerLoopResult(task_id="AUTO-T5", passed=True, attempts_used=1)],
            max_task_seconds=10, clock=clock, seconds_per_call=12.0,
        )
        st = _state(tmp_path)
        res = OuterLoop(inner, st, max_rounds=5).run_task(TASK, tmp_path)
        # 12 s per round × 2 rounds; the round-1 credit is what let round 2
        # start (12 s of spend against a 10 s budget).
        assert clock.now == 24.0
        assert res.passed is True and res.rounds_used == 2
        assert inner.calls == 2
        assert st.get_task("AUTO-T5")["status"] == STATUS_DONE
        assert list(st.task_dir("AUTO-T5").glob("feedback_round_*.md")) == \
            [st.task_dir("AUTO-T5") / "feedback_round_1.md"]

    def test_the_persisted_ledger_does_not_keep_the_dead_socket_minutes(
            self, tmp_path, monkeypatch):
        # The live case: 80 minutes on a silent socket, every retry dead, task
        # left todo. RUN-2's ledger (deadline_started_at.txt) is fed from the
        # session's wall clock, so without the credit the next run would open
        # the task with its whole max_task_seconds already consumed and stop
        # before a single call.
        clock = FakeClock(0.0)
        shim = ClockShim(_real_time, clock)
        shim.time = clock          # the ledger is wall clock, not monotonic
        monkeypatch.setattr(outer_loop_mod, "time", shim)
        st = _state(tmp_path)
        inner = FakeInnerLoop(
            [_unavailable_result(deadline_credit_s=4800.0)],
            max_task_seconds=3600, clock=clock, seconds_per_call=4800.0,
        )
        res = OuterLoop(inner, st, max_rounds=5).run_task(TASK, tmp_path)
        assert res.unavailable is True
        consumed, _legacy = outer_loop_mod.parse_budget_file(
            st.read_task_file("AUTO-T5", outer_loop_mod._BUDGET_FILE), clock.now, 3600)
        assert consumed == 0.0, "the wait on the dead socket is not the task's"

        # The next run has its full budget: the round runs and passes.
        again = FakeInnerLoop(
            [InnerLoopResult(task_id="AUTO-T5", passed=True, attempts_used=1)],
            max_task_seconds=3600, clock=clock, seconds_per_call=30.0,
        )
        res2 = OuterLoop(again, st, max_rounds=5).run_task(TASK, tmp_path)
        assert res2.passed is True and again.calls == 1
        consumed, _legacy = outer_loop_mod.parse_budget_file(
            st.read_task_file("AUTO-T5", outer_loop_mod._BUDGET_FILE), clock.now, 3600)
        assert consumed == 30.0, "time that produced a reply is still charged"

    def test_a_charged_round_still_reaches_the_ledger(self, tmp_path, monkeypatch):
        clock = FakeClock(0.0)
        shim = ClockShim(_real_time, clock)
        shim.time = clock
        monkeypatch.setattr(outer_loop_mod, "time", shim)
        st = _state(tmp_path)
        inner = FakeInnerLoop(
            [_failed_result("err A"), _failed_result("err B")],
            max_task_seconds=3600, clock=clock, seconds_per_call=12.0,
        )
        OuterLoop(inner, st, max_rounds=2).run_task(TASK, tmp_path)
        consumed, _legacy = outer_loop_mod.parse_budget_file(
            st.read_task_file("AUTO-T5", outer_loop_mod._BUDGET_FILE), clock.now, 3600)
        assert consumed == 24.0

    def test_a_legacy_result_without_the_stage_keeps_the_validator_wording(
            self, tmp_path):
        st = _state(tmp_path)
        res = OuterLoop(
            FakeInnerLoop([InnerLoopResult(task_id="AUTO-T5", passed=False,
                                           attempts_used=0, last_feedback="",
                                           records=[], unavailable=True,
                                           unavailable_reason="validator unavailable")]),
            st, max_rounds=5).run_task(TASK, tmp_path)
        assert res.unavailable is True and res.unavailable_stage == ""
        assert res.summary() == "[AUTO-T5] LEFT TODO — validator unavailable"


# ── 5. controller: no knowledge, no ticket, an unavailable metric row ────────

def _controller(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "test"
    ctrl.base_dir = base
    ctrl.config_path = "agents.ini"
    ctrl.task_mode = "code"
    ctrl.dry_run = False
    ctrl.agent_dir = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    ctrl._time_fn = _real_time.monotonic
    ctrl._start_time = _real_time.monotonic()
    ctrl.limits = RunLimits(max_runtime_sec=0, max_tasks_per_run=0)
    ctrl.state = StateStore(ctrl.agent_dir)
    ctrl.state.initialise("test", base)
    ctrl.state.upsert_task(make_task(id="AUTO-T5", title="t", instruction="x",
                                     target_files=[PATH]))
    ctrl.git = MagicMock()
    ctrl.run_trace = MagicMock()
    ctrl.progress_display = MagicMock()
    ctrl.metrics_stream = MagicMock()
    ctrl.auto_tuner = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
        promoted=False, new_prompt_score=0.0)
    return ctrl


class _ScriptedOuter:
    def __init__(self, result):
        self.result = result

    def run_task(self, task, base_dir, **kw):
        return self.result


def _run(ctrl, outer_result):
    with patch("tools.auto.outer_loop.make_outer_loop",
               return_value=_ScriptedOuter(outer_result)), \
         patch("tools.auto.commit_on_success.CommitOnSuccess"):
        return ctrl._run_task_loop()


class TestControllerCoderTransportPath:
    def test_no_knowledge_no_ticket_honest_metric(self, tmp_path):
        ctrl = _controller(tmp_path)
        result = OuterLoopResult(
            "AUTO-T5", False, 0, False, [], [_unavailable_result()], [],
            unavailable=True, unavailable_stage="coder")
        stop, done = _run(ctrl, result)

        assert stop is None and done == 0
        assert not (ctrl.agent_dir / "tasks" / "AUTO-T5" / "knowledge.md").exists()
        assert not list((ctrl.agent_dir / "tickets").glob("*"))
        assert ctrl.state.get_task("AUTO-T5")["status"] == STATUS_TODO
        ctrl.run_trace.log_task_blocked.assert_not_called()
        ctrl.git.discard_working_changes.assert_called_once()
        assert ctrl.metrics_stream.record_gate2.call_args.kwargs["unavailable"] is True
        assert ctrl.metrics_stream.record_gate2.call_args.kwargs["approved"] is False
        log = (ctrl.agent_dir / "run.log").read_text(encoding="utf-8")
        assert "coder transport failure" in log
        assert "validator unavailable" not in log

    def test_an_exhausted_task_still_gets_its_ticket(self, tmp_path):
        ctrl = _controller(tmp_path)
        result = OuterLoopResult("AUTO-T5", False, 10, True, [],
                                 [_failed_result("still broken")], [])
        _run(ctrl, result)
        assert (ctrl.agent_dir / "tasks" / "AUTO-T5" / "knowledge.md").exists()
        assert ctrl.state.get_task("AUTO-T5")["status"] == "blocked"
        assert ctrl.metrics_stream.record_gate2.call_args.kwargs["unavailable"] is False
        ctrl.run_trace.log_task_blocked.assert_called_once()
        assert "exhausted" in (ctrl.agent_dir / "run.log").read_text(encoding="utf-8")


# ── 6. config: fail-open ────────────────────────────────────────────────────

def _cfg(overrides: dict) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test",
                      "model": "test-model", "api_format": "openai"},
        **overrides,
    })
    return cfg


class TestConfigFailOpen:
    def test_malformed_config_degrades_to_the_defaults(self, tmp_path):
        cfg = _cfg({"auto": {"coder_transport_retries": "twice"},
                    "collect": {"error_retry_wait_sec": "soon"}})
        loop = make_inner_loop(cfg, tmp_path, coder=FakeCoder([]),
                               executor=FailingExecutor(), validator=ApprovingValidator())
        assert loop._coder_transport_retries == 2
        assert loop._coder_transport_wait_sec == 60.0
        assert loop._validator_unavailable_retries == 2
        assert loop._validator_unavailable_wait_sec == 60.0

    def test_negative_config_values_are_floored(self, tmp_path):
        cfg = _cfg({"auto": {"coder_transport_retries": "-3"},
                    "collect": {"error_retry_wait_sec": "-5"}})
        loop = make_inner_loop(cfg, tmp_path, coder=FakeCoder([]),
                               executor=FailingExecutor(), validator=ApprovingValidator())
        assert loop._coder_transport_retries == 0
        assert loop._coder_transport_wait_sec == 0.0

    def test_a_configured_retry_count_is_honoured(self, tmp_path):
        cfg = _cfg({"auto": {"coder_transport_retries": "5"},
                    "collect": {"error_retry_wait_sec": "1.5"}})
        loop = make_inner_loop(cfg, tmp_path, coder=FakeCoder([]),
                               executor=FailingExecutor(), validator=ApprovingValidator())
        assert loop._coder_transport_retries == 5
        assert loop._coder_transport_wait_sec == 1.5

    def test_direct_constructor_guards_bad_values(self, tmp_path):
        loop = InnerLoop(FakeCoder([]), FailingExecutor(), ApprovingValidator(),
                         coder_transport_retries="many",
                         coder_transport_wait_sec=None)
        assert loop._coder_transport_retries == 2
        assert loop._coder_transport_wait_sec == 60.0

    def test_agents_ini_documents_the_key(self):
        for name in ("agents.ini", "agents_128k.ini"):
            text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            assert "coder_transport_retries" in text, name
            assert "error_retry_wait_sec" in text, name


# ── 7. scripts/trace_round_snapshot.py: the cod transport column ────────────

def _snapshot_module():
    spec = importlib.util.spec_from_file_location(
        "trace_round_snapshot", PROJECT_ROOT / "scripts" / "trace_round_snapshot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_trace(base: Path, decisions):
    agent = base / ".agent"
    agent.mkdir(parents=True, exist_ok=True)
    with open(agent / "trace_r1.jsonl", "w", encoding="utf-8") as fh:
        rows = [{"kind": "run_start", "params": {"goal": "g"}, "content": ""}]
        rows += [{"kind": "decision", "content": c,
                  "params": {"task": "AUTO-T5", "attempt": 1, "stage": "coder", **p}}
                 for c, p in decisions]
        for i, row in enumerate(rows):
            row.update({"seq": i, "ts": "2026-09-16T00:00:00+00:00", "run_id": "r1",
                        "source": "coder", "target": "inner_loop"})
            fh.write(json.dumps(row) + "\n")


class TestSnapshotColumn:
    def test_read_run_counts_coder_transport(self, tmp_path):
        _write_trace(tmp_path, [("TRANSPORT", {"max_tokens": 4096, "budget_raised": False}),
                                ("TRANSPORT", {"max_tokens": 4096, "budget_raised": False}),
                                ("TRANSPORT", {"max_tokens": 4096, "budget_raised": False}),
                                ("REJECTED", {"max_tokens": 4096, "budget_raised": False})])
        snap = _snapshot_module().read_run(tmp_path)
        assert snap["coder"] == {"TRANSPORT": 3, "REJECTED": 1}
        assert snap["coder_budget_escalations"] == 0

    def test_main_prints_the_column_next_to_cod_esc(self, tmp_path, monkeypatch, capsys):
        _write_trace(tmp_path / "r1",
                     [("TRANSPORT", {"max_tokens": 4096, "budget_raised": False}),
                      ("REJECTED", {"max_tokens": 4096, "budget_raised": True})])
        monkeypatch.setattr(sys, "argv", ["trace_round_snapshot.py", str(tmp_path / "r1")])
        _snapshot_module().main()
        out = capsys.readouterr().out
        hdr, _, row, _, total = [ln for ln in out.splitlines() if ln.strip()][:5]
        assert hdr.index("cod esc") < hdr.index("cod transport") < hdr.index("g2 rej")
        for col, want in (("cod esc", "1"), ("cod transport", "1"), ("g2 rej", "0")):
            end = hdr.index(col) + len(col)
            assert row[:end].split()[-1] == want, col
            assert total[:end].split()[-1] == want, col

    def test_a_pre_run8_trace_counts_zero_transport(self, tmp_path):
        _write_trace(tmp_path, [("REJECTED", {"max_tokens": 4096, "budget_raised": False})])
        snap = _snapshot_module().read_run(tmp_path)
        assert snap["coder"] == {"REJECTED": 1}
        assert snap["coder"].get("TRANSPORT", 0) == 0
