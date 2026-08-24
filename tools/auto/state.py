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
    store.remove_task("AUTO-T4")          # AUTO-H1: quarantine a false positive
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
import os
from tools.auto.utils import _ts, safe_filename_component
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
    "impl_version":     int,
    "cited_locations":  list,
    "dependencies":     list,
}


# ─────────────────────────────────────────────────────────────────────────────
# Schema helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_task(
    id: str,            # noqa: A002 — "id" matches the task schema field name;
    title: str,         #              the builtin is not used inside this function.
    instruction: str,
    target_files: list[str] | None = None,
    acceptance_check: str = "",
    status: str = STATUS_TODO,
    round: int = 0,     # noqa: A002 — "round" matches the schema field; builtin unused.
    attempt: int = 0,
    impl_version: int = 1,
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
        "impl_version":     impl_version,
        "cited_locations":  cited_locations or [],
        "dependencies":     dependencies or [],
    }
    task.update(extra)
    _validate_task_schema(task)
    return task


def _validate_task_schema(task: dict) -> None:
    """Raise ValueError if *task* violates the plan.json task schema."""
    # LOOP-3: backfill impl_version for tasks created before this field existed
    # (existing plan.json files on disk, hand-rolled dicts in tests, etc.).
    if "impl_version" not in task:
        task["impl_version"] = 1
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

        Raises
        ------
        RuntimeError
            If a plan already exists on disk but was created for a
            *different* goal. Silently resuming it would execute leftover
            tasks for the old goal while the caller believes it is working
            on the new one, with no warning anywhere — see AUTO-BUG:
            StateStore.initialise() previously never compared the incoming
            goal against the stored plan's goal on the resume path.
        """
        self._ensure_dirs()

        if self._plan_path.exists():
            self._load_existing()
            stored_goal = self.get_goal().strip()
            incoming_goal = (goal or "").strip()
            if stored_goal != incoming_goal:
                raise RuntimeError(
                    f"StateStore.initialise: {self._plan_path} already holds a plan "
                    f"for a different goal — refusing to silently resume it under a "
                    f"new goal.\n"
                    f"  existing goal: {stored_goal!r}\n"
                    f"  new goal:      {incoming_goal!r}\n"
                    f"Point --base at a fresh directory for the new goal, or "
                    f"intentionally remove {self.agent_dir} first if you mean to "
                    f"discard the existing plan."
                )
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

    def remove_task(self, task_id: str) -> bool:
        """Remove a task from plan.json entirely and persist.

        AUTO-H1: used by ``tools.auto.plan_validator`` to drop a task that a
        re-validation pass confirmed is a false positive (the claimed gap is
        already closed, or its citation no longer resolves) — such a task
        must never be picked up by the execute loop again.

        This does NOT just flip the status to BLOCKED. A freshly-quarantined
        false-positive task has round == 0 (it never ran) and no
        ``BUG-FIX-`` prefix, which is exactly the shape
        ``AutoController._reset_resettable_blocked_tasks`` treats as safe to
        reset back to TODO at the start of the next session (its case 1:
        "unmet dependency" — see that method's docstring). Leaving the task
        BLOCKED would silently un-quarantine it on the very next ``--auto``
        invocation. Deleting the entry from plan.json is the only state that
        stays quarantined across sessions.

        Any OTHER task's ``dependencies`` list that still names *task_id* has
        that entry stripped too. A removed task was judged unnecessary — its
        target state already holds in the codebase — so anything that was
        only waiting on it can be treated as unblocked rather than left
        referencing a dependency that can now never become DONE, which would
        otherwise wedge the dependent task in BLOCKED forever (see the
        ``if not dep or dep["status"] != STATUS_DONE`` check in
        ``AutoController._run_task_loop``).

        Returns
        -------
        bool
            ``True`` if a task with *task_id* was found and removed;
            ``False`` if no such task existed (no-op).
        """
        tasks = self._plan.get("tasks", [])
        idx = next((i for i, t in enumerate(tasks) if t["id"] == task_id), None)
        if idx is None:
            return False

        del tasks[idx]

        cleared_from: list[str] = []
        for t in tasks:
            deps = t.get("dependencies") or []
            if task_id in deps:
                t["dependencies"] = [d for d in deps if d != task_id]
                cleared_from.append(t["id"])

        self._save_plan()
        self._refresh_progress()
        self.log(
            f"task {task_id} removed from plan"
            + (f" (cleared as a dependency of: {', '.join(cleared_from)})"
               if cleared_from else "")
        )
        return True

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

    def increment_impl_version(self, task_id: str) -> int:
        """Bump the impl_version counter for *task_id* and return the new value.

        Starts at 1 (set by make_task); each call increments by 1 and persists
        to plan.json so the version survives a resume.
        """
        tasks = self._plan.get("tasks", [])
        for t in tasks:
            if t["id"] == task_id:
                new_ver = t.get("impl_version", 1) + 1
                t["impl_version"] = new_ver
                self._save_plan()
                return new_ver
        raise ValueError(f"Task '{task_id}' not found in plan")

    def apply_rewrite(
        self,
        task_id: str,
        *,
        instruction: str,
        acceptance_check: str | None = None,
        title: str | None = None,
    ) -> int:
        """Persist a TaskRewriter rewrite and bump impl_version, atomically.

        Bugfix (rewrite lost on resume): a rewrite used to live only in the
        caller's local ``task`` variable for the rest of that process's
        lifetime — only the ``impl_version`` *number* was persisted, via the
        old bare ``increment_impl_version`` call. If the process was
        killed/restarted right after a rewrite (exactly the scenario the
        ``.agent/`` state store exists to survive), the next session reloaded
        the ORIGINAL, already-failing instruction from plan.json while
        impl_version still correctly claimed a rewrite had happened — so the
        coder was handed the very instruction it was told (via
        "prior_implementations") not to repeat.

        This method makes the rewritten ``instruction`` / ``acceptance_check``
        / ``title`` durable in the SAME call that bumps ``impl_version``, so
        a resumed session picks up the latest rewrite instead of silently
        discarding it. The task's TRUE original (v1) instruction is preserved
        once, in ``original_instruction``, the first time this is called for
        a task — OuterLoop needs that unchanged v1 baseline to correctly
        label version 1 in its "previously tried" history even after later
        rewrites overwrite ``instruction``.

        Returns the new impl_version.
        """
        tasks = self._plan.get("tasks", [])
        for t in tasks:
            if t["id"] == task_id:
                if "original_instruction" not in t:
                    t["original_instruction"] = t.get("instruction", "")
                t["instruction"] = instruction
                if acceptance_check:
                    t["acceptance_check"] = acceptance_check
                if title:
                    t["title"] = title
                new_ver = t.get("impl_version", 1) + 1
                t["impl_version"] = new_ver
                self._save_plan()
                return new_ver
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
            # stop_reason was passed as a **extra kwarg — do NOT pop it here;
            # self._progress.update(extra) below will set it.
            pass
        else:
            # Neither the named param nor **extra carries a stop_reason → clear
            # any previously recorded value so stale reasons don't survive a
            # status transition (e.g. "capped" → "running" on resume).
            self._progress.pop("stop_reason", None)
        self._progress.update(extra)
        self._refresh_progress(write=False)   # recalculates counts
        self._save_progress()

    def log(self, msg: str) -> None:
        """Append a timestamped line to run.log.

        BUGFIX (report §4, item 7): previously an unguarded file write — any
        OSError (disk full, permissions, run.log's parent directory removed
        mid-run) propagated straight out of every .log() call site, 62 of
        them across the codebase. The most damaging is inside
        outer_loop.py's AUTO-OUTER-GUARD-1 handler, whose entire documented
        purpose is "any exception inner_loop.run_task doesn't already
        handle itself shouldn't crash the whole multi-task run" — that
        handler itself calls self.state.log(...) unguarded, so an OSError
        from logging could defeat the very safety net it exists to
        provide.

        Unlike _atomic_write (used for plan.json/progress.json, where a
        silently failed state write is intentionally NOT swallowed because
        it's worse than a loud one), run.log is a best-effort diagnostic
        trail, not authoritative state — losing one line to a warning beats
        crashing the run that line was trying to record. Callers that need
        a write failure to be fatal already have _atomic_write /
        _save_plan / _save_progress for that.
        """
        line = f"[{_ts()}] {msg}\n"
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            logger.warning(
                "StateStore: failed to write to %s: %s — log line dropped: %s",
                self._log_path, exc, msg,
            )

    @staticmethod
    def _safe_task_id(task_id: str) -> str:
        """Return a filesystem-safe version of *task_id*.

        Strips path separators and leading dots so a task id like
        ``"../../evil"`` cannot escape the tasks directory.  The canonical
        form keeps only alphanumeric characters, hyphens, and underscores.

        Delegates to tools.auto.utils.safe_filename_component, also used by
        TicketStore, so both apply the identical rule to ids that flow
        between them (a ticket id is typically derived from a task id).
        """
        return safe_filename_component(task_id)

    def task_dir(self, task_id: str) -> Path:
        """Return (and create) the per-task artefact directory."""
        d = self._tasks_dir / self._safe_task_id(task_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_task_file(self, task_id: str, filename: str, content: str) -> Path:
        """Write *content* to .agent/tasks/<task_id>/<filename> and return the path.

        Uses the same atomic write as plan.json/progress.json (see
        ``_atomic_write``) so a process killed mid-write (e.g. OOM-killer)
        can't leave a truncated task file behind.
        """
        path = self.task_dir(task_id) / filename
        self._atomic_write(path, content)
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
        self._plan = self._load_json_with_backup(self._plan_path, "plan.json")
        self._validate_loaded_plan()
        if self._prog_path.exists():
            try:
                loaded = json.loads(
                    self._prog_path.read_text(encoding="utf-8")
                )
                # A file that PARSES is not necessarily usable.  Only the
                # JSONDecodeError case was handled, so valid JSON of the wrong
                # shape — a list, a string, null — was accepted here and then
                # crashed on the first write to self._progress, far from the
                # cause and with nothing naming the file:
                #
                #   list   -> TypeError: list indices must be integers
                #   string -> TypeError: 'str' object does not support item
                #             assignment
                #   null   -> TypeError: 'NoneType' object does not support
                #             item assignment
                #
                # No need to fail on it, though: the rebuild path below is
                # already the right answer, because progress counts are fully
                # derivable from the plan.  So a wrong-shape file recovers
                # exactly like a corrupt one instead of poisoning the run.
                # AUTO-FIX (medium-priority audit, DeepSeek-plan finding):
                # the isinstance(dict) check above accepts ANY dict, even
                # one missing the keys this class actually depends on
                # (done_count/pending_count/status) — e.g. progress.json
                # truncated to just "{}" by a partial write. That silently
                # produced a StateStore whose get_progress() callers had to
                # each separately guess a fallback for a key that should
                # have been there. Treat a dict missing any required key
                # the same as a wrong-shape file: rebuild from plan.json,
                # which is the already-established, no-data-lost recovery
                # path for exactly this situation.
                _required_progress_keys = {"done_count", "pending_count", "status"}
                if isinstance(loaded, dict):
                    _missing = _required_progress_keys - loaded.keys()
                    if not _missing:
                        self._progress = loaded
                        return
                    logger.warning(
                        "StateStore: progress.json is missing required "
                        "key(s) %s — rebuilding from plan.json instead "
                        "(progress counts are fully derivable from the "
                        "plan, so no data is lost).",
                        sorted(_missing),
                    )
                else:
                    logger.warning(
                        "StateStore: progress.json holds a %s, not an object — "
                        "rebuilding from plan.json instead (progress counts are "
                        "fully derivable from the plan, so no data is lost).",
                        type(loaded).__name__,
                    )
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                logger.warning(
                    "StateStore: progress.json is unreadable (%s) — rebuilding "
                    "from plan.json instead (progress counts are fully "
                    "derivable from the plan, so no data is lost).", exc,
                )
        # progress.json was lost or corrupted — rebuild from plan
        self._progress = {"status": "idle", "updated_at": _ts(),
                          "done_count": 0, "pending_count": 0}
        self._refresh_progress()

    def _validate_loaded_plan(self) -> None:
        """Check the SHAPE of a loaded plan, not just that it parsed.

        _load_json_with_backup already recovers from a corrupt file and, when
        recovery fails, raises a clear ``plan.json ... is corrupted`` error.
        But a file that is valid JSON of the WRONG SHAPE sailed straight past
        that guard and crashed later, raw and far from the cause:

            plan.json is a list           -> AttributeError: 'list' object has
                                             no attribute 'get'
            "tasks" is a string           -> TypeError: string indices must be
                                             integers
            a task missing "status"       -> KeyError: 'status'

        The schema is enforced on WRITE (upsert_task -> _validate_task_schema)
        and was simply never enforced on READ, so anything that reaches
        plan.json by another route — a hand edit, which this project's own
        recovery advice invites when it tells operators to reset a ticket
        status by hand; a partially-migrated schema; a file restored from an
        older version — produced an unactionable traceback instead of the
        clean, already-written diagnostic.

        Raising the same error type as the corruption path keeps the resume
        contract uniform: either the state is usable, or the operator is told
        which file is wrong and why.
        """
        if not isinstance(self._plan, dict):
            raise RuntimeError(
                f"plan.json ({self._plan_path}) is not a JSON object "
                f"(found {type(self._plan).__name__}) — the file is not a "
                f"usable plan"
            )

        tasks = self._plan.get("tasks", [])
        if not isinstance(tasks, list):
            raise RuntimeError(
                f"plan.json ({self._plan_path}) has a 'tasks' field of type "
                f"{type(tasks).__name__}, expected a list"
            )

        for idx, task in enumerate(tasks):
            if not isinstance(task, dict):
                raise RuntimeError(
                    f"plan.json ({self._plan_path}) task #{idx} is "
                    f"{type(task).__name__}, expected an object"
                )
            try:
                _validate_task_schema(task)
            except ValueError as exc:
                raise RuntimeError(
                    f"plan.json ({self._plan_path}) task #{idx} "
                    f"({task.get('id', '<no id>')}): {exc}"
                ) from exc

    def _load_json_with_backup(self, path: Path, what: str) -> dict:
        """Load JSON from *path*; on corruption, recover from the ``.bak``
        snapshot written just before the last atomic update, instead of
        letting a raw ``JSONDecodeError`` crash the resume path.

        Bugfix: plan.json/progress.json used to be written with a plain
        ``write_text()``, which truncates the file before writing — a
        process killed mid-write (the exact scenario ``max_task_seconds`` /
        wall-clock caps and a long unattended run are meant to survive) could
        leave a truncated file, and the next resume crashed here with an
        unhandled ``JSONDecodeError`` instead of resuming. Writes are now
        atomic (see ``_atomic_write``); this recovery path is the safety net
        for any file that is corrupted despite that (e.g. a manual edit).
        """
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            bak = path.with_suffix(path.suffix + ".bak")
            if bak.exists():
                try:
                    recovered = json.loads(bak.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as bak_exc:
                    raise RuntimeError(
                        f"{what} ({path}) is corrupted: {exc}. Its backup "
                        f"({bak}) is also unreadable: {bak_exc}. Manual "
                        "repair is needed before this run can resume."
                    ) from exc
                logger.warning(
                    "StateStore: %s is corrupted (%s) — recovered from "
                    "backup %s. The most recent update before the crash "
                    "may be lost.", path, exc, bak,
                )
                return recovered
            raise RuntimeError(
                f"{what} ({path}) is corrupted: {exc}. No backup ({bak}) "
                "is available. Manual repair is needed before this run "
                "can resume."
            ) from exc

    def _refresh_progress(self, *, write: bool = True) -> None:
        """Recalculate done/pending counts from current plan; optionally persist."""
        tasks = self._plan.get("tasks", [])
        self._progress["done_count"]    = sum(1 for t in tasks if t["status"] == STATUS_DONE)
        self._progress["pending_count"] = sum(1 for t in tasks if t["status"] not in (STATUS_DONE, STATUS_BLOCKED))
        self._progress["updated_at"]    = _ts()
        if write:
            self._save_progress()

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write *content* to *path* atomically, keeping a ``.bak`` snapshot
        of the previous contents.

        Bugfix: this used to be a plain ``path.write_text()``, which opens
        the file in truncate mode — a process killed between truncation and
        the write completing left a corrupt/empty file, and this fired on
        almost every task-status change, so the window was hit often over a
        multi-hour run. Now: write to a temp file in the same directory,
        then ``os.replace()`` swaps it into place — atomic on POSIX and
        Windows, so *path* is always either the previous complete file or
        the new complete file, never a partial write. The pre-write ``.bak``
        copy is best-effort (not itself atomic) but never touches *path*, so
        an interrupted backup can't put the primary file at risk.

        AUTO-FIX (medium-priority audit): the temp-file write and the
        ``os.replace`` swap used to be completely unguarded — every task-
        status update in the whole pipeline goes through this method, so an
        ``OSError`` here (disk full, permission denied, filesystem gone
        read-only mid-run) propagated as a bare, path-less traceback and
        could crash a multi-hour ``--auto`` run. Now the tmp file is
        cleaned up on failure (mirroring ``atomic_write_text``'s existing
        behavior in ``utils.py``) and the re-raised exception names *path*
        explicitly.

        Raises
        ------
        OSError
            Re-raised (via ``from exc``, with *path* named in the message)
            if the write or the atomic rename fails. This is intentionally
            NOT swallowed — a silently failed state write is worse than a
            loud one; callers that need this to be non-fatal must catch it
            themselves.
        """
        if path.exists():
            try:
                bak = path.with_suffix(path.suffix + ".bak")
                bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError as exc:
                logger.debug(
                    "StateStore: could not refresh backup for %s: %s", path, exc,
                )
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError(f"StateStore: failed to write {path} ({exc})") from exc

    def _save_plan(self) -> None:
        self._atomic_write(
            self._plan_path,
            json.dumps(self._plan, indent=2, ensure_ascii=False),
        )

    def _save_progress(self) -> None:
        self._atomic_write(
            self._prog_path,
            json.dumps(self._progress, indent=2, ensure_ascii=False),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
