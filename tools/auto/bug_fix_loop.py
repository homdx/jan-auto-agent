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
import shutil
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.agent_trace import tracer
from tools.auto.ticket_store import (
    TicketStore, make_ticket, TicketAlreadyExists, TicketError, TicketNotFound,
)

from tools.auto.state import STATUS_BLOCKED, STATUS_DONE

logger = logging.getLogger(__name__)

_FIX_PREFIX = "BUG-FIX-"

# Attempts a bug ticket may make through the expensive OuterLoop before an operator must step in; each attempt is a full budget.
MAX_FIX_ATTEMPTS = 2


def _root_task_id(task_id: str) -> str:
    """Strip any accumulated ``BUG-FIX-`` prefixes from a task id.

    A synthetic fix task (``BUG-FIX-AUTO-T1``) is upserted into state and,
    once committed, becomes a DONE task carrying the *same* acceptance check
    as the task it was created to repair.  ``controller._check_regressions``
    re-runs every DONE task's check after every later commit, so a regression
    that does not actually get repaired makes the fix task itself "regress".
    Deriving ids from that fix task's own id produced a new generation on
    every commit — ``BUG-FIX-BUG-FIX-AUTO-T1``, and so on — which:

      * defeated the ``already fixed`` / ``already deferred`` short-circuits,
        because every generation carried a brand-new ticket id; and
      * grew the ticket *filename* by 8 chars per generation until
        ``TicketStore._write`` died with ``OSError: [Errno 36] File name too
        long``, killing the whole run.

    Canonicalising back to the root id maps the fix task's failure onto the
    original bug ticket, which is what it actually is — so the existing
    dedup fires and the cascade terminates after one generation.
    """
    tid = task_id or ""
    while tid.startswith(_FIX_PREFIX):
        tid = tid[len(_FIX_PREFIX):]
    return tid or (task_id or "UNKNOWN")


_MAX_OUTPUT_CHARS = 800   # keep ticket bodies and fix instructions concise


# Result

@dataclass
class BugFixResult:
    """Outcome of a single post-commit regression handling attempt."""

    ticket_id:   str
    fix_task_id: str
    fixed:       bool
    commit_hash: Optional[str] = None
    exhausted:   bool = False
    skipped:     bool = False   # True when a terminal ticket state short-circuited
    verification_failed: bool = False
    """A ticket previously marked ``fixed`` whose acceptance check has failed
    again.  The recorded fix did not hold — the run must not report green."""

    def summary(self) -> str:
        if self.verification_failed:
            return (
                f"[{self.ticket_id}] VERIFICATION FAILED — a fix was recorded "
                f"as passing but the acceptance check fails again"
            )
        if self.skipped:
            if self.exhausted:
                return f"[{self.ticket_id}] already deferred — skipped"
            return f"[{self.ticket_id}] already fixed — skipped"
        if self.fixed:
            sha = self.commit_hash[:10] if self.commit_hash else "no-commit"
            return f"[{self.ticket_id}] FIXED — commit {sha}"
        if self.exhausted:
            return f"[{self.ticket_id}] EXHAUSTED — deferred"
        return f"[{self.ticket_id}] fix attempt did not pass"


