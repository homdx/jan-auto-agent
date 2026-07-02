"""tools/auto/ticket_store.py — AUTO-D1: ticket store (CRUD helpers).

Owns all I/O under ``.agent/tickets/``.  Each ticket is a single JSON file:

    .agent/tickets/<ticket-id>.json

Ticket schema
-------------
    id          str  — unique ticket identifier, e.g. "TICKET-AUTO-T1"
    type        str  — "bug" | "investigation"
    status      str  — "open" | "in-progress" | "fixed" | "deferred"
    linked_task str  — task id this ticket concerns (may be empty string)
    title       str  — short human-readable description
    body        str  — full detail / knowledge text
    created_at  str  — ISO-8601 UTC timestamp (set on create; never mutated)
    updated_at  str  — ISO-8601 UTC timestamp (set on every write)

Public surface
--------------
    from tools.auto.ticket_store import TicketStore, make_ticket

    ts = TicketStore(agent_dir)

    # Create
    ticket = make_ticket(
        id="TICKET-AUTO-T1",
        type="investigation",
        linked_task="AUTO-T1",
        title="Deferred: Fix retry logic",
        body="Round 1 failed: timeout …",
    )
    ts.create(ticket)           # writes .agent/tickets/TICKET-AUTO-T1.json

    # Read
    t = ts.get("TICKET-AUTO-T1")            # dict | None
    all_ = ts.list_all()                    # list[dict], sorted by created_at
    open_ = ts.list_by_status("open")       # filtered list

    # Update
    ts.update_status("TICKET-AUTO-T1", "fixed")
    ts.update_body("TICKET-AUTO-T1", "new body text")
    ts.update("TICKET-AUTO-T1", status="fixed", body="new body")  # multi-field

    # Delete
    ts.delete("TICKET-AUTO-T1")             # removes file; no-op if absent

AC (from Jira story AUTO-D1):
    * tickets persist and survive resume (JSON files on disk).
    * CRUD helpers: create / get / list_all / list_by_status /
                    update_status / update_body / update / delete.
    * Schema validated on create and update.
    * Duplicate create raises TicketAlreadyExists.
    * ``make_ticket`` convenience constructor fills in timestamps and validates.
"""

from __future__ import annotations

import json
import logging
from tools.auto.utils import _ts, atomic_write_text
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Valid field values ────────────────────────────────────────────────────────

TICKET_TYPES   = {"bug", "investigation"}
TICKET_STATUSES = {"open", "in-progress", "fixed", "deferred"}

# ── Required fields (str) in a ticket dict ───────────────────────────────────

_REQUIRED: dict[str, type] = {
    "id":          str,
    "type":        str,
    "status":      str,
    "linked_task": str,
    "title":       str,
    "body":        str,
    "created_at":  str,
    "updated_at":  str,
}


# ─────────────────────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────────────────────

class TicketError(RuntimeError):
    """Base class for TicketStore errors."""


class TicketAlreadyExists(TicketError):
    """Raised when ``create`` is called for a ticket id that already exists."""


class TicketNotFound(TicketError):
    """Raised when an update/delete targets a ticket id that does not exist."""


class TicketSchemaError(TicketError):
    """Raised when a ticket dict violates the schema."""


# ─────────────────────────────────────────────────────────────────────────────
# Schema helper
# ─────────────────────────────────────────────────────────────────────────────

def make_ticket(
    id: str,    # noqa: A002 — matches ticket schema field; builtin not used here.
    type: str,  # noqa: A002 — matches ticket schema field; builtin not used here.
    linked_task: str,
    title: str,
    body: str,
    status: str = "open",
    *,
    created_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    **extra: Any,
) -> dict:
    """Return a schema-valid ticket dict with timestamps set.

    Parameters
    ----------
    id:
        Unique ticket identifier (e.g. ``"TICKET-AUTO-T1"``).
    type:
        ``"bug"`` or ``"investigation"``.
    linked_task:
        The task id this ticket concerns.  Pass ``""`` if not linked.
    title:
        Short human-readable description.
    body:
        Full detail / knowledge text.
    status:
        One of ``TICKET_STATUSES`` (default ``"open"``).
    created_at / updated_at:
        ISO-8601 UTC strings.  Auto-set to now if omitted.
    **extra:
        Any additional fields to merge in (not validated beyond type check).

    Raises
    ------
    TicketSchemaError
        If any required field is invalid.
    """
    now = _ts()
    ticket: dict[str, Any] = {
        "id":          id,
        "type":        type,
        "status":      status,
        "linked_task": linked_task,
        "title":       title,
        "body":        body,
        "created_at":  created_at or now,
        "updated_at":  updated_at or now,
    }
    ticket.update(extra)
    _validate(ticket)
    return ticket


