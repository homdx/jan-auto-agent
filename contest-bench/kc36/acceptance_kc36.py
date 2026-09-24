"""KC-36 acceptance — the judge's own suite, written from the ticket, not from any entry.

Round 75. Every scenario is a line of the ticket's Acceptance list read literally,
through the public contract only: `KiloClient.wait_idle(..., on_deadline=)` on the
base's scripted tap and fake clock, `runner._churn(ws)`, `run_agent` / `run_round`
against the base's `tests/_kilo_fake.py`, `load_roster`. Helpers come from the
base's own test files — never an entry's new helper.

Copy into `<worktree>/tests/test_kc36_accept.py` and run
`python3 -m pytest tests/test_kc36_accept.py -q -p no:randomly --timeout=180 -n 0`.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import subprocess
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

from test_contest_kilo_client import _FakeClock, _ScriptedTap, _ev, _reject  # noqa: E402
from test_contest_roster import MINIMAL, add_to_contest, write_ini  # noqa: E402
from test_contest_runner import (  # noqa: E402
    ROUND,
    Harness,
    Sandbox,
    _BenchFake,
    _git,
    _make_backend,
    _write,
    make_config,
)
from tools.contest import kilo_client as kilo_client_module  # noqa: E402
from tools.contest import runner as runner_mod  # noqa: E402
from tools.contest.kilo_client import KiloClient, SessionRef  # noqa: E402
from tools.contest.roster import AgentSpec, RosterError, load_roster  # noqa: E402
from tools.contest.runner import AgentState, run_round  # noqa: E402
from tools.contest.workspace import Workspace  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


# ── wait_idle on a fake clock ───────────────────────────────────────────────

def _probe(monkeypatch, events, *, timeout, on_deadline="omit", window=None, every=1.0):
    clock = _FakeClock()
    monkeypatch.setattr(kilo_client_module.time, "monotonic", clock.monotonic)
    aborts: list = []

    class _Client(KiloClient):
        def __init__(self):
            pass

        def _abort_quietly(self, session):
            aborts.append(session.id)

    session = SessionRef(id="ses_probe", provider_id="p", model_id="m", directory="/nowhere")
    tap = _ScriptedTap(events, every=every, clock=clock)
    kw = {} if on_deadline == "omit" else {"on_deadline": on_deadline}
    res = _Client().wait_idle(tap, session, timeout, idle_event_timeout=window,
                              on_permission=_reject, on_question=lambda e: None, **kw)
    return res, aborts


def test_K1_no_on_deadline_is_todays_timeout(monkeypatch):
    res, aborts = _probe(monkeypatch, [], timeout=100.0)
    assert res.status == "timeout" and res.elapsed == 100.0 and aborts == ["ses_probe"]


def test_K2_extends_twice_then_aborts_once(monkeypatch):
    calls: list = []
    answers = [30, 30, None]

    def on_deadline(elapsed):
        calls.append(elapsed)
        return answers[len(calls) - 1]

    res, aborts = _probe(monkeypatch, [], timeout=100.0, on_deadline=on_deadline)
    assert calls == [100.0, 130.0, 160.0]
    assert res.status == "timeout" and aborts == ["ses_probe"]
    assert res.elapsed >= 160.0


@pytest.mark.parametrize("answer", [0, -5])
def test_K3_zero_or_negative_aborts_at_the_original_deadline(monkeypatch, answer):
    res, aborts = _probe(monkeypatch, [], timeout=100.0, on_deadline=lambda e: answer)
    assert res.status == "timeout" and res.elapsed == 100.0 and aborts == ["ses_probe"]


def test_K4_a_raising_callback_aborts_and_is_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def boom(elapsed):
        raise RuntimeError("kc36-boom")

    res, aborts = _probe(monkeypatch, [], timeout=100.0, on_deadline=boom)
    assert res.status == "timeout" and res.elapsed == 100.0 and aborts == ["ses_probe"]
    assert "kc36-boom" in caplog.text or "RuntimeError" in caplog.text


def test_K5_silence_mid_extension_ends_on_the_silence_clock(monkeypatch):
    calls: list = []

    def on_deadline(elapsed):
        calls.append(elapsed)
        return 1000 if len(calls) <= 3 else None

    beats = [_ev("session.status", status="busy") for _ in range(12)]   # t = 10 … 120
    res, aborts = _probe(monkeypatch, beats, timeout=100.0, on_deadline=on_deadline,
                         window=50.0, every=10.0)
    assert res.status == "timeout" and aborts == ["ses_probe"]
    assert res.elapsed == 170.0, res.elapsed          # last beat 120 + window 50
    assert calls == [100.0]


# ── _churn ──────────────────────────────────────────────────────────────────

def _lines(n: int) -> str:
    return "".join(f"x{i} = {i}\n" for i in range(n))


def test_C1_untracked_file(tmp_path):
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    _write(ws.path / "pkg" / "new.py", _lines(3))
    assert runner_mod._churn(ws) == (1, 3)


def test_C2_tracked_edit(tmp_path):
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    _write(ws.path / "pkg" / "thing.py", "def thing():\n    return 2\n")
    assert runner_mod._churn(ws) == (1, 2)


def test_C3_commit_above_base(tmp_path):
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    _write(ws.path / "pkg" / "c.py", _lines(4))
    _git(ws.path, "add", "-A")
    _git(ws.path, "commit", "-q", "-m", "c")
    assert runner_mod._churn(ws) == (1, 4)


def test_C4_git_failure_is_zero_not_a_raise(tmp_path):
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    gone = Workspace(agent=ws.agent, path=tmp_path / "deleted", branch=ws.branch,
                     base_sha=ws.base_sha, kind="worktree")
    assert runner_mod._churn(gone) == (0, 0)


def test_C5_smoke_tier_links_are_excluded(tmp_path):
    sb = Sandbox(tmp_path)
    ws = sb.ws("agent-a")
    (ws.path / ".smoke_tests").mkdir()
    (ws.path / ".smoke_tests" / "test_base.py").symlink_to(Path("..") / "tests" / "test_base.py")
    _write(ws.path / "pkg" / "new.py", _lines(3))
    assert runner_mod._churn(ws) == (1, 3)


# ── the runner, against the fake ────────────────────────────────────────────

def _grow(path: Path, seconds: float, stop: threading.Event) -> None:
    """Append one line to a tracked file every 0.05 s for *seconds*."""
    def run():
        end = time.monotonic() + seconds
        i = 0
        while time.monotonic() < end and not stop.is_set():
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"# grow {i}\n")
            i += 1
            time.sleep(0.05)
    threading.Thread(target=run, daemon=True).start()


def _session_of(fake, directory):
    return next(s for s in reversed(fake.sessions()) if s.directory == directory)


def _busy_turn(fake, scenario, grow_for=None, only=None):
    stop = threading.Event()

    def on_prompt(d, t):
        fake.pulse(_session_of(fake, d).id, 0.2, None)
        if grow_for and (only is None or Path(d).name == only):
            _grow(Path(d) / "pkg" / "thing.py", grow_for, stop)

    scenario["turns"][0]["on_prompt"] = on_prompt
    return stop


def _one(tmp_path, cfg, *, grow_for=None, prepare=None, wrap=None, caplog=None):
    sb = Sandbox(tmp_path)
    if prepare:
        prepare(sb.ws("agent-a").path)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        stop = _busy_turn(fake, scenario, grow_for)
        h = Harness(sb, fake, cfg)
        if wrap:
            wrap(h)
        started = time.monotonic()
        run = h.go()
        took = time.monotonic() - started
        stop.set()
    return run, took


def _waiting_lines(caplog) -> list:
    return [r.getMessage() for r in caplog.records if "WAITING" in r.getMessage()]


def test_R1_growing_worktree_is_extended_once(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    cfg = make_config(["agent-a"], turn_timeout_sec=2, turn_extend_sec=2, turn_max_sec=60,
                      idle_event_timeout_sec=60)
    run, _ = _one(tmp_path, cfg, grow_for=1.0)
    assert run.state is AgentState.STALLED
    ext = run.turns[0].get("extensions")
    assert ext and len(ext) == 1, run.turns[0]
    assert ext[0]["files"] == 1 and ext[0]["lines"] >= 1 and ext[0]["granted"] == 2
    assert set(ext[0]) >= {"at", "files", "lines", "granted"}


def test_R2_kc18_line_per_grant(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    cfg = make_config(["agent-a"], turn_timeout_sec=2, turn_extend_sec=2, turn_max_sec=60,
                      idle_event_timeout_sec=60)
    _one(tmp_path, cfg, grow_for=1.0)
    lines = _waiting_lines(caplog)
    assert any(re.search(r"agent-a: WAITING\s*—\s*\+\d+[ms] at \d+[ms] \(1 files?, \d+ lines?\)", m)
               for m in lines), lines


def test_R3_flat_worktree_stalls_with_no_extensions_and_names_the_sample(tmp_path):
    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=5, turn_max_sec=60,
                      idle_event_timeout_sec=60)
    run, _ = _one(tmp_path, cfg, prepare=lambda p: _write(p / "pkg" / "new.py", _lines(3)))
    assert run.state is AgentState.STALLED
    assert "extensions" not in run.turns[0]
    assert re.search(r"1 files?, 3 lines?", run.last_error or ""), run.last_error


def test_R4_growth_every_time_is_capped_at_turn_max_sec(tmp_path):
    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=2, turn_max_sec=4,
                      idle_event_timeout_sec=60)
    run, took = _one(tmp_path, cfg, grow_for=30)
    assert run.state is AgentState.STALLED
    granted = [e["granted"] for e in run.turns[0].get("extensions") or []]
    # the last grant is clipped so the turn totals turn_max_sec; a grant computed
    # from a live clock may be 0.9999 rather than 1, but never a third sliver
    assert len(granted) == 2 and granted[0] == 2, run.turns[0]
    assert abs(1 + sum(granted) - 4) < 0.05, granted
    assert took < 10, took


def test_R5_extend_zero_is_todays_path(tmp_path):
    seen: list = []

    def wrap(h):
        orig = h.backend.wait_idle

        def spy(*a, **kw):
            seen.append(dict(kw))
            return orig(*a, **kw)
        h.backend.wait_idle = spy

    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=0, turn_max_sec=60,
                      idle_event_timeout_sec=60)
    run, _ = _one(tmp_path, cfg, grow_for=5, wrap=wrap)
    assert run.state is AgentState.STALLED
    assert seen and all(kw.get("on_deadline") is None for kw in seen), seen
    assert run.last_error == "no idle after 1s"
    assert "extensions" not in run.turns[0]


def test_R5b_extend_zero_does_not_pass_on_deadline_at_all(tmp_path):
    """The ticket's letter: `on_deadline` is not passed at all (None is not absent)."""
    seen: list = []

    def wrap(h):
        orig = h.backend.wait_idle

        def spy(*a, **kw):
            seen.append(dict(kw))
            return orig(*a, **kw)
        h.backend.wait_idle = spy

    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=0, turn_max_sec=60,
                      idle_event_timeout_sec=60)
    _one(tmp_path, cfg, wrap=wrap)
    assert seen and all("on_deadline" not in kw for kw in seen), seen


