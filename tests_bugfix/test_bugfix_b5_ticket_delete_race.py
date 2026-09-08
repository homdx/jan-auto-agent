"""B5 -- ``TicketStore.delete`` had a TOCTOU pair on its "never raises" path.

The old body was ``if not path.exists(): return False`` followed by
``path.unlink()``. Those are two syscalls: a concurrent cleanup can remove
the file in between, and the unlink then raised ``FileNotFoundError`` out of
a method whose docstring promises a silent no-op.

Note the shape of the fix. Keeping the ``exists()`` check and softening the
unlink to ``missing_ok=True`` stops the exception but still returns ``True``
for a file this call did not remove -- half the contract, quietly wrong.
Dropping the check and letting the unlink be the single source of truth
keeps both halves honest, which is what
``test_lost_race_reports_false_not_true`` pins.

Without the fix ``test_lost_race_does_not_raise`` errors with
FileNotFoundError; with the ``missing_ok`` variant
``test_lost_race_reports_false_not_true`` fails instead.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.ticket_store import TicketStore  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> TicketStore:
    return TicketStore(tmp_path / "tickets")


def _make_ticket(store: TicketStore, ticket_id: str) -> Path:
    path = Path(store._path(ticket_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


class TestOrdinaryBehaviour:
    def test_existing_ticket_is_removed_and_returns_true(
        self, store: TicketStore
    ) -> None:
        path = _make_ticket(store, "T-1")
        assert store.delete("T-1") is True
        assert not path.exists()

    def test_absent_ticket_returns_false(self, store: TicketStore) -> None:
        assert store.delete("T-NOPE") is False


class TestLostRace:
    def test_lost_race_does_not_raise(self, store: TicketStore) -> None:
        _make_ticket(store, "T-1")
        with patch.object(
            Path, "unlink", side_effect=FileNotFoundError("vanished")
        ):
            store.delete("T-1")  # must not raise

    def test_lost_race_reports_false_not_true(self, store: TicketStore) -> None:
        """A call that removed nothing must not claim it removed something."""
        _make_ticket(store, "T-1")
        with patch.object(
            Path, "unlink", side_effect=FileNotFoundError("vanished")
        ):
            assert store.delete("T-1") is False


class TestOtherErrorsStillPropagate:
    def test_permission_error_is_not_swallowed(self, store: TicketStore) -> None:
        _make_ticket(store, "T-1")
        with patch.object(Path, "unlink", side_effect=PermissionError("read-only")):
            with pytest.raises(PermissionError):
                store.delete("T-1")
