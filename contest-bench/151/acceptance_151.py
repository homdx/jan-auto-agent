"""Judge's acceptance suite for round 151, written from the ticket alone.

The event-tap reconnect after `PATCH /config` (part A) and the `.kilo` project
file guards (part B), checked by behaviour only: `KiloBackend` over the
repository's own `tests/_kilo_fake.FakeKiloServer`, the runner through
`tests/test_contest_runner._run_one`, `--resume` through
`tests/test_contest_cli`'s sandbox. No private helper of any entry is named;
the one private attribute read is `KiloBackend._tap`, which the ticket names.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/151/acceptance_151.py -n 0 -q
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

import sys

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_cli as tcli  # noqa: E402
import test_contest_context_memory as ctm  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.contest import backend as backend_mod  # noqa: E402
from tools.contest import cli as contest_cli  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402
from tools.contest import kilo_client  # noqa: E402
from tools.contest.backend import (ContestBackendError, KiloBackend,  # noqa: E402
                                   KiloLimitRefused, drop_stale_kilo_file)
from tools.contest.kilo_client import EventTap, KiloServer  # noqa: E402
from tools.contest.runner import AgentRun, AgentState, RoundState  # noqa: E402
from tools.contest.workspace import prepare_round  # noqa: E402

pytestmark = pytest.mark.xdist_group("bench151")

DECLARED = 131_072
REFUSED_AT = 98_777
LIMIT = {"context": 98_777, "input": 79_021, "output": 8_192}


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _git(cwd, *args, check=True) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                         check=check)
    return out.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "b@b")
    _git(path, "config", "user.name", "b")
    (path / "README").write_text("x\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _track(repo: Path, rel: str, text: str = '{"provider": {}}\n') -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"track {rel}")


def _patches(fake) -> list:
    return [r for r in fake.calls("PATCH") if r["path"] == "/config"]


class _Live:
    """A fake Kilo and one `KiloBackend` on *directory*, closed on exit."""

    def __init__(self, tmp_path: Path, directory: Path, **kw) -> None:
        self.fake = FakeKiloServer(directory=str(directory)).start()
        self.server = KiloServer.attach(self.fake.url)
        self.log = tmp_path / "out" / "events.jsonl"
        before = self.fake.subscribers
        self.backend = KiloBackend(self.server, str(directory), events_log=str(self.log), **kw)
        deadline = time.monotonic() + 10
        while self.fake.subscribers <= before and time.monotonic() < deadline:
            time.sleep(0.02)

    def close(self) -> None:
        try:
            self.backend.close()
        finally:
            self.fake.stop()


@pytest.fixture
def live(tmp_path):
    made = []

    def make(directory: Path, **kw) -> _Live:
        one = _Live(tmp_path, directory, **kw)
        made.append(one)
        return one

    yield make
    for one in made:
        one.close()


def _tap_alive(tap) -> bool:
    thread = getattr(tap, "_thread", None)
    return thread is not None and thread.is_alive() and not tap._stop.is_set()


def _hears(fake, tap, timeout: float = 5.0) -> bool:
    """Whether *tap* receives an event the fake emits now."""
    marker = f"bench.ping.{time.monotonic_ns()}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fake._emit({"type": marker, "properties": {}})
        got = tap.wait(lambda e: e.get("type") == marker, 0.3)
        if got is not None:
            return True
    return False


def _failing_tap_class(fail_on: set, created: list):
    """An `EventTap` whose N-th construction's `start` raises (1-based, counting
    only taps made after this is installed)."""

    class FlakyTap(EventTap):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)
            self._n = len(created)

        def start(self):
            if self._n in fail_on or "all" in fail_on:
                raise OSError(f"bench: tap start {self._n} refused")
            return super().start()

    return FlakyTap


def _install_tap(monkeypatch, cls) -> None:
    monkeypatch.setattr(backend_mod, "EventTap", cls)
    monkeypatch.setattr(kilo_client, "EventTap", cls)


# ─────────────────────────────────────────────────────────────────────────────
# A1. a failing start inside the reconnect never leaves a dead tap
# ─────────────────────────────────────────────────────────────────────────────

def test_a1_one_failing_start_still_leaves_a_working_tap_or_a_backend_error(tmp_path, live,
                                                                             monkeypatch):
    repo = _repo(tmp_path / "repo")
    one = live(repo)
    created: list = []
    _install_tap(monkeypatch, _failing_tap_class({1}, created))
    try:
        one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    except ContestBackendError:
        return
    assert len(_patches(one.fake)) == 1
    tap = one.backend._tap
    assert _tap_alive(tap), "the backend kept a stopped tap after a failed start"
    assert _hears(one.fake, tap), "the tap after the reload hears nothing"


def test_a1_a_start_that_always_fails_is_a_backend_error_not_an_oserror(tmp_path, live,
                                                                        monkeypatch):
    repo = _repo(tmp_path / "repo")
    one = live(repo)
    created: list = []
    _install_tap(monkeypatch, _failing_tap_class({"all"}, created))
    with pytest.raises(ContestBackendError):
        one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    assert len(_patches(one.fake)) == 1, "the PATCH went out before the reconnect"


def test_a1_a_normal_reconnect_hears_the_reloaded_stream(tmp_path, live):
    repo = _repo(tmp_path / "repo")
    one = live(repo)
    old = one.backend._tap
    one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    tap = one.backend._tap
    assert tap is not old
    assert _tap_alive(tap)
    assert not _tap_alive(old)
    assert _hears(one.fake, tap)


def _remembered(tmp_path) -> Path:
    return ctm._memory(tmp_path, limit=None, last_ok=REFUSED_AT, prompt=None)


def _declared(config):
    return replace(config, agents=tuple(replace(a, context_limit=DECLARED)
                                        for a in config.agents))


def test_a1_runner_a_flaky_reconnect_is_one_patch_and_a_ready_run(tmp_path, monkeypatch):
    """The reconnect's first start fails, the retry (or a kept live tap) carries
    the run: READY, one PATCH for the one size."""
    created: list = []
    # tap 1 is the harness's own backend tap; tap 2 is the reconnect's first try
    _install_tap(monkeypatch, _failing_tap_class({2}, created))
    memory = _remembered(tmp_path)
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    config = _declared(ctm._config(tmp_path, memory=memory, max_continues_per_attempt=3))
    started = time.monotonic()
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario, config)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1
    assert time.monotonic() - started < 60, "the turn after the reload was heard"


def test_a1_runner_a_patch_that_answered_200_is_never_repeated(tmp_path, monkeypatch, caplog):
    """Every reconnect start fails: the PATCH did go over, so the size is
    marked pushed (one PATCH whatever happens next) and the failure is not
    reported as "not handed to Kilo"."""
    caplog.set_level(logging.INFO)
    # the harness's own tap must start: let the first start through
    calls = {"n": 0}

    class FirstOk(EventTap):
        def start(self):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("bench: reconnect refused")
            return super().start()

    _install_tap(monkeypatch, FirstOk)
    memory = _remembered(tmp_path)
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    config = _declared(ctm._config(tmp_path, memory=memory, max_continues_per_attempt=3,
                                   idle_event_timeout_sec=20))
    _sb, fake, _h, _run, _ = tr._run_one(tmp_path, scenario, config)
    assert len(_patches(fake)) == 1, "a window Kilo holds was PATCHed again"
    assert not any("not handed to Kilo" in r.getMessage() for r in caplog.records), \
        "a failed reconnect was reported as a window that never went over"


# ─────────────────────────────────────────────────────────────────────────────
# A2. interrupt() racing the reconnect leaves no tap thread running
# ─────────────────────────────────────────────────────────────────────────────

def test_a2_interrupt_mid_reconnect_leaves_no_live_tap(tmp_path, live, monkeypatch):
    repo = _repo(tmp_path / "repo")
    one = live(repo)
    first = one.backend._tap
    interrupting = threading.Event()
    created: list = []

    class SlowTap(EventTap):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

        def start(self):
            # the barrier: the reconnect's new tap starts only once interrupt()
            # is on its way, so the swap and the stop overlap
            interrupting.wait(2.0)
            time.sleep(0.3)
            return super().start()

    _install_tap(monkeypatch, SlowTap)
    errors: list = []

    def reconnect():
        try:
            one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=reconnect, daemon=True)
    t.start()
    time.sleep(0.3)
    interrupting.set()
    one.backend.interrupt()
    t.join(20)
    assert not t.is_alive(), "the reconnect hangs after an interrupt"
    assert one.backend.interrupted()
    for tap in [first, *created]:
        thread = getattr(tap, "_thread", None)
        if thread is not None:
            thread.join(3.0)
            assert not thread.is_alive(), "a tap thread outlived the interrupt"


# ─────────────────────────────────────────────────────────────────────────────
# A3. an injected tap is never rebuilt
# ─────────────────────────────────────────────────────────────────────────────

class _StubClient:
    def __init__(self) -> None:
        self.limits: list = []

    def set_model_limit(self, provider_id, model_id, limit):
        self.limits.append((provider_id, model_id, dict(limit)))


class _DummyTap:
    def __init__(self) -> None:
        self.stopped = False

    def wait(self, predicate, timeout):
        return None

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        return True

    def mark(self):
        return 0


def test_a3_an_injected_tap_with_events_log_is_never_rebuilt(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")

    class NoTaps(EventTap):
        def __init__(self, *a, **kw):
            raise AssertionError("a tap was built over the injected one")

    _install_tap(monkeypatch, NoTaps)
    tap = _DummyTap()
    client = _StubClient()
    # a server with no base_url at all: nothing may read it when tap= is given
    backend = KiloBackend(object(), str(repo), events_log=str(tmp_path / "e.jsonl"),
                          client=client, tap=tap)
    backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    assert client.limits, "the limit was not sent"
    assert backend._tap is tap
    assert not tap.stopped


# ─────────────────────────────────────────────────────────────────────────────
# A4. a reload that does not end the stream: bounded and logged
# ─────────────────────────────────────────────────────────────────────────────

def test_a4_a_reload_that_keeps_the_stream_is_bounded_and_logged(tmp_path, live, caplog):
    caplog.set_level(logging.DEBUG)
    repo = _repo(tmp_path / "repo")
    client = _StubClient()       # no PATCH reaches the fake: the stream never ends
    one = live(repo, client=client)
    started = time.monotonic()
    try:
        one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    except ContestBackendError:
        pass
    took = time.monotonic() - started
    assert client.limits
    assert took < 8.0, f"the reconnect took {took:.1f}s — not one bounded deadline"
    assert any(r.levelno >= logging.WARNING and r.name.startswith("tools.contest")
               for r in caplog.records), "the timeout was not logged"
    assert _tap_alive(one.backend._tap)


# ─────────────────────────────────────────────────────────────────────────────
# B5. only the two project files block the push
# ─────────────────────────────────────────────────────────────────────────────

def test_b5_a_tracked_rule_file_does_not_block_the_push(tmp_path, live):
    repo = _repo(tmp_path / "repo")
    _track(repo, ".kilo/rules/x.md", "# a rule\n")
    one = live(repo)
    one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    assert len(_patches(one.fake)) == 1


@pytest.mark.parametrize("name", [".kilo/kilo.jsonc", ".kilo/kilo.json"])
def test_b5_a_tracked_project_file_refuses_the_push(tmp_path, live, name):
    repo = _repo(tmp_path / "repo")
    _track(repo, name)
    one = live(repo)
    with pytest.raises(KiloLimitRefused):
        one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    assert _patches(one.fake) == []
    assert (repo / name).read_text(encoding="utf-8") == '{"provider": {}}\n'


def test_b5_tracked_kilo_files_names_no_rule_file(tmp_path):
    repo = _repo(tmp_path / "repo")
    _track(repo, ".kilo/rules/x.md", "# a rule\n")
    from tools.contest.backend import tracked_kilo_files
    assert not [p for p in tracked_kilo_files(str(repo)) if "rules" in str(p)]


# ─────────────────────────────────────────────────────────────────────────────
# B6. git that does not answer fails closed
# ─────────────────────────────────────────────────────────────────────────────

def _break_git(monkeypatch, how: str) -> None:
    real = subprocess.run

    def run(cmd, *a, **kw):
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
        if argv and str(argv[0]).endswith("git") and "ls-files" in argv:
            if how == "timeout":
                raise subprocess.TimeoutExpired(argv, kw.get("timeout") or 1)
            if how == "oserror":
                raise OSError("bench: no git")
            return subprocess.CompletedProcess(argv, 128, "", "fatal: index file corrupt\n")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


@pytest.mark.parametrize("how", ["timeout", "oserror", "fatal"])
def test_b6_git_that_does_not_answer_refuses_the_push(tmp_path, live, monkeypatch, how):
    repo = _repo(tmp_path / "repo")
    one = live(repo)
    _break_git(monkeypatch, how)
    with pytest.raises(KiloLimitRefused):
        one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    assert _patches(one.fake) == []


@pytest.mark.parametrize("how", ["timeout", "fatal"])
def test_b6_git_that_does_not_answer_deletes_nothing(tmp_path, monkeypatch, how):
    repo = _repo(tmp_path / "repo")
    target = repo / ".kilo" / "kilo.jsonc"
    target.parent.mkdir()
    target.write_text(json.dumps({"provider": {"kenary": {"models": {
        "m:free": {"limit": LIMIT}}}}}), encoding="utf-8")
    _break_git(monkeypatch, how)
    drop_stale_kilo_file(str(repo))
    assert target.is_file()


# ─────────────────────────────────────────────────────────────────────────────
# B7. --resume: the stale file is gone before KiloServer.spawn
# ─────────────────────────────────────────────────────────────────────────────

def test_b7_resume_drops_the_stale_file_before_the_server_spawns(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTEST_GATE_API_KEY", "unset-for-the-test")
    sb = tcli.Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    config = sb.config()
    prepared = {ws.agent: ws for ws in prepare_round(sb.repo, config, tcli.ROUND, "HEAD")}
    a, b = prepared["agent-a"], prepared["agent-b"]
    tcli._write(a.path / "pkg" / "thing.py", tcli.THING_CHANGED)
    tcli._git(a.path, "add", "-A")
    tcli._git(a.path, "commit", "-q", "-m", "KC-16: thing")
    commit = tcli._git(a.path, "rev-parse", "HEAD")
    spec_a = next(s for s in config.agents if s.name == "agent-a")
    spec_b = next(s for s in config.agents if s.name == "agent-b")
    stale = Path(b.path) / ".kilo" / "kilo.jsonc"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(json.dumps({"provider": {spec_b.provider_id: {"models": {
        spec_b.model_id: {"limit": LIMIT}}}}}, indent=2) + "\n", encoding="utf-8")
    prior = RoundState(round_no=tcli.ROUND, ticket=tcli.TICKET_01, base_sha=sb.base,
                       started_at=1.0,
                       agents=[AgentRun(agent=spec_a, workspace=a, state=AgentState.READY,
                                        commit=commit),
                               AgentRun(agent=spec_b, workspace=b, state=AgentState.WAITING,
                                        session_id="ses_gone")])
    tcli._write(sb.out() / "state.json", json.dumps(prior.to_dict()))

    seen: list = []
    holder: list = []

    def spawn(binary, *, log_path, **kwargs):
        # the intake's throwaway offer-check server (`_offer_server`, a
        # `kilo-offer-*` temp log) opens no workspace; the round's server is
        # the one that must find the workspace clean
        if "kilo-offer-" not in Path(log_path).name:
            seen.append(stale.exists())
        return KiloServer.attach(holder[-1].url)

    monkeypatch.setattr(contest_cli.KiloServer, "spawn", staticmethod(spawn))
    with FakeKiloServer({"turns": [{"on_prompt": tr.work_ready,
                                    "events": ["busy", "idle"]}]}) as fake:
        holder.append(fake)
        contest_cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests", "--resume"])
    assert seen, "the round's server was never spawned"
    assert seen[0] is False, "the stale project file was still there at spawn"


# ─────────────────────────────────────────────────────────────────────────────
# B8. an agent-authored project file survives to the harvest
# ─────────────────────────────────────────────────────────────────────────────

AGENT_FILE = '{\n  // the agent\'s own\n  "permission": {"bash": "ask"}\n}\n'


def _write_own_and_finish(directory, text):
    target = Path(directory) / ".kilo" / "kilo.jsonc"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(AGENT_FILE, encoding="utf-8")
    tr.work_ready(directory, text)


def test_b8_an_agent_file_survives_a_run_with_no_push(tmp_path):
    scenario = {"turns": [{"on_prompt": _write_own_and_finish, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario,
                                       _declared(ctm._config(tmp_path)))
    tr._assert_ready(run, sb.ws("agent-a"))
    assert _patches(fake) == []
    target = sb.ws("agent-a").path / ".kilo" / "kilo.jsonc"
    assert target.is_file() and target.read_text(encoding="utf-8") == AGENT_FILE


def test_b8_an_agent_file_survives_a_run_that_pushed(tmp_path):
    memory = _remembered(tmp_path)
    scenario = {"turns": [{"on_prompt": _write_own_and_finish, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = tr._run_one(
        tmp_path, scenario, _declared(ctm._config(tmp_path, memory=memory)))
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1
    target = sb.ws("agent-a").path / ".kilo" / "kilo.jsonc"
    assert target.is_file() and target.read_text(encoding="utf-8") == AGENT_FILE


# ─────────────────────────────────────────────────────────────────────────────
# B9. a push from a linked worktree leaves the main exclude alone
# ─────────────────────────────────────────────────────────────────────────────

def _common_exclude(repo: Path) -> Path:
    return repo / ".git" / "info" / "exclude"


def test_b9_a_worktree_push_leaves_the_main_exclude_byte_identical(tmp_path, live):
    main = _repo(tmp_path / "main")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "agent", str(wt))
    exclude = _common_exclude(main)
    before = exclude.read_bytes() if exclude.exists() else None
    one = live(wt)
    one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    assert len(_patches(one.fake)) == 1
    after = exclude.read_bytes() if exclude.exists() else None
    assert after == before, "the operator's main info/exclude was edited"
    assert _git(main, "status", "--porcelain") == ""


# ─────────────────────────────────────────────────────────────────────────────
# regressions: what 147–149 already did keeps working
# ─────────────────────────────────────────────────────────────────────────────

def test_r_the_written_project_file_stays_out_of_git_status(tmp_path, live):
    repo = _repo(tmp_path / "repo")
    one = live(repo)
    one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    target = repo / ".kilo" / "kilo.jsonc"      # what Kilo writes on the patch
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({"provider": {"kenary": {"models": {
        "m:free": {"limit": LIMIT}}}}}, indent=2), encoding="utf-8")
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_r_a_tracked_project_file_is_never_deleted(tmp_path):
    repo = _repo(tmp_path / "repo")
    _track(repo, ".kilo/kilo.jsonc")
    assert drop_stale_kilo_file(str(repo)) is False
    assert (repo / ".kilo" / "kilo.jsonc").is_file()


def test_r_one_patch_per_size_over_several_prompts(tmp_path):
    memory = _remembered(tmp_path)
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = tr._run_one(
        tmp_path, scenario,
        _declared(ctm._config(tmp_path, memory=memory, max_continues_per_attempt=4)))
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1


def test_r_a_runner_pushed_file_is_gone_after_the_run(tmp_path):
    """The file the runner's own push produced does not outlive the run."""
    memory = _remembered(tmp_path)
    written: list = []

    def kilo_writes_then_work(directory, text):
        # what Kilo does on the PATCH, with the runner's own numbers
        limit = cm.kilo_limit(REFUSED_AT, None, cm.DEFAULT_COMPACT_AT_PERCENT)
        target = Path(directory) / ".kilo" / "kilo.jsonc"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"provider": {"kenary": {"models": {
            "agent-a:free": {"limit": limit}}}}}, indent=2) + "\n", encoding="utf-8")
        written.append(target)
        tr.work_ready(directory, text)

    scenario = {"turns": [{"on_prompt": kilo_writes_then_work, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = tr._run_one(
        tmp_path, scenario, _declared(ctm._config(tmp_path, memory=memory)))
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_patches(fake)) == 1
    assert written and not written[0].exists()


# ─────────────────────────────────────────────────────────────────────────────
# B10/B11. the project file stays out of the diff next to what Kilo itself writes
# (live, 7.6.2: Kilo writes its own `.kilo/.gitignore`, no project names in it,
# into a `.kilo/` that has none, and never rewrites one)
# ─────────────────────────────────────────────────────────────────────────────

KILO_OWN_GITIGNORE = ("node_modules\npackage.json\npackage-lock.json\npnpm-lock.yaml\n"
                      "bun.lock\nyarn.lock\n.gitignore\nagent-manager.json")


def _kilo_writes(directory: Path) -> None:
    target = directory / ".kilo" / "kilo.jsonc"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"provider": {"kenary": {"models": {
        "m:free": {"limit": LIMIT}}}}}, indent=2), encoding="utf-8")


