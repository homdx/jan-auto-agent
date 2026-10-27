"""tools/auto/outer_loop.py — AUTO-C4: outer round loop + feedback files.

Wraps the AUTO-C3 inner loop (``InnerLoop.run_task``) in the outer *round* loop
for ONE task:

    for round in 1 .. max_rounds (default 10):
        prior = [compact summary of every PREVIOUS round]   # the only carry-over
        result = inner_loop.run_task(task, prior_feedback=prior)   # up to 5 attempts
        if result.passed:  → task done (AUTO-C5 commits)
        else:              → write a COMPACT feedback_round_<n>.md, start a fresh round

The key idea (and the thing that fixes the context-bloat / 500s seen earlier):
each new round starts with a **fresh context** seeded ONLY by the compact
per-round feedback files — never the full attempt-by-attempt transcript.  So the
context the model sees grows by *one short summary per round*, not by five
attempt logs per round.  Within a round, the inner loop keeps its own attempt
context; that context is discarded at round end and distilled into one file.

Counters (round / attempt) are persisted to the StateStore after every round, so
a run killed mid-task resumes from the next unfinished round rather than redoing
work.  Committing a passed task is AUTO-C5; turning an exhausted task into a
knowledge note + investigation ticket is AUTO-C6 — this module only drives the
rounds and produces the feedback files / result.

Public surface:

    from tools.auto.outer_loop import OuterLoop, OuterLoopResult, make_outer_loop

    outer = make_outer_loop(config, base_dir, state)        # builds InnerLoop too
    result = outer.run_task(task, base_dir)
    if result.passed:      ...   # AUTO-C5: git commit
    elif result.exhausted: ...   # AUTO-C6: knowledge + ticket

agents.ini [auto] keys
----------------------
max_rounds_per_task   — outer-loop cap (default 10)
max_attempts_per_task — inner-loop cap (default 5)   [used by make_inner_loop]
"""

from __future__ import annotations

import configparser
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from tools.agent_trace import tracer
from tools.auto.state import (
    StateStore, STATUS_IN_PROGRESS, STATUS_DONE, STATUS_BLOCKED, STATUS_TODO,
)
from tools.auto.inner_loop import make_inner_loop
from tools.auto.utils import highest_completed_round

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ROUNDS = 10
_FEEDBACK_GLOB = "feedback_round_*.md"
_FEEDBACK_RE = re.compile(r"feedback_round_(\d+)\.md$")
_MAX_FEEDBACK_CHARS = 800        # keep each round file compact

# The task budget ledger: the file name is kept, but its content holds consumed time and the session start (RUN-2).
_BUDGET_FILE = "deadline_started_at.txt"


@dataclass
class _TaskBudget:
    """One task's budget ledger for the duration of a single session."""
    max_seconds: float                  # max_task_seconds (0 disables the guard)
    consumed_s: float = 0.0             # seconds worked, summed over sessions
    session_started_at: float | None = None   # wall clock; set while active
    # Seconds a session spent in coder calls that died on the wire; they are taken back out of the ledger (RUN-8).
    credit_s: float = 0.0


def _nonneg_float(value: object, default: float | None) -> float | None:
    """Coerce a persisted number to a non-negative float, else *default*.

    The ledger is hand-writable and ``json.loads`` happily yields ``NaN`` and
    ``Infinity`` — both would poison every ``max()``/``min()`` below (NaN is
    never exhausted, Infinity always is), so they degrade to *default* too.
    """
    if isinstance(value, bool):
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(seconds):
        return default
    return max(seconds, 0.0)


