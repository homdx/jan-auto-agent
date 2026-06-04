"""tests/test_auto_g9.py — AUTO-G9: End-to-end integration test (5 pts)

Story ACs verified here
-----------------------
AUTO-G9 — End-to-end integration test
  AC-FULL — Full pipeline against a tiny sample repo with a fake LLM:
              plan.json populated → ≥1 task executed → ≥1 real git commit
              → progress.json written → trace file written.
  AC-RESUME — Kill after plan phase (plan committed, no tasks executed),
               restart, verify execution resumes: pending tasks run,
               plan is NOT rebuilt, DONE tasks are not re-run.
  AC-CAP — Wall-clock / task cap fires mid-loop and stops gracefully:
              progress.json records stop_reason, run is resumable.

Scope
-----
* test_auto_g1.py–g8.py  — unit / component tests for individual modules.
* test_auto_g9.py (this) — wires everything together with real git + real
  executor + fake LLM (patched ``tools.llm_stream.request_completion``).
  No live model is required; acceptance checks are trivially shell one-liners.
"""

from __future__ import annotations

import configparser
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController
from tools.auto.state import STATUS_DONE


# ─────────────────────────────────────────────────────────────────────────────
# Sample repo content
# ─────────────────────────────────────────────────────────────────────────────

# Initial source file — two functions so gate1's symbol-existence check passes
# for both candidate tasks.
_HELLO_PY_INITIAL = """\
def greet():
    return "hello"


def main():
    print(greet())
"""

# What the fake coder writes for any improvement task.
_HELLO_PY_IMPROVED = '''\
"""Hello module."""


def greet():
    """Return a greeting."""
    return "hello"


def main():
    """Entry point."""
    print(greet())
'''

# Architect candidates — two distinct titles/symbols so gate1 dedup keeps both.
_CANDIDATES_2 = [
    {
        "title": "Add module docstring to hello.py",
        "instruction": "Add a module-level docstring to the file.",
        "target_files": ["hello.py"],
        "acceptance_check": f"{sys.executable} -c \"pass\"",
        "cited_location": {
            "file": "hello.py",
            "symbol": "greet",
            "line_start": 1,
            "line_end": 2,
        },
    },
    {
        "title": "Add docstring to main function",
        "instruction": "Add a docstring to the main() function.",
        "target_files": ["hello.py"],
        "acceptance_check": f"{sys.executable} -c \"pass\"",
        "cited_location": {
            "file": "hello.py",
            "symbol": "main",
            "line_start": 5,
            "line_end": 6,
        },
    },
]

_CANDIDATES_1 = _CANDIDATES_2[:1]


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def _git_init(path: Path) -> None:
    """Initialise a real git repo with an initial commit containing hello.py."""
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


