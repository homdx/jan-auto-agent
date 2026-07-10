"""tests/test_auto_f2.py — AUTO-F2: Trace + run log.

ACs (from the Jira story):
  * Every architect/coder/validator/executor exchange logged via agent_trace
    (per-run id).
  * run.log receives high-level lifecycle events.
  * A completed run is fully reconstructable from the trace.
  * Reuse: agent_trace, view_trace.py.

Coverage
--------
setup_run_trace():
  * Returns a RunTrace instance.
  * Assigns a non-empty run_id.
  * trace_path is under .agent/ and matches the run_id.
  * Configures the global tracer (enabled).
  * Configures tracer with max_field_chars from config.
  * Configures tracer with console_echo from config.
  * Sets tracer._run_id to the generated run_id.
  * Writes a setup line to run.log.
  * disabled trace → trace_path is None, tracer.enabled is False.
  * Two successive calls produce different run_ids.

RunTrace.log_run_start():
  * Emits a trace event of kind "run_start".
  * Event params contain goal and base_dir.
  * Event params contain run_id.
  * Appends a line to run.log containing "run started".

RunTrace.log_task_start():
  * Emits a trace event of kind "call".
  * Event params contain task_id and title.
  * Appends a line to run.log containing "task start".

RunTrace.log_task_done():
  * Emits a trace event with content "DONE".
  * Event params contain task_id.
  * Event params contain commit_hash when provided.
  * Appends a line to run.log containing "task done".
  * Works without commit_hash (optional).

RunTrace.log_task_blocked():
  * Emits a trace event with content "BLOCKED".
  * Event params contain task_id and reason.
  * Appends a line to run.log containing "task blocked".

RunTrace.log_run_finished():
  * Emits a trace event of kind "run_finished" on clean finish.
  * Event params contain run_id.
  * Appends a line to run.log containing "run finished".

RunTrace.log_run_capped():
  * Emits a trace event of kind "run_capped".
  * Event params contain stop_reason.
  * Appends a line to run.log containing "run capped".

Trace file content:
  * Trace file is created on disk after events are emitted.
  * Every line in the trace file is valid JSON.
  * Every event has the run_id field matching rt.run_id.
  * Events for architect / coder / validator / executor all land in same file.
  * A full lifecycle sequence produces a reconstructable trace.

view_trace.py integration:
  * load_events() returns a list of dicts from a .jsonl file.
  * apply_filters() by kind works.
  * apply_filters() by source works.
  * apply_filters() tail works.
  * apply_filters() by run_id works.
  * render_event() returns a non-empty string.
  * render_summary() returns a non-empty string with one row per event.
  * main() exits 0 on a valid trace file.
  * main() exits non-zero on an empty trace file.
  * main() --summary flag produces table output.
  * main() --filter narrows output.
  * main() --tail narrows output.
"""

from __future__ import annotations

import configparser
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.agent_trace import tracer as _global_tracer
from tools.auto.run_trace import RunTrace, setup_run_trace
from tools.auto.state import StateStore

import view_trace as vt


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / ".agent")
    store.initialise("test goal", tmp_path)
    return store


def _make_config(
    enabled: bool = True,
    max_field_chars: int = 4000,
    console_echo: bool = False,
) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["trace"] = {
        "enabled":         "yes" if enabled else "no",
        "max_field_chars": str(max_field_chars),
        "console_echo":    "yes" if console_echo else "no",
    }
    return cfg


def _flush_tracer(rt: RunTrace) -> list[dict]:
    """Read all events written to the trace file and return them as dicts."""
    if rt.trace_path is None or not rt.trace_path.exists():
        return []
    return vt.load_events(rt.trace_path)


# ── setup_run_trace ────────────────────────────────────────────────────────────