def parse_budget_file(raw: str | None, now: float, max_seconds: float):
    """Parse a persisted budget ledger into ``(consumed_s, is_legacy)``.

    Never raises: the ledger is a hand-writable file read on every resume, and
    ``run_task`` must not die at the resume point because of it.

    * ``None`` / empty / unparseable → ``(0.0, False)`` — nothing was ever
      recorded, so the task starts with the full budget.
    * ``{"consumed_s": …}`` → the ledger. A ``session_started_at`` still present
      here means the previous session opened the ledger and died without
      closing it (Ctrl-C, OOM, kill between the two writes), so its open
      session is folded in — capped at ``max_seconds``. A crash mid-round costs
      at most one budget, never a night.
    * the legacy float-only shape → ``(0.0, True)``. The pre-RUN-2 code stored
      the task's first start timestamp and deduced consumed time from
      calendar-elapsed time, which is exactly what made a stopped run wake up
      exhausted. That number cannot be converted into consumed time, so it is
      treated as "unknown → 0 consumed", flagged so the caller can warn once,
      and rewritten by the caller in the current format.
    """
    if not raw:
        return 0.0, False
    text = str(raw).strip()
    if not text:
        return 0.0, False
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        consumed = _nonneg_float(data.get("consumed_s"), 0.0)
        marker = _nonneg_float(data.get("session_started_at"), None)
        if marker is not None:
            # Rule 3 of RUN-2: the unclosed session consumes at most one budget.
            consumed = consumed + min(max(now - marker, 0.0), max_seconds)
        return consumed, False
    try:
        float(text)
    except (TypeError, ValueError):
        return 0.0, False
    return 0.0, True


def render_budget_file(consumed_s: float, session_started_at: float | None = None) -> str:
    """Serialize a ledger. ``session_started_at`` is present only while the
    session is active; a closed session stores ``consumed_s`` alone."""
    payload = {"consumed_s": float(consumed_s)}
    if session_started_at is not None:
        payload["session_started_at"] = float(session_started_at)
    return json.dumps(payload, separators=(",", ":"))


def _coerce_impl_version(value: object) -> int:
    """Best-effort int read of a persisted ``impl_version``.

    FIX-2 #2: plan.json is a hand-writable file that has already been shown
    to carry corrupt values, so a bare ``int(impl_version or 1)`` raised
    ``ValueError`` on a non-numeric entry (and ``TypeError`` on the JSON
    null that Architect's ``_to_int_or_none`` writes for an unparseable
    line anchor) before the first round started. That escaped ``run_task``
    -- the per-task body of the whole ``--auto`` loop -- and dropped every
    task still pending in plan.json.

    A value that cannot be read as an integer means "we don't know how many
    rewrites this task had", so fall back to 1 -- the same starting value
    ``make_task`` gives every task and ``_coerce_counter`` (state.py) falls
    back to on the bump path -- rather than 0. Both fall back to a value
    that makes ``rewrites_done`` (``max(0, impl_version - 1)``) come out to
    0 either way, but only 1 keeps ``impl_version`` itself consistent with
    "no rewrites yet" everywhere else it is used in this function --
    feedback file headers ("impl v{impl_version}"), ``impl_versions_used``,
    and the trace events -- instead of surfacing a "v0" that should never
    exist. The persisted value is left untouched -- nothing about it can be
    safely repaired in place.
    """
    if isinstance(value, bool):
        return 1
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1

# LOOP-4: regex to extract impl version from file headers
_IMPL_HEADER_RE = re.compile(r"impl v(\d+)")


# Result

@dataclass
class OuterLoopResult:
    """Aggregate result of the outer round loop for one task."""
    task_id:             str
    passed:              bool
    rounds_used:         int
    exhausted:           bool
    feedback_files:      list[str] = field(default_factory=list)
    inner_results:       list = field(default_factory=list)   # list[InnerLoopResult]
    impl_versions_used:  list = field(default_factory=list)   # list[int] — LOOP-3
    # True when the round ended because the validator never answered, so it is not a rejection (RUN-7).
    unavailable:         bool = False
    # Which stage left the round unavailable ("coder" or "gate2"), so the summary names the real outage (RUN-8).
    unavailable_stage:   str = ""

    def summary(self) -> str:
        if self.passed:
            return f"[{self.task_id}] DONE in {self.rounds_used} round(s)"
        if self.unavailable:
            if self.unavailable_stage == "coder":
                return f"[{self.task_id}] LEFT TODO — coder transport failure"
            return f"[{self.task_id}] LEFT TODO — validator unavailable"
        return f"[{self.task_id}] EXHAUSTED after {self.rounds_used} round(s)"

    def knowledge(self) -> str:
        """Concatenated round feedback — the seed for AUTO-C6's knowledge note."""
        parts = []
        for path in self.feedback_files:
            try:
                parts.append(Path(path).read_text(encoding="utf-8"))
            except OSError:
                continue
        return "\n\n".join(parts)