def _write_ini(tmp: Path, *, max_tasks: int = 0) -> Path:
    """Write a minimal agents.ini and return its path."""
    ini = tmp / "agents.ini"
    ini.write_text(f"""
[auto]
git_user = agent
git_email = agent@test
max_rounds_per_task = 10
max_attempts_per_task = 5
exec_timeout_sec = 30
max_tasks_per_run = {max_tasks}

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


def _make_fake_llm(candidates: list) -> callable:
    """Return a ``request_completion`` replacement that never calls a real LLM.

    Dispatches by system-prompt content:
      * Architect  → JSON list of *candidates*
      * Gate-1     → ``{"verdict": "confirmed", ...}``
      * Gate-2     → ``{"approved": true, "feedback": ""}``
      * Coder      → JSON with the improved hello.py content
    """
    def _fake(url, headers, payload, **kwargs):
        messages = payload.get("messages", [])
        system = next(
            (m["content"] for m in messages if m.get("role") == "system"), ""
        )

        if "senior software architect" in system:
            return json.dumps(candidates)

        if "static code reviewer" in system:
            # Gate-1 LLM stage — always confirm
            return json.dumps({"verdict": "confirmed", "reason": "Valid improvement"})

        if "code-change validator" in system:
            # Gate-2 validator — always approve
            return json.dumps({"approved": True, "feedback": ""})

        # Coder — write the improved file
        return json.dumps({
            "files": [{"path": "hello.py", "content": _HELLO_PY_IMPROVED}]
        })

    return _fake


def _run(tmp: Path, *, n_candidates: int = 1,
         max_tasks: int = 0) -> AutoController:
    """Set up a real repo+config, run AutoController, return the controller."""
    repo = tmp / "repo"
    repo.mkdir()
    _git_init(repo)

    candidates = _CANDIDATES_2[:n_candidates]
    ini = _write_ini(tmp, max_tasks=max_tasks)

    with patch("tools.llm_stream.request_completion",
               side_effect=_make_fake_llm(candidates)):
        ctrl = AutoController(
            goal="improve current code",
            base_dir=repo,
            config_path=str(ini),
        )
        ctrl.run()

    return ctrl


def _git_log(repo: Path) -> list[str]:
    """Return the one-line git log for *repo* as a list of lines."""
    result = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"],
        capture_output=True, text=True, check=True,
    )
    return [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]


def _agent_dir(ctrl: AutoController) -> Path:
    return Path(ctrl.agent_dir)


# ─────────────────────────────────────────────────────────────────────────────
# AC-FULL — plan.json populated → ≥1 task executed → ≥1 commit → files written
# ─────────────────────────────────────────────────────────────────────────────

class TestFullPipeline:
    """Full end-to-end run with a single improvement task and a real git repo."""

    @pytest.fixture(scope="class")
    def ctrl(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("full")
        return _run(tmp, n_candidates=1)

    # ── plan.json ─────────────────────────────────────────────────────────────

    def test_plan_json_exists(self, ctrl):
        """AC-FULL: plan.json is present in .agent/."""
        assert (_agent_dir(ctrl) / "plan.json").exists()

    def test_plan_json_has_tasks(self, ctrl):
        """AC-FULL: plan.json contains at least one task."""
        plan = json.loads((_agent_dir(ctrl) / "plan.json").read_text())
        assert len(plan.get("tasks", [])) >= 1

    def test_plan_task_is_done(self, ctrl):
        """AC-FULL: the single task reaches status=done after execution."""
        tasks = ctrl.state.all_tasks()
        assert any(t["status"] == STATUS_DONE for t in tasks)

    # ── git commits ───────────────────────────────────────────────────────────

    def test_at_least_one_task_commit(self, ctrl):
        """AC-FULL: at least one auto(<id>): … commit exists."""
        log = _git_log(ctrl.base_dir)
        task_commits = [ln for ln in log if ln.split(" ", 1)[-1].startswith("auto(")]
        assert len(task_commits) >= 1

    def test_task_commit_message_format(self, ctrl):
        """AC-FULL: task commit follows auto(<task-id>): <title> convention."""
        log = _git_log(ctrl.base_dir)
        task_commits = [ln for ln in log if " auto(" in ln]
        assert task_commits, "no auto(...) commits found"
        # Format: <hash> auto(AUTO-T1): <title>
        msg = task_commits[0].split(" ", 1)[-1]
        assert msg.startswith("auto(AUTO-T")

    def test_plan_commit_present(self, ctrl):
        """AC-FULL: the plan emission produces a commit before task execution."""
        log = _git_log(ctrl.base_dir)
        plan_commits = [ln for ln in log if "emit plan" in ln or "AUTO-B5" in ln]
        assert plan_commits, "no plan-emission commit found"

    # ── progress.json ─────────────────────────────────────────────────────────

    def test_progress_json_exists(self, ctrl):
        """AC-FULL: progress.json is written during the run."""
        assert (_agent_dir(ctrl) / "progress.json").exists()

    def test_progress_status_idle_on_clean_finish(self, ctrl):
        """AC-FULL: status is 'idle' after a clean (un-capped) run."""
        prog = json.loads((_agent_dir(ctrl) / "progress.json").read_text())
        assert prog["status"] == "idle"

    # ── run.log ───────────────────────────────────────────────────────────────

    def test_run_log_exists(self, ctrl):
        """AC-FULL: run.log is written."""
        assert (_agent_dir(ctrl) / "run.log").exists()

    def test_run_log_records_task_done(self, ctrl):
        """AC-FULL: run.log mentions the task being completed."""
        log_text = (_agent_dir(ctrl) / "run.log").read_text(encoding="utf-8")
        assert "DONE" in log_text or "completed" in log_text

    # ── trace file ────────────────────────────────────────────────────────────

    def test_trace_file_exists(self, ctrl):
        """AC-FULL: trace_<run_id>.jsonl is created in .agent/."""
        traces = list(_agent_dir(ctrl).glob("trace_*.jsonl"))
        assert len(traces) >= 1

    def test_trace_file_is_valid_jsonl(self, ctrl):
        """AC-FULL: every line in the trace file is valid JSON."""
        traces = list(_agent_dir(ctrl).glob("trace_*.jsonl"))
        trace_path = traces[0]
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                json.loads(line)  # must not raise

    def test_improvements_md_written(self, ctrl):
        """AC-FULL: IMPROVEMENTS.md is written to the repo root."""
        assert (ctrl.base_dir / "IMPROVEMENTS.md").exists()


# ─────────────────────────────────────────────────────────────────────────────
# AC-RESUME — kill after plan, restart, finish
# ─────────────────────────────────────────────────────────────────────────────

class TestResume:
    """Simulate an interrupted run: plan committed, process killed, then resumed."""

    @pytest.fixture(scope="class")
    def _repo_after_kill(self, tmp_path_factory):
        """
        Run 1 (plan-only):  emit the plan, return before executing any task.
        Returns (repo, ini, agent_dir) after the "kill".
        """
        tmp = tmp_path_factory.mktemp("resume")
        repo = tmp / "repo"
        repo.mkdir()
        _git_init(repo)

        ini = _write_ini(tmp)
        candidates = _CANDIDATES_1

        # Patch _run_task_loop to return immediately — simulates process killed
        # right after the plan phase completed.
        def _no_execute(self_ctrl):
            return None, 0

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_fake_llm(candidates)), \
             patch.object(AutoController, "_run_task_loop", _no_execute):
            ctrl1 = AutoController(
                goal="improve current code", base_dir=repo, config_path=str(ini)
            )
            ctrl1.run()

        return repo, ini, candidates

    def test_plan_is_committed_after_kill(self, _repo_after_kill):
        """After a kill, plan.json exists and tasks are in 'todo' state."""
        repo, ini, _ = _repo_after_kill
        agent_dir = repo / ".agent"
        plan = json.loads((agent_dir / "plan.json").read_text())
        tasks = plan.get("tasks", [])
        assert len(tasks) >= 1
        # All tasks still pending — none executed
        assert all(t["status"] == "todo" for t in tasks)

    def test_no_task_commit_after_kill(self, _repo_after_kill):
        """After a kill, no auto(<task-id>): commits exist yet."""
        repo, ini, _ = _repo_after_kill
        log = _git_log(repo)
        task_commits = [ln for ln in log if " auto(AUTO-T" in ln]
        assert len(task_commits) == 0

    def test_resume_executes_pending_tasks(self, _repo_after_kill):
        """Run 2: restart resumes correctly — pending tasks are executed."""
        repo, ini, candidates = _repo_after_kill

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_fake_llm(candidates)):
            ctrl2 = AutoController(
                goal="improve current code", base_dir=repo, config_path=str(ini)
            )
            ctrl2.run()

        tasks = ctrl2.state.all_tasks()
        assert any(t["status"] == STATUS_DONE for t in tasks)

    def test_resume_does_not_rebuild_plan(self, _repo_after_kill):
        """Run 2: plan.json is the same object — plan phase is skipped."""
        repo, ini, candidates = _repo_after_kill

        # Capture whether review_clusters is called (it must NOT be in run 2)
        arch_calls = []

        original_review = None
        try:
            from tools.auto import pipeline as _pipe_mod
            original_review = _pipe_mod.review_clusters
        except Exception:
            pass

        def _spy_review(*a, **kw):
            arch_calls.append(1)
            return original_review(*a, **kw) if original_review else []

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_fake_llm(candidates)), \
             patch("tools.auto.pipeline.review_clusters",
                   side_effect=_spy_review):
            ctrl2 = AutoController(
                goal="improve current code", base_dir=repo, config_path=str(ini)
            )
            ctrl2.run()

        assert len(arch_calls) == 0, (
            "review_clusters was called on resume — plan phase should be skipped"
        )

    def test_resume_adds_task_commit(self, _repo_after_kill):
        """Run 2: at least one auto(<task-id>): commit is present after resume."""
        repo, ini, candidates = _repo_after_kill

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_fake_llm(candidates)):
            ctrl2 = AutoController(
                goal="improve current code", base_dir=repo, config_path=str(ini)
            )
            ctrl2.run()

        log = _git_log(repo)
        task_commits = [ln for ln in log if " auto(AUTO-T" in ln]
        assert len(task_commits) >= 1

    def test_resume_progress_idle(self, _repo_after_kill):
        """Run 2: progress.json is 'idle' after a clean resumption."""
        repo, ini, candidates = _repo_after_kill

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_fake_llm(candidates)):
            ctrl2 = AutoController(
                goal="improve current code", base_dir=repo, config_path=str(ini)
            )
            ctrl2.run()

        prog = json.loads((repo / ".agent" / "progress.json").read_text())
        assert prog["status"] == "idle"


# ─────────────────────────────────────────────────────────────────────────────
# AC-CAP — task cap stops the loop gracefully
# ─────────────────────────────────────────────────────────────────────────────

class TestCap:
    """Task cap fires mid-loop; progress.json records stop_reason; run resumable."""

    @pytest.fixture(scope="class")
    def _capped_ctrl(self, tmp_path_factory):
        """Run with 2 tasks but max_tasks_per_run=1."""
        tmp = tmp_path_factory.mktemp("cap")
        return _run(tmp, n_candidates=2, max_tasks=1)

    def test_task_cap_stops_at_one(self, _capped_ctrl):
        """AC-CAP: exactly 1 task is done when cap=1 and 2 tasks exist."""
        ctrl = _capped_ctrl
        done = [t for t in ctrl.state.all_tasks() if t["status"] == STATUS_DONE]
        assert len(done) == 1

    def test_task_cap_leaves_one_pending(self, _capped_ctrl):
        """AC-CAP: the second task is still pending after capping."""
        ctrl = _capped_ctrl
        pending = [
            t for t in ctrl.state.all_tasks()
            if t["status"] in ("todo", "in_progress")
        ]
        assert len(pending) >= 1

    def test_task_cap_progress_status_capped(self, _capped_ctrl):
        """AC-CAP: progress.json status is 'capped' after the run."""
        prog = json.loads(
            (_agent_dir(_capped_ctrl) / "progress.json").read_text()
        )
        assert prog["status"] == "capped"

    def test_task_cap_progress_stop_reason(self, _capped_ctrl):
        """AC-CAP: progress.json stop_reason is 'task_cap'."""
        prog = json.loads(
            (_agent_dir(_capped_ctrl) / "progress.json").read_text()
        )
        assert prog.get("stop_reason") == "task_cap"

    def test_task_cap_run_log_mentions_cap(self, _capped_ctrl):
        """AC-CAP: run.log records that the cap fired."""
        log_text = (_agent_dir(_capped_ctrl) / "run.log").read_text(encoding="utf-8")
        assert "cap" in log_text.lower()

    def test_task_cap_run_is_resumable(self, _capped_ctrl):
        """AC-CAP: a subsequent run picks up the remaining task."""
        ctrl = _capped_ctrl
        ini = ctrl.base_dir.parent / "agents.ini"  # written by _run()
        # Rewrite ini with no cap so resume completes
        ini.write_text(ini.read_text().replace(
            "max_tasks_per_run = 1", "max_tasks_per_run = 0"
        ))

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_fake_llm(_CANDIDATES_2)):
            ctrl2 = AutoController(
                goal="improve current code",
                base_dir=ctrl.base_dir,
                config_path=str(ini),
            )
            ctrl2.run()

        done = [t for t in ctrl2.state.all_tasks() if t["status"] == STATUS_DONE]
        assert len(done) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Sanity: run() returns 0 on both clean and capped runs
# ─────────────────────────────────────────────────────────────────────────────

class TestExitCodes:
    def test_clean_run_exits_zero(self, tmp_path):
        """A clean run returns exit code 0."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp_path)

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_fake_llm(_CANDIDATES_1)):
            ctrl = AutoController(
                goal="improve current code", base_dir=repo, config_path=str(ini)
            )
            rc = ctrl.run()

        assert rc == 0

    def test_capped_run_exits_zero(self, tmp_path):
        """A task-capped run also returns exit code 0 (graceful stop)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        ini = _write_ini(tmp_path, max_tasks=1)

        with patch("tools.llm_stream.request_completion",
                   side_effect=_make_fake_llm(_CANDIDATES_2)):
            ctrl = AutoController(
                goal="improve current code", base_dir=repo, config_path=str(ini)
            )
            rc = ctrl.run()

        assert rc == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))