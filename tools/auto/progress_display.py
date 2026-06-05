"""tools/auto/progress_display.py — AUTO-F1: Live progress display.

Console + progress.json output.  Each refresh() emits:

    architecture [2/4]  coding [3/8]   [✓✗⚙○○]  3/5  ·  attempt 2/5  ·  round 1/10
    ⚙  Fix login bug  (T-03)

The first line is the canonical banner (unchanged format, extended with the
visual bar and task fraction appended).  The second line shows the active task.

Dot bar legend:  ✓ done   ✗ failed   ⚙ running   ○ waiting

Public surface
--------------
    from tools.auto.progress_display import ProgressDisplay, make_progress_display

    display = make_progress_display(state, config)
    display.tick_arch()
    display.set_task(task_num=1, attempt=1, round_num=1,
                     task_id="T-01", title="Fix login bug")
    display.finish_task(passed=True)   # records ✓/✗ and calls tick_code()
"""

from __future__ import annotations

import configparser
import logging
import sys
from typing import IO, List, Optional

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS_DEFAULT = 5
_MAX_ROUNDS_DEFAULT   = 10

_SYM_DONE = "✓"
_SYM_FAIL = "✗"
_SYM_RUN  = "⚙"
_SYM_WAIT = "○"


# ─────────────────────────────────────────────────────────────────────────────
# Rendering helpers (pure functions — kept backward-compatible)
# ─────────────────────────────────────────────────────────────────────────────

