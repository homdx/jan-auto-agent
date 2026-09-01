"""tests/test_t37_ticket_store_oserror.py — AUTO-T37 regression.

Before the fix, TicketStore.create() called _ensure_dir() bare.  If mkdir()
raised OSError (permission denied, read-only filesystem, etc.) the exception
propagated uncaught through create() into bug_fix_loop.handle_regression(),
whose only guard was `except TicketAlreadyExists`.  The OSError then escaped
into the controller's per-task loop, crashing the whole --auto run immediately
after a successful commit.

After the fix:
  1. TicketStore.create() wraps _ensure_dir() in try/except OSError and
     re-raises as TicketError (a domain exception with a clear message).
  2. bug_fix_loop.handle_regression() also catches TicketError so the
     run degrades to "no ticket record" rather than aborting.

The tests here confirm both layers of the fix.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ── helpers ─────────────────────────────────────────────────────────────────


def _valid_ticket(**overrides):
    defaults = dict(
        id="BUG-AUTO-T37",
        type="bug",
        linked_task="AUTO-T37",
        title="Test ticket",
        body="body text",
        status="open",
    )
    defaults.update(overrides)
    return make_ticket(**defaults)


# ── Layer 1: TicketStore.create wraps OSError ────────────────────────────────


class TestCreateConvertsOSError:
    def test_oserror_from_ensure_dir_becomes_ticket_error(self, tmp_path):
        store = make_ticket_store(tmp_path / ".agent")
        ticket = _valid_ticket()
        with patch.object(store, "_ensure_dir", side_effect=OSError("Permission denied")):
            with pytest.raises(TicketError) as exc_info:
                store.create(ticket)
        # The TicketError message must mention the directory so the operator
        # can tell at a glance what went wrong.
        assert "ticket directory" in str(exc_info.value).lower() or "permission" in str(exc_info.value).lower()

    def test_ticket_error_chains_original_oserror(self, tmp_path):
        """__cause__ must be the original OSError so the full traceback is preserved."""
        store = make_ticket_store(tmp_path / ".agent")
        original = OSError("No space left on device")
        with patch.object(store, "_ensure_dir", side_effect=original):
            with pytest.raises(TicketError) as exc_info:
                store.create(_valid_ticket())
        assert exc_info.value.__cause__ is original

    def test_permission_denied_does_not_write_file(self, tmp_path):
        store = make_ticket_store(tmp_path / ".agent")
        with patch.object(store, "_ensure_dir", side_effect=OSError("Permission denied")):
            with pytest.raises(TicketError):
                store.create(_valid_ticket())
        # The tickets directory was never created and no file was written.
        tickets_dir = tmp_path / ".agent" / "tickets"
        assert not tickets_dir.exists()

    def test_successful_create_still_works(self, tmp_path):
        """Sanity: the happy path must not be broken by the fix."""
        store = make_ticket_store(tmp_path / ".agent")
        store.create(_valid_ticket())
        assert store.get("BUG-AUTO-T37") is not None

    def test_ticket_already_exists_still_raised(self, tmp_path):
        """TicketAlreadyExists (not OSError) must still propagate unchanged."""
        from tools.auto.ticket_store import TicketAlreadyExists
        store = make_ticket_store(tmp_path / ".agent")
        store.create(_valid_ticket())
        with pytest.raises(TicketAlreadyExists):
            store.create(_valid_ticket())


class TestExhaustionHandlerStillDegradesGracefully:
    """AUTO-T37 review finding: exhaustion_handler.py's _open_ticket() had
    its own pre-existing `except OSError` guard around ts.create(). Wrapping
    the OSError as TicketError inside TicketStore.create() means that guard
    would silently stop catching anything — a real regression the fix would
    otherwise have introduced. exhaustion_handler.py now also catches
    TicketError; this test proves the guard still holds.
    """

    def test_open_ticket_survives_oserror_after_wrap(self, tmp_path, monkeypatch):
        from tools.auto.exhaustion_handler import ExhaustionHandler
        from tools.auto.state import StateStore

        state = StateStore(tmp_path / ".agent")
        state.initialise("goal", tmp_path)
        handler = ExhaustionHandler(state)

        import tools.auto.ticket_store as ts_module
        monkeypatch.setattr(
            ts_module.TicketStore, "_ensure_dir",
            lambda self: (_ for _ in ()).throw(OSError("Permission denied")),
        )

        # Must not raise — the path is still returned even though the
        # underlying file write failed.
        path = handler._open_ticket(
            ticket_id="TICKET-AUTO-T37",
            linked_task="AUTO-T37",
            title="t",
            body="b",
        )
        assert path.name == "TICKET-AUTO-T37.json"
        assert not path.exists()





@dataclass
class _ExecResult:
    passed: bool = False
    exit_code: int = 1
    stdout: str = "FAIL"
    stderr: str = ""
    traceback: str = ""
    timed_out: bool = False


@dataclass
class _OuterResult:
    task_id: str = "FIX-AUTO-T37"
    passed: bool = True
    exhausted: bool = False
    rounds_used: int = 1
    feedback_files: list = field(default_factory=list)

    def knowledge(self) -> str:
        return "k"


class TestHandleRegressionToleratesTicketError:
    def test_oserror_does_not_crash_run(self, tmp_path):
        """The whole point of AUTO-T37: an OSError must not kill the --auto run."""
        from tools.auto.bug_fix_loop import BugFixLoop
        from tools.auto.state import StateStore

        state = StateStore(tmp_path / ".agent")
        state.initialise("goal", tmp_path)

        store = make_ticket_store(tmp_path / ".agent")
        # Simulate a read-only tickets directory: _ensure_dir raises OSError.
        with patch.object(store, "_ensure_dir", side_effect=OSError("Permission denied")):
            outer = MagicMock()
            outer.run_task.return_value = _OuterResult()
            result = BugFixLoop(outer, MagicMock(), store, state).handle_regression(
                {
                    "id": "AUTO-T37",
                    "title": "title",
                    "instruction": "instr",
                    "target_files": ["x.py"],
                    "acceptance_check": "pytest -q",
                },
                _ExecResult(),
                base_dir=tmp_path,
            )
        # Must return a result object, not raise.
        assert result is not None

    def test_no_ticket_file_written_on_oserror(self, tmp_path):
        """No ticket file should appear when create failed."""
        from tools.auto.bug_fix_loop import BugFixLoop
        from tools.auto.state import StateStore

        state = StateStore(tmp_path / ".agent")
        state.initialise("goal", tmp_path)
        store = make_ticket_store(tmp_path / ".agent")

        with patch.object(store, "_ensure_dir", side_effect=OSError("Read-only FS")):
            outer = MagicMock()
            outer.run_task.return_value = _OuterResult()
            BugFixLoop(outer, MagicMock(), store, state).handle_regression(
                {
                    "id": "AUTO-T37",
                    "title": "title",
                    "instruction": "instr",
                    "target_files": ["x.py"],
                    "acceptance_check": "pytest -q",
                },
                _ExecResult(),
                base_dir=tmp_path,
            )

        tickets_dir = tmp_path / ".agent" / "tickets"
        ticket_files = list(tickets_dir.glob("*.json")) if tickets_dir.exists() else []
        assert ticket_files == []
