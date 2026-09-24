"""KC-47 (round 91) judge's acceptance suite: a running `bash` call is not silence.

Written from the ticket's Acceptance list, through the public contract only:
`KiloClient.wait_idle(..., idle_event_timeout=)` over a scripted stream on a fake
clock (K*, D*), the runner end to end over the base's fake Kilo (R*), and the
prompt / runbook / committed config (P*). Must be red on the base (58e20e5).

K* — the ticket's Acceptance bullets, one test each.
D* — data that splits the top group: Kilo's real `bash` default (120 s in the
     7.6.2 build: `bashDefaultTimeoutMs ?? 120000`), the bound measured from the
     part's own `running` event, `pending` as open, `error` as closed, the
     command clipped at 120 chars, a non-`bash` tool with a timeout input.
"""
from __future__ import annotations

import configparser
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_contest_kilo_client import _FakeClock, _reject  # noqa: E402
from test_contest_runner import Harness, Sandbox, _BenchFake, make_config  # noqa: E402
from tools.contest import kilo_client as kilo_client_module  # noqa: E402
from tools.contest import runner as runner_mod  # noqa: E402
from tools.contest.kilo_client import IdleResult, KiloClient, SessionRef  # noqa: E402
from tools.contest.runner import AgentState  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

W = 300.0          # idle_event_timeout, round 86's number
START = 1000.0     # _FakeClock's start
CMD = "python3 -m pytest tests -n 4 2>&1 | tail -15"


# ── a scripted stream with explicit times ───────────────────────────────────

class _TimedTap:
    """`EventTap.wait` on a fake clock, each event released at its own time.

    ``events`` is ``[(seconds after start, event), …]`` in order. Like the
    base's `_ScriptedTap`: an event the predicate rejects is consumed, not
    returned; nothing due inside the caller's wait → the clock jumps to its end.
    """

    def __init__(self, events, clock):
        self._events = [(START + at, ev) for at, ev in events]
        self._clock = clock

    def wait(self, pred, timeout):
        deadline = self._clock.now + max(0.0, float(timeout))
        while True:
            if not self._events or self._events[0][0] > deadline:
                self._clock.now = deadline
                return None
            at, event = self._events.pop(0)
            self._clock.now = max(self._clock.now, at)
            if pred(event):
                return event


def _run(monkeypatch, events, *, timeout=10_000.0, window=W):
    clock = _FakeClock(START)
    monkeypatch.setattr(kilo_client_module.time, "monotonic", clock.monotonic)
    aborts: list = []

    class _Client(KiloClient):
        def __init__(self):
            pass

        def _abort_quietly(self, session):
            aborts.append(session.id)

    session = SessionRef(id="ses_probe", provider_id="p", model_id="m", directory="/nowhere")
    res = _Client().wait_idle(_TimedTap(events, clock), session, timeout,
                              idle_event_timeout=window, on_permission=_reject,
                              on_question=lambda e: None)
    return res, aborts


def _part(status, *, pid="prt_1", tool="bash", command=CMD, timeout_ms=None,
          session_id="ses_probe", with_input=True):
    state = {"status": status}
    if with_input:
        inp = {"command": command, "description": "run the suite"}
        if timeout_ms is not None:
            inp["timeout"] = timeout_ms
        state["input"] = inp
    else:
        state["input"] = {}
    if status == "completed":
        state["output"] = "ok"
    return {"type": "message.part.updated",
            "properties": {"sessionID": session_id,
                           "part": {"id": pid, "sessionID": session_id, "messageID": "msg_1",
                                    "type": "tool", "callID": "call_1", "tool": tool,
                                    "state": state}}}


def _idle(session_id="ses_probe"):
    return {"type": "session.idle", "properties": {"sessionID": session_id}}


def _busy(session_id="ses_probe"):
    return {"type": "session.status", "properties": {"sessionID": session_id, "status": "busy"}}


def _elapsed(res):
    return round(res.elapsed, 3)


def _cmd_of(open_tool):
    if isinstance(open_tool, dict):
        return open_tool.get("command")
    return getattr(open_tool, "command", None)


# ── K: the Acceptance list ──────────────────────────────────────────────────

