"""tests/test_auto_g3.py — AUTO-G3: Commit-on-success wiring (integration).

Story ACs verified here
-----------------------
AUTO-G3 — Commit-on-success wiring (2 pts)
  AC1 — N validated tasks → N commits authored by the agent identity;
         commit hashes stored in plan.json (``commit`` field on each task).
  AC2 — Commit message format is ``auto(<task-id>): <title>`` for every task.
  AC3 — Author name/email on each commit matches the agent identity from config.
  AC4 — No-git path: when controller.git is None, tasks still end DONE with
         no commit field (or commit="") and no crash.

How this differs from the G2 and C5 test suites
------------------------------------------------
* test_auto_c5.py — unit-tests CommitOnSuccess in isolation (FakeGitManager).
* test_auto_g2.py — verifies the controller *wiring* with mocks (patches
  CommitOnSuccess and outer_loop at the boundary).
* test_auto_g3.py (this file) — end-to-end integration: real GitManager,
  real git subprocess calls in a tmp repo, fake outer_loop only.

All tests are offline; no live LLM is needed.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.controller import AutoController, RunLimits
from tools.auto.git_manager import GitManager, make_git_manager
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import StateStore, STATUS_DONE


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_AGENT_USER  = "auto-agent-test"
_AGENT_EMAIL = "auto-agent-test@localhost"


def _git_config(user: str = _AGENT_USER, email: str = _AGENT_EMAIL) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": user, "git_email": email}
    return cfg


def _make_repo(path: Path) -> None:
    """Initialise a bare git repo with an initial commit so HEAD exists."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", _AGENT_USER],  cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", _AGENT_EMAIL], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)


def _make_task(task_id: str, title: str = "") -> dict:
    return {
        "id": task_id,
        "title": title or f"Task {task_id}",
        "instruction": "do something",
        "target_files": [],
        "acceptance_check": "true",
        "status": "todo",
        "dependencies": [],
        "attempt": 0,
        "round": 0,
        "cited_locations": [],
    }


def _make_controller(
    tmp_path: Path,
    tasks: list[dict],
    *,
    cfg: configparser.ConfigParser | None = None,
) -> AutoController:
    """Build a minimal AutoController with a *real* GitManager and StateStore."""
    base = tmp_path / "repo"
    base.mkdir()
    _make_repo(base)

    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "test"
    ctrl.base_dir = base
    ctrl.config_path = "agents.ini"
    ctrl.agent_dir = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"

    import time
    ctrl._time_fn   = time.monotonic
    ctrl._start_time = time.monotonic()
    ctrl.limits     = RunLimits()

    ctrl.state = StateStore(ctrl.agent_dir)
    ctrl.state.initialise("test", base)
    for t in tasks:
        ctrl.state.upsert_task(t)

    # Real GitManager — this is the key difference from G2 tests
    ctrl.git = make_git_manager(base, cfg or _git_config())

    ctrl.run_trace        = MagicMock()
    ctrl.progress_display = MagicMock()
    ctrl.metrics_stream   = MagicMock()
    ctrl.auto_tuner       = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = SimpleNamespace(
        promoted=False, new_prompt_score=0.0
    )

    return ctrl


def _passed_result(task_id: str) -> OuterLoopResult:
    inner = [SimpleNamespace(attempts_used=1, last_feedback="")]
    return OuterLoopResult(
        task_id=task_id,
        passed=True,
        rounds_used=1,
        exhausted=False,
        feedback_files=[],
        inner_results=inner,
    )


