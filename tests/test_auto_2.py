"""tests/test_auto_a2.py — Tests for AUTO-A2: .agent/ state store + resume.

Covers all ACs from the story:

  AC1: kill mid-run, restart → no repeated work; counts continue.
  AC2: plan.json schema documented and enforced
       (task: id, title, instruction, target_files, acceptance_check,
              status, round, attempt, cited_locations, dependencies).

Also exercises the full StateStore public API:
  - initialise (fresh + resume)
  - resume_info
  - upsert_task / get_task / all_tasks
  - set_task_status / increment_task_counters
  - update_progress / get_progress
  - log
  - write_task_file / read_task_file / task_dir
  - make_task / _validate_task_schema
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Make the project root importable regardless of where pytest is invoked from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_IN_PROGRESS,
    STATUS_TODO,
    StateStore,
    make_task,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_agent(tmp_path):
    """Return a fresh StateStore backed by a temp directory."""
    store = StateStore(tmp_path / ".agent")
    store.initialise("test goal", tmp_path)
    return store, tmp_path


@pytest.fixture()
def sample_task():
    return make_task(
        id="AUTO-T1",
        title="Sample task",
        instruction="Do something useful",
        target_files=["foo.py"],
        acceptance_check="pytest foo_test.py",
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — Fresh initialisation
# ─────────────────────────────────────────────────────────────────────────────

class TestFreshInit:
    def test_returns_true_on_fresh_run(self, tmp_path):
        store = StateStore(tmp_path / ".agent")
        is_fresh = store.initialise("my goal", tmp_path)
        assert is_fresh is True

    def test_creates_agent_dir(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("goal", tmp_path)
        assert agent_dir.is_dir()

    def test_creates_plan_json(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("goal", tmp_path)
        assert (agent_dir / "plan.json").is_file()

    def test_creates_progress_json(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("goal", tmp_path)
        assert (agent_dir / "progress.json").is_file()

    def test_creates_run_log(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("goal", tmp_path)
        assert (agent_dir / "run.log").is_file()

    def test_creates_tasks_dir(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("goal", tmp_path)
        assert (agent_dir / "tasks").is_dir()

    def test_creates_tickets_dir(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("goal", tmp_path)
        assert (agent_dir / "tickets").is_dir()

    def test_plan_json_stores_goal(self, tmp_path):
        store = StateStore(tmp_path / ".agent")
        store.initialise("improve everything", tmp_path)
        plan = json.loads((tmp_path / ".agent" / "plan.json").read_text())
        assert plan["goal"] == "improve everything"

    def test_plan_json_stores_base_dir(self, tmp_path):
        store = StateStore(tmp_path / ".agent")
        store.initialise("g", tmp_path)
        plan = json.loads((tmp_path / ".agent" / "plan.json").read_text())
        assert plan["base_dir"] == str(tmp_path)

    def test_plan_json_has_empty_tasks(self, tmp_path):
        store = StateStore(tmp_path / ".agent")
        store.initialise("g", tmp_path)
        plan = json.loads((tmp_path / ".agent" / "plan.json").read_text())
        assert plan["tasks"] == []

    def test_progress_json_initial_status_idle(self, tmp_path):
        store = StateStore(tmp_path / ".agent")
        store.initialise("g", tmp_path)
        progress = json.loads((tmp_path / ".agent" / "progress.json").read_text())
        assert progress["status"] == "idle"

    def test_goal_accessible_via_get_goal(self, tmp_path):
        store = StateStore(tmp_path / ".agent")
        store.initialise("my goal", tmp_path)
        assert store.get_goal() == "my goal"


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — Resume (kill mid-run, restart → no repeated work)
# ─────────────────────────────────────────────────────────────────────────────

class TestResume:
    def test_returns_false_on_resume(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        # First run
        s1 = StateStore(agent_dir)
        s1.initialise("goal", tmp_path)
        # Second run (resume)
        s2 = StateStore(agent_dir)
        is_fresh = s2.initialise("goal", tmp_path)
        assert is_fresh is False

    def test_tasks_persist_across_restart(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        task = make_task(id="AUTO-T1", title="t", instruction="i")

        s1 = StateStore(agent_dir)
        s1.initialise("goal", tmp_path)
        s1.upsert_task(task)

        # Simulate restart
        s2 = StateStore(agent_dir)
        s2.initialise("goal", tmp_path)
        assert s2.get_task("AUTO-T1") is not None

    def test_done_tasks_remain_done_after_restart(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        task = make_task(id="AUTO-T1", title="t", instruction="i")

        s1 = StateStore(agent_dir)
        s1.initialise("goal", tmp_path)
        s1.upsert_task(task)
        s1.set_task_status("AUTO-T1", STATUS_DONE)

        s2 = StateStore(agent_dir)
        s2.initialise("goal", tmp_path)
        assert s2.get_task("AUTO-T1")["status"] == STATUS_DONE

    def test_resume_info_correctly_classifies_done_vs_pending(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        s1 = StateStore(agent_dir)
        s1.initialise("goal", tmp_path)
        s1.upsert_task(make_task(id="AUTO-T1", title="t1", instruction="i"))
        s1.upsert_task(make_task(id="AUTO-T2", title="t2", instruction="i"))
        s1.set_task_status("AUTO-T1", STATUS_DONE)

        s2 = StateStore(agent_dir)
        s2.initialise("goal", tmp_path)
        info = s2.resume_info()

        assert "AUTO-T1" in info["done_ids"]
        assert "AUTO-T2" not in info["done_ids"]
        assert len(info["pending"]) == 1
        assert info["pending"][0]["id"] == "AUTO-T2"

    def test_attempt_and_round_counters_persist(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        s1 = StateStore(agent_dir)
        s1.initialise("goal", tmp_path)
        s1.upsert_task(make_task(id="AUTO-T1", title="t", instruction="i"))
        s1.increment_task_counters("AUTO-T1", attempt_delta=3, round_delta=1)

        s2 = StateStore(agent_dir)
        s2.initialise("goal", tmp_path)
        t = s2.get_task("AUTO-T1")
        assert t["attempt"] == 3
        assert t["round"] == 1

    def test_run_log_survives_restart(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        s1 = StateStore(agent_dir)
        s1.initialise("goal", tmp_path)
        s1.log("first run event")

        s2 = StateStore(agent_dir)
        s2.initialise("goal", tmp_path)
        s2.log("second run event")

        log_content = (agent_dir / "run.log").read_text()
        assert "first run event" in log_content
        assert "second run event" in log_content

    def test_multiple_restarts_no_data_loss(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        for i in range(1, 4):
            s = StateStore(agent_dir)
            s.initialise("goal", tmp_path)
            if i == 1:
                s.upsert_task(make_task(id="AUTO-T1", title="t", instruction="i"))
            if i == 2:
                s.set_task_status("AUTO-T1", STATUS_IN_PROGRESS)

        s_final = StateStore(agent_dir)
        s_final.initialise("goal", tmp_path)
        assert s_final.get_task("AUTO-T1")["status"] == STATUS_IN_PROGRESS


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — plan.json task schema
# ─────────────────────────────────────────────────────────────────────────────

class TestTaskSchema:
    """make_task() produces schema-valid dicts; _validate_task_schema enforces it."""

    def test_make_task_has_all_required_fields(self, sample_task):
        required = [
            "id", "title", "instruction", "target_files",
            "acceptance_check", "status", "round", "attempt",
            "cited_locations", "dependencies",
        ]
        for field in required:
            assert field in sample_task, f"Missing field: {field}"

    def test_make_task_default_status_is_todo(self, sample_task):
        assert sample_task["status"] == STATUS_TODO

    def test_make_task_default_round_zero(self, sample_task):
        assert sample_task["round"] == 0

    def test_make_task_default_attempt_zero(self, sample_task):
        assert sample_task["attempt"] == 0

    def test_make_task_default_lists_empty(self):
        t = make_task(id="X", title="t", instruction="i")
        assert t["target_files"] == []
        assert t["cited_locations"] == []
        assert t["dependencies"] == []

    def test_schema_rejects_missing_field(self):
        bad = {
            "id": "X", "title": "t", "instruction": "i",
            "target_files": [], "acceptance_check": "",
            "status": STATUS_TODO, "round": 0, "attempt": 0,
            "cited_locations": [],
            # 'dependencies' intentionally omitted
        }
        from tools.auto.state import _validate_task_schema
        with pytest.raises(ValueError, match="dependencies"):
            _validate_task_schema(bad)

    def test_schema_rejects_wrong_type_for_round(self):
        from tools.auto.state import _validate_task_schema
        bad = make_task(id="X", title="t", instruction="i")
        bad["round"] = "0"  # should be int
        with pytest.raises(ValueError, match="round"):
            _validate_task_schema(bad)

    def test_schema_rejects_invalid_status(self):
        from tools.auto.state import _validate_task_schema
        bad = make_task(id="X", title="t", instruction="i")
        bad["status"] = "invalid_status"
        with pytest.raises(ValueError, match="status"):
            _validate_task_schema(bad)

    def test_schema_rejects_empty_id(self):
        with pytest.raises(ValueError, match="id"):
            make_task(id="   ", title="t", instruction="i")

    def test_schema_rejects_empty_title(self):
        with pytest.raises(ValueError, match="title"):
            make_task(id="X", title="  ", instruction="i")

    def test_all_valid_statuses_accepted(self):
        from tools.auto.state import _validate_task_schema
        for status in (STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED):
            t = make_task(id="X", title="t", instruction="i", status=status)
            _validate_task_schema(t)  # should not raise

    def test_extra_kwargs_merged(self):
        t = make_task(id="X", title="t", instruction="i", commit="abc123")
        assert t["commit"] == "abc123"


# ─────────────────────────────────────────────────────────────────────────────
# upsert_task / get_task / all_tasks
# ─────────────────────────────────────────────────────────────────────────────

class TestUpsertAndGet:
    def test_upsert_new_task_appended(self, tmp_agent, sample_task):
        store, _ = tmp_agent
        store.upsert_task(sample_task)
        assert store.get_task("AUTO-T1") is not None

    def test_upsert_updates_existing_task(self, tmp_agent, sample_task):
        store, _ = tmp_agent
        store.upsert_task(sample_task)
        updated = dict(sample_task)
        updated["title"] = "Updated title"
        store.upsert_task(updated)
        assert store.get_task("AUTO-T1")["title"] == "Updated title"
        assert len(store.all_tasks()) == 1  # no duplicate

    def test_get_task_returns_none_for_unknown_id(self, tmp_agent):
        store, _ = tmp_agent
        assert store.get_task("NONEXISTENT") is None

    def test_all_tasks_returns_all(self, tmp_agent):
        store, _ = tmp_agent
        store.upsert_task(make_task(id="AUTO-T1", title="t1", instruction="i"))
        store.upsert_task(make_task(id="AUTO-T2", title="t2", instruction="i"))
        ids = {t["id"] for t in store.all_tasks()}
        assert ids == {"AUTO-T1", "AUTO-T2"}

    def test_upsert_persists_to_disk(self, tmp_path, sample_task):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("g", tmp_path)
        store.upsert_task(sample_task)

        plan = json.loads((agent_dir / "plan.json").read_text())
        assert any(t["id"] == "AUTO-T1" for t in plan["tasks"])

    def test_upsert_rejects_invalid_task(self, tmp_agent):
        store, _ = tmp_agent
        bad = {"id": "X"}  # missing many required fields
        with pytest.raises((ValueError, KeyError)):
            store.upsert_task(bad)


# ─────────────────────────────────────────────────────────────────────────────
# set_task_status
# ─────────────────────────────────────────────────────────────────────────────

class TestSetTaskStatus:
    def test_set_status_changes_persisted(self, tmp_agent, sample_task):
        store, tmp_path = tmp_agent
        store.upsert_task(sample_task)
        store.set_task_status("AUTO-T1", STATUS_IN_PROGRESS)

        plan = json.loads((tmp_path / ".agent" / "plan.json").read_text())
        assert plan["tasks"][0]["status"] == STATUS_IN_PROGRESS

    def test_set_status_with_extra_fields(self, tmp_agent, sample_task):
        store, _ = tmp_agent
        store.upsert_task(sample_task)
        store.set_task_status("AUTO-T1", STATUS_DONE, commit="deadbeef")
        assert store.get_task("AUTO-T1")["commit"] == "deadbeef"

    def test_set_status_invalid_raises(self, tmp_agent, sample_task):
        store, _ = tmp_agent
        store.upsert_task(sample_task)
        with pytest.raises(ValueError, match="Invalid status"):
            store.set_task_status("AUTO-T1", "flying")

    def test_set_status_unknown_task_raises(self, tmp_agent):
        store, _ = tmp_agent
        with pytest.raises(ValueError, match="not found"):
            store.set_task_status("GHOST", STATUS_DONE)

    def test_done_count_updated_in_progress_json(self, tmp_agent, sample_task):
        store, tmp_path = tmp_agent
        store.upsert_task(sample_task)
        store.set_task_status("AUTO-T1", STATUS_DONE)

        progress = json.loads((tmp_path / ".agent" / "progress.json").read_text())
        assert progress["done_count"] == 1
        assert progress["pending_count"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# increment_task_counters
# ─────────────────────────────────────────────────────────────────────────────

class TestIncrementCounters:
    def test_increment_attempt(self, tmp_agent, sample_task):
        store, _ = tmp_agent
        store.upsert_task(sample_task)
        store.increment_task_counters("AUTO-T1", attempt_delta=1)
        assert store.get_task("AUTO-T1")["attempt"] == 1

    def test_increment_round(self, tmp_agent, sample_task):
        store, _ = tmp_agent
        store.upsert_task(sample_task)
        store.increment_task_counters("AUTO-T1", round_delta=1)
        assert store.get_task("AUTO-T1")["round"] == 1

    def test_multiple_increments_accumulate(self, tmp_agent, sample_task):
        store, _ = tmp_agent
        store.upsert_task(sample_task)
        store.increment_task_counters("AUTO-T1", attempt_delta=2)
        store.increment_task_counters("AUTO-T1", attempt_delta=3)
        assert store.get_task("AUTO-T1")["attempt"] == 5

    def test_increment_unknown_task_raises(self, tmp_agent):
        store, _ = tmp_agent
        with pytest.raises(ValueError, match="not found"):
            store.increment_task_counters("GHOST", attempt_delta=1)


# ─────────────────────────────────────────────────────────────────────────────
# update_progress / get_progress
# ─────────────────────────────────────────────────────────────────────────────

class TestProgress:
    def test_update_progress_status(self, tmp_agent):
        store, _ = tmp_agent
        store.update_progress(status="running")
        assert store.get_progress()["status"] == "running"

    def test_update_progress_persists(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("g", tmp_path)
        store.update_progress(status="running")

        progress = json.loads((agent_dir / "progress.json").read_text())
        assert progress["status"] == "running"

    def test_progress_counts_reflect_tasks(self, tmp_agent):
        store, _ = tmp_agent
        store.upsert_task(make_task(id="T1", title="t", instruction="i"))
        store.upsert_task(make_task(id="T2", title="t", instruction="i"))
        store.set_task_status("T1", STATUS_DONE)
        store.update_progress(status="running")

        p = store.get_progress()
        assert p["done_count"] == 1
        assert p["pending_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# resume_info
# ─────────────────────────────────────────────────────────────────────────────

class TestResumeInfo:
    def test_empty_plan_returns_empty_info(self, tmp_agent):
        store, _ = tmp_agent
        info = store.resume_info()
        assert info["done_ids"] == set()
        assert info["in_progress"] == []
        assert info["pending"] == []

    def test_in_progress_tasks_in_info(self, tmp_agent):
        store, _ = tmp_agent
        store.upsert_task(make_task(id="T1", title="t", instruction="i",
                                    status=STATUS_IN_PROGRESS))
        info = store.resume_info()
        assert len(info["in_progress"]) == 1
        assert info["in_progress"][0]["id"] == "T1"

    def test_done_tasks_not_in_pending(self, tmp_agent):
        store, _ = tmp_agent
        store.upsert_task(make_task(id="T1", title="t", instruction="i"))
        store.upsert_task(make_task(id="T2", title="t", instruction="i"))
        store.set_task_status("T1", STATUS_DONE)
        info = store.resume_info()
        pending_ids = {t["id"] for t in info["pending"]}
        assert "T1" not in pending_ids
        assert "T2" in pending_ids

    def test_blocked_tasks_appear_in_pending(self, tmp_agent):
        store, _ = tmp_agent
        store.upsert_task(make_task(id="T1", title="t", instruction="i"))
        store.set_task_status("T1", STATUS_BLOCKED)
        info = store.resume_info()
        assert any(t["id"] == "T1" for t in info["pending"])


# ─────────────────────────────────────────────────────────────────────────────
# Per-task artefact files
# ─────────────────────────────────────────────────────────────────────────────

class TestTaskFiles:
    def test_task_dir_created(self, tmp_agent):
        store, _ = tmp_agent
        d = store.task_dir("AUTO-T1")
        assert d.is_dir()

    def test_write_and_read_task_file(self, tmp_agent):
        store, _ = tmp_agent
        store.write_task_file("AUTO-T1", "feedback.md", "some feedback")
        content = store.read_task_file("AUTO-T1", "feedback.md")
        assert content == "some feedback"

    def test_read_nonexistent_task_file_returns_none(self, tmp_agent):
        store, _ = tmp_agent
        assert store.read_task_file("AUTO-T1", "nope.md") is None

    def test_task_file_persists_on_disk(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("g", tmp_path)
        store.write_task_file("AUTO-T1", "out.txt", "hello")
        path = agent_dir / "tasks" / "AUTO-T1" / "out.txt"
        assert path.read_text() == "hello"


# ─────────────────────────────────────────────────────────────────────────────
# run.log
# ─────────────────────────────────────────────────────────────────────────────

class TestLog:
    def test_log_appends_message(self, tmp_agent):
        store, tmp_path = tmp_agent
        store.log("event one")
        store.log("event two")
        content = (tmp_path / ".agent" / "run.log").read_text()
        assert "event one" in content
        assert "event two" in content

    def test_log_lines_have_timestamps(self, tmp_agent):
        store, tmp_path = tmp_agent
        store.log("timestamped event")
        content = (tmp_path / ".agent" / "run.log").read_text()
        # Timestamps look like [2024-01-01T00:00:00Z]
        assert "[20" in content
