"""tools/auto/controller.py — AUTO-A1: Autonomous mode controller.

Entry point for the autonomous improvement mode.  All epic AUTO-A through
AUTO-F work will be built here.  This file provides the public surface that
main.py imports:

    from tools.auto.controller import AutoController, run_auto

Phase 1 (AUTO-A1 skeleton):
  * Validates inputs (non-empty goal, existing base_dir).
  * Prints the start banner echoing goal and base_dir.
  * Creates the .agent/ directory structure (AUTO-A2 will expand this).
  * Returns exit code 0 on success.
"""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

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

        self.goal = goal
        self.base_dir = base_path
        self.config_path = config_path
        self.agent_dir = base_path / ".agent"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Execute the autonomous run and return an exit code (0 = success)."""
        self._print_banner()
        self._init_state()
        return 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _print_banner(self) -> None:
        """Print the start banner echoing goal and base_dir (required by AC)."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[{ts}] 🤖 Autonomous mode starting")
        print(f"[{ts}]    goal     : {self.goal}")
        print(f"[{ts}]    base_dir : {self.base_dir}")
        print(f"[{ts}]    config   : {self.config_path}")

    def _init_state(self) -> None:
        """Create .agent/ directory skeleton (AUTO-A2 will expand this)."""
        # Core directories
        for subdir in ("tasks", "tickets"):
            (self.agent_dir / subdir).mkdir(parents=True, exist_ok=True)

        # progress.json — tracks overall run state
        progress_path = self.agent_dir / "progress.json"
        if not progress_path.exists():
            progress = {
                "goal": self.goal,
                "base_dir": str(self.base_dir),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "initialised",
                "architecture_done": False,
                "tasks_total": 0,
                "tasks_done": 0,
            }
            progress_path.write_text(json.dumps(progress, indent=2), encoding="utf-8")
            logger.debug("Created %s", progress_path)

        # run.log — append-mode high-level event log
        log_path = self.agent_dir / "run.log"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"[{datetime.now(timezone.utc).isoformat()}] run started  goal={self.goal!r}\n"
            )


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
    Errors are printed to stderr so the caller can ``sys.exit(code)``
    directly.
    """
    import sys

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
