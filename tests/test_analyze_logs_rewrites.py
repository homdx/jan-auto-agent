"""tests/test_analyze_logs_rewrites.py — prompt rewrite report mode.

Covers the new `--rewrites` / `--rewrites-only` analyze_logs.py feature:

  1. _extract_current_prompt_from_meta() pulls the embedded CURRENT PROMPT
     text out of the optimizer's meta-prompt (and degrades gracefully when
     the markers are absent).
  2. analyze() builds run["rewrite_attempts"] from prompt_denied /
     prompt_promoted events, pairing each with the old/new prompt text
     recovered from the preceding prompt_optimizer llm_request/llm_response.
  3. Each rewrite attempt carries score + promoted flag independent of the
     others (a denied attempt doesn't leak into a later promoted one, etc.)
  4. render_rewrite_report() shows score + outcome for every attempt, and
     the old → new diff only for promoted ones.
  5. tools/auto/auto_tuner.py emits kind="prompt_promoted" on promotion
     (mirroring the kind="prompt_denied" event added for the denied path),
     so the report has real success data to show.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyze_logs import (
    analyze,
    _extract_current_prompt_from_meta,
    render_rewrite_report,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _evt(kind: str, params: dict | None = None, run_id: str = "run1", **kw) -> dict:
    return {
        "run_id": run_id,
        "kind": kind,
        "source": kw.get("source", ""),
        "target": kw.get("target", ""),
        "ts": kw.get("ts", "2024-01-01T00:00:00"),
        "params": params or {},
        "content": kw.get("content", ""),
    }


META_PROMPT_TEMPLATE = (
    "You are a prompt engineering agent. You will be given:\n"
    "1. A current system prompt used by an AI agent\n"
    "2. A summary of recent failures when using that prompt\n"
    "\n"
    "Rewrite the prompt to fix the identified failure patterns.\n"
    "Keep the same JSON output format requirements.\n"
    "Return only the new prompt text, nothing else.\n"
    "\n"
    "CURRENT PROMPT:\n"
    "{current_prompt}\n"
    "\n"
    "FAILURE SUMMARY:\n"
    "{{\"avg_iterations\": 2.5}}\n"
)


def _optimizer_pair(old_prompt: str, new_prompt: str, run_id: str = "run1",
                     ts_req: str = "2024-01-01T00:01:00",
                     ts_resp: str = "2024-01-01T00:01:05") -> list[dict]:
    meta = META_PROMPT_TEMPLATE.format(current_prompt=old_prompt)
    return [
        _evt("llm_request", source="prompt_optimizer", target="llm",
             content=meta, run_id=run_id, ts=ts_req),
        _evt("llm_response", source="llm", target="prompt_optimizer",
             content=new_prompt, run_id=run_id, ts=ts_resp),
    ]


def _denied(score: float, reason: str = "not enough improvement",
            run_id: str = "run1", ts: str = "2024-01-01T00:01:10") -> dict:
    return _evt(
        "prompt_denied",
        {"agent": "validator", "score": score, "promoted": False},
        run_id=run_id, source="auto_tuner", target="prompt_evaluator",
        content=reason, ts=ts,
    )


def _promoted(score: float, reason: str = "score improved",
              run_id: str = "run1", ts: str = "2024-01-01T00:01:10") -> dict:
    return _evt(
        "prompt_promoted",
        {"agent": "validator", "score": score, "promoted": True},
        run_id=run_id, source="auto_tuner", target="prompt_evaluator",
        content=reason, ts=ts,
    )


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _capture(fn, *a, **kw) -> str:
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        fn(*a, **kw)
    finally:
        sys.stdout = old_stdout
    return _strip_ansi(buf.getvalue())


# ─────────────────────────────────────────────────────────────────────────────
# 1. _extract_current_prompt_from_meta
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractCurrentPromptFromMeta:
    def test_extracts_embedded_prompt(self) -> None:
        meta = META_PROMPT_TEMPLATE.format(current_prompt="You are a validator.")
        assert _extract_current_prompt_from_meta(meta) == "You are a validator."

    def test_multiline_prompt_preserved(self) -> None:
        old = "Line one.\nLine two.\nLine three."
        meta = META_PROMPT_TEMPLATE.format(current_prompt=old)
        assert _extract_current_prompt_from_meta(meta) == old

    def test_missing_marker_returns_empty(self) -> None:
        assert _extract_current_prompt_from_meta("unrelated llm_request content") == ""

    def test_empty_input_returns_empty(self) -> None:
        assert _extract_current_prompt_from_meta("") == ""

    def test_missing_end_marker_takes_rest_of_string(self) -> None:
        meta = "preamble\nCURRENT PROMPT:\nthe whole rest of the text"
        assert _extract_current_prompt_from_meta(meta) == "the whole rest of the text"


# ─────────────────────────────────────────────────────────────────────────────
# 2 & 3. analyze() builds rewrite_attempts correctly
# ─────────────────────────────────────────────────────────────────────────────

class TestAnalyzeRewriteAttempts:
    def test_denied_attempt_recorded(self) -> None:
        events = [
            *_optimizer_pair("old text", "new text"),
            _denied(0.72, "Insufficient improvement"),
        ]
        runs = analyze(events)
        attempts = runs["run1"]["rewrite_attempts"]
        assert len(attempts) == 1
        a = attempts[0]
        assert a["promoted"] is False
        assert a["score"] == 0.72
        assert a["agent"] == "validator"
        assert a["old_prompt"] == "old text"
        assert a["new_prompt"] == "new text"
        assert "Insufficient improvement" in a["reason"]

    def test_promoted_attempt_recorded(self) -> None:
        events = [
            *_optimizer_pair("old text", "new text"),
            _promoted(0.81, "Score improved"),
        ]
        runs = analyze(events)
        attempts = runs["run1"]["rewrite_attempts"]
        assert len(attempts) == 1
        a = attempts[0]
        assert a["promoted"] is True
        assert a["score"] == 0.81
        assert a["old_prompt"] == "old text"
        assert a["new_prompt"] == "new text"

    def test_denied_then_promoted_each_keep_own_score_and_prompts(self) -> None:
        events = [
            *_optimizer_pair("v0 prompt", "v1 candidate",
                              ts_req="2024-01-01T00:01:00", ts_resp="2024-01-01T00:01:05"),
            _denied(0.70, ts="2024-01-01T00:01:10"),
            *_optimizer_pair("v0 prompt", "v2 candidate",
                              ts_req="2024-01-01T00:05:00", ts_resp="2024-01-01T00:05:05"),
            _promoted(0.85, ts="2024-01-01T00:05:10"),
        ]
        runs = analyze(events)
        attempts = runs["run1"]["rewrite_attempts"]
        assert len(attempts) == 2

        denied, promoted = attempts
        assert denied["promoted"] is False
        assert denied["score"] == 0.70
        assert denied["new_prompt"] == "v1 candidate"

        assert promoted["promoted"] is True
        assert promoted["score"] == 0.85
        assert promoted["new_prompt"] == "v2 candidate"

    def test_no_rewrite_events_gives_empty_list(self) -> None:
        events = [_evt("run_start", {"goal": "x"})]
        runs = analyze(events)
        assert runs["run1"]["rewrite_attempts"] == []

    def test_outcome_without_preceding_optimizer_pair_has_empty_prompts(self) -> None:
        # e.g. a trace that was truncated mid-stream — should not crash and
        # should not fabricate prompt text.
        events = [_denied(0.5)]
        runs = analyze(events)
        attempts = runs["run1"]["rewrite_attempts"]
        assert len(attempts) == 1
        assert attempts[0]["old_prompt"] == ""
        assert attempts[0]["new_prompt"] == ""

    def test_pending_prompt_pair_does_not_leak_across_unrelated_events(self) -> None:
        # An optimizer pair followed by an unrelated event, then a denied
        # outcome much later with no fresh pair, should NOT reuse the stale
        # text from the first pair (it was already consumed once and reset).
        events = [
            *_optimizer_pair("old", "new"),
            _denied(0.6, ts="2024-01-01T00:01:10"),
            _evt("result", {"task_id": "T1"}, source="outer_loop",
                 content="DONE", ts="2024-01-01T00:02:00"),
            _denied(0.4, ts="2024-01-01T00:03:00"),
        ]
        runs = analyze(events)
        attempts = runs["run1"]["rewrite_attempts"]
        assert len(attempts) == 2
        assert attempts[0]["old_prompt"] == "old"
        # second denied has no fresh optimizer pair behind it
        assert attempts[1]["old_prompt"] == ""
        assert attempts[1]["new_prompt"] == ""

    def test_runs_are_isolated_per_run_id(self) -> None:
        events = [
            *_optimizer_pair("a-old", "a-new", run_id="runA"),
            _denied(0.5, run_id="runA"),
            *_optimizer_pair("b-old", "b-new", run_id="runB"),
            _promoted(0.9, run_id="runB"),
        ]
        runs = analyze(events)
        assert len(runs["runA"]["rewrite_attempts"]) == 1
        assert len(runs["runB"]["rewrite_attempts"]) == 1
        assert runs["runA"]["rewrite_attempts"][0]["promoted"] is False
        assert runs["runB"]["rewrite_attempts"][0]["promoted"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 4. render_rewrite_report()
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderRewriteReport:
    def _run(self, attempts: list[dict]) -> dict:
        return {"run_id": "run1", "rewrite_attempts": attempts}

    def test_empty_attempts_shows_placeholder(self) -> None:
        text = _capture(render_rewrite_report, self._run([]))
        assert "no auto-tuner rewrite attempts" in text

    def test_shows_score_and_denied_status(self) -> None:
        run = self._run([{
            "ts": "2024-01-01T00:00:00", "agent": "validator", "score": 0.42,
            "promoted": False, "reason": "too low", "old_prompt": "", "new_prompt": "",
        }])
        text = _capture(render_rewrite_report, run)
        assert "0.4200" in text
        assert "DENIED" in text
        assert "too low" in text

    def test_shows_score_and_promoted_status_with_diff(self) -> None:
        run = self._run([{
            "ts": "2024-01-01T00:00:00", "agent": "validator", "score": 0.91,
            "promoted": True, "reason": "great", "old_prompt": "old text",
            "new_prompt": "new text",
        }])
        text = _capture(render_rewrite_report, run)
        assert "0.9100" in text
        assert "PROMOTED" in text
        assert "old text" in text
        assert "new text" in text

    def test_denied_attempt_never_shows_diff_marker(self) -> None:
        run = self._run([{
            "ts": "2024-01-01T00:00:00", "agent": "validator", "score": 0.3,
            "promoted": False, "reason": "no", "old_prompt": "old", "new_prompt": "new",
        }])
        text = _capture(render_rewrite_report, run)
        # Even if old/new happen to be present, a denied attempt is not "ok"
        # and shouldn't render the diff section.
        assert "old → new prompt" not in text

    def test_summary_counts_promoted_and_denied(self) -> None:
        run = self._run([
            {"ts": "", "agent": "v", "score": 0.5, "promoted": False, "reason": "",
             "old_prompt": "", "new_prompt": ""},
            {"ts": "", "agent": "v", "score": 0.9, "promoted": True, "reason": "",
             "old_prompt": "a", "new_prompt": "b"},
            {"ts": "", "agent": "v", "score": 0.95, "promoted": True, "reason": "",
             "old_prompt": "c", "new_prompt": "d"},
        ])
        text = _capture(render_rewrite_report, run)
        assert "2 promoted" in text
        assert "1 denied" in text


# ─────────────────────────────────────────────────────────────────────────────
# 5. auto_tuner.py traces the promoted outcome too
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoTunerTracesPromotion:
    def _make_tuner(self, score: float = 0.85):
        from tools.auto.auto_tuner import AutoTuner
        from tools.prompt_evaluator import EvalResult

        prompt_store = MagicMock()
        prompt_store.get_current.return_value = "current prompt"

        metrics = MagicMock()
        metrics.summarize_failures.return_value = {
            "total_runs": 10, "avg_iterations": 3.0, "json_parse_failure_rate": 0.0,
        }

        optimizer = MagicMock()
        optimizer.generate_candidate.return_value = "candidate prompt"

        evaluator = MagicMock()
        evaluator.evaluate.return_value = EvalResult(
            promoted=True, score=score, reason="improved",
        )

        return AutoTuner(
            prompt_store=prompt_store,
            metrics_collector=metrics,
            prompt_optimizer=optimizer,
            prompt_evaluator=evaluator,
            agent_name="validator",
            min_runs=5,
            trigger_avg_iter=2.0,
        )

    def test_promoted_emits_prompt_promoted_event(self) -> None:
        tuner = self._make_tuner(score=0.85)
        emitted: list[dict] = []

        def fake_event(**kw):
            emitted.append(kw)

        with patch("tools.auto.auto_tuner.tracer.event", side_effect=fake_event):
            outcome = tuner.maybe_tune()

        assert outcome.promoted is True
        kinds = [e["kind"] for e in emitted]
        assert "prompt_promoted" in kinds

    def test_promoted_event_carries_score_and_agent(self) -> None:
        tuner = self._make_tuner(score=0.77)
        emitted: list[dict] = []

        def fake_event(**kw):
            emitted.append(kw)

        with patch("tools.auto.auto_tuner.tracer.event", side_effect=fake_event):
            tuner.maybe_tune()

        evt = next(e for e in emitted if e["kind"] == "prompt_promoted")
        assert evt["params"]["agent"] == "validator"
        assert evt["params"]["score"] == 0.77
        assert evt["params"]["promoted"] is True

    def test_denied_still_emits_prompt_denied_event(self) -> None:
        from tools.prompt_evaluator import EvalResult

        tuner = self._make_tuner()
        tuner.evaluator.evaluate.return_value = EvalResult(
            promoted=False, score=0.5, reason="not enough",
        )
        emitted: list[dict] = []

        def fake_event(**kw):
            emitted.append(kw)

        with patch("tools.auto.auto_tuner.tracer.event", side_effect=fake_event):
            outcome = tuner.maybe_tune()

        assert outcome.promoted is False
        kinds = [e["kind"] for e in emitted]
        assert "prompt_denied" in kinds
        assert "prompt_promoted" not in kinds
