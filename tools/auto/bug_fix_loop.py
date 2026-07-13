"""tools/auto/bug_fix_loop.py — AUTO-D2: post-commit bug detection and fix loop.

When a later task's acceptance check reveals a regression in already-committed
work, this module:

1. Opens a bug ticket in the TicketStore.
2. Synthesises a *fix task* from the regression evidence.
3. Runs the fix task through the C-loop (OuterLoop → InnerLoop).
4. If the fix passes, commits it and closes the ticket.
5. If the fix is exhausted, marks the ticket "deferred" and lets the run
   continue — the regression is recorded but does not block unrelated tasks.

Public surface
--------------
    from tools.auto.bug_fix_loop import BugFixLoop, BugFixResult

    bfl = BugFixLoop(
        outer_loop      = outer,       # OuterLoop instance
        commit_on_success = cos,       # CommitOnSuccess instance
        ticket_store    = ts,          # TicketStore instance
        state_store     = state,       # StateStore instance
    )

    result: BugFixResult = bfl.handle_regression(
        triggering_task = task,        # the task dict whose check found the bug
        exec_result     = exec_result, # ExecutionResult from the acceptance check
    )

    result.ticket_id       # e.g. "BUG-AUTO-T3-regression"
    result.fixed           # True if the fix was validated and committed
    result.commit_hash     # SHA or None
    result.fix_task_id     # e.g. "BUG-FIX-AUTO-T3"

Spec reference: AUTO-D2
    AC: a seeded regression produces a ticket, a fix commit, and a closed ticket.
    AC: a permanently-failing fix produces a "deferred" ticket, not a crash.
    Dep: AUTO-C3 (InnerLoop / OuterLoop), AUTO-D1 (TicketStore).

Ticket id convention
--------------------
  Bug ticket:  ``BUG-<triggering_task_id>``
  Fix task id: ``BUG-FIX-<triggering_task_id>``

Both are idempotent — if a bug ticket already exists for the same triggering
task, it is reused rather than duplicated (the fix loop re-runs on the
existing open ticket).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.agent_trace import tracer
from tools.auto.ticket_store import TicketStore, make_ticket, TicketAlreadyExists

logger = logging.getLogger(__name__)

_MAX_OUTPUT_CHARS = 800   # keep ticket bodies and fix instructions concise


# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BugFixResult:
    """Outcome of a single post-commit regression handling attempt."""

    ticket_id:   str
    fix_task_id: str
    fixed:       bool
    commit_hash: Optional[str] = None
    exhausted:   bool = False
    skipped:     bool = False   # True when the ticket already existed + is fixed

    def summary(self) -> str:
        if self.skipped:
            return f"[{self.ticket_id}] already fixed — skipped"
        if self.fixed:
            sha = self.commit_hash[:10] if self.commit_hash else "no-commit"
            return f"[{self.ticket_id}] FIXED — commit {sha}"
        if self.exhausted:
            return f"[{self.ticket_id}] EXHAUSTED — deferred"
        return f"[{self.ticket_id}] fix attempt did not pass"


# ─────────────────────────────────────────────────────────────────────────────
# BugFixLoop
# ─────────────────────────────────────────────────────────────────────────────

class BugFixLoop:
    """Handles a post-commit regression by opening a ticket and running a fix.

    Parameters
    ----------
    outer_loop:
        A ready :class:`~tools.auto.outer_loop.OuterLoop` instance (owns its
        own InnerLoop, coder, executor, and Gate-2 validator).
    commit_on_success:
        A ready :class:`~tools.auto.commit_on_success.CommitOnSuccess`
        instance.
    ticket_store:
        A ready :class:`~tools.auto.ticket_store.TicketStore` instance.
    state_store:
        The run's :class:`~tools.auto.state.StateStore` instance.  Used to
        register the synthetic fix task so state is resumable.
    """

    def __init__(
        self,
        outer_loop,
        commit_on_success,
        ticket_store: TicketStore,
        state_store,
    ) -> None:
        self._outer        = outer_loop
        self._cos          = commit_on_success
        self._tickets      = ticket_store
        self._state        = state_store

    # ── Public API ───────────────────────────────────────────────────────────

    def handle_regression(
        self,
        triggering_task: dict,
        exec_result,
        base_dir: str | Path = ".",
    ) -> BugFixResult:
        """Detect a regression, open/reuse a ticket, and attempt a fix.

        Parameters
        ----------
        triggering_task:
            The task dict that revealed the regression.  Must contain at
            least ``"id"``, ``"title"``, ``"target_files"``, and
            ``"acceptance_check"``.
        exec_result:
            The :class:`~tools.auto.executor.ExecutionResult` from the failed
            acceptance check of *triggering_task* (``exec_result.passed`` is
            ``False``).
        base_dir:
            Root of the repository (passed through to the outer loop).

        Returns
        -------
        BugFixResult
        """
        trig_id   = triggering_task.get("id", "UNKNOWN")
        ticket_id = f"BUG-{trig_id}"
        fix_id    = f"BUG-FIX-{trig_id}"

        tracer.event(
            "controller", "bug_fix_loop", "regression_detected",
            params={"triggering_task": trig_id, "ticket_id": ticket_id},
        )

        # ── 1. Open (or reuse) bug ticket ────────────────────────────────────
        existing = self._tickets.get(ticket_id)
        if existing and existing.get("status") == "fixed":
            logger.info(
                "BugFixLoop: ticket %s already fixed — skipping", ticket_id
            )
            return BugFixResult(ticket_id, fix_id, fixed=True, skipped=True)

        # Bugfix: a "deferred" ticket (a prior fix attempt already exhausted
        # its full OuterLoop rounds/rewrites budget) had no short-circuit —
        # only "fixed" did. controller._check_regressions re-runs EVERY
        # previously-DONE task's acceptance check after EVERY subsequent
        # commit for the rest of the run; a persistent, hard-to-fix
        # regression's status never changes, so every later commit
        # re-triggered this exact same expensive fix loop again from
        # scratch — burning a full attempt budget on a regression already
        # known not to resolve, over and over, for the rest of the run.
        # Once deferred, an operator is expected to look at the ticket and
        # decide whether to retry (exactly the manual ``status="open"``
        # reset already exercised by
        # test_existing_open_ticket_reused_not_duplicated) rather than the
        # system re-attempting it automatically on every unrelated commit.
        if existing and existing.get("status") == "deferred":
            logger.info(
                "BugFixLoop: ticket %s already deferred — skipping "
                "(reset its status to retry)", ticket_id,
            )
            return BugFixResult(ticket_id, fix_id, fixed=False, exhausted=True, skipped=True)

        if existing is None:
            body = self._build_ticket_body(triggering_task, exec_result)
            ticket = make_ticket(
                id=ticket_id,
                type="bug",
                linked_task=trig_id,
                title=f"Regression: {triggering_task.get('title', trig_id)}",
                body=body,
                status="open",
            )
            try:
                self._tickets.create(ticket)
            except TicketAlreadyExists:
                pass   # race-safe; continue with whatever is on disk
        else:
            # Ticket exists and is not fixed — update status to in-progress.
            self._tickets.update(ticket_id, status="in-progress")

        self._state.log(
            f"bug ticket {ticket_id} opened for regression in task {trig_id}"
        )
        tracer.event(
            "bug_fix_loop", "controller", "ticket_created",
            params={"ticket_id": ticket_id},
        )

        # ── 2. Build synthetic fix task ───────────────────────────────────────
        fix_task = self._build_fix_task(
            fix_id, ticket_id, triggering_task, exec_result
        )
        self._state.upsert_task(fix_task)

        # ── 3. Run the fix through the C-loop ─────────────────────────────────
        logger.info(
            "BugFixLoop: running fix loop for %s → %s", ticket_id, fix_id
        )
        outer_result = self._outer.run_task(fix_task, base_dir)

        # ── 4a. Fix passed — commit and close ticket ──────────────────────────
        if getattr(outer_result, "passed", False):
            sha = self._cos.commit(fix_task, outer_result)
            self._tickets.update(ticket_id, status="fixed")
            self._state.log(
                f"bug {ticket_id} FIXED — commit {sha[:10] if sha else 'none'} "
                f"(fix task {fix_id})"
            )
            tracer.event(
                "bug_fix_loop", "controller", "fixed",
                params={"ticket_id": ticket_id, "sha": (sha or "")[:12]},
            )
            return BugFixResult(ticket_id, fix_id, fixed=True, commit_hash=sha)

        # ── 4b. Fix exhausted — defer ticket, continue run ───────────────────
        if getattr(outer_result, "exhausted", False):
            knowledge = _truncate(
                outer_result.knowledge() if hasattr(outer_result, "knowledge")
                else "", _MAX_OUTPUT_CHARS
            )
            # BUGFIX: TicketStore.get() does a fresh disk read here and
            # returns None if the ticket file is absent — and self._outer
            # .run_task() above can run for a long time (many LLM calls /
            # retries), during which the ticket file could be deleted
            # externally (an operator cleaning up a stuck ticket, a
            # concurrent process, a filesystem hiccup). The ticket having
            # existed or been created earlier in this same call doesn't
            # guarantee it still exists now — a bare `[...]["body"]`
            # subscript on None would raise TypeError and crash the whole
            # bug-fix loop instead of just this one deferred update.
            _existing_ticket = self._tickets.get(ticket_id) or {}
            self._tickets.update(
                ticket_id,
                status="deferred",
                body=_existing_ticket.get("body", "") + (
                    f"\n\n## Fix attempt exhausted\n{knowledge}"
                ),
            )
            self._state.log(
                f"bug {ticket_id} fix EXHAUSTED — deferred (fix task {fix_id})"
            )
            tracer.event(
                "bug_fix_loop", "controller", "exhausted",
                params={"ticket_id": ticket_id},
            )
            return BugFixResult(
                ticket_id, fix_id, fixed=False, exhausted=True
            )

        # ── 4c. Partial failure (shouldn't normally happen) ───────────────────
        self._state.log(
            f"bug {ticket_id} fix loop returned without pass or exhaustion"
        )
        return BugFixResult(ticket_id, fix_id, fixed=False)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_ticket_body(self, triggering_task: dict, exec_result) -> str:
        """Build the initial bug ticket body from the regression evidence."""
        trig_id   = triggering_task.get("id", "")
        cmd       = triggering_task.get("acceptance_check", "")
        rc        = getattr(exec_result, "exit_code", "?")
        stdout    = _truncate(getattr(exec_result, "stdout", "") or "", _MAX_OUTPUT_CHARS // 2)
        stderr    = _truncate(
            getattr(exec_result, "traceback", "") or
            getattr(exec_result, "stderr", "") or "", _MAX_OUTPUT_CHARS // 2
        )
        timed_out = getattr(exec_result, "timed_out", False)
        timeout_note = " (timed out)" if timed_out else ""
        return (
            f"Regression detected in task {trig_id}{timeout_note}.\n\n"
            f"**Acceptance check:** `{cmd}`\n"
            f"**Exit code:** {rc}\n\n"
            f"### stdout\n```\n{stdout}\n```\n\n"
            f"### stderr / traceback\n```\n{stderr}\n```"
        )

    def _build_fix_task(
        self,
        fix_id: str,
        ticket_id: str,
        triggering_task: dict,
        exec_result,
    ) -> dict:
        """Return a synthetic task dict for the fix loop."""
        from tools.auto.state import make_task

        trig_title = triggering_task.get("title", triggering_task.get("id", ""))
        rc         = getattr(exec_result, "exit_code", "?")
        stdout     = _truncate(getattr(exec_result, "stdout", "") or "", _MAX_OUTPUT_CHARS // 2)
        stderr     = _truncate(
            getattr(exec_result, "traceback", "") or
            getattr(exec_result, "stderr", "") or "", _MAX_OUTPUT_CHARS // 2
        )
        target_files = triggering_task.get("target_files", [])
        check        = triggering_task.get("acceptance_check", "")

        instruction = (
            f"Fix the regression introduced by task {triggering_task.get('id', '')} "
            f"(ticket {ticket_id}).\n\n"
            f"The following acceptance check now fails:\n"
            f"  Command:   {check}\n"
            f"  Exit code: {rc}\n\n"
            f"Stdout:\n{stdout}\n\n"
            f"Stderr/traceback:\n{stderr}\n\n"
            f"Restore the failing check to exit 0 without breaking other "
            f"already-passing checks.  Only touch the files listed in "
            f"target_files unless the bug is clearly in another file."
        )

        return make_task(
            id=fix_id,
            title=f"Fix regression: {trig_title}",
            instruction=instruction,
            target_files=target_files,
            acceptance_check=check,
            dependencies=[],
            # tag for traceability
            linked_ticket=ticket_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_bug_fix_loop(
    config,
    base_dir,
    state_store,
    *,
    outer_loop=None,
    commit_on_success=None,
    ticket_store: Optional[TicketStore] = None,
) -> BugFixLoop:
    """Build a :class:`BugFixLoop` from config.

    Any of *outer_loop*, *commit_on_success*, or *ticket_store* can be
    injected (tests and the controller may supply their own).

    Parameters
    ----------
    config:
        A ``configparser.ConfigParser`` instance.
    base_dir:
        Root of the repository.
    state_store:
        The active ``StateStore`` for this run.
    outer_loop:
        Optional pre-built ``OuterLoop``.
    commit_on_success:
        Optional pre-built ``CommitOnSuccess``.
    ticket_store:
        Optional pre-built ``TicketStore``.
    """
    from tools.auto.outer_loop import make_outer_loop
    from tools.auto.commit_on_success import make_commit_on_success
    from tools.auto.ticket_store import make_ticket_store

    if outer_loop is None:
        outer_loop = make_outer_loop(config, base_dir, state_store)

    if commit_on_success is None:
        commit_on_success = make_commit_on_success(
            config, base_dir, state_store
        )

    if ticket_store is None:
        ticket_store = make_ticket_store(state_store.agent_dir)

    return BugFixLoop(
        outer_loop=outer_loop,
        commit_on_success=commit_on_success,
        ticket_store=ticket_store,
        state_store=state_store,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    return (
        text if len(text) <= max_chars
        else text[:max_chars] + f"… [+{len(text) - max_chars} chars]"
    )
