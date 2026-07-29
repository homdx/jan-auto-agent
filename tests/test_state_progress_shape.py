"""tests/test_state_progress_shape.py — progress.json shape on load.

The corrupt-file case was handled deliberately and well: StateStore logs
"progress.json is corrupted — rebuilding from plan.json instead (progress
counts are fully derivable from the plan, so no data is lost)" and recovers.

Only JSONDecodeError reached that branch, so a file that PARSES but holds the
wrong shape was accepted and then crashed on the first write to
self._progress, far from the cause and with nothing naming the file:

    list   -> TypeError: list indices must be integers
    string -> TypeError: 'str' object does not support item assignment
    null   -> TypeError: 'NoneType' object does not support item assignment

Unlike plan.json (see test_state_plan_shape.py) this does not need to fail at
all — the existing rebuild path is the correct answer, because the counts are
derivable from the plan. A wrong-shape file now recovers exactly like a
corrupt one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.state import StateStore, make_task


def _store(tmp_path: Path, progress: str | None):
    agent = tmp_path / ".agent"
    agent.mkdir(parents=True, exist_ok=True)
    task = make_task(id="T1", title="t", instruction="i",
                     target_files=["a.py"], acceptance_check="true")
    (agent / "plan.json").write_text(
        json.dumps({"goal": "g", "tasks": [task]}), encoding="utf-8"
    )
    if progress is not None:
        (agent / "progress.json").write_text(progress, encoding="utf-8")
    store = StateStore(agent)
    store.initialise("g", tmp_path)
    return store, agent


class TestProgressShapeRecovers:
    @pytest.mark.parametrize("progress,label", [
        ("{ truncated", "corrupt json"),
        ("[]", "list"),
        ('"a string"', "string"),
        ("null", "null"),
        ("42", "number"),
        ("", "empty file"),
    ])
    def test_unusable_progress_is_rebuilt_not_fatal(self, tmp_path, progress, label):
        store, agent = _store(tmp_path, progress)
        store.set_task_status("T1", "done")          # first write to _progress
        rebuilt = json.loads((agent / "progress.json").read_text(encoding="utf-8"))
        assert rebuilt["done_count"] == 1, label

    def test_valid_progress_is_preserved(self, tmp_path):
        """Recovery must not clobber a usable file."""
        store, agent = _store(tmp_path, json.dumps({
            "status": "running", "updated_at": "x",
            "done_count": 0, "pending_count": 1,
        }))
        store.set_task_status("T1", "done")
        prog = json.loads((agent / "progress.json").read_text(encoding="utf-8"))
        assert prog["status"] == "running"
        assert prog["done_count"] == 1

    def test_missing_progress_is_rebuilt(self, tmp_path):
        store, agent = _store(tmp_path, None)
        store.set_task_status("T1", "done")
        prog = json.loads((agent / "progress.json").read_text(encoding="utf-8"))
        assert prog["done_count"] == 1

    def test_rebuild_counts_match_the_plan(self, tmp_path):
        """The rebuild is only safe because counts derive from the plan."""
        agent = tmp_path / ".agent"
        agent.mkdir(parents=True, exist_ok=True)
        tasks = [
            make_task(id=f"T{i}", title="t", instruction="i",
                      target_files=["a.py"], acceptance_check="true")
            for i in range(1, 4)
        ]
        tasks[0]["status"] = "done"
        (agent / "plan.json").write_text(
            json.dumps({"goal": "g", "tasks": tasks}), encoding="utf-8"
        )
        (agent / "progress.json").write_text("[]", encoding="utf-8")
        store = StateStore(agent)
        store.initialise("g", tmp_path)
        prog = json.loads((agent / "progress.json").read_text(encoding="utf-8"))
        assert prog["done_count"] == 1
        assert prog["pending_count"] == 2