def test_K1_running_bash_outlives_the_silence_window(monkeypatch):
    """`bash` running with timeout 600000, silence W+1, then completed + idle."""
    res, aborts = _run(monkeypatch, [
        (1, _part("running", timeout_ms=600_000)),
        (1 + W + 1, _part("completed", timeout_ms=600_000)),
        (1 + W + 2, _idle()),
    ])
    assert res.status == "idle", res
    assert aborts == []


def test_K2_silence_past_the_calls_own_bound_times_out_and_names_it(monkeypatch):
    res, aborts = _run(monkeypatch, [
        (1, _part("running", timeout_ms=600_000)),
        (2000, _idle()),
    ])
    assert res.status == "timeout"
    assert aborts == ["ses_probe"]
    # the bound is 600 + W from the running event, not earlier
    assert _elapsed(res) == pytest.approx(1 + 600 + W, abs=1.0), res.elapsed
    assert _cmd_of(res.open_tool) == CMD


def test_K3_no_timeout_uses_a_finite_module_default(monkeypatch):
    res, aborts = _run(monkeypatch, [
        (1, _part("running")),          # no "timeout" in input
        (9000, _idle()),
    ])
    assert res.status == "timeout"
    assert aborts == ["ses_probe"]
    assert W < res.elapsed < 9000, res.elapsed


def test_K4_a_task_part_keeps_kc12s_clock(monkeypatch):
    res, aborts = _run(monkeypatch, [
        (1, _part("running", tool="task", timeout_ms=None)),
        (1000, _idle()),
    ])
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(1 + W, abs=0.01)
    assert aborts == ["ses_probe"]
    assert not getattr(res, "open_tool", None)


def test_K5a_one_completed_one_running_the_running_bound_applies(monkeypatch):
    res, aborts = _run(monkeypatch, [
        (1, _part("running", pid="prt_a", timeout_ms=60_000, command="ls")),
        (2, _part("running", pid="prt_b", timeout_ms=600_000)),
        (3, _part("completed", pid="prt_a", timeout_ms=60_000, command="ls")),
        (3 + 700, _part("completed", pid="prt_b", timeout_ms=600_000)),
        (3 + 701, _idle()),
    ])
    assert res.status == "idle", res
    assert aborts == []


def test_K5b_both_completed_back_to_kc12s_bound(monkeypatch):
    res, aborts = _run(monkeypatch, [
        (1, _part("running", pid="prt_a", timeout_ms=600_000)),
        (2, _part("running", pid="prt_b", timeout_ms=600_000)),
        (3, _part("completed", pid="prt_a", timeout_ms=600_000)),
        (4, _part("completed", pid="prt_b", timeout_ms=600_000)),
        (5000, _idle()),
    ])
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(4 + W, abs=0.01)
    assert aborts == ["ses_probe"]


def test_K6_another_sessions_bash_does_not_move_this_clock(monkeypatch):
    res, aborts = _run(monkeypatch, [
        (1, _part("running", timeout_ms=600_000, session_id="ses_other")),
        (5000, _idle()),
    ])
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(W, abs=0.01)
    assert aborts == ["ses_probe"]


def test_K7_turn_timeout_shorter_than_the_bash_timeout_wins(monkeypatch):
    res, aborts = _run(monkeypatch, [
        (1, _part("running", timeout_ms=2_400_000)),
        (9000, _idle()),
    ], timeout=500.0)
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(500.0, abs=0.01)
    assert aborts == ["ses_probe"]


def test_K8_no_open_bash_is_kc12_event_for_event(monkeypatch):
    res, aborts = _run(monkeypatch, [(1, _busy()), (2, _busy()), (5000, _idle())])
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(2 + W, abs=0.01)


def test_K9_silence_off_is_still_off(monkeypatch):
    """`idle_event_timeout=None` with a running bash: runs to the overall deadline."""
    res, aborts = _run(monkeypatch, [(1, _part("running", timeout_ms=1000))],
                       timeout=77.0, window=None)
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(77.0, abs=0.01)


def test_K10_idle_result_default_has_no_open_tool():
    r = IdleResult(status="idle")
    assert not getattr(r, "open_tool", None)


# ── D: data that splits the top group ───────────────────────────────────────