def test_R6_two_variants_on_one_model_extend_on_their_own_churn(tmp_path):
    names = ("hy3-var1", "hy3-var2")
    sb = Sandbox(tmp_path, agents=names)
    specs = tuple(AgentSpec(name=n, provider_id="kenary", model_id="hy3:free") for n in names)
    cfg = dataclasses.replace(
        make_config(list(names), max_parallel=2, turn_timeout_sec=2, turn_extend_sec=2,
                    turn_max_sec=60, idle_event_timeout_sec=60), agents=specs)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        stop = _busy_turn(fake, scenario, grow_for=1.0, only="hy3-var1")
        state = run_round(cfg, ROUND, sb.ticket_path, list(sb.workspaces),
                          make_backend=_make_backend(fake, sb.out_dir), out_dir=sb.out_dir)
        stop.set()
    by = {r.agent.name: r for r in state.agents}
    assert by["hy3-var1"].turns[0].get("extensions"), by["hy3-var1"].turns
    assert "extensions" not in by["hy3-var2"].turns[0], by["hy3-var2"].turns
    assert by["hy3-var2"].state is AgentState.STALLED


# ── roster ──────────────────────────────────────────────────────────────────

def test_S1_defaults(tmp_path):
    cfg = load_roster(write_ini(tmp_path, MINIMAL))
    assert (cfg.turn_extend_sec, cfg.turn_max_sec) == (600, 7200)


