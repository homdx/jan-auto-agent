"""tests/test_ticket_store_corrupt.py — one bad ticket must not kill the run.

TicketStore.list_all() already treats an unreadable ticket as a thing to skip
and warn about, not a reason to fail.  get() called _read() bare, so the very
same file that list_all() shrugs off killed the run through get():

    list_all() with a corrupt ticket: 0 tickets (skipped gracefully)
    get(): JSONDecodeError ...
    handle_regression: RAISED JSONDecodeError

That path matters because BugFixLoop's status gate calls get() on every
regression, so a single damaged ticket file took down the whole run from
inside _check_regressions — the same read/write asymmetry as update()
raising TicketNotFound, which had to be guarded for exactly this reason.

_read() also returned whatever json.loads produced, so a file that is valid
JSON of the wrong shape passed through and failed later on .get().

Unusable files are quarantined rather than deleted or overwritten, so a caller
that goes on to open a fresh ticket with the same id cannot destroy the
evidence of what went wrong.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.ticket_store import make_ticket, make_ticket_store


def _store(tmp_path: Path):
    store = make_ticket_store(tmp_path / ".agent")
    (tmp_path / ".agent" / "tickets").mkdir(parents=True, exist_ok=True)
    return store


def _plant(tmp_path: Path, ticket_id: str, content: str) -> Path:
    path = tmp_path / ".agent" / "tickets" / f"{ticket_id}.json"
    path.write_text(content, encoding="utf-8")
    return path


def _quarantined(tmp_path: Path) -> list[str]:
    return [p.name for p in (tmp_path / ".agent" / "tickets").iterdir()
            if ".corrupt-" in p.name]


class TestGetToleratesUnusableTickets:
    @pytest.mark.parametrize("content,label", [
        ("{ truncated mid-write", "corrupt json"),
        ("", "empty file"),
        ("[]", "valid json, list"),
        ('"a string"', "valid json, string"),
        ("null", "valid json, null"),
    ])
    def test_get_returns_none_instead_of_raising(self, tmp_path, content, label):
        store = _store(tmp_path)
        _plant(tmp_path, "BUG-AUTO-T1", content)
        assert store.get("BUG-AUTO-T1") is None, label

    def test_unusable_file_is_quarantined_not_deleted(self, tmp_path):
        store = _store(tmp_path)
        _plant(tmp_path, "BUG-AUTO-T1", "{ truncated")
        store.get("BUG-AUTO-T1")
        assert len(_quarantined(tmp_path)) == 1

    def test_quarantine_preserves_the_original_bytes(self, tmp_path):
        store = _store(tmp_path)
        _plant(tmp_path, "BUG-AUTO-T1", "{ truncated evidence")
        store.get("BUG-AUTO-T1")
        kept = (tmp_path / ".agent" / "tickets") / _quarantined(tmp_path)[0]
        assert kept.read_text(encoding="utf-8") == "{ truncated evidence"

    def test_fresh_ticket_can_be_created_afterwards(self, tmp_path):
        """Quarantine moves the id out of the way so recovery can proceed."""
        store = _store(tmp_path)
        _plant(tmp_path, "BUG-AUTO-T1", "{ truncated")
        assert store.get("BUG-AUTO-T1") is None
        store.create(make_ticket(
            id="BUG-AUTO-T1", type="bug", linked_task="AUTO-T1",
            title="t", body="b", status="open",
        ))
        assert store.get("BUG-AUTO-T1")["status"] == "open"

    def test_missing_ticket_still_returns_none_without_quarantine(self, tmp_path):
        store = _store(tmp_path)
        assert store.get("BUG-NOPE") is None
        assert _quarantined(tmp_path) == []

    def test_valid_ticket_is_unaffected(self, tmp_path):
        store = _store(tmp_path)
        store.create(make_ticket(
            id="BUG-AUTO-T1", type="bug", linked_task="AUTO-T1",
            title="t", body="b", status="deferred",
        ))
        assert store.get("BUG-AUTO-T1")["status"] == "deferred"
        assert _quarantined(tmp_path) == []


class TestRegressionPathSurvives:
    def test_handle_regression_survives_a_corrupt_ticket(self, tmp_path):
        """The actual failure: a dead run from inside _check_regressions."""
        from dataclasses import dataclass, field
        from unittest.mock import MagicMock
        from tools.auto.bug_fix_loop import BugFixLoop
        from tools.auto.state import StateStore

        @dataclass
        class ER:
            passed: bool = False
            exit_code: int = 4
            stdout: str = "F"
            stderr: str = ""
            traceback: str = ""
            timed_out: bool = False

        @dataclass
        class OR:
            task_id: str = "X"
            passed: bool = True
            exhausted: bool = False
            rounds_used: int = 1
            feedback_files: list = field(default_factory=list)

            def knowledge(self) -> str:
                return "k"

        state = StateStore(tmp_path / ".agent")
        state.initialise("g", tmp_path)
        store = _store(tmp_path)
        _plant(tmp_path, "BUG-AUTO-T1", "{ truncated mid-write")

        outer = MagicMock()
        outer.run_task.return_value = OR()
        result = BugFixLoop(outer, MagicMock(), store, state).handle_regression(
            {"id": "AUTO-T1", "title": "t", "instruction": "i",
             "target_files": ["a.py"], "acceptance_check": "pytest -q"},
            ER(), base_dir=tmp_path,
        )
        assert result is not None
        assert len(_quarantined(tmp_path)) == 1


class TestQuarantineSameSecondCollision:
    """Two quarantines of the SAME ticket id within the same wall-clock
    second must not collide on one destination path.

    The stamp is second-resolution, so path.with_suffix(f".json.corrupt-
    {stamp}") is identical for two calls in the same second — and
    Path.rename() silently replaces an existing destination on POSIX, so
    the second quarantine call overwrote the first's evidence with no
    error raised anywhere.  Reproducible with two ordinary back-to-back
    calls, no clock freezing required (confirmed separately); the frozen
    clock here just makes the collision deterministic for the test.

    Same shape as the previously-found (and, for the sibling case in
    bug_fix_loop.py's _clear_stale_fix_rounds, deliberately not fixed —
    judged not worth the added complexity) same-second timestamp
    collision.  Fixed here with the identical numeric-suffix
    disambiguation technique.
    """

    def test_two_quarantines_same_second_both_survive(self, tmp_path, monkeypatch):
        import tools.auto.ticket_store as ticket_store_module
        from datetime import datetime as real_datetime

        class _Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 7, 26, 12, 0, 0)

        monkeypatch.setattr(ticket_store_module, "datetime", _Frozen)

        store = _store(tmp_path)
        path = _plant(tmp_path, "BUG-AUTO-T1", "CORRUPT-A")
        store._quarantine(path, "first corruption")

        # A fresh file recreated at the same id, itself found corrupt
        # within the same frozen second.
        path = _plant(tmp_path, "BUG-AUTO-T1", "CORRUPT-B")
        store._quarantine(path, "second corruption")

        quarantined = _quarantined(tmp_path)
        assert len(quarantined) == 2, (
            f"expected two distinct quarantine files, got {len(quarantined)}: "
            f"{quarantined}"
        )

    def test_both_files_content_preserved(self, tmp_path, monkeypatch):
        """Not just two files — the ACTUAL content of each must survive."""
        import tools.auto.ticket_store as ticket_store_module
        from datetime import datetime as real_datetime

        class _Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 7, 26, 12, 0, 0)

        monkeypatch.setattr(ticket_store_module, "datetime", _Frozen)

        store = _store(tmp_path)
        path = _plant(tmp_path, "BUG-AUTO-T1", "CORRUPT-A")
        store._quarantine(path, "first corruption")
        path = _plant(tmp_path, "BUG-AUTO-T1", "CORRUPT-B")
        store._quarantine(path, "second corruption")

        contents = {
            p.read_text(encoding="utf-8")
            for p in (tmp_path / ".agent" / "tickets").glob("*.corrupt-*")
        }
        assert contents == {"CORRUPT-A", "CORRUPT-B"}

    def test_third_collision_in_the_same_second_also_survives(self, tmp_path, monkeypatch):
        import tools.auto.ticket_store as ticket_store_module
        from datetime import datetime as real_datetime

        class _Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 7, 26, 12, 0, 0)

        monkeypatch.setattr(ticket_store_module, "datetime", _Frozen)

        store = _store(tmp_path)
        for i in range(3):
            path = _plant(tmp_path, "BUG-AUTO-T1", f"CORRUPT-{i}")
            store._quarantine(path, f"corruption {i}")

        assert len(_quarantined(tmp_path)) == 3

    def test_different_ticket_ids_never_collide_regardless_of_clock(self, tmp_path, monkeypatch):
        """Sanity check: the fix must not change behaviour for the common
        case of two DIFFERENT tickets quarantined in the same second."""
        import tools.auto.ticket_store as ticket_store_module
        from datetime import datetime as real_datetime

        class _Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 7, 26, 12, 0, 0)

        monkeypatch.setattr(ticket_store_module, "datetime", _Frozen)

        store = _store(tmp_path)
        store._quarantine(_plant(tmp_path, "BUG-AUTO-T1", "A"), "r1")
        store._quarantine(_plant(tmp_path, "BUG-AUTO-T2", "B"), "r2")

        names = _quarantined(tmp_path)
        assert len(names) == 2
        assert any(n.startswith("BUG-AUTO-T1") for n in names)
        assert any(n.startswith("BUG-AUTO-T2") for n in names)
        # neither should have needed the numeric disambiguation suffix
        assert not any("-001" in n for n in names)
