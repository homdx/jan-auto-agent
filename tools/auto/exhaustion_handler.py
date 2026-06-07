"""tools/auto/exhaustion_handler.py — AUTO-C6: exhaustion → knowledge + ticket.

Called by the controller after ``OuterLoop.run_task`` returns an *exhausted*
result (``result.exhausted is True``):

    from tools.auto.exhaustion_handler import ExhaustionHandler

    handler = ExhaustionHandler(state_store)
    outcome = handler.handle(task, outer_result)
    # outcome.knowledge_path  → Path to .agent/tasks/<id>/knowledge.md
    # outcome.ticket_id       → e.g. "TICKET-AUTO-T1"
    # outcome.ticket_path     → Path to .agent/tickets/<ticket_id>.json

Responsibilities
----------------
1. Task is already BLOCKED (set by OuterLoop). Verify / re-assert that status.
2. Write ``.agent/tasks/<id>/knowledge.md`` — a human-readable note that
   captures everything learned across all failed rounds:
   - Task title, instruction, acceptance check
   - All per-round feedback collected by OuterLoopResult.knowledge()
   - A short "deferred investigation" header
3. Open a deferred investigation ticket in ``.agent/tickets/<ticket_id>.json``
   with schema:
   {
     "id":          "TICKET-<task_id>",
     "type":        "investigation",
     "status":      "open",
     "linked_task": "<task_id>",
     "title":       "Deferred: <task title>",
     "body":        "<same content as knowledge.md>",
     "created_at":  "<ISO-8601 UTC>"
   }
4. Log the event via StateStore.log().
5. Return ``ExhaustionOutcome`` with paths to both artefacts.
6. If a dependency is BLOCKED the controller should halt dependent tasks;
   this module does NOT make that decision — it handles one task at a time.

Spec reference: AUTO-C6
    AC: a permanently-failing task does not stall the whole run; a ticket with
        the knowledge is created.
    AC: mark task BLOCKED (already done by OuterLoop; re-asserted here).
    Dep: AUTO-D1 (ticket store CRUD). This module ships its own minimal ticket
         writer so it is self-contained; AUTO-D1 will extend / replace the
         ticket I/O layer when implemented.

Ticket schema (compatible with future AUTO-D1 CRUD helpers):
    id          str  — "TICKET-<task_id>"
    type        str  — "bug" | "investigation"  (always "investigation" here)
    status      str  — "open" | "in-progress" | "fixed" | "deferred"
    linked_task str  — task_id this ticket concerns
    title       str  — short description
    body        str  — full knowledge text
    created_at  str  — ISO-8601 UTC timestamp
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pathlib import Path

from tools.agent_trace import tracer
from tools.auto.state import StateStore, STATUS_BLOCKED
from tools.auto.ticket_store import (
    make_ticket,
    make_ticket_store,
)

logger = logging.getLogger(__name__)

# ── Ticket field constants (re-exported for backward compat) ─────────────────
TICKET_TYPE_INVESTIGATION = "investigation"
TICKET_STATUS_OPEN        = "open"


# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExhaustionOutcome:
    """Artefacts produced for one exhausted task."""
    task_id:        str
    ticket_id:      str
    knowledge_path: Path
    ticket_path:    Path

    def summary(self) -> str:
        return (
            f"[{self.task_id}] BLOCKED — "
            f"knowledge → {self.knowledge_path.name}, "
            f"ticket → {self.ticket_id}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# ExhaustionHandler
# ─────────────────────────────────────────────────────────────────────────────

class ExhaustionHandler:
    """Handles the aftermath of an exhausted task (AUTO-C6).

    Parameters
    ----------
    state_store:
        The run's active :class:`~tools.auto.state.StateStore`.
    """

    def __init__(self, state_store: StateStore) -> None:
        self._state = state_store

    # ── Public API ────────────────────────────────────────────────────────────

    def handle(self, task: dict, outer_result) -> ExhaustionOutcome:
        """Write knowledge note + investigation ticket for an exhausted *task*.

        Parameters
        ----------
        task:
            The task dict (must contain ``"id"``, ``"title"``,
            ``"instruction"``, ``"acceptance_check"``).
        outer_result:
            The ``OuterLoopResult`` returned by ``OuterLoop.run_task``
            (``result.exhausted`` is expected to be ``True``).

        Returns
        -------
        ExhaustionOutcome
            Paths to the knowledge note and the ticket JSON.
        """
        task_id    = task.get("id", "UNKNOWN")
        title      = task.get("title", "")
        instruction= task.get("instruction", "")
        check      = task.get("acceptance_check", "")
        ticket_id  = f"TICKET-{task_id}"

        tracer.event(
            "controller", "exhaustion_handler", "handle_start",
            params={"task": task_id},
        )

        # 1. Re-assert BLOCKED status (OuterLoop already sets this; idempotent).
        self._state.set_task_status(task_id, STATUS_BLOCKED)

        # 2. Build the knowledge text from accumulated round feedback.
        knowledge_text = self._build_knowledge(
            task_id, title, instruction, check, outer_result
        )

        # 3. Write .agent/tasks/<id>/knowledge.md
        kpath = self._state.write_task_file(task_id, "knowledge.md", knowledge_text)
        logger.info("ExhaustionHandler: wrote knowledge note → %s", kpath)

        # 4. Write .agent/tickets/<ticket_id>.json via TicketStore (AUTO-D1)
        tpath = self._open_ticket(ticket_id, task_id, title, knowledge_text)
        logger.info("ExhaustionHandler: opened ticket %s → %s", ticket_id, tpath)

        # 5. Log to run.log
        self._state.log(
            f"task {task_id} BLOCKED — knowledge written, "
            f"investigation ticket {ticket_id} opened"
        )

        tracer.event(
            "exhaustion_handler", "controller", "handled",
            params={
                "task":       task_id,
                "ticket":     ticket_id,
                "knowledge":  str(kpath),
            },
        )

        return ExhaustionOutcome(
            task_id=task_id,
            ticket_id=ticket_id,
            knowledge_path=kpath,
            ticket_path=tpath,
        )

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_knowledge(
        self,
        task_id: str,
        title: str,
        instruction: str,
        acceptance_check: str,
        outer_result,
    ) -> str:
        """Compile a markdown knowledge note from round feedback."""
        rounds_used = getattr(outer_result, "rounds_used", "?")
        raw_knowledge = ""
        if outer_result is not None and hasattr(outer_result, "knowledge"):
            try:
                raw_knowledge = outer_result.knowledge() or ""
            except Exception:  # noqa: BLE001
                raw_knowledge = ""

        sections = [
            f"# Deferred Investigation: {title}",
            f"**Task ID:** {task_id}",
            f"**Rounds attempted:** {rounds_used}",
            "",
            "## Instruction",
            instruction or "(none)",
            "",
            "## Acceptance check",
            f"```\n{acceptance_check or '(none)'}\n```",
            "",
            "## What was tried (per-round feedback)",
        ]
        if raw_knowledge.strip():
            sections.append(raw_knowledge)
        else:
            sections.append("_(no round feedback recorded)_")

        sections += [
            "",
            "## Next steps",
            "- Review the per-round feedback above to understand why the task failed.",
            "- Consider breaking the task into smaller subtasks.",
            "- Check for environment or dependency issues that automated fixing cannot resolve.",
        ]

        return "\n".join(sections) + "\n"

    def _open_ticket(
        self,
        ticket_id: str,
        linked_task: str,
        title: str,
        body: str,
    ) -> Path:
        """Create a ticket via TicketStore (AUTO-D1) and return its path."""
        ts = make_ticket_store(self._state.agent_dir)
        ticket = make_ticket(
            id=ticket_id,
            type=TICKET_TYPE_INVESTIGATION,
            linked_task=linked_task,
            title=f"Deferred: {title}",
            body=body,
        )
        # Idempotent: if a ticket already exists (e.g. resume), skip creation.
        if ts.exists(ticket_id):
            logger.debug("_open_ticket: %s already exists — skipping create", ticket_id)
        else:
            ts.create(ticket)
        return ts.path(ticket_id)


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_exhaustion_handler(state_store: StateStore) -> ExhaustionHandler:
    """Convenience factory — returns an :class:`ExhaustionHandler` for *state_store*."""
    return ExhaustionHandler(state_store)
