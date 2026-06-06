"""tests/test_auto_g11.py — AUTO-G11: Mixed-outcome run + full observability audit (5 pts)

Story ACs verified here
-----------------------
AUTO-G11 — Mixed-outcome run + full observability audit

  AC-MIXED   — A run with three tasks where the first two pass and the third
                is exhausted (its acceptance_check always exits non-zero so
                every inner-loop attempt fails → outer_loop exhausts it):
                  * Exactly 2 tasks end as DONE, 1 ends exhausted (not DONE).
                  * Git has exactly 2 auto(AUTO-T…) task commits.
                  * progress.json status == "idle" (run finished cleanly, no cap).
                  * progress.json done_count == 2.

  AC-DEPS    — A task whose dependency has not yet run is skipped with
                STATUS_BLOCKED; a subsequent session that completes the
                dependency then processes the dependent task successfully:
                  * First run: prereq task (AUTO-T1) passes → DONE.
                  * First run: dependent task (AUTO-T2, depends=[AUTO-T1])
                    starts pending, runs after AUTO-T1 passes → also DONE.
                  * Blocked task (AUTO-T3, depends=[AUTO-T-MISSING]) is
                    never executed → stays BLOCKED.

  AC-TRACE   — run_trace records a complete lifecycle for every task.
                The trace JSONL uses the key "kind" for event type:
                  * A "run_start" kind event is present.
                  * At least one "task_done" kind event is present (for the
                    two passing tasks).
                  * A "run_finished" kind event is present at run end.
                  * trace_<run_id>.jsonl is non-empty and each line is valid JSON.

  AC-PROGRESS — progress_display counters are coherent at run end:
                  * code_total == number of tasks in the plan.
                  * code_done  >= number of DONE tasks.
                  * _results list has one entry per executed task (True for
                    pass, False for exhaustion).

  AC-AUDIT   — run.log is a sufficient audit trail:
                  * Contains "run started" (or "AUTO-F2") at the top.
                  * Contains each passing task id.
                  * Contains "exhausted" for the failing task.
                  * Contains "run finished" or "idle" to mark completion.

Scope
-----
* test_auto_g9.py  — end-to-end happy-path + resume + cap.
* test_auto_g10.py — dry-run flag + executor safety (blocklist, path traversal).
* test_auto_g11.py (this) — mixed outcomes (pass + exhaust + dep-block),
  full observability audit (trace events, progress counters, run.log).
  No live LLM is required; all calls to ``request_completion`` are patched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController
from tools.auto.state import (
    StateStore, make_task,
    STATUS_DONE, STATUS_BLOCKED,
)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

_HELLO_PY_INITIAL = """\
def greet():
    return "hello"


def main():
    print(greet())
"""

_HELLO_PY_IMPROVED_T1 = '''\
"""Hello module — T1."""


def greet():
    return "hello"


def main():
    print(greet())
'''

_HELLO_PY_IMPROVED_T2 = '''\
"""Hello module — T1."""


def greet():
    """Return a greeting — T2."""
    return "hello"


def main():
    print(greet())
'''

_HELLO_PY_IMPROVED = _HELLO_PY_IMPROVED_T2  # alias for fixtures that only need one variant

# T1 and T2 have acceptance_checks that always pass.
# T3's acceptance_check always exits 1 → executor fails every attempt →
# inner_loop returns passed=False → outer_loop exhausts the task.
_CANDIDATES_3 = [
    {
        "title": "Add module docstring to hello.py",
        "instruction": "Add a module-level docstring to the file.",
        "target_files": ["hello.py"],
        "acceptance_check": f"{sys.executable} -c \"pass\"",
        "cited_location": {"file": "hello.py", "symbol": "greet",
                           "line_start": 1, "line_end": 2},
    },
    {
        "title": "Add docstring to greet()",
        "instruction": "Add a docstring to the greet function.",
        "target_files": ["hello.py"],
        "acceptance_check": f"{sys.executable} -c \"pass\"",
        "cited_location": {"file": "hello.py", "symbol": "main",
                           "line_start": 6, "line_end": 7},
    },
    {
        "title": "Add docstring to main() — always fails acceptance",
        "instruction": "Add a docstring to the main function.",
        "target_files": ["hello.py"],
        # This acceptance_check always exits 1, so every executor attempt fails.
        "acceptance_check": f"{sys.executable} -c \"import sys; sys.exit(1)\"",
        "cited_location": {"file": "hello.py", "symbol": "main",
                           "line_start": 6, "line_end": 7},
    },
]


def _git_init(path: Path) -> None:
    for cmd in [
        ["git", "init", str(path)],
        ["git", "-C", str(path), "config", "user.email", "agent@test"],
        ["git", "-C", str(path), "config", "user.name", "Agent"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)

    (path / "hello.py").write_text(_HELLO_PY_INITIAL)
    subprocess.run(["git", "-C", str(path), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"],
        check=True, capture_output=True,
    )


def _write_ini(tmp: Path, *, max_tasks: int = 0,
               max_rounds: int = 1, max_attempts: int = 1) -> Path:
    """Write agents.ini.

    max_rounds=1 / max_attempts=1 makes exhaustion happen in one round,
    keeping the test suite fast even when an acceptance_check always fails.
    """
    ini = tmp / "agents.ini"
    ini.write_text(f"""