# BugFixLoop

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
        max_fix_attempts: int = MAX_FIX_ATTEMPTS,
    ) -> None:
        self._outer            = outer_loop
        self._cos              = commit_on_success
        self._tickets          = ticket_store
        self._state            = state_store
        self._max_fix_attempts = max(1, int(max_fix_attempts))

    # ── Public API ───────────────────────────────────────────────────────────

    def _clear_stale_fix_rounds(self, fix_id: str) -> None:
        """Archive a fix task's feedback rounds so a retry is a REAL retry.

        OuterLoop does not derive its starting round from task state — it
        counts ``feedback_round_*.md`` files on disk (``start_round =
        done_rounds + 1``) and returns "already exhausted" immediately when
        that exceeds ``max_rounds``.  So once a fix attempt has burned its
        rounds, every later attempt returns exhausted instantly having done
        no work at all:

            highest_completed_round = 10, max_rounds = 10
            -> start_round = 11 > 10 -> instant exhausted, ZERO new work

        That made the operator reset (ticket status -> "open", which restores
        the attempt budget) completely inert — the documented way to give a
        stuck regression another chance silently did nothing.  This is the
        same trap ``_reset_resettable_blocked_tasks`` documents for BLOCKED
        tasks: "a bare status reset never touches" the files OuterLoop reads.

        The rounds are moved aside rather than deleted, so the accumulated
        feedback stays inspectable next to the task instead of being thrown
        away to buy a retry.
        """
        try:
            tdir = self._state.task_dir(fix_id)
            rounds = sorted(tdir.glob("feedback_round_*.md"))
            if not rounds:
                return
            # Archive stamps are UTC, matching the run log and ticket timestamps (FIX-1 #8).
            stamp   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive = tdir / f"previous_attempt_{stamp}"
            # Two archives in the same second get a suffix; without it the second would overwrite the first's feedback (bugfix).
            suffix_n = 0
            # Retry mkdir on collision instead of checking first, so a concurrent writer cannot make archiving fail silently (bugfix).
            while True:
                try:
                    archive.mkdir(parents=True)
                    break
                except FileExistsError:
                    suffix_n += 1
                    archive = tdir / f"previous_attempt_{stamp}-{suffix_n:03d}"
            for f in rounds:
                f.rename(archive / f.name)
            logger.info(
                "BugFixLoop: archived %d stale feedback round(s) for %s to %s "
                "so the retry starts from round 1",
                len(rounds), fix_id, archive.name,
            )
            # Reset the time budget too: a stale deadline ledger would re-block the retry at once (bugfix).
            deadline_path = tdir / "deadline_started_at.txt"
            if deadline_path.exists():
                try:
                    deadline_path.unlink()
                except OSError as exc:
                    logger.warning(
                        "BugFixLoop: could not remove %s for %s — the "
                        "retry may inherit a stale wall-clock budget: %s",
                        deadline_path.name, fix_id, exc,
                    )
            self._prune_old_archives(tdir)
        except OSError as exc:
            logger.warning(
                "BugFixLoop: could not archive stale rounds for %s: %s — the "
                "retry may return exhausted without doing work", fix_id, exc,
            )

    def _discard_fix_residue(self, fix_id: str) -> None:
        """Drop a failed fix attempt's uncommitted edits.

        This is the documented "Bug 2" hazard, which the controller already
        guards for main tasks but nobody guarded for fix attempts:

            the coder writes its candidate into base_dir BEFORE validation, so
            an attempt that ends without a commit leaves that edit dirty — and
            commit() stages everything (git add -u && git add .), which sweeps
            it into the NEXT successful task's commit.

        For a fix attempt the consequence is worse than mis-attribution: the
        edit that gets swept in is code that FAILED its acceptance check, so
        broken work lands silently under an unrelated task's message.
        Reproduced on a real repo before this fix:

            fix attempt exhausted (acceptance check FAILED)
              git status: M f.py
              f.py      : BROKEN half-finished fix

        Safe by the same argument the controller relies on: handle_regression
        is entered from _check_regressions immediately after a commit, so the
        working tree is HEAD plus only this fix attempt's edits.  Resetting to
        HEAD therefore discards exactly that residue and nothing already
        committed.  A no-op when git is unavailable.
        """
        git = getattr(self._cos, "_git", None) if self._cos is not None else None
        if git is None:
            return                      # no-git mode — nothing to clean
        try:
            git.discard_working_changes()
            self._state.log(
                f"fix task {fix_id} uncommitted edits discarded "
                f"(attempt ended without a commit)"
            )
        except Exception as exc:        # pragma: no cover — defensive
            logger.warning(
                "BugFixLoop: could not discard residue for %s: %s — its edits "
                "may be swept into the next commit", fix_id, exc,
            )

    def _prune_old_archives(self, tdir: Path, *, keep: int = 3) -> None:
        """Cap the ``previous_attempt_*`` directories _clear_stale_fix_rounds
        creates, mirroring Executor._prune_old_workspaces for the same
        reason: an operator repeatedly resetting a persistently-broken
        ticket — precisely the scenario the reset exists for — creates one
        more archive per reset with no bound, so a ticket that takes many
        resets to finally resolve (or one that is reset periodically and
        never resolves) accumulates archive directories forever, one full
        MAX_FIX_ATTEMPTS-worth of round files each.

        Kept small deliberately: unlike a task workspace (a full repo copy,
        expensive to keep many of), each archive is a handful of small
        markdown files, so the retention count favours a longer forensic
        trail over disk savings.  Oldest-first by directory name, which
        sorts chronologically because the timestamp format
        (``%Y%m%dT%H%M%S``) is lexicographically ordered.
        """
        try:
            archives = sorted(
                p for p in tdir.iterdir()
                if p.is_dir() and p.name.startswith("previous_attempt_")
            )
        except OSError:
            return
        for old in archives[:-keep] if keep > 0 else archives:
            try:
                shutil.rmtree(old)
            except OSError as exc:
                logger.warning(
                    "BugFixLoop: could not prune old archive %s: %s", old, exc
                )

    def _park_fix_task(self, fix_id: str) -> None:
        """Mark a non-succeeding synthetic fix task BLOCKED.

        _build_fix_task registers the fix task with the default STATUS_TODO,
        and only the 4a success path clears it (via CommitOnSuccess).  On the
        exhausted / no-verdict paths it stayed TODO forever — and
        StateStore.resume_info() counts every task that is not DONE or
        BLOCKED as "pending".  So a resumed run picked the fix task out of
        the main queue and spent a fresh full OuterLoop budget on the exact
        regression that had just been deferred as unfixable, completely
        bypassing the ticket short-circuit (which only guards re-entry via
        _check_regressions, not the task queue).

        BLOCKED is the honest status: it cannot proceed without a human, and
        the ticket is that human's handle.  Resetting the ticket to "open"
        re-registers the fix task as TODO through the normal path, so this
        does not make a retry impossible.
        """
        # Park only if the fix task exists; checking first keeps the common case quiet.
        existing_task = self._state.get_task(fix_id)
        if existing_task is None:
            return
        if existing_task.get("status") in (STATUS_DONE, STATUS_BLOCKED):
            return                          # already settled — nothing to do
        try:
            self._state.set_task_status(fix_id, STATUS_BLOCKED)
            logger.info(
                "BugFixLoop: parked fix task %s as BLOCKED (its regression is "
                "not resolvable without a human)", fix_id,
            )
        except Exception as exc:            # pragma: no cover — defensive
            logger.warning(
                "BugFixLoop: could not park fix task %s: %s", fix_id, exc
            )

    def _safe_update(self, ticket_id: str, **fields) -> bool:
        """Update a ticket, tolerating its file having vanished.

        TicketStore.update() re-reads from disk and raises TicketNotFound if
        the file is gone.  handle_regression can spend a long time inside
        OuterLoop (many LLM calls), during which an operator cleaning up a
        stuck ticket — or a concurrent process — can delete it.  An
        unhandled raise here propagates out through _check_regressions and
        kills the whole run over a bookkeeping write, which is exactly the
        failure mode already guarded against on the exhaustion path.

        Returns True if the write landed.
        """
        try:
            self._tickets.update(ticket_id, **fields)
            return True
        except TicketNotFound:
            logger.warning(
                "BugFixLoop: ticket %s disappeared before update %s — "
                "continuing", ticket_id, sorted(fields),
            )
            return False
        except (TicketError, OSError) as exc:
            # A corrupt or unwritable ticket is logged, not allowed to crash the run from bookkeeping (bugfix).
            logger.warning(
                "BugFixLoop: could not update ticket %s (%s) — "
                "continuing", ticket_id, exc,
            )
            return False

    @staticmethod
    def _attempts_of(ticket: Optional[dict]) -> int:
        """Read fix_attempts defensively.

        Tickets are JSON on disk and may predate this field, be hand-edited
        by an operator, or carry a non-numeric value.  A bare int() would
        raise ValueError/TypeError and take the run down; an unreadable
        counter degrades to 0, which is safe because the status gate still
        bounds the work.
        """
        try:
            return max(0, int((ticket or {}).get("fix_attempts", 0) or 0))
        except (TypeError, ValueError):
            logger.warning("BugFixLoop: unreadable fix_attempts — treating as 0")
            return 0

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
        root_id   = _root_task_id(trig_id)
        ticket_id = f"BUG-{root_id}"
        fix_id    = f"{_FIX_PREFIX}{root_id}"

        tracer.event(
            "controller", "bug_fix_loop", "regression_detected",
            params={"triggering_task": trig_id, "ticket_id": ticket_id},
        )

        # 1. Ticket state gate: a ticket that is not open is left alone.
        existing = self._tickets.get(ticket_id)
        status   = (existing or {}).get("status")
        attempts = self._attempts_of(existing)

        # An operator reset to open restores the attempt budget; otherwise the retry would get one attempt and log an impossible count.
        if status == "open" and attempts:
            logger.info(
                "BugFixLoop: ticket %s was reset to open — restoring the fix "
                "attempt budget (was %d)", ticket_id, attempts,
            )
            attempts = 0
            # Restoring the counter alone is not enough — see the helper.
            self._clear_stale_fix_rounds(fix_id)

        # A fixed ticket that fails its check is a contradiction: escalate once to a state an operator must look at.
        if status == "fixed":
            logger.error(
                "BugFixLoop: ticket %s is marked fixed but its acceptance "
                "check failed again (rc=%s) — the recorded fix did not hold; "
                "escalating to verification-failed",
                ticket_id, getattr(exec_result, "exit_code", "?"),
            )
            self._safe_update(
                ticket_id,
                status="verification-failed",
                body=(existing.get("body", "") +
                      "\n\n## Verification failed\n" +
                      self._build_ticket_body(triggering_task, exec_result)),
            )
            self._state.log(
                f"VERIFICATION FAILED: {ticket_id} was marked fixed but "
                f"acceptance check for {trig_id} still fails "
                f"(rc={getattr(exec_result, 'exit_code', '?')})"
            )
            tracer.event(
                "bug_fix_loop", "controller", "verification_failed",
                params={"ticket_id": ticket_id, "triggering_task": trig_id},
            )
            self._park_fix_task(fix_id)
            return BugFixResult(
                ticket_id, fix_id, fixed=False,
                skipped=True, verification_failed=True,
            )

        if status == "verification-failed":
            logger.info(
                "BugFixLoop: ticket %s already verification-failed — skipping "
                "(reset its status to retry)", ticket_id,
            )
            self._park_fix_task(fix_id)
            return BugFixResult(
                ticket_id, fix_id, fixed=False,
                skipped=True, verification_failed=True,
            )

        # A deferred ticket is not re-attempted on later commits; an operator resets it to open.
        if status == "deferred":
            logger.info(
                "BugFixLoop: ticket %s already deferred — skipping "
                "(reset its status to retry)", ticket_id,
            )
            self._park_fix_task(fix_id)
            return BugFixResult(
                ticket_id, fix_id, fixed=False, exhausted=True, skipped=True,
            )

        # An in-progress ticket from an unfinished attempt is bounded by the attempt counter, then deferred (bugfix).
        if status == "in-progress" and attempts >= self._max_fix_attempts:
            logger.warning(
                "BugFixLoop: ticket %s has used its %d fix attempts without "
                "reaching a verdict — deferring", ticket_id,
                self._max_fix_attempts,
            )
            self._safe_update(ticket_id, status="deferred")
            self._state.log(
                f"bug {ticket_id} deferred — {attempts} inconclusive fix "
                f"attempts, no pass or exhaustion verdict"
            )
            tracer.event(
                "bug_fix_loop", "controller", "attempts_exhausted",
                params={"ticket_id": ticket_id, "attempts": attempts},
            )
            # Discard the uncommitted fix edits before deferring, as the other defer branches do (bugfix).
            self._discard_fix_residue(fix_id)
            self._park_fix_task(fix_id)
            return BugFixResult(
                ticket_id, fix_id, fixed=False, exhausted=True, skipped=True,
            )

        if existing is None:
            body = self._build_ticket_body(triggering_task, exec_result)
            ticket = make_ticket(
                id=ticket_id,
                type="bug",
                # Link the ticket to the root task, not the synthetic fix task, so the root can find it.
                linked_task=root_id,
                title=f"Regression: {triggering_task.get('title', trig_id)}",
                body=body,
                status="open",
                fix_attempts=0,
            )
            try:
                self._tickets.create(ticket)
            except TicketAlreadyExists:
                pass   # race-safe; continue with whatever is on disk
            except TicketError as exc:
                # A ticket write failure is logged; losing the record is acceptable, crashing after a commit is not (AUTO-T37).
                logger.warning(
                    "BugFixLoop: ticket %s could not be persisted (%s) "
                    "— continuing without ticket record",
                    ticket_id, exc,
                )

        # Count the attempt before the long fix loop, so a run killed mid-fix cannot resume with a free attempt.
        attempts += 1
        self._safe_update(
            ticket_id, status="in-progress", fix_attempts=attempts,
        )

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
        # A new regression on the same root re-opens the done fix task explicitly, so it is not left silently unfixed (A1).
        self._state.upsert_task(fix_task, allow_downgrade=True)

        # ── 3. Run the fix through the C-loop ─────────────────────────────────
        logger.info(
            "BugFixLoop: running fix loop for %s → %s", ticket_id, fix_id
        )
        try:
            outer_result = self._outer.run_task(fix_task, base_dir)
        except BaseException:
            # On a crash, discard the residue and park the fix task, so the main queue never runs it outside the attempt bound (bugfix).
            self._discard_fix_residue(fix_id)
            self._park_fix_task(fix_id)
            # Settle the label if this crash used the last attempt, so the ticket does not look live forever.
            if attempts >= self._max_fix_attempts:
                self._safe_update(ticket_id, status="deferred")
                self._state.log(
                    f"bug {ticket_id} deferred — fix loop crashed on its "
                    f"last attempt ({attempts}/{self._max_fix_attempts})"
                )
            raise

        # ── 4a. Fix passed — commit and close ticket ──────────────────────────
        if getattr(outer_result, "passed", False):
            if self._cos is not None:
                sha = self._cos.commit(fix_task, outer_result)
            else:
                # Without git there is no commit sha; the fix is marked done by hand, as the controller does (non-fatal git setup).
                sha = None
                try:
                    self._state.set_task_status(fix_id, STATUS_DONE)
                except Exception as exc:        # pragma: no cover — defensive
                    logger.warning(
                        "BugFixLoop: could not mark fix task %s DONE: %s",
                        fix_id, exc,
                    )
                self._state.log(
                    f"bug {ticket_id} fix passed — marked DONE without a "
                    f"commit (no-git mode)"
                )

            # A commit that returns no sha (error or nothing staged) marks nothing done; the status follows the real outcome.
            settled = (self._state.get_task(fix_id) or {}).get("status") == STATUS_DONE
            if not sha and not settled:
                # No-git mode has no git commit at all, so report the state-store failure rather than a git error (AUTO-FIX).
                no_git_mode = self._cos is None
                reason = (
                    "could not mark the fix task DONE (no-git mode)"
                    if no_git_mode else "the commit failed (git error)"
                )
                logger.error(
                    "BugFixLoop: fix task %s passed its check but %s — "
                    "NOT marking %s fixed",
                    fix_id, reason, ticket_id,
                )
                self._state.log(
                    f"bug {ticket_id} fix passed but {reason} "
                    f"(attempt {attempts}/{self._max_fix_attempts}) — "
                    f"not marking fixed"
                )
                tracer.event(
                    "bug_fix_loop", "controller", "fix_not_committed",
                    params={"ticket_id": ticket_id, "fix_task": fix_id},
                )
                self._discard_fix_residue(fix_id)
                self._park_fix_task(fix_id)
                if attempts >= self._max_fix_attempts:
                    self._safe_update(ticket_id, status="deferred")
                    self._state.log(
                        f"bug {ticket_id} deferred — fix attempts exhausted "
                        f"without a commit"
                    )
                    return BugFixResult(
                        ticket_id, fix_id, fixed=False, exhausted=True,
                    )
                return BugFixResult(ticket_id, fix_id, fixed=False)

            self._safe_update(ticket_id, status="fixed")
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
            # The ticket may have vanished during a long run: re-read it defensively, so a missing ticket cannot crash the loop (BUGFIX).
            _existing_ticket = self._tickets.get(ticket_id) or {}
            self._safe_update(
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
            self._discard_fix_residue(fix_id)
            self._park_fix_task(fix_id)
            return BugFixResult(
                ticket_id, fix_id, fixed=False, exhausted=True
            )

        # 4c: no verdict from OuterLoop: the ticket stays in progress only while attempts remain, then is deferred like any other.
        logger.warning(
            "BugFixLoop: fix loop for %s returned without pass or exhaustion "
            "(attempt %d/%d)", ticket_id, attempts, self._max_fix_attempts,
        )
        self._state.log(
            f"bug {ticket_id} fix loop returned without pass or exhaustion "
            f"(attempt {attempts}/{self._max_fix_attempts})"
        )
        if attempts >= self._max_fix_attempts:
            self._safe_update(ticket_id, status="deferred")
            self._state.log(
                f"bug {ticket_id} deferred — fix attempts exhausted with no "
                f"verdict"
            )
            self._discard_fix_residue(fix_id)
            self._park_fix_task(fix_id)
            return BugFixResult(
                ticket_id, fix_id, fixed=False, exhausted=True,
            )
        self._discard_fix_residue(fix_id)
        self._park_fix_task(fix_id)
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


# Factory

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
        # None means "no git" when the controller already decided that; rebuilding a helper here would re-raise its swallowed GitError.
        from tools.auto.git_manager import GitError
        try:
            commit_on_success = make_commit_on_success(
                config, base_dir, state_store
            )
        except (GitError, OSError) as exc:
            logger.warning(
                "make_bug_fix_loop: git unavailable (%s) — bug fixes will be "
                "marked DONE without commits", exc,
            )
            commit_on_success = None

    if ticket_store is None:
        ticket_store = make_ticket_store(state_store.agent_dir)

    return BugFixLoop(
        outer_loop=outer_loop,
        commit_on_success=commit_on_success,
        ticket_store=ticket_store,
        state_store=state_store,
    )


# Helpers

def _truncate(text: str, max_chars: int) -> str:
    text = text or ""
    return (
        text if len(text) <= max_chars
        else text[:max_chars] + f"… [+{len(text) - max_chars} chars]"
    )
