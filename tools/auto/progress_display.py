"""tools/auto/progress_display.py — AUTO-F1: Live progress display.

Console + progress.json output in the format specified by the story:

    architecture [x/N]  coding [y/M]
    task k · attempt a/5 · round r/10

``refresh()`` is the single write point — it both prints to the console
(or any TextIO sink) and persists the current snapshot to progress.json
via the StateStore so unattended runs are fully observable externally.

The display is deliberately simple: each ``refresh()`` emits a fresh
line-pair (no ANSI cursor tricks) so piped/logged output stays readable.

Public surface
--------------
    from tools.auto.progress_display import ProgressDisplay, make_progress_display

    display = make_progress_display(state, config)   # factory
    # — or —
    display = ProgressDisplay(
        state        = state,
        arch_total   = 4,
        code_total   = 8,
        max_attempts = 5,
        max_rounds   = 10,
    )

    display.tick_arch()                   # one architecture cluster reviewed
    display.set_task(task_num=1, attempt=1, round_num=1)  # entering task loop
    display.tick_code()                   # one coding task finished
"""

from __future__ import annotations

import configparser
import logging
import sys
from pathlib import Path
from typing import IO, Optional

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS_DEFAULT = 5
_MAX_ROUNDS_DEFAULT   = 10


# ─────────────────────────────────────────────────────────────────────────────
# Rendering helpers (pure functions — easy to unit-test in isolation)
# ─────────────────────────────────────────────────────────────────────────────

def render_banner(
    arch_done:  int,
    arch_total: int,
    code_done:  int,
    code_total: int,
) -> str:
    """Return the run-level progress line.

    >>> render_banner(2, 4, 3, 8)
    'architecture [2/4]  coding [3/8]'
    """
    return f"architecture [{arch_done}/{arch_total}]  coding [{code_done}/{code_total}]"


def render_task_line(
    task_num:    int,
    attempt:     int,
    max_attempts: int,
    round_num:   int,
    max_rounds:  int,
) -> str:
    """Return the per-task progress line.

    >>> render_task_line(1, 2, 5, 1, 10)
    'task 1 · attempt 2/5 · round 1/10'
    """
    return (
        f"task {task_num} · "
        f"attempt {attempt}/{max_attempts} · "
        f"round {round_num}/{max_rounds}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ProgressDisplay
# ─────────────────────────────────────────────────────────────────────────────

class ProgressDisplay:
    """Tracks run-level and per-task progress; emits the canonical format.

    Parameters
    ----------
    state:
        ``StateStore`` instance whose ``update_progress()`` is called on every
        ``refresh()`` to persist the snapshot to ``progress.json``.
    arch_total:
        Total number of architecture clusters to review.
    code_total:
        Total number of coding tasks in the plan.
    max_attempts:
        Inner-loop cap (default 5).  Used in the task line denominator.
    max_rounds:
        Outer-loop cap (default 10).  Used in the task line denominator.
    out:
        Text sink for console output.  Defaults to ``sys.stdout``.
        Pass a ``StringIO`` in tests to capture output.
    """

    def __init__(
        self,
        state,                               # StateStore — typed loosely to avoid circular import
        arch_total:   int,
        code_total:   int,
        max_attempts: int = _MAX_ATTEMPTS_DEFAULT,
        max_rounds:   int = _MAX_ROUNDS_DEFAULT,
        out:          Optional[IO[str]] = None,
    ) -> None:
        self._state        = state
        self.arch_total    = int(arch_total)
        self.code_total    = int(code_total)
        self.max_attempts  = int(max_attempts)
        self.max_rounds    = int(max_rounds)
        self._out          = out  # None → sys.stdout resolved lazily so tests can swap it

        # Mutable counters
        self.arch_done  = 0
        self.code_done  = 0
        self.task_num   = 0     # 0 = not yet on a task
        self.attempt    = 0
        self.round_num  = 0

    # ── High-level mutators ──────────────────────────────────────────────────

    def tick_arch(self) -> None:
        """Record one more architecture cluster as reviewed and refresh."""
        self.arch_done = min(self.arch_done + 1, self.arch_total)
        self.refresh()

    def tick_code(self) -> None:
        """Record one more coding task as complete and refresh."""
        self.code_done = min(self.code_done + 1, self.code_total)
        self.refresh()

    def set_task(
        self,
        task_num:  int,
        attempt:   int,
        round_num: int,
    ) -> None:
        """Update per-task counters and refresh.

        Call at the start of every inner attempt and at every round boundary
        so ``progress.json`` and the console stay current.
        """
        self.task_num  = int(task_num)
        self.attempt   = int(attempt)
        self.round_num = int(round_num)
        self.refresh()

    # ── Rendering ────────────────────────────────────────────────────────────

    def banner(self) -> str:
        """Return the current run-level banner string (no newline)."""
        return render_banner(
            self.arch_done, self.arch_total,
            self.code_done, self.code_total,
        )

    def task_line(self) -> str:
        """Return the current per-task detail string (no newline).

        Returns an empty string when no task has been started yet.
        """
        if self.task_num == 0:
            return ""
        return render_task_line(
            self.task_num, self.attempt, self.max_attempts,
            self.round_num, self.max_rounds,
        )

    def refresh(self) -> None:
        """Print current state to the console and persist to progress.json.

        Never raises — errors are logged and swallowed so a display glitch
        can't abort an autonomous run.
        """
        try:
            self._print_lines()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProgressDisplay: console write failed: %s", exc)

        try:
            self._persist()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ProgressDisplay: progress.json update failed: %s", exc)

    # ── Private ──────────────────────────────────────────────────────────────

    def _print_lines(self) -> None:
        sink = self._out if self._out is not None else sys.stdout
        sink.write(self.banner() + "\n")
        tl = self.task_line()
        if tl:
            sink.write(tl + "\n")
        sink.flush()

    def _persist(self) -> None:
        """Push current snapshot into progress.json via StateStore."""
        extra = {
            "arch_done":   self.arch_done,
            "arch_total":  self.arch_total,
            "code_done":   self.code_done,
            "code_total":  self.code_total,
        }
        if self.task_num:
            extra.update({
                "current_task_num": self.task_num,
                "current_attempt":  self.attempt,
                "current_round":    self.round_num,
            })
        # StateStore.update_progress preserves the existing 'status' key;
        # we pass the current status through so we don't accidentally reset it.
        current_status = self._state.get_progress().get("status", "running")
        self._state.update_progress(current_status, **extra)


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_progress_display(
    state,
    config: configparser.ConfigParser,
    *,
    arch_total: int = 0,
    code_total: int = 0,
    out: Optional[IO[str]] = None,
) -> ProgressDisplay:
    """Build a ``ProgressDisplay`` from a ``StateStore`` and ``agents.ini`` config.

    *arch_total* and *code_total* should be provided once the Architect has
    finalised the plan; they default to 0 so the factory can be called early
    and the totals patched in later via ``display.arch_total = N``.
    """
    max_attempts = config.getint("auto", "max_attempts_per_task", fallback=_MAX_ATTEMPTS_DEFAULT)
    max_rounds   = config.getint("auto", "max_rounds_per_task",   fallback=_MAX_ROUNDS_DEFAULT)

    return ProgressDisplay(
        state        = state,
        arch_total   = arch_total,
        code_total   = code_total,
        max_attempts = max_attempts,
        max_rounds   = max_rounds,
        out          = out,
    )
