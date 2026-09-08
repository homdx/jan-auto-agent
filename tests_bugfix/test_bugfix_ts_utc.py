"""tests_bugfix/test_bugfix_ts_utc.py

FIX-1 #8 -- ``tools/auto/utils.py::_ts()`` stamped every ticket with local
time, not UTC:

    def _ts() -> str:
        '''Return the current local time as an ISO-8601 string.'''
        return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

``ticket_store.py`` documents ``created_at`` / ``updated_at`` as "ISO-8601
UTC timestamp" (lines 16-17, 147), and every write goes through ``_ts()``.
``datetime.now()`` with no argument is the *local* system clock, so every
ticket, ``progress.json`` stamp, and ``run.log`` line was labelled UTC while
actually carrying local time. Invisible on one machine; the moment an agent
and a pipeline span environments (a reader in UTC opening tickets written by
a box in UTC+5) the chronology is corrupted, and nothing in the string
reveals it.

The fix takes the clock in UTC and says so in the string (trailing "Z"),
matching the pre-existing convention already used elsewhere in this
codebase -- ``tools/collect/manifest.py::_utc_iso_now()`` and every
explicit timestamp literal in ``tests/test_auto_d1.py`` (e.g.
``"2024-01-01T00:00:00Z"``) -- so this is not a new format, just brings
``_ts()`` in line with what every other UTC timestamp in the project
already looks like.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.auto.bug_fix_loop as bug_fix_loop  # noqa: E402
import tools.auto.ticket_store as ticket_store  # noqa: E402
import tools.auto.utils as utils  # noqa: E402
from tools.auto.state import StateStore  # noqa: E402
from tools.auto.ticket_store import make_ticket, make_ticket_store  # noqa: E402
from tools.auto.utils import _ts  # noqa: E402

FIXED_UTC = datetime(2026, 9, 7, 12, 30, 0, tzinfo=timezone.utc)


def _fake_datetime(offset: timedelta) -> type[datetime]:
    """A datetime clock whose *local* wall time sits at UTC+*offset*.

    ``now()`` (no tz arg -- the OLD, buggy code path) renders the fixed
    instant as that local zone would show it; ``now(tz)`` (the FIXED code
    path) renders the true instant in *tz*. The instant itself never
    moves -- only which face of the clock a caller asks to read.
    """

    class _FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return FIXED_UTC.astimezone(timezone(offset)).replace(tzinfo=None)
            return FIXED_UTC.astimezone(tz)

    return _FakeDatetime


class TestTsWithOffsetClock:
    """The stamp must be the UTC instant, whatever the local zone is.

    Each case points the process at a *different* local offset around the
    same fixed instant; a regression back to ``datetime.now()`` (no tz)
    would leak that local offset into the string and fail every one of
    these except the (deliberately included) zero-offset case.
    """

    @pytest.mark.parametrize(
        "offset",
        [
            timedelta(hours=5),
            timedelta(hours=-6),
            timedelta(0),
            timedelta(hours=5, minutes=30),
        ],
        ids=["utc+5", "utc-6", "utc+0", "utc+5:30"],
    )
    def test_stamp_matches_utc_instant_regardless_of_local_offset(
        self, monkeypatch, offset
    ):
        monkeypatch.setattr(utils, "datetime", _fake_datetime(offset))
        assert _ts() == "2026-09-07T12:30:00Z"

    def test_local_time_is_not_leaked(self, monkeypatch):
        """The classic symptom: a local clock bleeding into a UTC label."""
        monkeypatch.setattr(utils, "datetime", _fake_datetime(timedelta(hours=5)))
        # Under the old bug, a UTC+5 local clock at the fixed instant would
        # read 17:30, not 12:30.
        assert _ts() != "2026-09-07T17:30:00Z"


class TestTsWithRealClock:
    def test_format_is_iso8601_utc_with_z(self):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", _ts())

    def test_is_now_in_utc(self):
        parsed = datetime.fromisoformat(_ts().replace("Z", "+00:00"))
        assert abs((parsed - datetime.now(timezone.utc)).total_seconds()) < 5

    def test_is_timezone_aware_after_parsing(self):
        parsed = datetime.fromisoformat(_ts().replace("Z", "+00:00"))
        assert parsed.utcoffset() == timedelta(0)


class TestTicketStoreStampsAreUtc:
    def test_created_and_updated_are_utc(self, monkeypatch):
        monkeypatch.setattr(utils, "datetime", _fake_datetime(timedelta(hours=5)))
        made = make_ticket(
            id="TICKET-AUTO-T1", type="bug", linked_task="AUTO-T1",
            title="Stale index.lock", body="git refused to write",
        )
        assert made["created_at"] == "2026-09-07T12:30:00Z"
        assert made["updated_at"] == "2026-09-07T12:30:00Z"

    def test_explicit_timestamps_are_not_overwritten(self):
        made = make_ticket(
            id="TICKET-AUTO-T1", type="investigation", linked_task="",
            title="Deferred", body="later",
            created_at="2020-01-01T00:00:00Z",
        )
        assert made["created_at"] == "2020-01-01T00:00:00Z"

    def test_update_refreshes_updated_at_in_utc(self, tmp_path, monkeypatch):
        store = make_ticket_store(tmp_path / ".agent")
        store.create(make_ticket(
            id="TICKET-AUTO-T1", type="bug", linked_task="",
            title="t", body="b",
        ))
        monkeypatch.setattr(utils, "datetime", _fake_datetime(timedelta(hours=5)))
        store.update_status("TICKET-AUTO-T1", "fixed")
        ticket = store.get("TICKET-AUTO-T1")
        assert ticket["updated_at"] == "2026-09-07T12:30:00Z"
        assert ticket["created_at"].endswith("Z")


class TestRunLogStampsAreUtc:
    def test_log_line_prefix_is_utc_marked(self, tmp_path, monkeypatch):
        agent_dir = tmp_path / ".agent"
        store = StateStore(agent_dir)
        store.initialise("test", tmp_path)

        monkeypatch.setattr(utils, "datetime", _fake_datetime(timedelta(hours=5)))
        store.log("task AUTO-T1 completed")

        lines = (agent_dir / "run.log").read_text(encoding="utf-8").splitlines()
        # initialise() already logged a line before the clock was swapped;
        # only the line written after the swap is pinned here.
        assert lines[-1].startswith("[2026-09-07T12:30:00Z] task AUTO-T1 completed")


class TestQuarantineStampIsUtc:
    """ticket_store._quarantine names corrupt tickets on the UTC clock."""

    def test_corrupt_filename_carries_utc_stamp(self, tmp_path, monkeypatch):
        agent_dir = tmp_path / ".agent"
        store = make_ticket_store(agent_dir)
        store.create(make_ticket(
            id="TICKET-AUTO-T1", type="bug", linked_task="",
            title="t", body="b",
        ))
        path = next((agent_dir / "tickets").glob("*.json"))
        path.write_text("{ not json", encoding="utf-8")

        monkeypatch.setattr(
            ticket_store, "datetime", _fake_datetime(timedelta(hours=5))
        )
        assert store.get("TICKET-AUTO-T1") is None  # triggers quarantine

        quarantined = list((agent_dir / "tickets").glob("*.corrupt-*"))
        assert len(quarantined) == 1
        # Under the old bug a UTC+5 box would have written ...-20260907T173000.
        assert quarantined[0].name.endswith(".corrupt-20260907T123000Z")

    def test_collision_suffix_still_applies(self, tmp_path, monkeypatch):
        """The numeric-suffix guard (a prior fix) survives the clock change."""
        agent_dir = tmp_path / ".agent"
        store = make_ticket_store(agent_dir)
        monkeypatch.setattr(ticket_store, "datetime", _fake_datetime(timedelta(0)))

        for _i in (1, 2):
            # Same id twice: the quarantine rename frees the .json path, so
            # the second create/corrupt cycle lands on the identical stamped
            # destination -- which is exactly what the suffix guards.
            store.create(make_ticket(
                id="TICKET-AUTO-T1", type="bug", linked_task="",
                title="t", body="b",
            ))
            path = next(
                p for p in (agent_dir / "tickets").glob("*.json")
                if "corrupt" not in p.name
            )
            path.write_text("{ not json", encoding="utf-8")
            store.get("TICKET-AUTO-T1")

        names = sorted(p.name for p in (agent_dir / "tickets").glob("*.corrupt-*"))
        assert len(names) == 2
        assert names[0].endswith(".corrupt-20260907T123000Z")
        assert names[1].endswith(".corrupt-20260907T123000Z-001")


class TestArchiveStampIsUtc:
    """bug_fix_loop archive directories are named on the UTC clock."""

    def test_archive_dir_carries_utc_stamp(self, tmp_path, monkeypatch):
        agent_dir = tmp_path / ".agent"
        state = StateStore(agent_dir)
        state.initialise("test", tmp_path)

        tdir = state.task_dir("BUG-FIX-AUTO-T1")
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "feedback_round_1.md").write_text("round 1", encoding="utf-8")

        monkeypatch.setattr(
            bug_fix_loop, "datetime", _fake_datetime(timedelta(hours=5))
        )
        loop = bug_fix_loop.BugFixLoop.__new__(bug_fix_loop.BugFixLoop)
        loop._state = state
        loop._clear_stale_fix_rounds("BUG-FIX-AUTO-T1")

        archives = [p for p in tdir.iterdir() if p.name.startswith("previous_attempt_")]
        assert len(archives) == 1
        # Under the old bug a UTC+5 box would have written ..._20260907T173000.
        assert archives[0].name == "previous_attempt_20260907T123000Z"
        assert (archives[0] / "feedback_round_1.md").exists()