def test_S2_keys_parse(tmp_path):
    text = add_to_contest(add_to_contest(MINIMAL, "turn_extend_sec = 120"), "turn_max_sec = 3600")
    cfg = load_roster(write_ini(tmp_path, text))
    assert (cfg.turn_extend_sec, cfg.turn_max_sec) == (120, 3600)


def test_S3_max_below_turn_timeout_names_both(tmp_path):
    text = add_to_contest(add_to_contest(MINIMAL, "turn_timeout_sec = 1800"), "turn_max_sec = 900")
    with pytest.raises(RosterError) as e:
        load_roster(write_ini(tmp_path, text))
    msg = str(e.value)
    assert "unknown key" not in msg, msg
    assert "turn_max_sec" in msg and "turn_timeout_sec" in msg


def test_S4_negative_is_rejected(tmp_path):
    with pytest.raises(RosterError) as e:
        load_roster(write_ini(tmp_path, add_to_contest(MINIMAL, "turn_extend_sec = -1")))
    assert "unknown key" not in str(e.value), str(e.value)


def test_S5_committed_ini_carries_both(tmp_path):
    text = (REPO_ROOT / "contest.ini").read_text(encoding="utf-8")
    assert re.search(r"^turn_extend_sec\s*=", text, re.M)
    assert re.search(r"^turn_max_sec\s*=", text, re.M)


