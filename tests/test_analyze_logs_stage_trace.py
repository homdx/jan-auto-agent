"""tests/test_analyze_logs_stage_trace.py — analyze_logs.py AUTO-CR-27 coverage.

Verifies that analyze_logs.analyze() correctly parses the new per-stage gate
events emitted by inner_loop._trace_stage():

  kind="decision", target="inner_loop", source=<stage>,
  params={"task": id, "attempt": N, "stage": <stage>, ...},
  content=REJECTED|APPROVED|EXHAUSTED|ACCEPTED_AT_CAP|ERROR

Covers:
  1. Each stage verdict is counted in task["stages"][stage][verdict]
  2. "overall APPROVED"   increments task["approved"] and sets iterations
  3. "overall EXHAUSTED"  increments task["rejected"] and sets iterations
  4. Creative-only stages (canon, fact, continuity, prosody) are parsed
  5. Events for an unknown task_id auto-create a minimal task entry
  6. Fallback to _current_task when params["task"] is absent
  7. render_stage_breakdown() prints nothing when stages is empty
  8. render_stage_breakdown() prints ordered gate log when stages are present
  9. Timeline renders stage decisions with stage label and verdict colour
 10. Old validator result events continue to work (backward compat)
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analyze_logs import analyze, render_stage_breakdown, render_timeline


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _e(kind: str, src: str = "", tgt: str = "", content: str = "",
       params: dict | None = None, run_id: str = "run1",
       ts: str = "2024-01-01T00:00:00") -> dict:
    return {"run_id": run_id, "kind": kind, "source": src, "target": tgt,
            "ts": ts, "params": params or {}, "content": content}


def _stage(stage: str, verdict: str, task: str = "AUTO-T1",
           attempt: int = 1, run_id: str = "run1", **extra) -> dict:
    params = {"task": task, "attempt": attempt, "stage": stage, **extra}
    return _e("decision", src=stage, tgt="inner_loop",
              content=verdict, params=params, run_id=run_id)


def _task_start(task_id: str = "AUTO-T1", title: str = "task",
                run_id: str = "run1") -> dict:
    return _e("call", src="controller", tgt="outer_loop",
              params={"task_id": task_id, "title": title}, run_id=run_id)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _capture(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        fn(*args, **kwargs)
    finally:
        sys.stdout = old
    return _strip_ansi(buf.getvalue())


# ─────────────────────────────────────────────────────────────────────────────
# 1–4: analyze() stage parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestStageAccumulation:
    def test_rejected_stage_is_counted(self):
        events = [_task_start(), _stage("gate2", "REJECTED")]
        runs = analyze(events)
        assert runs["run1"]["tasks"]["AUTO-T1"]["stages"]["gate2"]["REJECTED"] == 1

    def test_multiple_verdicts_same_stage(self):
        events = [
            _task_start(),
            _stage("gate2", "REJECTED", attempt=1),
            _stage("gate2", "REJECTED", attempt=2),
        ]
        runs = analyze(events)
        assert runs["run1"]["tasks"]["AUTO-T1"]["stages"]["gate2"]["REJECTED"] == 2

    def test_distinct_stages_tracked_independently(self):
        events = [
            _task_start(),
            _stage("coder",    "REJECTED", attempt=1),
            _stage("executor", "REJECTED", attempt=2),
            _stage("gate2",    "REJECTED", attempt=2),
        ]
        runs = analyze(events)
        s = runs["run1"]["tasks"]["AUTO-T1"]["stages"]
        assert s["coder"]["REJECTED"]    == 1
        assert s["executor"]["REJECTED"] == 1
        assert s["gate2"]["REJECTED"]    == 1

    def test_accepted_at_cap_is_counted(self):
        events = [_task_start(), _stage("canon", "ACCEPTED_AT_CAP", cap=2)]
        runs = analyze(events)
        assert runs["run1"]["tasks"]["AUTO-T1"]["stages"]["canon"]["ACCEPTED_AT_CAP"] == 1

    def test_error_verdict_is_counted(self):
        events = [_task_start(), _stage("coder", "ERROR", error="boom")]
        runs = analyze(events)
        assert runs["run1"]["tasks"]["AUTO-T1"]["stages"]["coder"]["ERROR"] == 1

    def test_creative_stages_canon_fact_continuity_prosody(self):
        events = [
            _task_start(),
            _stage("canon",       "REJECTED", attempt=1),
            _stage("fact",        "REJECTED", attempt=2),
            _stage("continuity",  "ACCEPTED_AT_CAP", attempt=3),
            _stage("prosody",     "REJECTED", attempt=4),
        ]
        runs = analyze(events)
        s = runs["run1"]["tasks"]["AUTO-T1"]["stages"]
        assert s["canon"]["REJECTED"]            == 1
        assert s["fact"]["REJECTED"]             == 1
        assert s["continuity"]["ACCEPTED_AT_CAP"] == 1
        assert s["prosody"]["REJECTED"]          == 1


class TestOverallStage:
    def test_overall_approved_increments_approved_field(self):
        events = [_task_start(), _stage("overall", "APPROVED", attempt=2)]
        runs = analyze(events)
        t = runs["run1"]["tasks"]["AUTO-T1"]
        assert t["approved"] == 1
        assert t["iterations"] == 2

    def test_overall_exhausted_increments_rejected_field(self):
        events = [_task_start(), _stage("overall", "EXHAUSTED", attempt=3)]
        runs = analyze(events)
        t = runs["run1"]["tasks"]["AUTO-T1"]
        assert t["rejected"] == 1
        assert t["iterations"] == 3

    def test_iterations_takes_max_attempt_seen(self):
        # A second outer round could re-emit an overall event at a higher attempt.
        events = [
            _task_start(),
            _stage("overall", "EXHAUSTED", attempt=3),
            _stage("overall", "APPROVED",  attempt=5),
        ]
        runs = analyze(events)
        t = runs["run1"]["tasks"]["AUTO-T1"]
        assert t["iterations"] == 5
        assert t["approved"] == 1
        assert t["rejected"] == 1


class TestFallbackAndAutocreate:
    def test_unknown_task_id_autocreates_task_entry(self):
        events = [_stage("gate2", "REJECTED", task="NEW-T99")]
        runs = analyze(events)
        assert "NEW-T99" in runs["run1"]["tasks"]
        assert runs["run1"]["tasks"]["NEW-T99"]["stages"]["gate2"]["REJECTED"] == 1

    def test_falls_back_to_current_task_when_task_param_missing(self):
        """Events where params["task"] is absent use run["_current_task"]."""
        task_start = _task_start("AUTO-T5", "my task")
        # Craft a stage event with no "task" key in params — simulates an older
        # emitter that only set "stage" and "attempt".
        stage_evt = _e("decision", src="gate2", tgt="inner_loop",
                       content="REJECTED",
                       params={"attempt": 1, "stage": "gate2"})
        runs = analyze([task_start, stage_evt])
        assert "AUTO-T5" in runs["run1"]["tasks"]
        assert runs["run1"]["tasks"]["AUTO-T5"]["stages"]["gate2"]["REJECTED"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 7–8: render_stage_breakdown()
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderStageBreakdown:
    def test_empty_stages_prints_nothing(self):
        task = {"stages": {}}
        text = _capture(render_stage_breakdown, task)
        assert text.strip() == ""

    def test_absent_stages_key_prints_nothing(self):
        text = _capture(render_stage_breakdown, {})
        assert text.strip() == ""

    def test_single_rejected_stage_shown(self):
        task = {"stages": {"gate2": {"REJECTED": 2, "ACCEPTED_AT_CAP": 0,
                                     "ERROR": 0, "APPROVED": 0, "EXHAUSTED": 0}}}
        text = _capture(render_stage_breakdown, task)
        assert "Gate-2" in text
        assert "2" in text

    def test_pipeline_order_coder_before_gate2_before_overall(self):
        task = {"stages": {
            "overall": {"APPROVED": 1, "REJECTED": 0, "ACCEPTED_AT_CAP": 0,
                        "ERROR": 0, "EXHAUSTED": 0},
            "gate2":   {"REJECTED": 1, "APPROVED": 0, "ACCEPTED_AT_CAP": 0,
                        "ERROR": 0, "EXHAUSTED": 0},
            "coder":   {"REJECTED": 0, "APPROVED": 0, "ACCEPTED_AT_CAP": 0,
                        "ERROR": 1, "EXHAUSTED": 0},
        }}
        text = _capture(render_stage_breakdown, task)
        lines = [l for l in text.splitlines() if any(
            k in l for k in ("Coder", "Gate-2 (LLM)", "Overall"))]
        names = [l.split(":")[0].strip() for l in lines]
        assert names.index("Coder") < names.index("Gate-2 (LLM)") < names.index("Overall")

    def test_accepted_at_cap_shown(self):
        task = {"stages": {"prosody": {"REJECTED": 1, "ACCEPTED_AT_CAP": 1,
                                       "ERROR": 0, "APPROVED": 0, "EXHAUSTED": 0}}}
        text = _capture(render_stage_breakdown, task)
        assert "cap" in text.lower() or "⚠" in text

    def test_creative_stage_labels_present(self):
        task = {"stages": {
            "canon":      {"REJECTED": 1, "ACCEPTED_AT_CAP": 0, "ERROR": 0,
                           "APPROVED": 0, "EXHAUSTED": 0},
            "fact":       {"REJECTED": 1, "ACCEPTED_AT_CAP": 0, "ERROR": 0,
                           "APPROVED": 0, "EXHAUSTED": 0},
            "continuity": {"ACCEPTED_AT_CAP": 1, "REJECTED": 0, "ERROR": 0,
                           "APPROVED": 0, "EXHAUSTED": 0},
            "prosody":    {"REJECTED": 2, "ACCEPTED_AT_CAP": 0, "ERROR": 0,
                           "APPROVED": 0, "EXHAUSTED": 0},
        }}
        text = _capture(render_stage_breakdown, task)
        for label in ("Canon", "Fact", "Continuity", "Prosody"):
            assert label in text, f"Missing label: {label}"


# ─────────────────────────────────────────────────────────────────────────────
# 9: timeline renders inner_loop decisions
# ─────────────────────────────────────────────────────────────────────────────

class TestTimelineStageEvents:
    def _run_with_events(self, evts: list[dict]) -> dict:
        runs = analyze(evts)
        return runs["run1"]

    def test_inner_loop_decision_shown_in_timeline(self):
        events = [_task_start(), _stage("gate2", "REJECTED", attempt=1)]
        run = self._run_with_events(events)
        text = _capture(render_timeline, run)
        assert "Gate-2" in text
        assert "REJECTED" in text

    def test_overall_approved_shown_in_timeline(self):
        events = [_task_start(), _stage("overall", "APPROVED", attempt=2)]
        run = self._run_with_events(events)
        text = _capture(render_timeline, run)
        assert "Overall" in text
        assert "APPROVED" in text

    def test_creative_stage_shown_in_timeline(self):
        events = [
            _task_start("AUTO-T2", "chapter_01 task"),
            _stage("prosody", "ACCEPTED_AT_CAP", task="AUTO-T2", attempt=3),
        ]
        run = self._run_with_events(events)
        text = _capture(render_timeline, run)
        assert "Prosody" in text
        assert "ACCEPTED_AT_CAP" in text


# ─────────────────────────────────────────────────────────────────────────────
# 10: backward compat — old validator result events still work
# ─────────────────────────────────────────────────────────────────────────────

class TestBackwardCompat:
    def test_validator_result_still_increments_approved(self):
        """Traces predating AUTO-CR-27 only have kind="result" from validator_agent."""
        import json
        verdict_content = json.dumps({"approved": True, "feedback": ""})
        events = [
            _task_start(),
            _e("result", src="validator_agent", tgt="inner_loop",
               content=verdict_content),
        ]
        runs = analyze(events)
        t = runs["run1"]["tasks"]["AUTO-T1"]
        assert t["approved"] == 1
        assert t["stages"] == {}   # no stage data in old traces

    def test_validator_result_still_increments_rejected(self):
        import json
        verdict_content = json.dumps({"approved": False, "feedback": "bad"})
        events = [
            _task_start(),
            _e("result", src="validator_agent", tgt="inner_loop",
               content=verdict_content),
        ]
        runs = analyze(events)
        t = runs["run1"]["tasks"]["AUTO-T1"]
        assert t["rejected"] == 1
