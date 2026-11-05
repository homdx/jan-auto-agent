"""336: a write of the event log blocked on a busy disk must not stop the events.

`EventTap` read the stream and wrote `events.jsonl` on one thread, so a flush held up by
writeback held back the next event, and `wait_idle`'s silence clock declared an agent
that was working the whole time stalled. Seen as `status='timeout'` at last event + silence.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import _kilo_fake  # noqa: E402
from tools.contest.kilo_client import EventTap, KiloClient, KiloServer  # noqa: E402

SCENARIO = {"turns": [{"events": ["busy"], "subagent": {"work_sec": 3, "beat_sec": 0.1},
                       "assistant": "done"}]}


def _rig(tmp_path):
    fake = _kilo_fake.FakeKiloServer(SCENARIO, directory=str(tmp_path)).start()
    server = KiloServer.attach(fake.url)
    client = KiloClient(server, str(tmp_path))
    tap = EventTap(fake.url, str(tmp_path), str(tmp_path / "events.jsonl")).start()
    deadline = time.monotonic() + 30
    while not fake.subscribers and time.monotonic() < deadline:
        time.sleep(0.02)
    session = client.create_session("kenary", "hy3:free", rules={}, title="t")
    return fake, server, client, tap, session


def _close(fake, server, tap):
    tap.stop()
    tap.join(30)
    server.close()
    fake.stop()


def test_a_write_stuck_on_the_disk_does_not_look_like_silence(tmp_path, monkeypatch):
    real = EventTap._write_log
    release = threading.Event()
    stuck = threading.Event()

    def blocked(self, entry):
        if entry["event"].get("type") == "session.created":   # the child is working from here on
            stuck.set()
            release.wait(30)
        return real(self, entry)

    monkeypatch.setattr(EventTap, "_write_log", blocked)
    fake, server, client, tap, session = _rig(tmp_path)
    try:
        client.prompt(session, "use the task tool")
        res = client.wait_idle(tap, session, 60.0, idle_event_timeout=1.5,
                               on_permission=lambda event: ("reject", ""),
                               on_question=lambda event: None)
        assert stuck.is_set()
        assert res.status == "idle", res
    finally:
        release.set()
        _close(fake, server, tap)


def test_the_log_is_whole_and_in_order_once_the_reader_has_ended(tmp_path, monkeypatch):
    real = EventTap._write_log

    def slow(self, entry):
        time.sleep(0.01)
        return real(self, entry)

    monkeypatch.setattr(EventTap, "_write_log", slow)
    fake, server, client, tap, session = _rig(tmp_path)
    try:
        for n in range(30):
            fake._emit({"type": "test.mark", "properties": {"n": n}})
        deadline = time.monotonic() + 30
        while sum(e.get("type") == "test.mark" for e in tap.events) < 30 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        _close(fake, server, tap)
    entries = [json.loads(line) for line in Path(tmp_path / "events.jsonl").read_text().splitlines()]
    assert [e["event"]["properties"]["n"] for e in entries if e["event"]["type"] == "test.mark"] == list(range(30))
    assert [e["event"]["type"] for e in entries] == [e.get("type") for e in tap.events]
    assert entries[-1]["event"]["type"] == "tap.closed"


def test_a_failing_write_does_not_stop_the_events(tmp_path, monkeypatch):
    def broken(self, entry):
        raise OSError("no space left on device")

    monkeypatch.setattr(EventTap, "_write_log", broken)
    fake, server, client, tap, session = _rig(tmp_path)
    try:
        client.prompt(session, "use the task tool")
        res = client.wait_idle(tap, session, 60.0, idle_event_timeout=1.5,
                               on_permission=lambda event: ("reject", ""),
                               on_question=lambda event: None)
        assert res.status == "idle", res
    finally:
        _close(fake, server, tap)