def _validate(ticket: dict) -> None:
    """Raise :class:`TicketSchemaError` if *ticket* violates the schema."""
    for field, expected in _REQUIRED.items():
        if field not in ticket:
            raise TicketSchemaError(f"Missing required field '{field}'")
        if not isinstance(ticket[field], expected):
            raise TicketSchemaError(
                f"Field '{field}' must be {expected.__name__}, "
                f"got {type(ticket[field]).__name__}"
            )
    if ticket["type"] not in TICKET_TYPES:
        raise TicketSchemaError(
            f"'type' must be one of {TICKET_TYPES}, got '{ticket['type']}'"
        )
    if ticket["status"] not in TICKET_STATUSES:
        raise TicketSchemaError(
            f"'status' must be one of {TICKET_STATUSES}, got '{ticket['status']}'"
        )
    if not ticket["id"].strip():
        raise TicketSchemaError("'id' must be a non-empty string")
    if not ticket["title"].strip():
        raise TicketSchemaError("'title' must be a non-empty string")


# ─────────────────────────────────────────────────────────────────────────────
# TicketStore
# ─────────────────────────────────────────────────────────────────────────────

class TicketStore:
    """Manages ticket persistence under *tickets_dir*.

    Each ticket is stored as ``<tickets_dir>/<ticket-id>.json``.  All writes
    are atomic at the JSON level (full file rewrite on every mutation).

    Parameters
    ----------
    tickets_dir:
        Path to the ``tickets/`` directory (need not exist yet; created
        on first write).
    """

    def __init__(self, tickets_dir: str | Path) -> None:
        self._dir = Path(tickets_dir)

    # ── Create ───────────────────────────────────────────────────────────────

    def create(self, ticket: dict) -> None:
        """Persist a new ticket.

        Parameters
        ----------
        ticket:
            A schema-valid ticket dict, typically produced by
            :func:`make_ticket`.

        Raises
        ------
        TicketSchemaError
            If the ticket dict is invalid.
        TicketAlreadyExists
            If a ticket with the same ``id`` already exists on disk.
        """
        _validate(ticket)
        path = self._path(ticket["id"])
        if path.exists():
            raise TicketAlreadyExists(
                f"Ticket '{ticket['id']}' already exists at {path}"
            )
        self._ensure_dir()
        self._write(path, ticket)
        logger.debug("TicketStore.create: %s", ticket["id"])

    # ── Read ─────────────────────────────────────────────────────────────────

    def get(self, ticket_id: str) -> Optional[dict]:
        """Return the ticket dict for *ticket_id*, or ``None`` if not found."""
        path = self._path(ticket_id)
        if not path.exists():
            return None
        return self._read(path)

    def list_all(self) -> list[dict]:
        """Return all tickets sorted by ``created_at`` (ascending)."""
        tickets = []
        if not self._dir.exists():
            return tickets
        for p in self._dir.glob("*.json"):
            try:
                tickets.append(self._read(p))
            except Exception as exc:  # noqa: BLE001
                logger.warning("TicketStore.list_all: skipping %s — %s", p.name, exc)
        tickets.sort(key=lambda t: t.get("created_at", ""))
        return tickets

    def list_by_status(self, status: str) -> list[dict]:
        """Return all tickets whose ``status`` matches *status*.

        Parameters
        ----------
        status:
            One of ``TICKET_STATUSES``.

        Raises
        ------
        TicketSchemaError
            If *status* is not a valid ticket status.
        """
        if status not in TICKET_STATUSES:
            raise TicketSchemaError(
                f"'status' must be one of {TICKET_STATUSES}, got '{status}'"
            )
        return [t for t in self.list_all() if t.get("status") == status]

    def list_by_type(self, ticket_type: str) -> list[dict]:
        """Return all tickets whose ``type`` matches *ticket_type*."""
        if ticket_type not in TICKET_TYPES:
            raise TicketSchemaError(
                f"'type' must be one of {TICKET_TYPES}, got '{ticket_type}'"
            )
        return [t for t in self.list_all() if t.get("type") == ticket_type]

    def list_by_task(self, task_id: str) -> list[dict]:
        """Return all tickets linked to *task_id*."""
        return [t for t in self.list_all() if t.get("linked_task") == task_id]

    def exists(self, ticket_id: str) -> bool:
        """Return ``True`` if a ticket with *ticket_id* exists on disk."""
        return self._path(ticket_id).exists()

    # ── Update ───────────────────────────────────────────────────────────────

    def update_status(self, ticket_id: str, status: str) -> None:
        """Change the status of an existing ticket.

        Raises
        ------
        TicketNotFound
            If the ticket does not exist.
        TicketSchemaError
            If *status* is not a valid ticket status.
        """
        self.update(ticket_id, status=status)

    def update_body(self, ticket_id: str, body: str) -> None:
        """Replace the body text of an existing ticket."""
        self.update(ticket_id, body=body)

    def update(self, ticket_id: str, **fields: Any) -> None:
        """Merge *fields* into an existing ticket and persist.

        ``updated_at`` is always refreshed.  ``id`` and ``created_at`` are
        immutable and silently ignored if passed.

        Raises
        ------
        TicketNotFound
            If the ticket does not exist.
        TicketSchemaError
            If the resulting ticket dict fails schema validation.
        """
        path = self._path(ticket_id)
        if not path.exists():
            raise TicketNotFound(f"Ticket '{ticket_id}' not found")

        ticket = self._read(path)
        # Protect immutable fields
        fields.pop("id", None)
        fields.pop("created_at", None)
        ticket.update(fields)
        ticket["updated_at"] = _ts()
        _validate(ticket)
        self._write(path, ticket)
        logger.debug("TicketStore.update: %s  fields=%s", ticket_id, list(fields))

    # ── Delete ───────────────────────────────────────────────────────────────

    def delete(self, ticket_id: str) -> bool:
        """Remove the ticket file.

        Returns
        -------
        bool
            ``True`` if the file existed and was deleted; ``False`` if it was
            already absent (no-op — never raises).
        """
        path = self._path(ticket_id)
        if not path.exists():
            logger.debug("TicketStore.delete: %s not found — no-op", ticket_id)
            return False
        path.unlink()
        logger.debug("TicketStore.delete: removed %s", ticket_id)
        return True

    def path(self, ticket_id: str) -> Path:
        """Return the filesystem path for *ticket_id* (file may not yet exist).

        This is the public equivalent of the private ``_path`` helper and
        provides a stable API contract for callers that need the ticket path
        (e.g. exhaustion_handler) without coupling to internal naming.
        """
        return self._path(ticket_id)

    # ── Private ──────────────────────────────────────────────────────────────

    def _path(self, ticket_id: str) -> Path:
        return self._dir / f"{ticket_id}.json"

    def _ensure_dir(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write(path: Path, ticket: dict) -> None:
        atomic_write_text(
            path,
            json.dumps(ticket, indent=2, ensure_ascii=False),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def make_ticket_store(agent_dir: str | Path) -> TicketStore:
    """Return a :class:`TicketStore` rooted at ``<agent_dir>/tickets/``.

    This is the preferred factory for the controller and ``ExhaustionHandler``.

    Parameters
    ----------
    agent_dir:
        The ``.agent/`` directory (the parent of ``tickets/``).
    """
    return TicketStore(Path(agent_dir) / "tickets")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