def test_D1_default_is_kilos_120s(monkeypatch):
    """Kilo 7.6.2: `bashDefaultTimeoutMs ?? 120000` — the bound is 120 + W."""
    res, _ = _run(monkeypatch, [(1, _part("running")), (9000, _idle())])
    assert _elapsed(res) == pytest.approx(1 + 120 + W, abs=1.0), res.elapsed


def test_D2_bound_is_measured_from_the_running_event(monkeypatch):
    """A busy beat at t=100 resets KC-12's clock, not the bash bound's start."""
    res, _ = _run(monkeypatch, [
        (10, _part("running", timeout_ms=600_000)),
        (100, _busy()),
        (9000, _idle()),
    ])
    assert _elapsed(res) == pytest.approx(10 + 600 + W, abs=1.0), res.elapsed


def test_D3_pending_is_open(monkeypatch):
    """`pending` (no input yet) is open — it gets the default, not W."""
    res, _ = _run(monkeypatch, [
        (1, _part("pending", with_input=False)),
        (1 + W + 10, _part("completed", timeout_ms=None)),
        (1 + W + 11, _idle()),
    ])
    assert res.status == "idle", res


def test_D4_error_status_closes_the_part(monkeypatch):
    res, _ = _run(monkeypatch, [
        (1, _part("running", timeout_ms=600_000)),
        (2, _part("error", timeout_ms=600_000)),
        (5000, _idle()),
    ])
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(2 + W, abs=0.01)


def test_D5_command_clipped_to_120_chars(monkeypatch):
    long_cmd = "python3 -m pytest " + "tests/test_x.py " * 30
    res, _ = _run(monkeypatch, [
        (1, _part("running", timeout_ms=1000, command=long_cmd)),
        (5000, _idle()),
    ])
    assert res.status == "timeout"
    cmd = _cmd_of(res.open_tool)
    assert cmd and long_cmd.startswith(cmd.rstrip("…").rstrip()) and len(cmd) <= 121, cmd


def test_D6_non_bash_tool_with_a_timeout_input_is_not_suspended(monkeypatch):
    res, _ = _run(monkeypatch, [
        (1, _part("running", tool="webfetch", timeout_ms=600_000)),
        (5000, _idle()),
    ])
    assert _elapsed(res) == pytest.approx(1 + W, abs=0.01)


def test_D7_open_tool_reports_running_for(monkeypatch):
    res, _ = _run(monkeypatch, [(1, _part("running", timeout_ms=1000)), (5000, _idle())])
    ot = res.open_tool
    rf = ot.get("running_for") if isinstance(ot, dict) else getattr(ot, "running_for", None)
    assert ot and (ot.get("tool") if isinstance(ot, dict) else getattr(ot, "tool", None)) == "bash"
    assert rf == pytest.approx(1 + W, abs=1.0), ot


def test_D8_mimos_exact_race(monkeypatch):
    """Round 86 mimo: timeout 300000, Kilo's own kill `completed` at ~300 s —
    the runner must not have fired first; the agent then goes on to idle."""
    res, aborts = _run(monkeypatch, [
        (1, _part("running", timeout_ms=300_000)),
        (1 + 300.5, _part("completed", timeout_ms=300_000)),
        (1 + 360, _busy()),
        (1 + 400, _idle()),
    ])
    assert res.status == "idle", res
    assert aborts == []


# ── R: the runner end to end over the fake Kilo ─────────────────────────────

def _session_of(fake, directory):
    return next(s for s in reversed(fake.sessions()) if s.directory == directory)


def _stall_run(tmp_path, *, bash_timeout_ms=None, command=CMD, idle=1, turn=20, pause=6):
    cfg = make_config(["agent-a"], turn_timeout_sec=turn, idle_event_timeout_sec=idle)
    sb = Sandbox(tmp_path)
    scenario = {"turns": [{"pause_before_idle_sec": pause}]}
    with _BenchFake(scenario) as fake:
        def on_prompt(d, t):
            if bash_timeout_ms is not None:
                sid = _session_of(fake, d).id
                ev = _part("running", timeout_ms=bash_timeout_ms, command=command,
                           session_id=sid)
                fake._emit(ev)

        scenario["turns"][0]["on_prompt"] = on_prompt
        started = time.monotonic()
        run = Harness(sb, fake, cfg).go()
        took = time.monotonic() - started
    return run, took


