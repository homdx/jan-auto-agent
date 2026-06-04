"""tests/test_auto_d1.py — AUTO-D1: ticket store CRUD.

ACs (from the Jira story):
  * Tickets persist and survive resume (files remain on disk across instances).
  * CRUD helpers:
      - create  — writes ticket JSON; raises TicketAlreadyExists on duplicate.
      - get     — returns dict or None.
      - list_all — returns all tickets sorted by created_at.
      - list_by_status — filters by status; raises TicketSchemaError on bad status.
      - list_by_type  — filters by type.
      - list_by_task  — filters by linked_task.
      - exists  — bool existence check.
      - update_status — changes status; raises TicketNotFound if absent.
      - update_body   — replaces body; raises TicketNotFound if absent.
      - update        — multi-field; id and created_at are immutable.
      - delete        — removes file; no-op (returns False) if absent.
  * Schema validated on create and update (TicketSchemaError).
  * make_ticket convenience constructor fills timestamps and validates.
  * make_ticket_store factory roots store under <agent_dir>/tickets/.
  * ExhaustionHandler uses TicketStore (integration smoke-test).
"""

import json
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.ticket_store import (
    TicketStore,
    TicketAlreadyExists,
    TicketNotFound,
    TicketSchemaError,
    TICKET_TYPES,
    TICKET_STATUSES,
    make_ticket,
    make_ticket_store,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _store(tmp_path: Path) -> TicketStore:
    d = tmp_path / "tickets"
    d.mkdir()
    return TicketStore(d)


def _bug(id="TICKET-T1", status="open", linked_task="AUTO-T1") -> dict:
    return make_ticket(
        id=id,
        type="bug",
        linked_task=linked_task,
        title="Something broke",
        body="It really broke.",
        status=status,
    )


def _inv(id="TICKET-T2", linked_task="AUTO-T2") -> dict:
    return make_ticket(
        id=id,
        type="investigation",
        linked_task=linked_task,
        title="Deferred: investigate X",
        body="Rounds 1-10 all timed out.",
    )


# ── make_ticket ───────────────────────────────────────────────────────────────

class TestMakeTicket:
    def test_returns_valid_dict(self):
        t = _bug()
        assert t["id"] == "TICKET-T1"
        assert t["type"] == "bug"
        assert t["status"] == "open"
        assert "created_at" in t and t["created_at"]
        assert "updated_at" in t and t["updated_at"]

    def test_default_status_is_open(self):
        t = make_ticket(id="X", type="bug", linked_task="", title="T", body="B")
        assert t["status"] == "open"

    def test_custom_timestamps_respected(self):
        t = make_ticket(id="X", type="bug", linked_task="", title="T", body="B",
                        created_at="2024-01-01T00:00:00Z",
                        updated_at="2024-06-01T00:00:00Z")
        assert t["created_at"] == "2024-01-01T00:00:00Z"
        assert t["updated_at"] == "2024-06-01T00:00:00Z"

    def test_extra_kwargs_merged(self):
        t = make_ticket(id="X", type="bug", linked_task="", title="T", body="B",
                        custom_field="hello")
        assert t["custom_field"] == "hello"

    def test_raises_on_bad_type(self):
        with pytest.raises(TicketSchemaError):
            make_ticket(id="X", type="nonsense", linked_task="", title="T", body="B")

    def test_raises_on_bad_status(self):
        with pytest.raises(TicketSchemaError):
            make_ticket(id="X", type="bug", linked_task="", title="T", body="B",
                        status="pending")

    def test_raises_on_empty_id(self):
        with pytest.raises(TicketSchemaError):
            make_ticket(id="", type="bug", linked_task="", title="T", body="B")

    def test_raises_on_empty_title(self):
        with pytest.raises(TicketSchemaError):
            make_ticket(id="X", type="bug", linked_task="", title="", body="B")


# ── create ────────────────────────────────────────────────────────────────────

class TestCreate:
    def test_file_written(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        assert (tmp_path / "tickets" / "TICKET-T1.json").exists()

    def test_file_is_valid_json(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        data = json.loads((tmp_path / "tickets" / "TICKET-T1.json").read_text())
        assert data["id"] == "TICKET-T1"

    def test_raises_on_duplicate(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        with pytest.raises(TicketAlreadyExists):
            ts.create(_bug())

    def test_raises_schema_error_on_invalid_ticket(self, tmp_path):
        ts = _store(tmp_path)
        with pytest.raises(TicketSchemaError):
            ts.create({"id": "X"})   # missing required fields

    def test_creates_tickets_dir_if_absent(self, tmp_path):
        ts = TicketStore(tmp_path / "nonexistent" / "tickets")
        ts.create(_bug())
        assert (tmp_path / "nonexistent" / "tickets" / "TICKET-T1.json").exists()


# ── get ───────────────────────────────────────────────────────────────────────

class TestGet:
    def test_returns_dict(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        t = ts.get("TICKET-T1")
        assert isinstance(t, dict) and t["id"] == "TICKET-T1"

    def test_returns_none_for_missing(self, tmp_path):
        ts = _store(tmp_path)
        assert ts.get("TICKET-NOPE") is None

    def test_roundtrips_all_fields(self, tmp_path):
        ts = _store(tmp_path)
        original = _bug()
        ts.create(original)
        loaded = ts.get("TICKET-T1")
        for key in original:
            assert loaded[key] == original[key]


# ── exists ────────────────────────────────────────────────────────────────────

class TestExists:
    def test_true_after_create(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        assert ts.exists("TICKET-T1") is True

    def test_false_when_absent(self, tmp_path):
        ts = _store(tmp_path)
        assert ts.exists("TICKET-NOPE") is False

    def test_false_after_delete(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        ts.delete("TICKET-T1")
        assert ts.exists("TICKET-T1") is False


# ── list_all ──────────────────────────────────────────────────────────────────

class TestListAll:
    def test_empty_when_no_tickets(self, tmp_path):
        assert _store(tmp_path).list_all() == []

    def test_empty_when_dir_absent(self, tmp_path):
        ts = TicketStore(tmp_path / "no_dir")
        assert ts.list_all() == []

    def test_returns_all_tickets(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug("TICKET-T1"))
        ts.create(_inv("TICKET-T2"))
        all_ = ts.list_all()
        assert len(all_) == 2
        ids = {t["id"] for t in all_}
        assert ids == {"TICKET-T1", "TICKET-T2"}

    def test_sorted_by_created_at(self, tmp_path):
        ts = _store(tmp_path)
        t1 = make_ticket(id="TICKET-A", type="bug", linked_task="", title="A",
                         body="", created_at="2024-01-01T00:00:00Z",
                         updated_at="2024-01-01T00:00:00Z")
        t2 = make_ticket(id="TICKET-B", type="bug", linked_task="", title="B",
                         body="", created_at="2024-06-01T00:00:00Z",
                         updated_at="2024-06-01T00:00:00Z")
        ts.create(t2)
        ts.create(t1)
        result = ts.list_all()
        assert result[0]["id"] == "TICKET-A"
        assert result[1]["id"] == "TICKET-B"


# ── list_by_status ────────────────────────────────────────────────────────────

class TestListByStatus:
    def test_filters_correctly(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug("TICKET-T1", status="open"))
        ts.create(_bug("TICKET-T2", status="fixed"))
        ts.create(_bug("TICKET-T3", status="open"))
        open_ = ts.list_by_status("open")
        assert len(open_) == 2
        assert all(t["status"] == "open" for t in open_)

    def test_empty_list_when_no_match(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug("TICKET-T1", status="open"))
        assert ts.list_by_status("deferred") == []

    def test_raises_on_invalid_status(self, tmp_path):
        with pytest.raises(TicketSchemaError):
            _store(tmp_path).list_by_status("unknown")


# ── list_by_type ──────────────────────────────────────────────────────────────

class TestListByType:
    def test_filters_by_type(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug("TICKET-T1"))
        ts.create(_inv("TICKET-T2"))
        bugs = ts.list_by_type("bug")
        assert len(bugs) == 1 and bugs[0]["id"] == "TICKET-T1"

    def test_raises_on_invalid_type(self, tmp_path):
        with pytest.raises(TicketSchemaError):
            _store(tmp_path).list_by_type("junk")


# ── list_by_task ──────────────────────────────────────────────────────────────

class TestListByTask:
    def test_filters_by_linked_task(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug("TICKET-T1", linked_task="AUTO-T1"))
        ts.create(_bug("TICKET-T2", linked_task="AUTO-T2"))
        ts.create(_bug("TICKET-T3", linked_task="AUTO-T1"))
        result = ts.list_by_task("AUTO-T1")
        assert len(result) == 2
        assert all(t["linked_task"] == "AUTO-T1" for t in result)

    def test_empty_when_no_match(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug("TICKET-T1", linked_task="AUTO-T1"))
        assert ts.list_by_task("AUTO-T99") == []


# ── update_status ─────────────────────────────────────────────────────────────

class TestUpdateStatus:
    def test_status_changes(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        ts.update_status("TICKET-T1", "fixed")
        assert ts.get("TICKET-T1")["status"] == "fixed"

    def test_updated_at_refreshed(self, tmp_path):
        ts = _store(tmp_path)
        t = make_ticket(id="TICKET-T1", type="bug", linked_task="", title="T",
                        body="", updated_at="2000-01-01T00:00:00Z")
        ts.create(t)
        ts.update_status("TICKET-T1", "fixed")
        assert ts.get("TICKET-T1")["updated_at"] != "2000-01-01T00:00:00Z"

    def test_raises_not_found(self, tmp_path):
        with pytest.raises(TicketNotFound):
            _store(tmp_path).update_status("TICKET-GHOST", "fixed")

    def test_raises_schema_error_on_bad_status(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        with pytest.raises(TicketSchemaError):
            ts.update_status("TICKET-T1", "resolved")

    @pytest.mark.parametrize("status", sorted(TICKET_STATUSES))
    def test_all_valid_statuses_accepted(self, tmp_path, status):
        ts = _store(tmp_path)
        ts.create(_bug())
        ts.update_status("TICKET-T1", status)
        assert ts.get("TICKET-T1")["status"] == status


# ── update_body ───────────────────────────────────────────────────────────────

class TestUpdateBody:
    def test_body_replaced(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        ts.update_body("TICKET-T1", "new body content")
        assert ts.get("TICKET-T1")["body"] == "new body content"

    def test_raises_not_found(self, tmp_path):
        with pytest.raises(TicketNotFound):
            _store(tmp_path).update_body("TICKET-GHOST", "x")


# ── update (multi-field) ──────────────────────────────────────────────────────

class TestUpdate:
    def test_multi_field_update(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        ts.update("TICKET-T1", status="in-progress", body="new details")
        t = ts.get("TICKET-T1")
        assert t["status"] == "in-progress"
        assert t["body"] == "new details"

    def test_id_is_immutable(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        ts.update("TICKET-T1", id="TICKET-HACKED")
        assert ts.get("TICKET-T1")["id"] == "TICKET-T1"
        assert ts.get("TICKET-HACKED") is None

    def test_created_at_is_immutable(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        original_created = ts.get("TICKET-T1")["created_at"]
        ts.update("TICKET-T1", created_at="1970-01-01T00:00:00Z")
        assert ts.get("TICKET-T1")["created_at"] == original_created

    def test_raises_not_found(self, tmp_path):
        with pytest.raises(TicketNotFound):
            _store(tmp_path).update("TICKET-GHOST", status="fixed")

    def test_raises_schema_error_on_bad_field(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        with pytest.raises(TicketSchemaError):
            ts.update("TICKET-T1", status="bad-status")


# ── delete ────────────────────────────────────────────────────────────────────

class TestDelete:
    def test_file_removed(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        ts.delete("TICKET-T1")
        assert not (tmp_path / "tickets" / "TICKET-T1.json").exists()

    def test_returns_true_when_deleted(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        assert ts.delete("TICKET-T1") is True

    def test_returns_false_when_absent(self, tmp_path):
        ts = _store(tmp_path)
        assert ts.delete("TICKET-NOPE") is False

    def test_no_op_does_not_raise(self, tmp_path):
        _store(tmp_path).delete("TICKET-NOPE")   # must not raise

    def test_get_returns_none_after_delete(self, tmp_path):
        ts = _store(tmp_path)
        ts.create(_bug())
        ts.delete("TICKET-T1")
        assert ts.get("TICKET-T1") is None


# ── persistence / resume ──────────────────────────────────────────────────────

class TestPersistence:
    def test_survives_new_store_instance(self, tmp_path):
        """Tickets written by one TicketStore instance are readable by a new one."""
        ts1 = _store(tmp_path)
        ts1.create(_bug())
        ts1.create(_inv())

        ts2 = TicketStore(tmp_path / "tickets")
        assert ts2.get("TICKET-T1")["id"] == "TICKET-T1"
        assert ts2.get("TICKET-T2")["id"] == "TICKET-T2"
        assert len(ts2.list_all()) == 2

    def test_updates_persist_across_instances(self, tmp_path):
        ts1 = _store(tmp_path)
        ts1.create(_bug())
        ts1.update_status("TICKET-T1", "fixed")

        ts2 = TicketStore(tmp_path / "tickets")
        assert ts2.get("TICKET-T1")["status"] == "fixed"

    def test_delete_persists_across_instances(self, tmp_path):
        ts1 = _store(tmp_path)
        ts1.create(_bug())
        ts1.delete("TICKET-T1")

        ts2 = TicketStore(tmp_path / "tickets")
        assert ts2.get("TICKET-T1") is None


# ── factory ───────────────────────────────────────────────────────────────────

class TestFactory:
    def test_make_ticket_store_roots_under_tickets(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        ts = make_ticket_store(agent_dir)
        ts.create(_bug())
        assert (agent_dir / "tickets" / "TICKET-T1.json").exists()

    def test_make_ticket_store_creates_dir(self, tmp_path):
        agent_dir = tmp_path / ".agent"
        # agent_dir does NOT exist yet
        ts = make_ticket_store(agent_dir)
        ts.create(_bug())
        assert (agent_dir / "tickets").is_dir()


# ── integration: ExhaustionHandler uses TicketStore ───────────────────────────

class TestExhaustionHandlerIntegration:
    """Smoke-test that ExhaustionHandler now delegates to TicketStore."""

    def test_ticket_readable_via_ticket_store(self, tmp_path):
        from dataclasses import dataclass, field as dc_field
        from tools.auto.exhaustion_handler import ExhaustionHandler
        from tools.auto.state import StateStore, make_task as make_state_task

        @dataclass
        class FakeResult:
            task_id: str = "AUTO-T1"
            passed: bool = False
            exhausted: bool = True
            rounds_used: int = 10
            feedback_files: list = dc_field(default_factory=list)
            def knowledge(self): return "Round 1: timed out"

        agent_dir = tmp_path / ".agent"
        st = StateStore(agent_dir)
        st.initialise("goal", tmp_path)
        st.upsert_task(make_state_task(
            id="AUTO-T1", title="Fix retry", instruction="x", target_files=[]
        ))

        TASK = {"id": "AUTO-T1", "title": "Fix retry",
                "instruction": "x", "acceptance_check": "pytest -q"}

        ExhaustionHandler(st).handle(TASK, FakeResult())

        ts = make_ticket_store(agent_dir)
        ticket = ts.get("TICKET-AUTO-T1")
        assert ticket is not None
        assert ticket["type"] == "investigation"
        assert ticket["status"] == "open"
        assert ticket["linked_task"] == "AUTO-T1"
        assert "timed out" in ticket["body"]

    def test_handler_idempotent_on_existing_ticket(self, tmp_path):
        """Re-running handle on an already-blocked task must not raise."""
        from dataclasses import dataclass, field as dc_field
        from tools.auto.exhaustion_handler import ExhaustionHandler
        from tools.auto.state import StateStore, make_task as make_state_task

        @dataclass
        class FakeResult:
            task_id: str = "AUTO-T1"
            passed: bool = False
            exhausted: bool = True
            rounds_used: int = 10
            feedback_files: list = dc_field(default_factory=list)
            def knowledge(self): return "err"

        agent_dir = tmp_path / ".agent"
        st = StateStore(agent_dir)
        st.initialise("goal", tmp_path)
        st.upsert_task(make_state_task(
            id="AUTO-T1", title="t", instruction="x", target_files=[]
        ))
        TASK = {"id": "AUTO-T1", "title": "t",
                "instruction": "x", "acceptance_check": ""}

        handler = ExhaustionHandler(st)
        handler.handle(TASK, FakeResult())
        handler.handle(TASK, FakeResult())   # second call must not raise

        ts = make_ticket_store(agent_dir)
        assert len(ts.list_by_task("AUTO-T1")) == 1   # still only one ticket


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
