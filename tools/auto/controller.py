"""tools/auto/controller.py — AUTO-A1 / AUTO-A2: Autonomous mode controller.

Entry point for the autonomous improvement mode.  Provides the public surface
that main.py imports:

    from tools.auto.controller import AutoController, run_auto

AUTO-A1 (entry point):
  * Validates inputs (non-empty goal, existing base_dir).
  * Prints start banner echoing goal and base_dir.
  * Routes --auto / /auto to this module; interactive/one-shot paths untouched.

AUTO-A2 (state store + resume):
  * Delegates all .agent/ I/O to StateStore (tools/auto/state.py).
  * On start: loads existing state and resumes (skips DONE tasks,
    continues IN_PROGRESS tasks) — kill mid-run, restart → no repeated work.
  * plan.json schema enforced via make_task() / _validate_task_schema().
  * progress.json updated after every logical step.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools.auto.state import StateStore, STATUS_DONE, STATUS_IN_PROGRESS

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# AutoController
# ─────────────────────────────────────────────────────────────────────────────

class AutoController:
    """Orchestrates an autonomous improvement run.

    Parameters
    ----------
    goal:
        A non-empty string describing what the agent should achieve,
        e.g. ``"improve current code"``.
    base_dir:
        Absolute (or relative) path to the project root that will be
        reviewed and modified.  Must exist.
    config_path:
        Path to ``agents.ini`` (default ``"agents.ini"``).

    Raises
    ------
    ValueError
        If *goal* is empty.
    FileNotFoundError
        If *base_dir* does not exist.
    """

    def __init__(
        self,
        goal: str,
        base_dir: str | os.PathLike = ".",
        config_path: str = "agents.ini",
    ) -> None:
        goal = goal.strip() if goal else ""
        if not goal:
            raise ValueError("AutoController requires a non-empty goal string.")

        base_path = Path(base_dir).resolve()
        if not base_path.exists():
            raise FileNotFoundError(f"base_dir does not exist: {base_path}")

        self.goal        = goal
        self.base_dir    = base_path
        self.config_path = config_path
        self.agent_dir   = base_path / ".agent"

        # AUTO-A2: StateStore owns all .agent/ I/O
        self.state = StateStore(self.agent_dir)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Execute the autonomous run and return an exit code (0 = success)."""
        self._print_banner()

        # AUTO-A2: initialise (fresh) or resume (existing state)
        is_fresh = self.state.initialise(self.goal, self.base_dir)

        resume_info = self.state.resume_info()
        if not is_fresh:
            self._print_resume_summary(resume_info)

        # Update progress to "running"
        self.state.update_progress(status="running")

        # ── Future epics (AUTO-B Architect, AUTO-C Coder loop, …) hook in here ──
        # Each step will call self.state.upsert_task / set_task_status / log / etc.

        self.state.update_progress(status="idle")
        self.state.log("run finished (AUTO-A2 skeleton — no tasks yet)")
        return 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _print_banner(self) -> None:
        ts = _ts()
        print(f"[{ts}] 🤖 Autonomous mode starting")
        print(f"[{ts}]    goal     : {self.goal}")
        print(f"[{ts}]    base_dir : {self.base_dir}")
        print(f"[{ts}]    config   : {self.config_path}")

    def _print_resume_summary(self, info: dict) -> None:
        done    = len(info["done_ids"])
        pending = len(info["pending"])
        ts = _ts()
        print(f"[{ts}] ♻️  Resuming existing run — "
              f"{done} done, {pending} pending, "
              f"{len(info['done_ids'])} skipped")
        if info["done_ids"]:
            print(f"[{ts}]    skipping: {', '.join(sorted(info['done_ids']))}")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrapper used by main.py
# ─────────────────────────────────────────────────────────────────────────────

def run_auto(
    goal: str,
    base_dir: str | os.PathLike = ".",
    config_path: str = "agents.ini",
) -> int:
    """Create an :class:`AutoController` and run it.

    Returns the integer exit code (0 on success, non-zero on error).
    """
    try:
        controller = AutoController(goal=goal, base_dir=base_dir, config_path=config_path)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    try:
        return controller.run()
    except Exception as exc:  # pragma: no cover
        logger.exception("Unhandled error in autonomous run: %s", exc)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1