def _git_log(repo: Path, fmt: str, n: int = 20) -> list[str]:
    r = subprocess.run(
        ["git", "log", f"--format={fmt}", f"-{n}"],
        cwd=repo, capture_output=True, text=True, check=True,
    )
    return [line for line in r.stdout.splitlines() if line.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — N tasks → N commits with hashes stored
# ─────────────────────────────────────────────────────────────────────────────

class TestG3CommitCount:
    def _run_n_tasks(self, tmp_path: Path, n: int):
        tasks = [_make_task(f"T-{i}", f"Fix thing {i}") for i in range(1, n + 1)]
        ctrl = _make_controller(tmp_path, tasks)

        results = [_passed_result(f"T-{i}") for i in range(1, n + 1)]
        call_count = [0]

        def run_task_with_change(task, base_dir):
            # Write a new file so git has something to stage for each task
            call_count[0] += 1
            (ctrl.base_dir / f"change_{call_count[0]}.txt").write_text(
                f"change {call_count[0]}\n"
            )
            return results[call_count[0] - 1]

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = run_task_with_change

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        return ctrl, tasks_done

    def test_single_task_produces_one_commit(self, tmp_path):
        """AC1: one passing task → exactly one auto-commit on top of init commit."""
        ctrl, tasks_done = self._run_n_tasks(tmp_path, 1)
        assert tasks_done == 1

        msgs = _git_log(ctrl.base_dir, "%s")
        # First message (most recent) must be the auto-commit
        assert msgs[0].startswith("auto(T-1):")

    def test_three_tasks_produce_three_commits(self, tmp_path):
        """AC1: three passing tasks → three auto-commits (one per task)."""
        ctrl, tasks_done = self._run_n_tasks(tmp_path, 3)
        assert tasks_done == 3

        msgs = _git_log(ctrl.base_dir, "%s")
        auto_commits = [m for m in msgs if m.startswith("auto(")]
        assert len(auto_commits) == 3

    def test_hashes_stored_in_plan_json(self, tmp_path):
        """AC1: commit hash is written to the task's ``commit`` field in plan.json."""
        ctrl, tasks_done = self._run_n_tasks(tmp_path, 2)
        assert tasks_done == 2

        real_hashes = _git_log(ctrl.base_dir, "%H")
        auto_hashes = {h for h in real_hashes}

        for tid in ("T-1", "T-2"):
            task_record = ctrl.state.get_task(tid)
            assert task_record["status"] == STATUS_DONE
            stored_hash = task_record.get("commit")
            assert stored_hash, f"No commit hash stored for {tid}"
            assert stored_hash in auto_hashes, (
                f"Stored hash {stored_hash!r} for {tid} not found in git log"
            )

    def test_each_hash_is_unique(self, tmp_path):
        """AC1: every task gets its own distinct commit hash."""
        ctrl, _ = self._run_n_tasks(tmp_path, 3)

        hashes = [
            ctrl.state.get_task(f"T-{i}").get("commit")
            for i in range(1, 4)
        ]
        assert len(set(hashes)) == 3, "Expected 3 distinct commit hashes"


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — Commit message format: auto(<task-id>): <title>
# ─────────────────────────────────────────────────────────────────────────────

class TestG3CommitMessageFormat:
    def test_message_format_single_task(self, tmp_path):
        """AC2: commit subject is exactly ``auto(<id>): <title>``."""
        task = _make_task("AUTO-T42", "Fix the off-by-one")
        ctrl = _make_controller(tmp_path, [task])

        fake_outer = MagicMock()

        def run_task_write_file(t, base_dir):
            (ctrl.base_dir / "patch.py").write_text("x = 1\n")
            return _passed_result("AUTO-T42")

        fake_outer.run_task.side_effect = run_task_write_file

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        msgs = _git_log(ctrl.base_dir, "%s")
        assert msgs[0] == "auto(AUTO-T42): Fix the off-by-one"

    def test_message_format_multiple_tasks(self, tmp_path):
        """AC2: each task's commit follows the format independently."""
        tasks = [
            _make_task("G3-1", "Refactor module A"),
            _make_task("G3-2", "Add type hints"),
        ]
        ctrl = _make_controller(tmp_path, tasks)

        call_count = [0]

        def run_task_write_file(t, base_dir):
            call_count[0] += 1
            (ctrl.base_dir / f"file_{call_count[0]}.py").write_text("pass\n")
            return _passed_result(t["id"])

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = run_task_write_file

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        msgs = _git_log(ctrl.base_dir, "%s")
        auto_msgs = [m for m in msgs if m.startswith("auto(")]
        assert "auto(G3-1): Refactor module A" in auto_msgs
        assert "auto(G3-2): Add type hints" in auto_msgs


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — Commit author identity matches agent config
# ─────────────────────────────────────────────────────────────────────────────

class TestG3AuthorIdentity:
    def test_author_name_and_email_on_commit(self, tmp_path):
        """AC3: auto-commits carry the agent name/email from config."""
        custom_user  = "my-bot"
        custom_email = "bot@example.org"
        cfg = _git_config(user=custom_user, email=custom_email)

        task = _make_task("ID-1", "Some work")
        ctrl = _make_controller(tmp_path, [task], cfg=cfg)

        def run_task_write_file(t, base_dir):
            (ctrl.base_dir / "work.txt").write_text("done\n")
            return _passed_result("ID-1")

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = run_task_write_file

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        # Verify the most recent commit's author
        author_names  = _git_log(ctrl.base_dir, "%an")
        author_emails = _git_log(ctrl.base_dir, "%ae")
        assert author_names[0]  == custom_user
        assert author_emails[0] == custom_email

    def test_default_identity_used_when_no_config(self, tmp_path):
        """AC3: default agent identity (auto-agent) used when config has no overrides."""
        cfg = configparser.ConfigParser()  # empty — no [auto] section
        task = _make_task("ID-DEF", "Default identity task")
        ctrl = _make_controller(tmp_path, [task], cfg=cfg)

        def run_task_write_file(t, base_dir):
            (ctrl.base_dir / "default.txt").write_text("x\n")
            return _passed_result("ID-DEF")

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = run_task_write_file

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        author_names = _git_log(ctrl.base_dir, "%an")
        assert author_names[0] == "auto-agent"


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — No-git path: tasks end DONE, no crash
# ─────────────────────────────────────────────────────────────────────────────

class TestG3NoGitPath:
    def test_no_git_task_marked_done(self, tmp_path):
        """AC4: git=None → task still reaches STATUS_DONE with no crash."""
        task = _make_task("T-NOGIT", "No git task")
        ctrl = _make_controller(tmp_path, [task])
        ctrl.git = None  # simulate git unavailable

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-NOGIT")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            stop_reason, tasks_done = ctrl._run_task_loop()

        assert stop_reason is None
        assert tasks_done == 1
        assert ctrl.state.get_task("T-NOGIT")["status"] == STATUS_DONE

    def test_no_git_multiple_tasks_all_done(self, tmp_path):
        """AC4: multiple tasks all complete DONE without git."""
        tasks = [_make_task(f"NG-{i}") for i in range(3)]
        ctrl = _make_controller(tmp_path, tasks)
        ctrl.git = None

        fake_outer = MagicMock()
        fake_outer.run_task.side_effect = [
            _passed_result(f"NG-{i}") for i in range(3)
        ]

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            _, tasks_done = ctrl._run_task_loop()

        assert tasks_done == 3
        for i in range(3):
            assert ctrl.state.get_task(f"NG-{i}")["status"] == STATUS_DONE

    def test_no_git_run_log_written(self, tmp_path):
        """AC4: no-git path still writes a run.log completion entry."""
        task = _make_task("T-LOG", "Log test")
        ctrl = _make_controller(tmp_path, [task])
        ctrl.git = None

        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-LOG")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            ctrl._run_task_loop()

        log = (ctrl.agent_dir / "run.log").read_text()
        assert "T-LOG" in log
        assert "completed" in log


# ─────────────────────────────────────────────────────────────────────────────
# Nothing-to-stage edge case
# ─────────────────────────────────────────────────────────────────────────────

class TestG3NothingToStage:
    def test_task_done_when_nothing_staged(self, tmp_path):
        """If outer_loop passes but no files changed, task is still DONE (no new commit)."""
        task = _make_task("T-NOSTAGE", "Idempotent task")
        ctrl = _make_controller(tmp_path, [task])

        # outer_loop "passes" but writes no files — nothing to stage
        fake_outer = MagicMock()
        fake_outer.run_task.return_value = _passed_result("T-NOSTAGE")

        with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
             patch("tools.auto.exhaustion_handler.make_exhaustion_handler"):
            _, tasks_done = ctrl._run_task_loop()

        assert tasks_done == 1
        t = ctrl.state.get_task("T-NOSTAGE")
        assert t["status"] == STATUS_DONE
        # commit field should be empty string (nothing staged) or absent
        commit_val = t.get("commit", "")
        assert commit_val == "" or commit_val is None