def test_b10_kilo_s_own_ignore_file_does_not_let_the_push_show(tmp_path, live):
    repo = _repo(tmp_path / "repo")
    (repo / ".kilo" / "rules").mkdir(parents=True)
    (repo / ".kilo" / "rules" / "x.md").write_text("# rule\n", encoding="utf-8")
    (repo / ".kilo" / ".gitignore").write_text(KILO_OWN_GITIGNORE, encoding="utf-8")
    one = live(repo)
    one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    _kilo_writes(repo)
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    assert "kilo.jsonc" not in status, status


def test_b10w_in_a_linked_worktree_too(tmp_path, live):
    """The rounds run in linked worktrees, where Kilo's own `.kilo/.gitignore`
    is just as likely and git's exclude is the operator's shared file."""
    main = _repo(tmp_path / "main")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "agent", str(wt))
    (wt / ".kilo" / "rules").mkdir(parents=True)
    (wt / ".kilo" / "rules" / "x.md").write_text("# rule\n", encoding="utf-8")
    (wt / ".kilo" / ".gitignore").write_text(KILO_OWN_GITIGNORE, encoding="utf-8")
    one = live(wt)
    one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    _kilo_writes(wt)
    status = _git(wt, "status", "--porcelain", "--untracked-files=all")
    assert "kilo.jsonc" not in status, status


def test_b11_a_second_push_after_the_kilo_dir_went_is_hidden_again(tmp_path, live):
    import shutil
    repo = _repo(tmp_path / "repo")
    one = live(repo)
    one.backend.set_model_limit("kenary", "m:free", dict(LIMIT))
    shutil.rmtree(repo / ".kilo", ignore_errors=True)     # the drop at the end of a leg
    two = live(repo)
    two.backend.set_model_limit("kenary", "m:free", {**LIMIT, "context": 90_000})
    _kilo_writes(repo)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