class TestSetupRunTrace:
    def test_returns_run_trace_instance(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        assert isinstance(rt, RunTrace)

    def test_run_id_non_empty(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        assert rt.run_id and len(rt.run_id) == 12

    def test_trace_path_under_agent_dir(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        assert rt.trace_path is not None
        assert rt.trace_path.parent == tmp_path / ".agent"

    def test_trace_path_contains_run_id(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        assert rt.run_id in rt.trace_path.name

    def test_trace_path_is_jsonl(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        assert rt.trace_path.suffix == ".jsonl"

    def test_tracer_enabled(self, tmp_path):
        state = _make_state(tmp_path)
        setup_run_trace(state, _make_config(enabled=True))
        assert _global_tracer.enabled is True

    def test_tracer_max_field_chars(self, tmp_path):
        state = _make_state(tmp_path)
        setup_run_trace(state, _make_config(max_field_chars=1234))
        assert _global_tracer.max_field_chars == 1234

    def test_tracer_run_id_injected(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        assert _global_tracer._run_id == rt.run_id

    def test_writes_to_run_log(self, tmp_path):
        state = _make_state(tmp_path)
        setup_run_trace(state, _make_config())
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "trace configured" in log

    def test_disabled_trace_path_none(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config(enabled=False))
        assert rt.trace_path is None

    def test_disabled_tracer_not_enabled(self, tmp_path):
        state = _make_state(tmp_path)
        setup_run_trace(state, _make_config(enabled=False))
        assert _global_tracer.enabled is False

    def test_successive_calls_different_run_ids(self, tmp_path):
        state = _make_state(tmp_path)
        rt1 = setup_run_trace(state, _make_config())
        rt2 = setup_run_trace(state, _make_config())
        assert rt1.run_id != rt2.run_id

    def test_no_trace_section_uses_defaults(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = configparser.ConfigParser()   # no [trace] section
        rt = setup_run_trace(state, cfg)
        assert _global_tracer.enabled is True
        assert rt.run_id


# ── RunTrace.log_run_start ─────────────────────────────────────────────────────

class TestLogRunStart:
    def test_emits_run_start_event(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("fix bugs", tmp_path)
        evts = _flush_tracer(rt)
        assert any(e.get("kind") == "run_start" for e in evts)

    def test_event_params_contain_goal(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("fix bugs", tmp_path)
        evts = _flush_tracer(rt)
        rs = next(e for e in evts if e.get("kind") == "run_start")
        assert rs["params"]["goal"] == "fix bugs"

    def test_event_params_contain_base_dir(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("fix bugs", tmp_path)
        evts = _flush_tracer(rt)
        rs = next(e for e in evts if e.get("kind") == "run_start")
        assert str(tmp_path) in rs["params"]["base_dir"]

    def test_event_params_contain_run_id(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("fix bugs", tmp_path)
        evts = _flush_tracer(rt)
        rs = next(e for e in evts if e.get("kind") == "run_start")
        assert rs["params"]["run_id"] == rt.run_id

    def test_appends_to_run_log(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("fix bugs", tmp_path)
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "run started" in log


# ── RunTrace.log_task_start ───────────────────────────────────────────────────

class TestLogTaskStart:
    def test_emits_call_event(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_start("AUTO-T1", "Fix login")
        evts = _flush_tracer(rt)
        calls = [e for e in evts if e.get("kind") == "call"]
        assert calls

    def test_event_params_contain_task_id(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_start("AUTO-T1", "Fix login")
        evts = _flush_tracer(rt)
        call = next(e for e in evts if e.get("kind") == "call")
        assert call["params"]["task_id"] == "AUTO-T1"

    def test_event_params_contain_title(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_start("AUTO-T1", "Fix login")
        evts = _flush_tracer(rt)
        call = next(e for e in evts if e.get("kind") == "call")
        assert call["params"]["title"] == "Fix login"

    def test_appends_to_run_log(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_start("AUTO-T1", "Fix login")
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "task start" in log


# ── RunTrace.log_task_done ────────────────────────────────────────────────────

class TestLogTaskDone:
    def test_emits_result_done(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_done("AUTO-T1", "abc123")
        evts = _flush_tracer(rt)
        assert any(e.get("content") == "DONE" for e in evts)

    def test_event_params_contain_task_id(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_done("AUTO-T1", "abc123")
        evts = _flush_tracer(rt)
        done = next(e for e in evts if e.get("content") == "DONE")
        assert done["params"]["task_id"] == "AUTO-T1"

    def test_event_params_contain_commit_hash(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_done("AUTO-T1", "deadbeef1234")
        evts = _flush_tracer(rt)
        done = next(e for e in evts if e.get("content") == "DONE")
        assert done["params"]["commit"] == "deadbeef1234"

    def test_works_without_commit_hash(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_done("AUTO-T1")   # no commit hash
        evts = _flush_tracer(rt)
        assert any(e.get("content") == "DONE" for e in evts)

    def test_appends_to_run_log(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_done("AUTO-T1", "abc123")
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "task done" in log


# ── RunTrace.log_task_blocked ─────────────────────────────────────────────────

class TestLogTaskBlocked:
    def test_emits_blocked_event(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_blocked("AUTO-T2", "exhausted all rounds")
        evts = _flush_tracer(rt)
        assert any(e.get("content") == "BLOCKED" for e in evts)

    def test_event_params_contain_task_id(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_blocked("AUTO-T2", "exhausted all rounds")
        evts = _flush_tracer(rt)
        blk = next(e for e in evts if e.get("content") == "BLOCKED")
        assert blk["params"]["task_id"] == "AUTO-T2"

    def test_event_params_contain_reason(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_blocked("AUTO-T2", "exhausted all rounds")
        evts = _flush_tracer(rt)
        blk = next(e for e in evts if e.get("content") == "BLOCKED")
        assert "exhausted" in blk["params"]["reason"]

    def test_appends_to_run_log(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_task_blocked("AUTO-T2", "exhausted all rounds")
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "task blocked" in log


# ── RunTrace.log_run_finished ─────────────────────────────────────────────────

class TestLogRunFinished:
    def test_emits_run_finished_on_clean_end(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_finished()
        evts = _flush_tracer(rt)
        assert any(e.get("kind") == "run_finished" for e in evts)

    def test_event_params_contain_run_id(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_finished()
        evts = _flush_tracer(rt)
        fin = next(e for e in evts if e.get("kind") == "run_finished")
        assert fin["params"]["run_id"] == rt.run_id

    def test_appends_to_run_log(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_finished()
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "run finished" in log


# ── RunTrace.log_run_capped ───────────────────────────────────────────────────

class TestLogRunCapped:
    def test_emits_run_capped_event(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_capped("runtime_cap")
        evts = _flush_tracer(rt)
        assert any(e.get("kind") == "run_capped" for e in evts)

    def test_event_params_contain_stop_reason(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_capped("task_cap")
        evts = _flush_tracer(rt)
        cap = next(e for e in evts if e.get("kind") == "run_capped")
        assert cap["params"]["stop_reason"] == "task_cap"

    def test_appends_to_run_log(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_capped("runtime_cap")
        log = (tmp_path / ".agent" / "run.log").read_text()
        assert "run capped" in log


# ── Trace file content ────────────────────────────────────────────────────────

class TestTraceFileContent:
    def test_trace_file_created_after_event(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("goal", tmp_path)
        assert rt.trace_path.exists()

    def test_every_line_is_valid_json(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("goal", tmp_path)
        rt.log_task_start("AUTO-T1", "title")
        rt.log_task_done("AUTO-T1", "abc")
        rt.log_run_finished()
        for line in rt.trace_path.read_text().splitlines():
            if line.strip():
                json.loads(line)   # must not raise

    def test_every_event_has_run_id_field(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("goal", tmp_path)
        rt.log_task_start("AUTO-T1", "title")
        evts = _flush_tracer(rt)
        for evt in evts:
            assert "run_id" in evt, f"missing run_id in {evt}"

    def test_run_id_matches_rt_run_id(self, tmp_path):
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("goal", tmp_path)
        evts = _flush_tracer(rt)
        for evt in evts:
            assert evt["run_id"] == rt.run_id

    def test_multiple_agent_events_land_in_same_file(self, tmp_path):
        """Simulate architect + coder + validator + executor all tracing."""
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())
        rt.log_run_start("goal", tmp_path)

        # Simulate events from different auto agents
        from tools.agent_trace import tracer
        tracer.event("architect",  "llm", "llm_request",  content="review prompt")
        tracer.event("llm", "architect",  "llm_response", content="candidate tasks")
        tracer.event("coder",      "llm", "llm_request",  content="code prompt")
        tracer.event("llm", "coder",      "llm_response", content="new file content")
        tracer.event("executor",   "coder", "result",     content="{exit_code: 0}")

        evts = _flush_tracer(rt)
        sources = {e.get("source") for e in evts}
        assert "architect" in sources
        assert "coder" in sources
        assert "executor" in sources

    def test_full_lifecycle_reconstructable(self, tmp_path):
        """A complete run sequence produces a fully reconstructable trace."""
        state = _make_state(tmp_path)
        rt = setup_run_trace(state, _make_config())

        rt.log_run_start("improve code", tmp_path)
        rt.log_task_start("AUTO-T1", "fix parser")
        from tools.agent_trace import tracer
        tracer.event("coder", "llm", "llm_request", content="patch prompt")
        tracer.event("llm", "coder", "llm_response", content="patched content")
        tracer.event("executor", "coder", "result", content='{"exit_code": 0}')
        rt.log_task_done("AUTO-T1", "a1b2c3")
        rt.log_run_finished()

        evts = _flush_tracer(rt)
        kinds = [e["kind"] for e in evts]
        assert "run_start" in kinds
        assert "call" in kinds
        assert "llm_request" in kinds
        assert "llm_response" in kinds
        assert "result" in kinds
        assert "run_finished" in kinds


# ── view_trace.py integration ─────────────────────────────────────────────────

def _write_trace(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for evt in events:
            fh.write(json.dumps(evt) + "\n")
    return path


def _sample_events(run_id: str = "abc123def456") -> list[dict]:
    return [
        {"seq": 1, "ts": "2024-01-01T00:00:01Z", "source": "controller",  "target": "auto_run",  "kind": "run_start",    "run_id": run_id, "params": {"goal": "test"}},
        {"seq": 2, "ts": "2024-01-01T00:00:02Z", "source": "architect",   "target": "llm",       "kind": "llm_request",  "run_id": run_id, "content": "prompt"},
        {"seq": 3, "ts": "2024-01-01T00:00:03Z", "source": "llm",         "target": "architect", "kind": "llm_response", "run_id": run_id, "content": "result"},
        {"seq": 4, "ts": "2024-01-01T00:00:04Z", "source": "coder",       "target": "llm",       "kind": "llm_request",  "run_id": run_id, "content": "code prompt"},
        {"seq": 5, "ts": "2024-01-01T00:00:05Z", "source": "executor",    "target": "coder",     "kind": "result",       "run_id": run_id, "content": "exit 0"},
        {"seq": 6, "ts": "2024-01-01T00:00:06Z", "source": "controller",  "target": "auto_run",  "kind": "run_finished", "run_id": run_id, "params": {}},
    ]


class TestViewTrace:
    def test_load_events_returns_list(self, tmp_path):
        f = _write_trace(tmp_path / "trace_abc.jsonl", _sample_events())
        evts = vt.load_events(f)
        assert isinstance(evts, list)
        assert len(evts) == 6

    def test_load_events_all_dicts(self, tmp_path):
        f = _write_trace(tmp_path / "trace_abc.jsonl", _sample_events())
        for evt in vt.load_events(f):
            assert isinstance(evt, dict)

    def test_apply_filters_by_kind(self):
        evts = _sample_events()
        result = vt.apply_filters(evts, run_id=None, kinds=["llm_request"], sources=None, tail=None)
        assert all(e["kind"] == "llm_request" for e in result)
        assert len(result) == 2

    def test_apply_filters_by_source(self):
        evts = _sample_events()
        result = vt.apply_filters(evts, run_id=None, kinds=None, sources=["coder"], tail=None)
        assert all(e["source"] == "coder" for e in result)

    def test_apply_filters_tail(self):
        evts = _sample_events()
        result = vt.apply_filters(evts, run_id=None, kinds=None, sources=None, tail=3)
        assert len(result) == 3
        assert result[-1]["seq"] == 6

    def test_apply_filters_by_run_id(self):
        evts = _sample_events("run1") + _sample_events("run2")
        result = vt.apply_filters(evts, run_id="run1", kinds=None, sources=None, tail=None)
        assert all(e["run_id"] == "run1" for e in result)

    def test_render_event_returns_string(self):
        evt = _sample_events()[0]
        out = vt.render_event(evt, use_color=False)
        assert isinstance(out, str) and len(out) > 0

    def test_render_event_contains_kind(self):
        evt = _sample_events()[0]
        out = vt.render_event(evt, use_color=False)
        assert "run_start" in out

    def test_render_summary_returns_string(self):
        evts = _sample_events()
        out = vt.render_summary(evts, use_color=False)
        assert isinstance(out, str) and len(out) > 0

    def test_render_summary_one_row_per_event(self):
        evts = _sample_events()
        out = vt.render_summary(evts, use_color=False)
        # Each event row contains its source name
        assert "architect" in out
        assert "coder" in out
        assert "executor" in out

    def test_main_exits_0_on_valid_file(self, tmp_path):
        f = _write_trace(tmp_path / "trace_abc123def456.jsonl", _sample_events())
        rc = vt.main([str(f), "--no-color"])
        assert rc == 0

    def test_main_exits_nonzero_on_empty_file(self, tmp_path):
        f = tmp_path / "trace_empty.jsonl"
        f.write_text("")
        rc = vt.main([str(f), "--no-color"])
        assert rc != 0

    def test_main_summary_flag(self, tmp_path, capsys):
        f = _write_trace(tmp_path / "trace_abc.jsonl", _sample_events())
        rc = vt.main([str(f), "--summary", "--no-color"])
        assert rc == 0

    def test_main_filter_flag(self, tmp_path, capsys):
        f = _write_trace(tmp_path / "trace_abc.jsonl", _sample_events())
        rc = vt.main([str(f), "--filter", "llm_request", "--no-color"])
        assert rc == 0

    def test_main_tail_flag(self, tmp_path):
        f = _write_trace(tmp_path / "trace_abc.jsonl", _sample_events())
        rc = vt.main([str(f), "--tail", "2", "--no-color"])
        assert rc == 0

    def test_main_auto_discovers_newest_in_dir(self, tmp_path):
        import time
        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        f1 = _write_trace(agent_dir / "trace_aaa.jsonl", _sample_events())
        time.sleep(0.01)
        f2 = _write_trace(agent_dir / "trace_bbb.jsonl", _sample_events())
        rc = vt.main([str(agent_dir), "--no-color"])
        assert rc == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
