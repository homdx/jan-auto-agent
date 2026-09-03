"""tests_bugfix/test_bugfix_ticket_store_update_oserror.py

TicketStore.create() wraps its _write() call in try/except OSError and
re-raises as TicketError (AUTO-T37). TicketStore.update() does NOT — a
disk-full, permission-denied, or read-only-filesystem failure from
atomic_write_text() propagates as a raw OSError past update()'s
docstring (which only lists TicketNotFound and TicketSchemaError).

This is the same asymmetry AUTO-T37 fixed for create(): the raw OSError
escapes into callers like bug_fix_loop._safe_update() that catch
(TicketError, OSError) — but update() is also called directly from
exhaustion_handler and controller code that may not, and the
inconsistency is itself the bug (create's _ensure_dir + _write are both
guarded; update's _write is not).

The test patches _write to raise OSError and asserts update() wraps it
as TicketError, matching create()'s established contract.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.ticket_store import (
    TicketError,
    TicketStore,
    make_ticket,
    make_ticket_store,
)


def _valid_ticket(**overrides):
    defaults = dict(
        id="BUG-AUTO-T39",
        type="bug",
        linked_task="AUTO-T39",
        title="Test ticket",
        body="body text",
        status="open",
    )
    defaults.update(overrides)
    return make_ticket(**defaults)


class TestUpdateConvertsOSError:
    def test_oserror_from_write_becomes_ticket_error(self, tmp_path):
        """update() should wrap _write() OSError as TicketError, matching
        create()'s contract."""
        store = make_ticket_store(tmp_path / ".agent")
        store.create(_valid_ticket())
        with patch.object(store, "_write", side_effect=OSError("No space left on device")):
            with pytest.raises(TicketError) as exc_info:
                store.update("BUG-AUTO-T39", status="in-progress")
        assert "No space left" in str(exc_info.value) or "write" in str(exc_info.value).lower()
        assert isinstance(exc_info.value.__cause__, OSError)

    def test_ticket_error_chains_original_oserror(self, tmp_path):
        """__cause__ must be the original OSError for the full traceback."""
        store = make_ticket_store(tmp_path / ".agent")
        store.create(_valid_ticket())
        original = OSError("Permission denied")
        with patch.object(store, "_write", side_effect=original):
            with pytest.raises(TicketError) as exc_info:
                store.update("BUG-AUTO-T39", body="new body")
        assert exc_info.value.__cause__ is original

    def test_successful_update_still_works(self, tmp_path):
        """Sanity: the happy path must not be broken by the fix."""
        store = make_ticket_store(tmp_path / ".agent")
        store.create(_valid_ticket())
        store.update("BUG-AUTO-T39", status="in-progress")
        ticket = store.get("BUG-AUTO-T39")
        assert ticket is not None
        assert ticket["status"] == "in-progress"

    def test_update_body_still_works(self, tmp_path):
        """Sanity: update_body (which calls update) must still work."""
        store = make_ticket_store(tmp_path / ".agent")
        store.create(_valid_ticket())
        store.update_body("BUG-AUTO-T39", "updated body text")
        assert store.get("BUG-AUTO-T39")["body"] == "updated body text"