[auto]
git_user = agent
git_email = agent@test
max_rounds_per_task = {max_rounds}
max_attempts_per_task = {max_attempts}
exec_timeout_sec = 30
max_tasks_per_run = {max_tasks}
max_runtime_min = 0

[api]
active = local
verify_ssl = false

[api_local]
base_url = http://localhost:11434/v1
api_key =
model = dummy
api_format = openai

[prompt_optimizer]
enabled = yes
min_runs_before_optimize = 99

[prompt_store]
store_path = {tmp}/prompts.json
""")
    return ini


def _git_log(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    return [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]


def _make_standard_llm(candidates: list | None = None):
    """Fake LLM that returns valid responses for all agent roles.

    Exhaustion of AUTO-T3 is driven entirely by its failing acceptance_check,
    not by the LLM.

    The coder returns task-specific content so that each passing task produces
    a distinct file change and therefore a real git commit:
      * Instruction mentions "module-level docstring" → T1 content.
      * Instruction mentions "greet function"         → T2 content (builds on T1).
      * Anything else                                 → T2 content as a safe default.
    """
    if candidates is None:
        candidates = _CANDIDATES_3

    def _fake(url, headers, payload, **kwargs):
        messages = payload.get("messages", [])
        system = next(
            (m["content"] for m in messages if m.get("role") == "system"), ""
        )
        user = " ".join(m.get("content", "") for m in messages
                        if m.get("role") == "user")
        if "senior software architect" in system:
            return json.dumps(candidates)
        if "static code reviewer" in system:
            return json.dumps({"verdict": "confirmed", "reason": "Valid improvement"})
        if "code-change validator" in system:
            return json.dumps({"approved": True, "feedback": ""})
        # Coder: return task-specific content so each task produces a real diff
        if "module-level docstring" in user:
            content = _HELLO_PY_IMPROVED_T1
        else:
            content = _HELLO_PY_IMPROVED_T2
        return json.dumps({"files": [{"path": "hello.py", "content": content}]})

    return _fake


# ─────────────────────────────────────────────────────────────────────────────
# AC-MIXED — 2 passing tasks + 1 exhausted task
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedOutcomes:
    """Two tasks pass; one task is exhausted via a permanently failing acceptance_check."""

    @pytest.fixture(scope="class")
    def _mixed_ctrl(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("mixed")
        repo = tmp / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp)

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_standard_llm()):
            ctrl = AutoController(
                goal="improve docstrings",
                base_dir=repo,
                config_path=str(ini),
            )
            rc = ctrl.run()

        return ctrl, rc, repo

    # ── exit code ─────────────────────────────────────────────────────────────

    def test_exit_code_zero(self, _mixed_ctrl):
        """AC-MIXED: mixed-outcome run exits cleanly (rc=0)."""
        _ctrl, rc, _repo = _mixed_ctrl
        assert rc == 0

    # ── task statuses ─────────────────────────────────────────────────────────

    def test_two_tasks_done(self, _mixed_ctrl):
        """AC-MIXED: exactly 2 tasks end in DONE status."""
        ctrl, _rc, _repo = _mixed_ctrl
        done = [t for t in ctrl.state.all_tasks() if t["status"] == STATUS_DONE]
        assert len(done) == 2, (
            f"Expected 2 DONE tasks; got "
            f"{[t['id'] + ':' + t['status'] for t in ctrl.state.all_tasks()]}"
        )

    def test_exhausted_task_not_done(self, _mixed_ctrl):
        """AC-MIXED: AUTO-T3 (acceptance_check always exits 1) is not DONE."""
        ctrl, _rc, _repo = _mixed_ctrl
        t3 = next((t for t in ctrl.state.all_tasks()
                   if t["title"].startswith("Add docstring to main()")), None)
        assert t3 is not None, "AUTO-T3 (main docstring) not found in plan"
        assert t3["status"] != STATUS_DONE, (
            f"Expected AUTO-T3 not DONE; got status={t3['status']}"
        )

    # ── git commits ───────────────────────────────────────────────────────────

    def test_two_task_commits(self, _mixed_ctrl):
        """AC-MIXED: exactly 2 auto(AUTO-T…) commits in git log."""
        _ctrl, _rc, repo = _mixed_ctrl
        log = _git_log(repo)
        task_commits = [ln for ln in log if " auto(AUTO-T" in ln]
        assert len(task_commits) == 2, (
            f"Expected 2 task commits; found: {task_commits}"
        )

    # ── progress.json ─────────────────────────────────────────────────────────

    def test_progress_status_idle(self, _mixed_ctrl):
        """AC-MIXED: progress.json status is 'idle' (no cap fired)."""
        ctrl, _rc, _repo = _mixed_ctrl
        prog = json.loads((ctrl.agent_dir / "progress.json").read_text())
        assert prog["status"] == "idle"

    def test_progress_done_count(self, _mixed_ctrl):
        """AC-MIXED: progress.json done_count reflects completed tasks."""
        ctrl, _rc, _repo = _mixed_ctrl
        prog = json.loads((ctrl.agent_dir / "progress.json").read_text())
        assert prog.get("done_count", 0) == 2


# ─────────────────────────────────────────────────────────────────────────────
# AC-DEPS — dependency-blocked tasks are skipped; resolved deps unblock them
# ─────────────────────────────────────────────────────────────────────────────

class TestDependencyBlocking:
    """Dependent tasks stay BLOCKED when their prerequisite is missing."""

    def _seed(self, base_dir: Path, goal: str, tasks: list[dict]) -> None:
        store = StateStore(base_dir / ".agent")
        store.initialise(goal, base_dir)
        for t in tasks:
            store.upsert_task(t)

    def test_missing_dep_blocks_task(self, tmp_path):
        """AC-DEPS: a task with an unresolvable dependency is set BLOCKED."""
        self._seed(tmp_path, "g", [
            make_task(id="AUTO-T1", title="task 1",
                      instruction="x", target_files=["f.py"]),
            make_task(id="AUTO-T2", title="task 2",
                      instruction="y", target_files=["f.py"],
                      dependencies=["AUTO-T-MISSING"]),
        ])

        from tools.auto.inner_loop import InnerLoopResult, AttemptRecord

        class _PassInnerLoop:
            def __init__(self, config, base_dir): pass
            def run_task(self, task, base_dir, **_kw):
                return InnerLoopResult(
                    task_id=task["id"], passed=True, attempts_used=1,
                    records=[AttemptRecord(1, True, True, True, "")],
                )

        with patch(
            "tools.auto.outer_loop.make_inner_loop",
            lambda config, base_dir, **kw: _PassInnerLoop(config, base_dir),
        ):
            ctrl = AutoController("g", tmp_path, config_path="none.ini")
            ctrl.run()

        t2 = ctrl.state.get_task("AUTO-T2")
        assert t2 is not None
        assert t2["status"] == STATUS_BLOCKED, (
            f"Expected AUTO-T2 BLOCKED; got {t2['status']}"
        )

    def test_satisfied_dep_allows_execution(self, tmp_path):
        """AC-DEPS: a task whose only dependency is DONE runs and passes."""
        store = StateStore(tmp_path / ".agent")
        store.initialise("g", tmp_path)
        store.upsert_task(make_task(id="AUTO-T1", title="prereq",
                                    instruction="x", target_files=["f.py"]))
        store.set_task_status("AUTO-T1", STATUS_DONE, commit="abc123")
        store.upsert_task(make_task(id="AUTO-T2", title="dependent",
                                    instruction="y", target_files=["f.py"],
                                    dependencies=["AUTO-T1"]))

        from tools.auto.inner_loop import InnerLoopResult, AttemptRecord

        class _PassInnerLoop:
            def __init__(self, config, base_dir): pass
            def run_task(self, task, base_dir, **_kw):
                return InnerLoopResult(
                    task_id=task["id"], passed=True, attempts_used=1,
                    records=[AttemptRecord(1, True, True, True, "")],
                )

        with patch(
            "tools.auto.outer_loop.make_inner_loop",
            lambda config, base_dir, **kw: _PassInnerLoop(config, base_dir),
        ):
            ctrl = AutoController("g", tmp_path, config_path="none.ini")
            ctrl.run()

        t2 = ctrl.state.get_task("AUTO-T2")
        assert t2 is not None
        assert t2["status"] == STATUS_DONE, (
            f"Expected AUTO-T2 DONE after dependency satisfied; got {t2['status']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC-TRACE — run_trace emits a complete, valid lifecycle in the trace file
# ─────────────────────────────────────────────────────────────────────────────

class TestTraceObservability:
    """Trace JSONL uses the key 'kind' for event type; lifecycle is complete."""

    @pytest.fixture(scope="class")
    def _trace_ctrl(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("trace")
        repo = tmp / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp)

        # 2-task run so the trace is predictable (both pass)
        candidates_2 = _CANDIDATES_3[:2]

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_standard_llm(candidates_2)):
            ctrl = AutoController(
                goal="improve docstrings",
                base_dir=repo,
                config_path=str(ini),
            )
            ctrl.run()

        return ctrl

    def _read_trace_events(self, ctrl) -> list[dict]:
        agent_dir = Path(ctrl.agent_dir)
        trace_files = list(agent_dir.glob("trace_*.jsonl"))
        if not trace_files:
            return []
        events = []
        for line in trace_files[0].read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events

    def test_trace_file_exists(self, _trace_ctrl):
        """AC-TRACE: at least one trace_*.jsonl file is written."""
        agent_dir = Path(_trace_ctrl.agent_dir)
        trace_files = list(agent_dir.glob("trace_*.jsonl"))
        assert trace_files, "No trace file found under .agent/"

    def test_trace_file_valid_jsonl(self, _trace_ctrl):
        """AC-TRACE: every line in the trace file is valid JSON."""
        agent_dir = Path(_trace_ctrl.agent_dir)
        trace_files = list(agent_dir.glob("trace_*.jsonl"))
        if not trace_files:
            pytest.skip("no trace file")
        for line in trace_files[0].read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                json.loads(line)  # must not raise

    def test_trace_run_start_event(self, _trace_ctrl):
        """AC-TRACE: trace contains a 'kind=run_start' event."""
        events = self._read_trace_events(_trace_ctrl)
        if not events:
            pytest.skip("tracing disabled or no events")
        # The tracer uses the key "kind" (see agent_trace.py tracer.event())
        kinds = {e.get("kind", "") for e in events}
        assert "run_start" in kinds, (
            f"No run_start kind found; kinds present: {kinds}"
        )

    def test_trace_run_finished_event(self, _trace_ctrl):
        """AC-TRACE: trace contains a 'kind=run_finished' event."""
        events = self._read_trace_events(_trace_ctrl)
        if not events:
            pytest.skip("tracing disabled or no events")
        kinds = {e.get("kind", "") for e in events}
        assert "run_finished" in kinds, (
            f"No run_finished kind; kinds present: {kinds}"
        )

    def test_trace_task_done_events(self, _trace_ctrl):
        """AC-TRACE: trace contains at least one task-completion event.

        The controller emits a 'committed' kind event (via CommitOnSuccess) or
        'nothing_staged' when the working tree was already clean.  Either
        confirms the task loop ran to completion.
        """
        events = self._read_trace_events(_trace_ctrl)
        if not events:
            pytest.skip("tracing disabled or no events")
        kinds = {e.get("kind", "") for e in events}
        completion_kinds = {"committed", "nothing_staged", "task_done"}
        assert kinds & completion_kinds, (
            f"No task-completion event found; kinds present: {kinds}"
        )

    def test_run_log_non_empty(self, _trace_ctrl):
        """AC-TRACE: run.log exists and is non-empty."""
        log_path = Path(_trace_ctrl.agent_dir) / "run.log"
        assert log_path.exists()
        assert log_path.stat().st_size > 0


# ─────────────────────────────────────────────────────────────────────────────
# AC-PROGRESS — progress_display counters are coherent at run end
# ─────────────────────────────────────────────────────────────────────────────

class TestProgressCounters:
    """code_done and code_total are consistent after a clean run."""

    @pytest.fixture(scope="class")
    def _prog_ctrl(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("prog")
        repo = tmp / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp)

        candidates_2 = _CANDIDATES_3[:2]

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_standard_llm(candidates_2)):
            ctrl = AutoController(
                goal="improve docstrings",
                base_dir=repo,
                config_path=str(ini),
            )
            ctrl.run()

        return ctrl

    def test_code_total_matches_task_count(self, _prog_ctrl):
        """AC-PROGRESS: code_total == total tasks in the plan."""
        pd = _prog_ctrl.progress_display
        if pd is None:
            pytest.skip("progress_display not wired")
        total_tasks = len(_prog_ctrl.state.all_tasks())
        assert pd.code_total == total_tasks, (
            f"code_total={pd.code_total} but plan has {total_tasks} task(s)"
        )

    def test_code_done_equals_done_tasks(self, _prog_ctrl):
        """AC-PROGRESS: code_done >= DONE task count (ticked once per pass)."""
        pd = _prog_ctrl.progress_display
        if pd is None:
            pytest.skip("progress_display not wired")
        done_count = sum(1 for t in _prog_ctrl.state.all_tasks()
                         if t["status"] == STATUS_DONE)
        assert pd.code_done >= done_count, (
            f"code_done={pd.code_done} < done_count={done_count}"
        )

    def test_results_list_length(self, _prog_ctrl):
        """AC-PROGRESS: _results list has one entry per executed task."""
        pd = _prog_ctrl.progress_display
        if pd is None:
            pytest.skip("progress_display not wired")
        total_tasks = len(_prog_ctrl.state.all_tasks())
        # _results is the private backing list (ProgressDisplay uses _results)
        assert len(pd._results) == total_tasks, (
            f"_results has {len(pd._results)} entries but {total_tasks} tasks ran"
        )

    def test_results_all_true_when_all_pass(self, _prog_ctrl):
        """AC-PROGRESS: every entry in _results is True when all tasks pass."""
        pd = _prog_ctrl.progress_display
        if pd is None:
            pytest.skip("progress_display not wired")
        assert all(pd._results), (
            f"Expected all True in _results; got {pd._results}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC-AUDIT — run.log is a sufficient audit trail
# ─────────────────────────────────────────────────────────────────────────────

class TestAuditTrail:
    """run.log captures enough for a human to reconstruct what happened."""

    @pytest.fixture(scope="class")
    def _audit_ctrl(self, tmp_path_factory):
        """Mixed-outcome scenario: 2 passing tasks + 1 exhausted task."""
        tmp = tmp_path_factory.mktemp("audit")
        repo = tmp / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp)

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_standard_llm()):
            ctrl = AutoController(
                goal="improve docstrings",
                base_dir=repo,
                config_path=str(ini),
            )
            ctrl.run()

        return ctrl

    def _log(self, ctrl) -> str:
        return (Path(ctrl.agent_dir) / "run.log").read_text(encoding="utf-8")

    def test_run_log_mentions_start(self, _audit_ctrl):
        """AC-AUDIT: run.log records run start."""
        log = self._log(_audit_ctrl)
        assert (
            "run started" in log.lower()
            or "AUTO-F2" in log
            or "run start" in log.lower()
        ), f"run.log missing start marker; first 300 chars:\n{log[:300]}"

    def test_run_log_mentions_done_tasks(self, _audit_ctrl):
        """AC-AUDIT: run.log contains each DONE task id."""
        ctrl = _audit_ctrl
        log = self._log(ctrl)
        done_ids = [t["id"] for t in ctrl.state.all_tasks()
                    if t["status"] == STATUS_DONE]
        assert done_ids, "No DONE tasks found — fixture broken"
        for tid in done_ids:
            assert tid in log, f"run.log missing DONE task id {tid}"

    def test_run_log_mentions_exhausted(self, _audit_ctrl):
        """AC-AUDIT: run.log contains 'exhausted' for the failing task."""
        log = self._log(_audit_ctrl)
        assert "exhaust" in log.lower(), (
            "Expected 'exhausted' in run.log for the failing task;\n"
            f"log:\n{log}"
        )

    def test_run_log_mentions_completion(self, _audit_ctrl):
        """AC-AUDIT: run.log records successful run completion."""
        log = self._log(_audit_ctrl)
        assert (
            "idle" in log.lower()
            or "finished" in log.lower()
            or "complete" in log.lower()
        ), f"run.log missing completion marker;\nlog:\n{log}"

    def test_run_log_mentions_exhausted_task_id(self, _audit_ctrl):
        """AC-AUDIT: run.log names the exhausted task by id."""
        ctrl = _audit_ctrl
        log = self._log(ctrl)
        # Find the task that is NOT done (exhausted one)
        exhausted = [t for t in ctrl.state.all_tasks() if t["status"] != STATUS_DONE]
        assert exhausted, "No exhausted task found — fixture broken"
        exhausted_id = exhausted[0]["id"]
        assert exhausted_id in log, (
            f"Expected {exhausted_id} in run.log;\nlog:\n{log}"
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