def test_R1_stall_during_bash_names_the_command_end_to_end(tmp_path):
    """1 s window, bash asked for 1.5 s → the stall fires at ~2.5 s, not ~1 s,
    and the runner's last_error names the call."""
    run, took = _stall_run(tmp_path, bash_timeout_ms=1500)
    assert run.state is AgentState.STALLED
    assert run.last_error.startswith("no event for 1s during bash: python3 -m pytest tests -n 4"), \
        run.last_error
    assert took >= 2.3, took


def test_R2_stall_without_bash_is_todays_line(tmp_path):
    run, took = _stall_run(tmp_path, bash_timeout_ms=None)
    assert run.state is AgentState.STALLED
    assert run.last_error == "no event for 1s", run.last_error


def test_R3_round86_shape_through_the_runner(tmp_path):
    """The runner's line for the round 86 shape, with the backend's result stubbed."""
    cfg = make_config(["agent-a"], turn_timeout_sec=3600, idle_event_timeout_sec=300)
    sb = Sandbox(tmp_path)
    with _BenchFake({"turns": [{"idle": False}]}) as fake:
        h = Harness(sb, fake, cfg)
        fields = {f for f in getattr(IdleResult, "__dataclass_fields__", {})}
        assert "open_tool" in fields
        h.backend.wait_idle = lambda session, timeout, **kw: IdleResult(
            status="timeout", elapsed=300.5,
            open_tool={"tool": "bash", "command": CMD, "running_for": 300.5})
        run = h.go()
    assert run.last_error.startswith("no event for 300s during bash: python3 -m pytest tests -n 4"), \
        run.last_error


# ── P: prompt, runbook, config ──────────────────────────────────────────────

SENTENCE = "Running the test suite on this machine can take up to 20 minutes under load"


def _const():
    for mod in (runner_mod, kilo_client_module):
        v = getattr(mod, "AGENT_TEST_TIMEOUT_MS", None)
        if v is not None:
            return v
    return None


def test_P1_constant_is_1200000():
    assert _const() == 1_200_000


def test_P2_prompt_carries_the_sentence_and_the_number(tmp_path):
    text = runner_mod.round_prompt("agent-a", tmp_path / "t.md", "abc1234")
    flat = " ".join(text.split())
    assert SENTENCE in flat
    assert "at least 1200000 ms" in flat


def test_P3_runbook_carries_the_same_sentence():
    doc = (REPO_ROOT / "docs" / "collect-epics" / "RUN-THE-EPIC-COMPETITION.md").read_text()
    flat = " ".join(doc.replace(">", " ").split())
    assert SENTENCE in flat
    assert "1200000 ms" in flat or "{AGENT_TEST_TIMEOUT_MS}" in flat


def test_P4_below_the_committed_turn_timeout():
    cp = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    cp.read(REPO_ROOT / "contest.ini")
    turn = None
    for sec in cp.sections():
        if cp.has_option(sec, "turn_timeout_sec"):
            turn = cp.getfloat(sec, "turn_timeout_sec")
    assert turn and _const() / 1000 < turn


# ── round 2: data that splits the top group ─────────────────────────────────

def test_D9_an_open_short_bash_never_cuts_a_live_session_earlier_than_kc12(monkeypatch):
    """A `bash` asked for 1 s is open (its `completed` never came), but the
    session keeps talking: the bound is the *largest* of KC-12's and the part's,
    so the last event at t=500 carries the turn to 500 + W."""
    res, aborts = _run(monkeypatch, [
        (1, _part("running", timeout_ms=1000)),
        (250, _busy()),
        (500, _busy()),
        (9000, _idle()),
    ])
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(500 + W, abs=1.0), res.elapsed


def test_D10_two_open_bash_parts_the_largest_bound_applies(monkeypatch):
    res, _ = _run(monkeypatch, [
        (1, _part("running", pid="prt_a", timeout_ms=60_000, command="sleep 1")),
        (2, _part("running", pid="prt_b", timeout_ms=600_000)),
        (9000, _idle()),
    ])
    assert res.status == "timeout"
    assert _elapsed(res) == pytest.approx(2 + 600 + W, abs=1.0), res.elapsed
