"""KC-12 black-box scenarios: driven through the public client API and the
fake's own emitter, independent of what an entry added to _kilo_fake.py.
KC12_REPO points at the worktree under test."""
from __future__ import annotations

import inspect
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(os.environ["KC12_REPO"]).resolve()
for _p in (str(REPO), str(REPO / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.contest import kilo_client as kc  # noqa: E402
from tools.contest.kilo_client import EventTap, KiloClient, KiloServer  # noqa: E402

RULES = [
    {"permission": "*", "pattern": "*", "action": "allow"},
    {"permission": "external_directory", "pattern": "*", "action": "ask"},
]


def _reject(event):
    return "reject", "scn: no"


def _noq(event):
    return None


class _P:
    def __init__(self, fake, server, client, tap, session, directory):
        self.fake, self.server, self.client, self.tap = fake, server, client, tap
        self.session, self.directory = session, directory


@contextmanager
def _probe(tmp_path, scenario):
    directory = str(tmp_path)
    fake = FakeKiloServer(scenario, directory=directory, reply_timeout=20.0).start()
    server = KiloServer.attach(fake.url)
    client = KiloClient(server, directory)
    tap = EventTap(fake.url, directory, str(tmp_path / "events.jsonl")).start()
    for _ in range(100):
        if fake.subscribers:
            break
        time.sleep(0.02)
    assert fake.subscribers
    session = client.create_session("kenary", "hy3:free", rules=RULES, title="scn")
    try:
        yield _P(fake, server, client, tap, session, directory)
    finally:
        tap.stop(); tap.join(); server.close(); fake.stop()


def _pulse(fake, sid, etype, period, duration, then=None):
    """Emit {etype, sessionID=sid} every `period` s for `duration` s, then `then`."""
    def run():
        end = time.monotonic() + duration
        while time.monotonic() < end:
            props = {} if sid is None else {"sessionID": sid}
            fake._emit({"type": etype, "properties": props})
            time.sleep(period)
        if then is not None:
            fake._emit(then)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _wait(h, timeout, **kw):
    started = time.monotonic()
    res = h.client.wait_idle(h.tap, h.session, timeout, on_permission=_reject,
                             on_question=_noq, **kw)
    return res, time.monotonic() - started


def _aborts(h):
    return [c for c in h.fake.calls(method="POST", path="/abort")
            if c["path"] == f"/session/{h.session.id}/abort"]


# 0 — the contract
def test_s0_signature_verbatim():
    sig = inspect.signature(kc.KiloClient.wait_idle)
    names = list(sig.parameters)
    assert names == ["self", "tap", "session", "timeout", "idle_event_timeout",
                     "on_permission", "on_question"]
    p = sig.parameters["idle_event_timeout"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is None
    assert sig.parameters["on_permission"].kind is inspect.Parameter.KEYWORD_ONLY


# 1 — a silent session is aborted at idle_event_timeout, not at timeout
def test_s1_stall_is_cut_at_idle_event_timeout(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        res, wall = _wait(h, 30.0, idle_event_timeout=0.5)
    assert res.status == "timeout"
    assert res.error is None
    assert wall < 3.0 and res.elapsed < 3.0
    assert len(_aborts(h)) == 1
    assert h.fake.sessions()[0].aborted is True


# 2 — omitted: today's behaviour (a slow answer waits the full timeout)
def test_s2_omitted_waits_the_full_timeout(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "delay": 1.2, "assistant": "slow"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        res, wall = _wait(h, 10.0)
    assert res.status == "idle"
    assert wall >= 1.0
    assert not _aborts(h)


# 2b — None is the same as omitted
def test_s2b_none_is_disabled(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "delay": 1.2, "assistant": "slow"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        res, wall = _wait(h, 10.0, idle_event_timeout=None)
    assert res.status == "idle"
    assert wall >= 1.0


# 3 — any event of this session keeps it alive across several windows
def test_s3_any_own_event_keeps_it_alive(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        idle = {"type": "session.idle", "properties": {"sessionID": h.session.id}}
        _pulse(h.fake, h.session.id, "file.edited", 0.2, 2.2, then=idle)
        res, wall = _wait(h, 30.0, idle_event_timeout=0.6)
    assert res.status == "idle", res
    assert wall >= 2.0
    assert not _aborts(h)


# 3b — message.part.updated (a type wait_idle never filtered on) counts too
def test_s3b_unknown_event_type_counts(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        idle = {"type": "session.idle", "properties": {"sessionID": h.session.id}}
        _pulse(h.fake, h.session.id, "message.part.updated", 0.2, 2.2, then=idle)
        res, wall = _wait(h, 30.0, idle_event_timeout=0.6)
    assert res.status == "idle", res
    assert wall >= 2.0


# 4 — another session's events do not count
def test_s4_other_session_events_do_not_count(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        other = h.client.create_session("kenary", "hy3:free", rules=RULES, title="o")
        h.client.prompt(h.session, "go")
        _pulse(h.fake, other.id, "file.edited", 0.2, 4.0)
        res, wall = _wait(h, 30.0, idle_event_timeout=0.6)
    assert res.status == "timeout", res
    assert wall < 2.5
    assert len(_aborts(h)) == 1


# 5 — events without a sessionID (server.heartbeat) do not count
def test_s5_heartbeat_without_session_does_not_count(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        _pulse(h.fake, None, "server.heartbeat", 0.2, 4.0)
        res, wall = _wait(h, 30.0, idle_event_timeout=0.6)
    assert res.status == "timeout", res
    assert wall < 2.5
    assert len(_aborts(h)) == 1


# 6 — the overall timeout still wins over a chatty session
def test_s6_overall_timeout_wins(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        _pulse(h.fake, h.session.id, "file.edited", 0.2, 6.0)
        res, wall = _wait(h, 1.5, idle_event_timeout=5.0)
    assert res.status == "timeout"
    assert 1.2 <= wall < 4.0
    assert len(_aborts(h)) == 1


# 7 — silence partway through is caught
def test_s7_mid_flight_silence_is_caught(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        _pulse(h.fake, h.session.id, "file.edited", 0.2, 1.2)
        res, wall = _wait(h, 30.0, idle_event_timeout=0.6)
    assert res.status == "timeout"
    assert 1.5 <= wall < 4.0
    assert len(_aborts(h)) == 1


# 8 — the ticket's turn shape exists in the fake and stalls under 3 s
def test_s8_pause_before_idle_shape(tmp_path):
    scenario = {"turns": [{"pause_before_idle_sec": 2}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        res, wall = _wait(h, 30.0, idle_event_timeout=0.5)
    assert res.status == "timeout"
    assert wall < 3.0
    assert h.fake.sessions()[0].aborted is True
    assert not h.fake.turn_errors
    assert h.fake.events_of("session.status"), "busy was not emitted"


# 9 — a permission mid-turn with the stall clock set still works
def test_s9_permission_still_answered(tmp_path):
    scenario = {"turns": [{"events": ["busy"],
                           "permission": {"permission": "external_directory",
                                          "patterns": ["/tmp/*"],
                                          "metadata": {"command": "rm /tmp/x"}},
                           "assistant": "done"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        res, wall = _wait(h, 10.0, idle_event_timeout=2.0)
    assert res.status == "idle"
    assert len(res.permissions) == 1
    assert h.fake.events_of("permission.replied")


# 10 — session.error still comes back as error with the clock set
def test_s10_session_error(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "error": {"name": "boom", "message": "x"}}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        res, wall = _wait(h, 10.0, idle_event_timeout=2.0)
    assert res.status == "error"
    assert res.error["name"] == "boom"


# 11 — two turns: the clock restarts per call; turn 2 idle is turn 2's
def test_s11_two_turns_each_with_the_clock(tmp_path):
    scenario = {"turns": [{"events": ["busy", "idle"], "assistant": "one"},
                          {"events": ["busy"], "delay": 0.3, "assistant": "two"}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "a")
        r1, _ = _wait(h, 10.0, idle_event_timeout=1.0)
        assert r1.status == "idle"
        time.sleep(1.3)   # longer than idle_event_timeout between turns
        h.client.prompt(h.session, "b")
        r2, w2 = _wait(h, 10.0, idle_event_timeout=1.0)
        assert r2.status == "idle", r2
        assert h.client.last_assistant_text(h.session) == "two"
    assert not _aborts(h)


# 12 — monotonic on both sides: a wall-clock jump must not fire the stall
def test_s12_wall_clock_jump_is_ignored(tmp_path, monkeypatch):
    scenario = {"turns": [{"events": ["busy"], "delay": 1.0, "assistant": "ok"}]}
    real_time = time.time
    monkeypatch.setattr(kc.time, "time", lambda: real_time() + 3600.0)
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        res, wall = _wait(h, 10.0, idle_event_timeout=5.0)
    assert res.status == "idle", res
    assert not _aborts(h)


# 13 — no busy poll: a 2 s quiet wait costs well under 2 s of CPU
def test_s13_no_busy_poll(tmp_path):
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "go")
        c0 = time.process_time()
        res, wall = _wait(h, 30.0, idle_event_timeout=2.0)
        cpu = time.process_time() - c0
    assert res.status == "timeout"
    assert cpu < 0.8, cpu


# 14 — the runner: fallback gone, keyword passed straight through
def test_s14_runner_fallback_deleted():
    src = (REPO / "tools/contest/runner.py").read_text()
    assert "_silence_watch" not in src
    assert "inspect.signature" not in src and "import inspect" not in src
    assert "idle_event_timeout=config.idle_event_timeout_sec" in src.replace(" ", "") \
        or "idle_event_timeout=" in src


# 15 — nothing hardcoded in the client
def test_s15_no_constant_in_client():
    src = (REPO / "tools/contest/kilo_client.py").read_text()
    assert "contest.ini" not in src
    body = inspect.getsource(kc.KiloClient.wait_idle)
    import re
    assert not re.search(r"(?<![\w.])(300|120|60)(?![\w.])", body), "a number in wait_idle"


# 16 — a turn that emits nothing at all is stalled from the moment the wait starts
def test_s16_total_silence_from_the_start(tmp_path):
    scenario = {"turns": [{"events": ["busy", "idle"], "assistant": "one"},
                          {"events": [], "idle": False}]}
    with _probe(tmp_path, scenario) as h:
        h.client.prompt(h.session, "a")
        r1, _ = _wait(h, 10.0, idle_event_timeout=1.0)
        assert r1.status == "idle"
        h.client.prompt(h.session, "b")
        r2, w2 = _wait(h, 10.0, idle_event_timeout=0.5)
    assert r2.status == "timeout", r2
    assert w2 < 2.5, w2
    assert len(_aborts(h)) == 1
