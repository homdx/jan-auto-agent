"""tools/auto/state.py — AUTO-A2: Persistent state store for autonomous runs.

Owns all I/O under .agent/

    .agent/
    ├── plan.json        — task backlog (schema enforced)
    ├── progress.json    — run-level counters + status
    ├── run.log          — append-only human-readable log
    ├── tasks/           — per-task artefact directories
    └── tickets/         — defect / investigation tickets

Public surface consumed by controller.py:

    store = StateStore(agent_dir)
    is_fresh = store.initialise(goal, base_dir)
    info     = store.resume_info()          # -> {done_ids, pending, in_progress}
    store.update_progress(status="running")
    store.update_progress(status="capped", stop_reason="runtime_cap")   # AUTO-A4
    store.upsert_task(make_task(...))
    store.set_task_status("AUTO-T1", STATUS_DONE, commit="abc123")
    store.log("something happened")

plan.json task schema (enforced by _validate_task_schema):
    id               str   — unique identifier, e.g. "AUTO-T1"
    title            str   — short human description
    instruction      str   — full instruction for the Coder agent
    target_files     list  — file paths the task will touch
    acceptance_check str   — shell command whose exit-0 means success
    status           str   — todo | in_progress | done | blocked
    round            int   — current outer loop round (0-indexed)
    attempt          int   — current inner attempt within round (0-indexed)
    cited_locations  list  — [{file, symbol, line_start, line_end}, ...]
    dependencies     list  — list of task ids that must be DONE first
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Status constants ────────────────────────────────────────────────────────
STATUS_TODO        = "todo"
STATUS_IN_PROGRESS = "in_progress"
STATUS_DONE        = "done"
STATUS_BLOCKED     = "blocked"

_VALID_STATUSES = {STATUS_TODO, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED}

# ── Required top-level fields in a task dict ────────────────────────────────
_REQUIRED_TASK_FIELDS: dict[str, type] = {
    "id":               str,
    "title":            str,
    "instruction":      str,
    "target_files":     list,
    "acceptance_check": str,
    "status":           str,
    "round":            int,
    "attempt":          int,
    "cited_locations":  list,
    "dependencies":     list,
}


# ─────────────────────────────────────────────────────────────────────────────
# Schema helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_task(
    id: str,
    title: str,
    instruction: str,
    target_files: list[str] | None = None,
    acceptance_check: str = "",
    status: str = STATUS_TODO,
    round: int = 0,
    attempt: int = 0,
    cited_locations: list[dict] | None = None,
    dependencies: list[str] | None = None,
    **extra: Any,
) -> dict:
    """Return a schema-valid task dict.  Extra keyword args are merged in."""
    task = {
        "id":               id,
        "title":            title,
        "instruction":      instruction,
        "target_files":     target_files or [],
        "acceptance_check": acceptance_check,
        "status":           status,
        "round":            round,
        "attempt":          attempt,
        "cited_locations":  cited_locations or [],
        "dependencies":     dependencies or [],
    }
    task.update(extra)
    _validate_task_schema(task)
    return task


def _validate_task_schema(task: dict) -> None:
    """Raise ValueError if *task* violates the plan.json task schema."""
    for field, expected_type in _REQUIRED_TASK_FIELDS.items():
        if field not in task:
            raise ValueError(f"Task schema violation: missing field '{field}'")
        if not isinstance(task[field], expected_type):
            raise ValueError(
                f"Task schema violation: field '{field}' must be {expected_type.__name__}, "
                f"got {type(task[field]).__name__}"
            )
    if task["status"] not in _VALID_STATUSES:
        raise ValueError(
            f"Task schema violation: status must be one of {_VALID_STATUSES}, "
            f"got '{task['status']}'"
        )
    if not task["id"].strip():
        raise ValueError("Task schema violation: 'id' must be a non-empty string")
    if not task["title"].strip():
        raise ValueError("Task schema violation: 'title' must be a non-empty string")


# ─────────────────────────────────────────────────────────────────────────────
# StateStore
# ─────────────────────────────────────────────────────────────────────────────

class StateStore:
    """Manages all persistent state under *agent_dir* (.agent/).

    All writes are atomic at the JSON level: the full file is rewritten on
    every mutating call so that a mid-run kill leaves a consistent (though
    possibly stale) snapshot.

    Parameters
    ----------
    agent_dir:
        Path to the .agent/ directory (need not exist yet).
    """

    def __init__(self, agent_dir: str | Path) -> None:
        self.agent_dir    = Path(agent_dir)
        self._plan_path   = self.agent_dir / "plan.json"
        self._prog_path   = self.agent_dir / "progress.json"
        self._log_path    = self.agent_dir / "run.log"
        self._tasks_dir   = self.agent_dir / "tasks"
        self._tickets_dir = self.agent_dir / "tickets"

        self._plan: dict     = {}
        self._progress: dict = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def initialise(self, goal: str, base_dir: str | Path) -> bool:
        """Create fresh state or load existing state for resume.

        Returns
        -------
        bool
            ``True`` if a brand-new run was created; ``False`` if existing
            state was loaded (resume path).
        """
        self._ensure_dirs()

        if self._plan_path.exists():
            self._load_existing()
            self.log("StateStore loaded — resuming existing run")
            return False

        self._create_fresh(goal, Path(base_dir))
        self.log("StateStore initialised — fresh run")
        return True

    # ── Query API ────────────────────────────────────────────────────────────

    def resume_info(self) -> dict:
        """Return a summary of tasks by status for the resume banner.

        Returns
        -------
        dict with keys:
            done_ids    — set of task ids with status DONE
            in_progress — list of task dicts with status IN_PROGRESS
            pending     — list of task dicts that are actionable (todo + in_progress)
        """
        tasks = self._plan.get("tasks", [])
        done_ids    = {t["id"] for t in tasks if t["status"] == STATUS_DONE}
        in_progress = [t for t in tasks if t["status"] == STATUS_IN_PROGRESS]
        pending     = [t for t in tasks if t["status"] not in (STATUS_DONE, STATUS_BLOCKED)]
        return {
            "done_ids":    done_ids,
            "in_progress": in_progress,
            "pending":     pending,
        }

    def get_task(self, task_id: str) -> dict | None:
        """Return the task dict for *task_id*, or None if not found."""
        for t in self._plan.get("tasks", []):
            if t["id"] == task_id:
                return t
        return None

    def all_tasks(self) -> list[dict]:
        """Return all tasks in plan order."""
        return list(self._plan.get("tasks", []))

    def get_goal(self) -> str:
        return self._plan.get("goal", "")

    def get_base_dir(self) -> str:
        return self._plan.get("base_dir", "")

    def get_progress(self) -> dict:
        return dict(self._progress)

    # ── Mutating API ─────────────────────────────────────────────────────────

    def upsert_task(self, task: dict) -> None:
        """Insert or update a task in plan.json.

        The task is validated against the schema before writing.  If a task
        with the same ``id`` already exists it is replaced; otherwise appended.
        """
        _validate_task_schema(task)
        tasks = self._plan.setdefault("tasks", [])
        for i, t in enumerate(tasks):
            if t["id"] == task["id"]:
                tasks[i] = task
                self._save_plan()
                return
        tasks.append(task)
        self._save_plan()

    def set_task_status(
        self,
        task_id: str,
        status: str,
        **extra_fields: Any,
    ) -> None:
        """Update *status* (and optional extra fields) for the given task.

        Also updates progress.json counters so they stay in sync.

        Parameters
        ----------
        task_id:
            The ``id`` field of the task to update.
        status:
            One of the STATUS_* constants.
        **extra_fields:
            Arbitrary extra fields to merge into the task (e.g.
            ``commit="abc123"`` or ``round=2``).

        Raises
        ------
        ValueError
            If *status* is not in *_VALID_STATUSES* or the task is not found.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'; must be one of {_VALID_STATUSES}")

        tasks = self._plan.get("tasks", [])
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = status
                t.update(extra_fields)
                self._save_plan()
                self._refresh_progress()
                return

        raise ValueError(f"Task '{task_id}' not found in plan")

    def increment_task_counters(
        self,
        task_id: str,
        *,
        attempt_delta: int = 0,
        round_delta: int = 0,
    ) -> None:
        """Increment attempt/round counters for a task and persist."""
        tasks = self._plan.get("tasks", [])
        for t in tasks:
            if t["id"] == task_id:
                t["attempt"] = t.get("attempt", 0) + attempt_delta
                t["round"]   = t.get("round", 0)   + round_delta
                self._save_plan()
                return
        raise ValueError(f"Task '{task_id}' not found in plan")

    def update_progress(
        self,
        status: str,
        stop_reason: str | None = None,
        **extra: Any,
    ) -> None:
        """Set the run-level status and recalculate counters, then persist.

        Parameters
        ----------
        status:
            New run-level status string (e.g. ``"running"``, ``"idle"``,
            ``"capped"``).
        stop_reason:
            Optional reason the run was stopped early — one of
            ``"runtime_cap"`` or ``"task_cap"``.  Written to progress.json
            so a resumed run can report why it stopped last time.
            Pass ``None`` to clear a previously recorded stop_reason.
        **extra:
            Any additional key/value pairs to merge into progress.json.
        """
        self._progress["status"]     = status
        self._progress["updated_at"] = _ts()
        if stop_reason is not None:
            self._progress["stop_reason"] = stop_reason
        elif "stop_reason" in extra:
            # allow explicit kwarg path too
            pass
        else:
            self._progress.pop("stop_reason", None)
        self._progress.update(extra)
        self._refresh_progress(write=False)   # recalculates counts
        self._save_progress()

    def log(self, msg: str) -> None:
        """Append a timestamped line to run.log."""
        line = f"[{_ts()}] {msg}\n"
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    @staticmethod
    def _safe_task_id(task_id: str) -> str:
        """Return a filesystem-safe version of *task_id*.

        Strips path separators and leading dots so a task id like
        ``"../../evil"`` cannot escape the tasks directory.  The canonical
        form keeps only alphanumeric characters, hyphens, and underscores.
        """
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9_\-]", "_", task_id)
        safe = safe.strip("_") or "task"
        return safe

    def task_dir(self, task_id: str) -> Path:
        """Return (and create) the per-task artefact directory."""
        d = self._tasks_dir / self._safe_task_id(task_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_task_file(self, task_id: str, filename: str, content: str) -> Path:
        """Write *content* to .agent/tasks/<task_id>/<filename> and return the path."""
        path = self.task_dir(task_id) / filename
        path.write_text(content, encoding="utf-8")
        return path

    def read_task_file(self, task_id: str, filename: str) -> str | None:
        """Read a per-task file; return None if it doesn't exist."""
        path = self.task_dir(task_id) / filename
        return path.read_text(encoding="utf-8") if path.exists() else None

    # ── Private ──────────────────────────────────────────────────────────────

    def _ensure_dirs(self) -> None:
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self._tasks_dir.mkdir(exist_ok=True)
        self._tickets_dir.mkdir(exist_ok=True)

    def _create_fresh(self, goal: str, base_dir: Path) -> None:
        ts = _ts()
        self._plan = {
            "goal":       goal,
            "base_dir":   str(base_dir),
            "created_at": ts,
            "tasks":      [],
        }
        self._progress = {
            "status":        "idle",
            "updated_at":    ts,
            "done_count":    0,
            "pending_count": 0,
        }
        self._save_plan()
        self._save_progress()

    def _load_existing(self) -> None:
        self._plan = json.loads(self._plan_path.read_text(encoding="utf-8"))
        if self._prog_path.exists():
            self._progress = json.loads(self._prog_path.read_text(encoding="utf-8"))
        else:
            # progress.json was lost — rebuild from plan
            self._progress = {"status": "idle", "updated_at": _ts(),
                              "done_count": 0, "pending_count": 0}
            self._refresh_progress()

    def _refresh_progress(self, *, write: bool = True) -> None:
        """Recalculate done/pending counts from current plan; optionally persist."""
        tasks = self._plan.get("tasks", [])
        self._progress["done_count"]    = sum(1 for t in tasks if t["status"] == STATUS_DONE)
        self._progress["pending_count"] = sum(1 for t in tasks if t["status"] not in (STATUS_DONE, STATUS_BLOCKED))
        self._progress["updated_at"]    = _ts()
        if write:
            self._save_progress()

    def _save_plan(self) -> None:
        self._plan_path.write_text(
            json.dumps(self._plan, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _save_progress(self) -> None:
        self._prog_path.write_text(
            json.dumps(self._progress, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")