# ── round 2: data that splits the top group ─────────────────────────────────

def test_R7_silence_after_an_extension_is_named_a_silence_stall(tmp_path):
    """Extended at 1 s to 6 s, then the stream goes quiet at ~1.4 s: the silence
    clock ends the turn at ~3.4 s — under the *extended* deadline, so it is a
    silence stall (`no event for 2s`, `stalled`), not `no idle after`."""
    cfg = make_config(["agent-a"], turn_timeout_sec=1, turn_extend_sec=5, turn_max_sec=60,
                      idle_event_timeout_sec=2)
    sb = Sandbox(tmp_path)
    scenario = {"turns": [{"events": ["busy"], "idle": False}]}
    with _BenchFake(scenario) as fake:
        stop = threading.Event()

        def on_prompt(d, t):
            fake.pulse(_session_of(fake, d).id, 0.2, 7)
            _grow(Path(d) / "pkg" / "thing.py", 0.8, stop)

        scenario["turns"][0]["on_prompt"] = on_prompt
        run = Harness(sb, fake, cfg).go()
        stop.set()
    assert run.state is AgentState.STALLED
    assert run.turns[0].get("extensions"), run.turns[0]
    assert run.turns[0]["idle_status"] == "stalled", run.turns[0]
    assert run.last_error == "no event for 2s", run.last_error
