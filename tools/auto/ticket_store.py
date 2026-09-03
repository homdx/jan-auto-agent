"""tools/auto/ticket_store.py — AUTO-D1: ticket store (CRUD helpers).

Owns all I/O under ``.agent/tickets/``.  Each ticket is a single JSON file:

    .agent/tickets/<ticket-id>.json

Ticket schema
-------------
    id          str  — unique ticket identifier, e.g. "TICKET-AUTO-T1"
    type        str  — "bug" | "investigation"
    status      str  — "open" | "in-progress" | "fixed" | "deferred"
                       | "verification-failed"
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
from datetime import datetime
import logging
from tools.auto.utils import _ts, atomic_write_text, safe_filename_component
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Valid field values ────────────────────────────────────────────────────────

TICKET_TYPES   = {"bug", "investigation"}
# "verification-failed" is terminal-but-loud: a fix was recorded as passing and
# committed, yet the acceptance check fails again on a later re-check. The
# recorded fix did not hold, so neither "fixed" (a lie — the run would report
# green over a broken check) nor "deferred" (implies the agent gave up while
# trying, not that it wrongly believed it had succeeded) describes it. An
# operator resets it to "open" to allow another attempt.
TICKET_STATUSES = {"open", "in-progress", "fixed", "deferred",
                   "verification-failed"}

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
        # AUTO-T37 FIX: _ensure_dir() (mkdir) can raise OSError on permission
        # errors or read-only file systems.  Convert to TicketError so callers
        # get a domain exception rather than a raw OS error — and can catch it
        # alongside TicketAlreadyExists without also swallowing unrelated errors.
        try:
            self._ensure_dir()
        except OSError as exc:
            raise TicketError(
                f"Could not create ticket directory '{self._dir}': {exc}"
            ) from exc
        # BUGFIX (audit): _write() itself was unguarded here — a disk-full
        # or permission error raised a raw OSError past this method's own
        # documented Raises: (TicketSchemaError, TicketAlreadyExists),
        # inconsistent with the _ensure_dir() conversion right above it.
        try:
            self._write(path, ticket)
        except OSError as exc:
            raise TicketError(
                f"Could not write ticket '{ticket['id']}' to {path}: {exc}"
            ) from exc
        logger.debug("TicketStore.create: %s", ticket["id"])

    # ── Read ─────────────────────────────────────────────────────────────────

    def get(self, ticket_id: str) -> Optional[dict]:
        """Return the ticket dict for *ticket_id*, or ``None`` if unusable.

        Unreadable files are quarantined rather than raised.  ``list_all()``
        already treats one bad ticket as a thing to skip and warn about, not a
        reason to fail — but ``get()`` called ``_read()`` bare, so the same
        file that ``list_all()`` shrugs off killed the run through ``get()``:

            list_all() with a corrupt ticket: 0 tickets (skipped gracefully)
            get(): JSONDecodeError ...
            handle_regression: RAISED JSONDecodeError

        That path matters because BugFixLoop's status gate calls ``get()`` on
        every regression, so a single damaged ticket file took down the whole
        run from inside ``_check_regressions`` — the same asymmetry as the
        write path, where ``update()`` raising ``TicketNotFound`` had to be
        guarded for exactly this reason.

        ``_read()`` also returned whatever ``json.loads`` produced, so a file
        that is valid JSON of the wrong shape (a list, a string) passed
        through and failed later on ``.get()``.  Both cases are handled here.

        The bad file is renamed aside instead of being overwritten, so a
        caller that goes on to open a fresh ticket cannot destroy the evidence
        of what went wrong.
        """
        path = self._path(ticket_id)
        if not path.exists():
            return None
        try:
            ticket = self._read(path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            self._quarantine(path, str(exc))
            return None
        if not isinstance(ticket, dict):
            self._quarantine(
                path, f"expected a JSON object, got {type(ticket).__name__}"
            )
            return None
        # AUTO-FIX (medium-priority audit, DeepSeek-plan finding): a dict
        # missing required fields (id/type/status/linked_task — the same
        # schema make_ticket() always produces) used to pass through
        # untouched. Callers that trust get()'s return shape (e.g.
        # indexing ticket["status"] directly) would then crash far from
        # here with no indication the ticket file itself was incomplete.
        # Quarantine it the same way an unreadable or wrong-shape file
        # already is, rather than handing back a ticket-shaped dict that
        # isn't actually one.
        _required_ticket_keys = {"id", "type", "status", "linked_task"}
        _missing = _required_ticket_keys - ticket.keys()
        if _missing:
            self._quarantine(
                path, f"missing required field(s) {sorted(_missing)}"
            )
            return None
        return ticket

    def _quarantine(self, path: Path, reason: str) -> None:
        """Move an unusable ticket aside and log why.

        Renaming rather than deleting keeps the damaged file for inspection;
        renaming rather than leaving it in place means a caller that decides
        to open a fresh ticket with the same id will not silently overwrite
        the only evidence of the problem.

        The stamp is second-resolution, so two quarantines of the SAME
        ticket id within the same wall-clock second would otherwise collide
        on one destination path — the second rename landing on the first
        silently overwrites it, since Path.rename() replaces an existing
        destination on POSIX with no error.  Low severity in practice (both
        files are already-discarded corrupt tickets; nothing downstream
        distinguishes one quarantine copy from two), but reproducible with
        two ordinary back-to-back calls, no mocking required.  A numeric
        suffix disambiguates so neither call's evidence is destroyed.
        """
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        dest  = path.with_suffix(f".json.corrupt-{stamp}")
        suffix_n = 0
        while dest.exists():
            suffix_n += 1
            dest = path.with_suffix(f".json.corrupt-{stamp}-{suffix_n:03d}")
        try:
            path.rename(dest)
            logger.warning(
                "TicketStore: %s is unusable (%s) — quarantined as %s; "
                "treating the ticket as absent",
                path.name, reason, dest.name,
            )
        except OSError as exc:
            logger.warning(
                "TicketStore: %s is unusable (%s) and could not be "
                "quarantined (%s) — treating the ticket as absent",
                path.name, reason, exc,
            )

    def list_all(self) -> list[dict]:
        """Return all tickets sorted by ``created_at`` (ascending)."""
        tickets = []
        if not self._dir.exists():
            return tickets
        for p in self._dir.glob("*.json"):
            try:
                ticket = self._read(p)
            except Exception as exc:  # noqa: BLE001
                logger.warning("TicketStore.list_all: skipping %s — %s", p.name, exc)
                continue
            # AUTO-FIX: _read() only does json.loads — a file that is
            # valid JSON of the wrong shape (a list, a string) used to
            # pass this try/except (no exception raised) and get appended
            # as-is, then blow up AttributeError on the .sort() below,
            # which every non-dict entry reaches regardless of its own
            # position in the glob. get() already treats this exact case
            # ("expected a JSON object, got <type>") as unusable; skip it
            # here too instead of contradicting get()'s quarantine design.
            if not isinstance(ticket, dict):
                logger.warning(
                    "TicketStore.list_all: skipping %s — expected a JSON "
                    "object, got %s",
                    p.name, type(ticket).__name__,
                )
                continue
            tickets.append(ticket)
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

        try:
            ticket = self._read(path)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            self._quarantine(path, str(exc))
            raise TicketSchemaError(
                f"Ticket '{ticket_id}' is corrupt ({exc}) — quarantined"
            ) from exc
        # AUTO-FIX: a file that is valid JSON of the wrong shape (a list,
        # a string) used to reach ticket.update(fields) below and raise a
        # raw AttributeError. get() already quarantines this exact case;
        # do the same here and raise the TicketSchemaError this method's
        # own docstring already documents, instead of an undocumented
        # AttributeError leaking out of an internal implementation detail.
        if not isinstance(ticket, dict):
            self._quarantine(
                path, f"expected a JSON object, got {type(ticket).__name__}"
            )
            raise TicketSchemaError(
                f"Ticket '{ticket_id}' is corrupt (expected a JSON object, "
                f"got {type(ticket).__name__}) — quarantined"
            )
        # Protect immutable fields
        fields.pop("id", None)
        fields.pop("created_at", None)
        ticket.update(fields)
        ticket["updated_at"] = _ts()
        _validate(ticket)
        # BUGFIX: create() wraps _write() in try/except OSError and re-raises
        # as TicketError (AUTO-T37). update() did not — a disk-full,
        # permission-denied, or read-only-filesystem failure from
        # atomic_write_text() propagated as a raw OSError past this method's
        # own docstring (which only lists TicketNotFound and
        # TicketSchemaError), inconsistent with create()'s established contract.
        try:
            self._write(path, ticket)
        except OSError as exc:
            raise TicketError(
                f"Could not write ticket '{ticket_id}' to {path}: {exc}"
            ) from exc
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
        # Bugfix: ticket ids are typically system-generated (e.g.
        # "TICKET-AUTO-T1", "BUG-FIX-AUTO-T1") so this was not previously
        # exploitable, but unlike state.py's task directories (which have
        # always sanitized task_id via _safe_task_id) this path was built
        # straight from the raw id with no defense if that ever changes —
        # a ticket id containing ".." or "/" could escape self._dir.
        return self._dir / f"{safe_filename_component(ticket_id)}.json"

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