def render_banner(
    arch_done:  int,
    arch_total: int,
    code_done:  int,
    code_total: int,
) -> str:
    """Return the canonical run-level progress line (unchanged format).

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
    """Return the canonical per-task progress line (unchanged format).

    >>> render_task_line(1, 2, 5, 1, 10)
    'task 1 · attempt 2/5 · round 1/10'
    """
    return (
        f"task {task_num} · "
        f"attempt {attempt}/{max_attempts} · "
        f"round {round_num}/{max_rounds}"
    )


def _dot_bar(results: List[Optional[bool]], total: int, current: int) -> str:
    """Build the visual dot bar.

    results[i]: True=done ✓, False=failed ✗, None=not started ○.
    current: 1-based index of the task currently running (0 = none).
    """
    dots = []
    for i in range(total):
        if i < len(results) and results[i] is not None:
            dots.append(_SYM_DONE if results[i] else _SYM_FAIL)
        elif i == current - 1 and (i >= len(results) or results[i] is None):
            dots.append(_SYM_RUN)
        else:
            dots.append(_SYM_WAIT)
    return "[" + "".join(dots) + "]"


def _rich_suffix(
    task_num:    int,
    attempt:     int,
    max_attempts: int,
    round_num:   int,
    max_rounds:  int,
    code_done:   int,
    code_total:  int,
    results:     List[Optional[bool]],
) -> str:
    """Return the visual suffix appended to the canonical banner line."""
    bar   = _dot_bar(results, code_total, task_num)
    frac  = f"{code_done}/{code_total}"
    parts = [bar, frac]
    if task_num > 0:
        parts.append(f"attempt {attempt}/{max_attempts}")
        parts.append(f"round {round_num}/{max_rounds}")
    return "  " + "  ·  ".join(parts)


def _task_detail_line(task_num: int, task_id: str, title: str) -> str:
    """Return the active-task detail line (second console line)."""
    if task_num == 0:
        return ""
    label   = title or task_id or f"task {task_num}"
    id_part = f"  ({task_id})" if task_id and title else ""
    return f"{_SYM_RUN}  {label}{id_part}"


# ─────────────────────────────────────────────────────────────────────────────
# ProgressDisplay
# ─────────────────────────────────────────────────────────────────────────────

class ProgressDisplay:
    """Tracks run-level and per-task progress; emits the canonical format.

    Parameters
    ----------
    state:
        StateStore whose update_progress() is called on every refresh().
    arch_total:
        Total architecture clusters to review.
    code_total:
        Total coding tasks in the plan.
    max_attempts:
        Inner-loop cap (default 5).
    max_rounds:
        Outer-loop cap (default 10).
    out:
        Text sink for console output.  Defaults to sys.stdout.
    """

    def __init__(
        self,
        state,
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
        self._out          = out

        self.arch_done  = 0
        self.code_done  = 0
        self.task_num   = 0
        self.attempt    = 0
        self.round_num  = 0
        self._task_id   = ""
        self._title     = ""

        # Per-task result history: True=passed, False=failed, None=not started
        self._results: List[Optional[bool]] = []

    # ── High-level mutators ──────────────────────────────────────────────────

    def tick_arch(self) -> None:
        """Record one more architecture cluster as reviewed and refresh."""
        self.arch_done = min(self.arch_done + 1, self.arch_total)
        self.refresh()

    def tick_code(self) -> None:
        """Record one more coding task as complete and refresh."""
        self.code_done = min(self.code_done + 1, self.code_total)
        self.refresh()

    def record_result(self, passed: bool) -> None:
        """Record the outcome (✓/✗) for the current task without ticking.

        Call just before tick_code() so the dot bar reflects the outcome.
        """
        idx = self.task_num - 1
        if idx >= 0:
            while len(self._results) <= idx:
                self._results.append(None)
            self._results[idx] = passed

    def finish_task(self, passed: bool) -> None:
        """Convenience: record_result + tick_code in one call."""
        self.record_result(passed)
        self.tick_code()

    def set_task(
        self,
        task_num:  int,
        attempt:   int,
        round_num: int,
        task_id:   str = "",
        title:     str = "",
    ) -> None:
        """Update per-task counters and refresh.

        Call at the start of every inner attempt and at every round boundary.
        task_id and title are optional; they enrich the second console line.
        """
        self.task_num  = int(task_num)
        self.attempt   = int(attempt)
        self.round_num = int(round_num)
        self._task_id  = task_id or ""
        self._title    = title or ""
        self.refresh()

    # ── Rendering ────────────────────────────────────────────────────────────

    def banner(self) -> str:
        """Return the current run-level banner string (canonical format)."""
        return render_banner(
            self.arch_done, self.arch_total,
            self.code_done, self.code_total,
        )

    def task_line(self) -> str:
        """Return the current per-task detail string (canonical format).

        Returns empty string when no task has been started yet.
        """
        if self.task_num == 0:
            return ""
        return render_task_line(
            self.task_num, self.attempt, self.max_attempts,
            self.round_num, self.max_rounds,
        )

    def refresh(self) -> None:
        """Print current state to console and persist to progress.json."""
        try:
            self._print_lines()
        except Exception as exc:
            logger.warning("ProgressDisplay: console write failed: %s", exc)
        try:
            self._persist()
        except Exception as exc:
            logger.warning("ProgressDisplay: progress.json update failed: %s", exc)

    # ── Private ──────────────────────────────────────────────────────────────

    def _print_lines(self) -> None:
        sink = self._out if self._out is not None else sys.stdout

        # Line 1: canonical banner + visual suffix
        suffix = _rich_suffix(
            self.task_num, self.attempt, self.max_attempts,
            self.round_num, self.max_rounds,
            self.code_done, self.code_total,
            self._results,
        )
        sink.write(self.banner() + suffix + "\n")

        # Line 2: canonical task line (always present when a task is active)
        tl = self.task_line()
        if tl:
            sink.write(tl + "\n")
            # Line 3: rich ⚙ title line when task metadata is available
            if self._title or self._task_id:
                sink.write(_task_detail_line(self.task_num, self._task_id, self._title) + "\n")

        sink.flush()

    def _persist(self) -> None:
        extra = {
            "arch_done":  self.arch_done,
            "arch_total": self.arch_total,
            "code_done":  self.code_done,
            "code_total": self.code_total,
        }
        if self.task_num:
            extra.update({
                "current_task_num": self.task_num,
                "current_attempt":  self.attempt,
                "current_round":    self.round_num,
            })
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
    """Build a ProgressDisplay from a StateStore and agents.ini config."""
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