# OuterLoop

class OuterLoop:
    """Drives up to ``max_rounds`` fresh-context rounds for one task (AUTO-C4)."""

    def __init__(
        self,
        inner_loop,
        state: StateStore,
        max_rounds: int = _DEFAULT_MAX_ROUNDS,
        rewrite_every_n_rounds: int = 2,
        max_rewrites: int = 5,
        task_rewriter=None,
    ) -> None:
        self.inner_loop             = inner_loop
        self.state                  = state
        self.max_rounds             = max(1, int(max_rounds))
        self.rewrite_every_n_rounds = max(1, int(rewrite_every_n_rounds))
        self.max_rewrites           = max(0, int(max_rewrites))
        self.task_rewriter          = task_rewriter  # optional; None disables rewriting

    def run_task(self, task: dict, base_dir: str | Path) -> OuterLoopResult:
        """Run the outer loop for *task*.  Resumes from the next unfinished
        round if feedback files already exist.  Never raises."""
        task_id = task.get("id", "?")

        # The task budget ledger opens before the first round and closes in finally, so every exit path records the time once (RUN-2).
        _budget = self._begin_task_budget(task_id)
        try:
            return self._run_rounds(task, base_dir, task_id, _budget)
        finally:
            self._end_task_budget(task_id, _budget)

    def _run_rounds(
        self, task: dict, base_dir: str | Path, task_id: str, budget: _TaskBudget
    ) -> OuterLoopResult:
        """The round loop, given an already-open budget ledger."""
        # One wall-clock budget covers all rounds of a task and survives restarts, so resuming does not reset it (AUTO-CR-33, RUN-2).
        _task_deadline = None
        if budget.max_seconds > 0:
            _remaining = max(budget.max_seconds - budget.consumed_s, 0.0)
            _task_deadline = time.monotonic() + _remaining
        # The deadline is passed only to inner loops that accept it, so older fakes still work (AUTO-CR-33).
        try:
            import inspect as _inspect
            _inner_accepts_deadline = (
                "deadline"
                in _inspect.signature(self.inner_loop.run_task).parameters
            )
        except (ValueError, TypeError):
            _inner_accepts_deadline = False

        # The first instruction is kept separately, so a resumed task still labels version 1 correctly (LOOP-4).
        original_instruction: str = task.get("original_instruction") or task.get("instruction", "")

        # ── Resume: existing feedback files mean prior rounds already ran ──
        done_rounds = self._existing_rounds(task_id)
        start_round = done_rounds + 1
        feedback_files = [str(p) for p in self._feedback_paths(task_id)]
        inner_results: list = []

        # The rewrite counter is seeded from the persisted impl_version, so max_rewrites holds across restarts (LOOP-2, FIX-2 #2).
        impl_version = _coerce_impl_version(task.get("impl_version", 1))
        rewrites_done = max(0, impl_version - 1)
        impl_versions_used: list[int] = []

        if start_round > self.max_rounds:
            # Already exhausted in a prior session.
            self.state.set_task_status(task_id, STATUS_BLOCKED)
            return OuterLoopResult(task_id, False, self.max_rounds, True,
                                   feedback_files, inner_results)

        self.state.set_task_status(task_id, STATUS_IN_PROGRESS)
        tracer.event("controller", "outer_loop", "run_start",
                     params={"task": task_id, "start_round": start_round,
                             "max_rounds": self.max_rounds,
                             "impl_version": impl_version})

        for rnd in range(start_round, self.max_rounds + 1):
            # The task-wide budget is checked before each round, since a run once ran away for 5 hours (AUTO-CR-33).
            if _task_deadline is not None and time.monotonic() >= _task_deadline:
                logger.warning(
                    "OuterLoop: task %s wall-clock budget (%ds = %.1f min) "
                    "exhausted across rounds — stopping before round %d.",
                    task_id, int(budget.max_seconds), budget.max_seconds / 60.0, rnd,
                )
                self.state.set_task_status(task_id, STATUS_BLOCKED)
                # Every result path reports the implementations used, including this one (BUGFIX).
                return OuterLoopResult(task_id, False, rnd - 1, True,
                                       feedback_files, inner_results,
                                       impl_versions_used)

            # Fresh context: seed ONLY with the compact prior-round summaries.
            prior = self._read_round_feedback(task_id)
            # The round counter is read from the store, since the caller's task dict is a detached copy (RUN-7).
            _round_at_entry = (self.state.get_task(task_id) or {}).get("round", rnd - 1)
            self.state.set_task_status(task_id, STATUS_IN_PROGRESS, round=rnd)

            # Prior implementation history tells the coder which strategies already failed (LOOP-4).
            prior_impls = self._build_impl_history(
                task_id, impl_version, original_instruction
            )

            _rt_kwargs = dict(
                prior_feedback=prior,
                prior_implementations=prior_impls or None,
            )
            if _inner_accepts_deadline:
                _rt_kwargs["deadline"] = _task_deadline   # AUTO-CR-33: shared budget
            try:
                res = self.inner_loop.run_task(task, base_dir, **_rt_kwargs)
            except Exception as exc:
                # An exception that escapes the inner loop fails this task closed, not the whole run (AUTO-OUTER-GUARD-1).
                logger.exception(
                    "%s: inner_loop.run_task raised in round %d — %s",
                    task_id, rnd, exc,
                )
                self.state.set_task_status(task_id, STATUS_BLOCKED)
                self.state.log(
                    f"{task_id}: inner_loop.run_task raised in round {rnd} "
                    f"— {exc}"
                )
                tracer.event("outer_loop", "controller", "result",
                             params={"task": task_id, "passed": False,
                                     "round": rnd, "error": str(exc)})
                return OuterLoopResult(task_id, False, rnd, True,
                                       feedback_files, inner_results,
                                       impl_versions_used)
            inner_results.append(res)
            # Seconds a dead coder call used are credited back to the shared deadline (RUN-8).
            try:
                _credit = float(getattr(res, "deadline_credit_s", 0.0) or 0.0)
            except (TypeError, ValueError):
                _credit = 0.0
            if _task_deadline is not None and _credit > 0:
                _task_deadline += _credit
            # The same seconds are taken out of the persisted ledger, since the session clock still counted them (RUN-2).
            if _credit > 0:
                budget.credit_s += _credit
            # The round is set by set_task_status; here only the attempt count is added.
            self.state.increment_task_counters(
                task_id, attempt_delta=getattr(res, "attempts_used", 0),
            )

            # An unavailable validator means the round never happened: the status and round counter are undone, and no feedback is written (RUN-7).
            if getattr(res, "unavailable", False) is True:
                # The two outages read differently, but both end the same way (RUN-8).
                _u_stage = str(getattr(res, "unavailable_stage", "") or "")
                _u_label = ("coder transport failure" if _u_stage == "coder"
                            else "validator unavailable")
                logger.warning(
                    "OuterLoop: task %s left todo — %s (%s)",
                    task_id, _u_label,
                    getattr(res, "unavailable_reason", "") or "no detail",
                )
                self.state.set_task_status(task_id, STATUS_TODO, round=_round_at_entry)
                self.state.log(
                    f"{task_id}: round {rnd} — {_u_label}, left todo "
                    f"(no feedback file, no round consumed)"
                )
                tracer.event("outer_loop", "controller", "result",
                             params={"task": task_id, "passed": False,
                                     "round": rnd, "unavailable": True,
                                     "unavailable_stage": _u_stage})
                return OuterLoopResult(task_id, False, rnd - 1, False,
                                       feedback_files, inner_results,
                                       impl_versions_used, unavailable=True,
                                       unavailable_stage=_u_stage)

            impl_versions_used.append(impl_version)

            if getattr(res, "passed", False):
                self.state.set_task_status(task_id, STATUS_DONE)
                self.state.log(f"{task_id}: passed in round {rnd} "
                               f"({getattr(res, 'attempts_used', '?')} attempts)")
                tracer.event("outer_loop", "controller", "result",
                             params={"task": task_id, "passed": True, "round": rnd,
                                     "impl_version": impl_version})
                return OuterLoopResult(task_id, True, rnd, False,
                                       feedback_files, inner_results,
                                       impl_versions_used)

            # A failed round writes one compact feedback file; a failed write is logged, not allowed to abort the task (AUTO-FIX).
            try:
                fpath = self._write_round_feedback(task_id, rnd, res, impl_version)
                feedback_files.append(str(fpath))
                self.state.log(f"{task_id}: round {rnd} failed — wrote {fpath.name}")
            except OSError as exc:
                logger.error(
                    "OuterLoop: failed to write round-%d feedback for %s — %s "
                    "(continuing without a persisted feedback file for this round)",
                    rnd, task_id, exc,
                )
                try:
                    self.state.log(
                        f"{task_id}: round {rnd} failed — feedback write also "
                        f"failed ({exc})"
                    )
                except OSError:
                    pass  # run.log itself is unwritable too — already logged above

            # A rewrite is due on a schedule, and only when the coder had its context satisfied (LOOP-2).
            if (
                self.task_rewriter is not None
                and self.max_rewrites > 0
                and rnd >= 3
                and (rnd - 1) % self.rewrite_every_n_rounds == 0
                and rewrites_done < self.max_rewrites
                and getattr(res, "context_satisfied", True)
            ):
                failure_history = self._read_round_feedback(task_id)
                impl_num = rewrites_done + 2  # v1 → first rewrite → v2, etc.
                logger.info(
                    "round %d failed — architect rewriting task (impl v%d)",
                    rnd, impl_num,
                )
                self.state.log(
                    f"{task_id}: round {rnd} failed — architect rewriting task "
                    f"(impl v{impl_num})"
                )

                try:
                    new_task = self.task_rewriter.rewrite(task, failure_history)
                except Exception as exc:
                    # A failing rewriter is logged, not allowed to escape run_task (BUGFIX).
                    logger.warning(
                        "outer_loop: task_rewriter.rewrite failed for %s — "
                        "continuing with the original task unchanged: %s",
                        task_id, exc,
                    )
                    new_task = task
                if new_task is not task:
                    # A real rewrite is recorded on disk and in state, so a resumed task keeps the new instruction (LOOP-3, BUGFIX).
                    try:
                        impl_version = self.state.apply_rewrite(
                            task_id,
                            instruction=new_task.get("instruction", ""),
                            acceptance_check=new_task.get("acceptance_check", ""),
                            title=new_task.get("title"),
                        )
                    except Exception as exc:
                        # A failed rewrite apply is treated as no rewrite, so the artifact and version bump are not left behind (BUGFIX).
                        logger.warning(
                            "outer_loop: apply_rewrite failed for %s — discarding "
                            "this round's rewrite, continuing with the original "
                            "task (it was never durably recorded): %s", task_id, exc,
                        )
                    else:
                        rewrite_body = (
                            f"# Rewrite after round {rnd} — impl v{impl_version} — "
                            f"task {task_id}\n\n"
                            f"## New instruction\n{new_task.get('instruction', '')}\n\n"
                            f"## Acceptance check\n{new_task.get('acceptance_check', '')}\n"
                        )
                        try:
                            self.state.write_task_file(
                                task_id,
                                f"rewrite_round_{rnd}.md",
                                rewrite_body,
                            )
                        except OSError as exc:
                            # The human-readable copy of a rewrite is best-effort; the rewrite itself still applies (BUGFIX).
                            logger.warning(
                                "outer_loop: could not write rewrite_round_%d.md "
                                "for %s — continuing without it: %s",
                                rnd, task_id, exc,
                            )
                        task = new_task
                        rewrites_done += 1
                        tracer.event(
                            "outer_loop", "task_rewriter", "rewrite",
                            content=new_task.get("instruction", ""),
                            params={
                                "task": task_id,
                                "round": rnd,
                                "impl_version": impl_version,
                                "rewrites_done": rewrites_done,
                            },
                        )
                else:
                    logger.warning(
                        "TaskRewriter returned original task unchanged for %r "
                        "(parse/network error) — continuing with current strategy",
                        task_id,
                    )

        # All rounds exhausted → BLOCKED (AUTO-C6 will write knowledge + ticket).
        self.state.set_task_status(task_id, STATUS_BLOCKED)
        tracer.event("outer_loop", "controller", "result",
                     params={"task": task_id, "passed": False,
                             "rounds": self.max_rounds, "exhausted": True,
                             "impl_version": impl_version})
        return OuterLoopResult(task_id, False, self.max_rounds, True,
                               feedback_files, inner_results, impl_versions_used)

    # ── private ──────────────────────────────────────────────────────────────

    def _task_budget_seconds(self) -> float:
        """This task's wall-clock budget in seconds (0 disables the guard).

        A malformed ``max_task_seconds`` must mean "no guard", not an
        exception at the resume point — but FL-1 (round 84): "malformed"
        must also cover a value that ``float()`` *accepts*, not just one
        that raises.  ``float(MagicMock())`` is ``1.0`` (MagicMock implements
        ``__float__``), and ``float(True)`` is ``1.0`` too (``bool`` is an
        ``int`` subclass) — the old ``float(getattr(...) or 0)`` silently
        turned either one into a real 1-second budget nobody configured.
        Only a real, finite, positive ``int``/``float`` is accepted; a
        ``bool``, a ``MagicMock``, a string that happens to parse, an enum,
        or a missing/``None`` attribute all mean "no guard".
        """
        value = getattr(self.inner_loop, "max_task_seconds", 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(seconds) or seconds <= 0:
            return 0.0
        return seconds

    def _begin_task_budget(self, task_id: str) -> _TaskBudget:
        """Open this task's budget ledger and start this session's clock.

        RUN-2: AUTO-CR-33 gave every task one wall-clock budget across all its
        rounds, and the audit fix made it survive a restart by persisting the
        task's first start time and deducting elapsed time on every resume.
        That deduction is *calendar* time, not *run* time — stop the run in the
        evening, resume in the morning, and every task that had already started
        is over budget before it does anything. What survives a restart is now
        the seconds this task was actually worked:

            {"consumed_s": 1234.5, "session_started_at": 1757820964.1}

        ``consumed_s`` is summed over sessions; ``session_started_at`` is
        present only while a session is active, and _end_task_budget clears it.
        """
        mts = self._task_budget_seconds()
        if mts <= 0:
            return _TaskBudget(0.0)
        now = time.time()
        try:
            raw = self.state.read_task_file(task_id, _BUDGET_FILE)
        except OSError:
            raw = None
        consumed, legacy = parse_budget_file(raw, now, mts)
        if legacy:
            logger.warning(
                "OuterLoop: task %s has a legacy %s holding only a start "
                "timestamp — that value cannot be turned into consumed time, "
                "so the budget starts at 0 consumed and the file is rewritten "
                "in the current format", task_id, _BUDGET_FILE,
            )
        if consumed > 0:
            logger.info(
                "OuterLoop: task %s resumes with %d s of %d s budget already used",
                task_id, int(consumed), int(mts),
            )
        budget = _TaskBudget(mts, consumed, now)
        try:
            self.state.write_task_file(
                task_id, _BUDGET_FILE, render_budget_file(consumed, now),
            )
        except OSError as exc:
            logger.warning(
                "OuterLoop: could not persist the budget ledger for %s — the "
                "consumed time will not survive a resume this time: %s",
                task_id, exc,
            )
        return budget

    def _end_task_budget(self, task_id: str, budget: _TaskBudget) -> None:
        """Close this session's budget ledger: fold in its elapsed time.

        Called from run_task's ``finally``, so this runs on every exit path —
        pass, block, exhaustion, exception and KeyboardInterrupt alike. The
        KeyboardInterrupt / SIGTERM case that DOES reach this line is a clean
        stop, which is the correct behaviour. A kill that does not (SIGKILL,
        power loss) leaves ``session_started_at`` behind, and
        :func:`parse_budget_file` folds that open session in capped at the
        budget — a crash mid-round consumes at most one budget, never a night.
        """
        if budget.max_seconds <= 0 or budget.session_started_at is None:
            return
        elapsed = max(time.time() - budget.session_started_at, 0.0)
        # Time spent in coder calls that died on the wire is taken back out of the task total (RUN-8).
        elapsed = max(elapsed - max(budget.credit_s, 0.0), 0.0)
        try:
            self.state.write_task_file(
                task_id, _BUDGET_FILE, render_budget_file(budget.consumed_s + elapsed),
            )
        except OSError as exc:
            logger.warning(
                "OuterLoop: could not persist the consumed budget for %s — the "
                "next resume will under-count this session's time: %s",
                task_id, exc,
            )

    def _feedback_paths(self, task_id: str) -> list[Path]:
        """Existing feedback files, sorted by round number (numeric)."""
        d = self.state.task_dir(task_id)
        paths = list(d.glob(_FEEDBACK_GLOB))
        return sorted(paths, key=lambda p: self._round_of(p.name))

    @staticmethod
    def _round_of(name: str) -> int:
        m = _FEEDBACK_RE.search(name)
        return int(m.group(1)) if m else 0

    def _existing_rounds(self, task_id: str) -> int:
        # Uses the shared round counter, so the controller and this loop agree on how many rounds a task has used.
        return highest_completed_round(self.state.task_dir(task_id))

    def _read_round_feedback(self, task_id: str) -> list[str]:
        """Return the compact summary text of each prior round, in order."""
        out: list[str] = []
        for p in self._feedback_paths(task_id):
            try:
                out.append(p.read_text(encoding="utf-8"))
            except OSError:
                continue
        return out

    def _write_round_feedback(
        self, task_id: str, rnd: int, res, impl_version: int = 1
    ) -> Path:
        """Distil a failed round into ONE compact markdown file."""
        last = _truncate(getattr(res, "last_feedback", "") or "", _MAX_FEEDBACK_CHARS)
        attempts = getattr(res, "attempts_used", "?")
        body = (
            f"# Round {rnd} — impl v{impl_version} — task {task_id}\n"
            f"{attempts} attempt(s), all failed.\n\n"
            f"Final issue to fix next round:\n{last}\n"
        )
        return self.state.write_task_file(task_id, f"feedback_round_{rnd}.md", body)

    def _build_impl_history(
        self,
        task_id: str,
        current_impl_version: int,
        original_instruction: str,
    ) -> list[dict]:
        """Return one entry per impl version < current_impl_version (LOOP-4).

        Each entry has keys:
          version          — int, e.g. 1
          strategy_summary — first line of the instruction used for that version
          why_failed       — first line of the last failure for that version

        Reads rewrite_round_*.md for instructions and feedback_round_*.md for
        failures so it works correctly on resumed runs too.
        """
        if current_impl_version <= 1:
            return []

        d = self.state.task_dir(task_id)

        # ── instruction per impl version ─────────────────────────────────────
        impl_instruction: dict[int, str] = {1: original_instruction}
        for rpath in sorted(d.glob("rewrite_round_*.md")):
            try:
                text = rpath.read_text(encoding="utf-8")
                first_line = text.splitlines()[0] if text else ""
                m = _IMPL_HEADER_RE.search(first_line)
                if not m:
                    continue
                ver = int(m.group(1))
                if "## New instruction\n" in text:
                    instr = text.split("## New instruction\n", 1)[1]
                    if "## Acceptance check" in instr:
                        instr = instr.split("## Acceptance check")[0]
                    impl_instruction[ver] = instr.strip()
            except (OSError, ValueError):
                continue

        # ── last failure per impl version ─────────────────────────────────────
        impl_last_failure: dict[int, str] = {}
        for fpath in self._feedback_paths(task_id):
            try:
                text = fpath.read_text(encoding="utf-8")
                first_line = text.splitlines()[0] if text else ""
                m = _IMPL_HEADER_RE.search(first_line)
                if not m:
                    continue
                ver = int(m.group(1))
                if "Final issue to fix next round:\n" in text:
                    issue = text.split("Final issue to fix next round:\n", 1)[1].strip()
                    impl_last_failure[ver] = issue   # last file wins → highest round
            except (OSError, ValueError):
                continue

        # ── assemble one entry per previous version ───────────────────────────
        result: list[dict] = []
        for ver in range(1, current_impl_version):
            raw_instr   = impl_instruction.get(ver, "(unknown strategy)")
            raw_failure = impl_last_failure.get(ver, "(reason not recorded)")
            # Keep each entry to one compact line so coder context stays short.
            summary = raw_instr.splitlines()[0][:160] if raw_instr else ""
            failure = raw_failure.splitlines()[0][:200] if raw_failure else ""
            result.append({
                "version":          ver,
                "strategy_summary": summary,
                "why_failed":       failure,
            })
        return result


def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    return text if len(text) <= max_chars else text[:max_chars] + f"… [+{len(text) - max_chars} chars]"


# Factory

def make_outer_loop(
    config: configparser.ConfigParser,
    base_dir: str | Path,
    state: StateStore,
    *,
    inner_loop=None,
    task_mode: str = "code",
    run_goal: str = "",
    collect_bridge=None,
) -> OuterLoop:
    """Build an :class:`OuterLoop`, constructing the inner loop from config
    unless one is injected (tests / the controller may supply their own).

    AUTO-DM-1: ``task_mode`` is forwarded to ``make_inner_loop`` and stored
    on the constructed inner loop's validator so domain-aware prompts are used.
    Defaults to ``"code"`` — no behavioural change for existing call sites.

    AUTO-CR-22-1: ``run_goal`` is forwarded to ``make_inner_loop`` so the
    fact/prosody gates see the run goal even when the architect didn't echo
    it into a task's own ``instruction``.
    """
    # Config reads are guarded per call, so a malformed value falls back to the default (config-crash audit).
    try:
        max_rounds = config.getint("auto", "max_rounds_per_task",
                                   fallback=_DEFAULT_MAX_ROUNDS)
    except ValueError as exc:
        logger.warning(
            "config [auto] max_rounds_per_task is malformed (%s) — using default %r",
            exc, _DEFAULT_MAX_ROUNDS,
        )
        max_rounds = _DEFAULT_MAX_ROUNDS
    try:
        rewrite_every_n_rounds = config.getint("auto", "rewrite_every_n_rounds", fallback=2)
    except ValueError as exc:
        logger.warning(
            "config [auto] rewrite_every_n_rounds is malformed (%s) — using default 2",
            exc,
        )
        rewrite_every_n_rounds = 2
    try:
        max_rewrites = config.getint("auto", "max_rewrites", fallback=5)
    except ValueError as exc:
        logger.warning(
            "config [auto] max_rewrites is malformed (%s) — using default 5",
            exc,
        )
        max_rewrites = 5

    # A rewriter cannot fire with fewer than three rounds; that mismatch is reported once, not silent (selfrun E2E).
    if max_rewrites > 0 and max_rounds < 3 and task_mode != "creative":
        logger.warning(
            "make_outer_loop: max_rewrites=%d is configured but "
            "max_rounds_per_task=%d < 3 — the task rewriter only fires from "
            "round 3, so it is UNREACHABLE with this config. Raise "
            "[auto] max_rounds_per_task to >= 3 or set max_rewrites = 0.",
            max_rewrites, max_rounds,
        )

    if inner_loop is None:
        inner_loop = make_inner_loop(config, base_dir, task_mode=task_mode,
                                      run_goal=run_goal,   # AUTO-DM-1 / AUTO-CR-22-1
                                      collect_bridge=collect_bridge)  # COLLECT-24

    # A task rewriter is built only when configured, and never in creative mode (LOOP-2, AUTO-CR-27).
    task_rewriter = None
    if max_rewrites > 0 and task_mode != "creative":
        try:
            from tools.auto.architect import TaskRewriter

            active     = config.get("api", "active", fallback="local")
            section    = f"api_{active}"
            base_url   = config.get(section, "base_url")
            api_key    = config.get(section, "api_key",    fallback="")
            model      = config.get(section, "model")
            api_fmt    = config.get(section, "api_format", fallback="openai")
            verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

            task_rewriter = TaskRewriter(
                config=config,
                base_url=base_url,
                api_key=api_key,
                model=model,
                api_format=api_fmt,
                verify_ssl=verify_ssl,
            )
        except Exception as exc:
            logger.warning(
                "make_outer_loop: could not build TaskRewriter — rewriting disabled: %s",
                exc,
            )

    return OuterLoop(
        inner_loop,
        state,
        max_rounds=max_rounds,
        rewrite_every_n_rounds=rewrite_every_n_rounds,
        max_rewrites=max_rewrites,
        task_rewriter=task_rewriter,
    )
