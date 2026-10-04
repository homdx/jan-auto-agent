"""tests/test_contest_kilo_tap_reconnect.py — round 151: the reconnect never leaves a dead tap, and the `.kilo` guards agree.

A `PATCH /config` on 7.6.2 reloads the directory's instance and ends its
`/event` stream, so the backend has to reopen the stream for the next turn to be
heard at all. Two families were left open:

  1. the reopen. `EventTap(...).start()` can raise (the log directory cannot be
     made, the log cannot be opened), and `_reconnect_tap` used to publish a
     replacement before it existed — the backend kept the stopped old tap and
     every later wait sat out its silence clock, reading a live stream as a
     silent model. Or `interrupt()` ran between "give up the old tap" and
     "publish the new one" and stopped only the old one, leaving a reader on a
     stream nobody owns.
  2. the guards. `tracked_kilo_files` refused a push if *anything* under
     `.kilo/` was tracked and failed open when `git` said nothing;
     `drop_stale_kilo_file` ran only after the server had already read the
     workspace's config, and deleted a `.kilo/kilo.jsonc` the agent had written
     on purpose; and a linked worktree's `--git-path info/exclude` is the
     repository's *common* file, so a push from a worktree wrote into the
     operator's own checkout.

Offline: `tests/_kilo_fake.py` on 127.0.0.1, real `git` in `tmp_path`, no live
provider config and no network beyond the loopback fake.
"""

from __future__ import annotations

import json
import logging
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

import test_contest_context_memory as ctm  # noqa: E402
import test_contest_overflow_wording as tow  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.contest import cli as cli_mod  # noqa: E402
from tools.contest.backend import (  # noqa: E402
    ContestBackendError,
    KiloBackend,
    KiloLimitRefused,
    KiloServer,
    KiloTapReconnectError,
    drop_stale_kilo_file,
    tracked_kilo_files,
)
from tools.contest.kilo_client import EventTap  # noqa: E402
from tools.contest.workspace import Workspace  # noqa: E402
import tools.contest.backend as backend_mod  # noqa: E402

#: The ``limit`` the runner pushes for a model remembered at 98 777 tokens —
#: `context_memory.kilo_limit(98_777, None)`, the shape `PATCH /config` sends.
PUSHED_LIMIT = {"context": 131_072, "input": 98_777, "output": 20_000}

#: `.kilo/kilo.jsonc` the agent writes on its own: no model limit in it, so no
#: content compare against a pushed window matches it.
AGENT_FILE = '{"provider": {"timeout": {"request": 600}}}\n'


def _git(repo: Path, *args: str) -> str:
    """One `git` in *repo*: its stdout, or an `AssertionError` carrying stderr."""
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert out.returncode == 0, f"git {' '.join(args)}: {out.stderr.strip()}"
    return out.stdout.strip()


def _repo(path: Path, tracked: dict | None = None) -> Path:
    """A git repository at *path* with one base commit.

    *tracked* is ``{relative path: text}`` committed into it — the checkout that
    already holds what the guards have to see.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    for relative, text in (tracked or {"README.md": "base\n"}).items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


def _ignored(repo: Path, relative: str) -> bool:
    """Whether `git` ignores *relative* in *repo* — 0 is ignored, 1 is not."""
    out = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", relative],
                         capture_output=True, text=True)
    return out.returncode == 0


def _backend(fake, directory: str, log: str) -> KiloBackend:
    """One `KiloBackend` over the fake, its tap connected."""
    backend = KiloBackend(KiloServer.attach(fake.url), directory, events_log=log)
    backend.wait_ready()
    return backend


# ─────────────────────────────────────────────────────────────────────────────
# A. the reconnect
# ─────────────────────────────────────────────────────────────────────────────

def test_a_failed_tap_start_is_retried_and_the_next_turn_is_heard(tmp_path, monkeypatch):
    """`EventTap.start` raising on the first attempt: the retry is what the
    backend publishes, so the next turn's events are heard and the size is
    pushed exactly once."""
    ws = _repo(tmp_path / "ws")
    with FakeKiloServer() as fake:
        backend = _backend(fake, str(ws), str(tmp_path / "out" / "events.jsonl"))
        old_tap = backend._tap

        class _FailsOnceEventTap(EventTap):
            """One construction raises, so only the retry produces a tap."""
            fails = 1

            def __init__(self, *args, **kwargs):
                if type(self).fails:
                    type(self).fails -= 1
                    raise OSError("simulated: the event stream will not open")
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(backend_mod, "EventTap", _FailsOnceEventTap)
        started = time.monotonic()
        backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)
        assert time.monotonic() - started < 15, "one retry, not a clock of settles"

        assert backend._tap is not old_tap, "the replacement is published"
        (patch,) = fake.calls("PATCH")
        assert (patch["method"], patch["path"]) == ("PATCH", "/config")
        assert patch["body"]["provider"]["kenary"]["models"]["agent-a:free"]["limit"] \
            == PUSHED_LIMIT

        # the replacement is a live reader: the next turn's events reach it
        session = backend.create_session("kenary", "agent-a:free", rules=[], title="t")
        backend.prompt(session, "go on")
        assert backend._tap.wait(
            lambda event: event.get("type") in ("session.created", "session.idle"), 5.0) is not None
        backend.close()


def test_a_reconnect_that_cannot_reopen_raises_and_keeps_the_live_tap(tmp_path, caplog):
    """Both start attempts failing: `KiloTapReconnectError` — the patch did land,
    so the caller must remember the window and keep its watch off — and the old
    tap is still the one the backend publishes. Its stream ended at the reload,
    but it was never stopped by us, so a caller waiting on it wakes at once with
    `closed` instead of sitting out its silence clock on a stream nobody reads."""
    caplog.set_level(logging.WARNING, logger="tools.contest.backend")
    ws = _repo(tmp_path / "ws")
    with FakeKiloServer() as fake:
        backend = _backend(fake, str(ws), str(tmp_path / "out" / "events.jsonl"))
        old_tap = backend._tap

        # the log the reconnect would write to is a directory: `open()` raises
        base, directory, _log = backend._tap_args
        hole = tmp_path / "hole"
        hole.mkdir()
        backend._tap_args = (base, directory, str(hole))

        with pytest.raises(KiloTapReconnectError, match="could not be reopened"):
            backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)

        assert isinstance(KiloTapReconnectError("x"), ContestBackendError), \
            "the runner catches one class for both"
        assert backend._tap is old_tap
        assert not old_tap._stop.is_set(), "the live tap was not stopped by us"
        assert old_tap.join(2.0) is True
        assert old_tap.events[-1]["type"] == "tap.closed"
        assert "stream ended" in old_tap.events[-1]["properties"]["error"]
        assert fake.calls("PATCH"), "the patch did land: only the stream is broken"
        attempts = [record.getMessage() for record in caplog.records
                    if "event tap start attempt" in record.getMessage()]
        assert len(attempts) == 2, "the start was retried once"

        session = backend.create_session("kenary", "agent-a:free", rules=[], title="t")
        started = time.monotonic()
        result = backend.wait_idle(session, 120.0, idle_event_timeout=None,
                                   on_permission=lambda event: ("once", ""),
                                   on_question=lambda event: None)
        assert time.monotonic() - started < 10
        assert result.status == "closed"
        backend.close()


def test_interrupt_racing_a_reconnect_leaves_no_tap_thread_running(tmp_path, caplog, monkeypatch):
    """A Ctrl-C that lands while the replacement exists but is not yet published:
    `interrupt` takes the same lock the swap holds, so it stops the tap the swap
    publishes — and nothing is left reading a stream nobody owns."""
    caplog.set_level(logging.WARNING, logger="tools.contest.backend")
    ws = _repo(tmp_path / "ws")
    with FakeKiloServer() as fake:
        backend = _backend(fake, str(ws), str(tmp_path / "out" / "events.jsonl"))
        old_tap = backend._tap
        session = backend.create_session("kenary", "agent-a:free", rules=[], title="t")

        class _HeldTap(EventTap):
            """The real tap, whose `wait` parks while the swap holds the lock."""
            _up = threading.Event()
            _release = threading.Event()

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)

            def wait(self, pred, timeout):
                if not type(self)._release.is_set():
                    type(self)._up.set()          # built, and not yet published
                    type(self)._release.wait(10.0)
                return super().wait(pred, timeout)

        monkeypatch.setattr(backend_mod, "EventTap", _HeldTap)
        interrupt_called = threading.Event()

        def release():
            assert interrupt_called.wait(10.0)
            _HeldTap._release.set()

        def reconnect():
            backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)

        threading.Thread(target=release, daemon=True).start()
        swap = threading.Thread(target=reconnect, daemon=True)
        swap.start()
        assert _HeldTap._up.wait(10.0), "the replacement exists before it is published"
        interrupt_called.set()
        backend.interrupt(session)      # blocks on the lock the swap is holding
        swap.join(10.0)
        assert not swap.is_alive()

        new_tap = backend._tap
        assert backend.interrupted() is True
        assert new_tap is not old_tap
        assert new_tap._stop.is_set() and new_tap.join(5.0) is True
        assert old_tap._stop.is_set() and old_tap.join(2.0) is True

        # the next wait does not sit out its silence clock on a stopped stream
        started = time.monotonic()
        result = backend.wait_idle(session, 120.0, idle_event_timeout=None,
                                   on_permission=lambda event: ("once", ""),
                                   on_question=lambda event: None)
        assert time.monotonic() - started < 10
        assert result.status == "closed"
        backend.close()


def test_an_injected_tap_is_never_rebuilt(tmp_path, monkeypatch):
    """A `tap=` the caller built is the caller's: `events_log` alone does not
    make the backend rebuild it, and the patch still goes out once."""
    constructions: list = []

    class _SpyEventTap(EventTap):
        def __init__(self, *args, **kwargs):
            constructions.append(args)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(backend_mod, "EventTap", _SpyEventTap)
    ws = _repo(tmp_path / "ws")
    with FakeKiloServer() as fake:
        log = str(tmp_path / "out" / "events.jsonl")
        injected = EventTap(fake.url, str(ws), log).start()
        backend = KiloBackend(KiloServer.attach(fake.url), str(ws), events_log=log,
                              tap=injected)
        assert backend._tap_args is None, "an injected tap leaves no arguments to rebuild"
        assert constructions == []

        backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)

        assert backend._tap is injected, "the caller's tap is the one still in place"
        assert constructions == [], "nothing was built over it"
        (patch,) = fake.calls("PATCH")
        assert patch["path"] == "/config"
        backend.close()


def test_a_reload_that_does_not_end_the_stream_bounds_the_reconnect(tmp_path, caplog):
    """A reload that keeps the stream open — the instance was not disposed, so
    neither of the reconnect's waits ever sees what it waits for: the two share
    one deadline instead of each taking `settle`, and both timeouts are logged."""
    caplog.set_level(logging.WARNING, logger="tools.contest.backend")
    ws = _repo(tmp_path / "ws")
    settle = 1.0
    with FakeKiloServer() as fake:
        backend = _backend(fake, str(ws), str(tmp_path / "out" / "events.jsonl"))
        old_tap = backend._tap
        # the caller's reload went out; this one did not dispose the instance
        backend._client.set_model_limit = lambda provider_id, model_id, limit: None

        started = time.monotonic()
        backend._reconnect_tap(settle=settle)
        elapsed = time.monotonic() - started

    assert elapsed < 3.0 * settle, f"the reconnect is bounded by one settle, got {elapsed:.1f}s"
    assert backend._tap is not old_tap, "a replacement is still published"
    assert old_tap._stop.is_set() and old_tap.join(2.0) is True
    messages = [record.getMessage() for record in caplog.records]
    assert any("did not end within" in message for message in messages)
    assert any("did not report" in message for message in messages)
    backend.close()


# ─────────────────────────────────────────────────────────────────────────────
# B. the `.kilo` guards
# ─────────────────────────────────────────────────────────────────────────────

def test_a_tracked_rules_file_does_not_stop_the_push(tmp_path):
    """A checkout that tracks `.kilo/rules/x.md` but no project file: only a
    tracked project file can land in the diff, so the push goes through — and
    the worktree is left clean of ours."""
    ws = _repo(tmp_path / "ws", {".kilo/rules/x.md": "always run the tests\n"})
    assert tracked_kilo_files(str(ws)) == [], "a rules file is not a project file"
    with FakeKiloServer() as fake:
        backend = _backend(fake, str(ws), str(tmp_path / "out" / "events.jsonl"))
        backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)
        assert _ignored(ws, ".kilo/kilo.jsonc") is True, "ours is ignored"
        assert _ignored(ws, ".kilo/rules/x.md") is False, "the agent's file stays committable"
        assert _git(ws, "status", "--porcelain") == "", "nothing of ours in the worktree"
        assert (ws / ".kilo" / ".gitignore").read_text(encoding="utf-8").splitlines() \
            == list(backend_mod._KILO_IGNORE_LINES)
        assert len(fake.calls("PATCH")) == 1
        backend.close()


def test_a_tracked_project_file_still_refuses_the_push(tmp_path, caplog):
    """A checkout that tracks `.kilo/kilo.jsonc`: an ignore rule would not help,
    so the push is refused, the file is untouched, and no patch is sent."""
    caplog.set_level(logging.WARNING, logger="tools.contest.backend")
    tracked = '{"provider": {"kenary": {"models": {"agent-a:free": {"limit": {"context": 300000}}}}}}\n'
    ws = _repo(tmp_path / "ws", {".kilo/kilo.jsonc": tracked})
    assert tracked_kilo_files(str(ws)) == [".kilo/kilo.jsonc"]
    with FakeKiloServer() as fake:
        backend = _backend(fake, str(ws), str(tmp_path / "out" / "events.jsonl"))
        with pytest.raises(KiloLimitRefused, match="tracks"):
            backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)
        assert fake.calls("PATCH") == []
        assert (ws / ".kilo" / "kilo.jsonc").read_text(encoding="utf-8") == tracked
        assert any("not handed to Kilo" in record.getMessage()
                   for record in caplog.records)
        backend.close()


@pytest.mark.parametrize("how", ["timeout", "refused"])
def test_a_push_is_refused_when_git_will_not_answer(tmp_path, how, monkeypatch):
    """`git` silent — a timeout, or a tree it refuses to read: refusing is the
    only safe answer, because a tracked project file would then be rewritten
    into the agent's diff unseen."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with FakeKiloServer() as fake:
        backend = _backend(fake, str(ws), str(tmp_path / "out" / "events.jsonl"))

        def run(*args, **kwargs):
            if how == "timeout":
                raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout") or 10.0)
            return subprocess.CompletedProcess(args[0], 128, stdout="",
                                               stderr="fatal: unable to read index")

        monkeypatch.setattr(backend_mod.subprocess, "run", run)
        with pytest.raises(KiloLimitRefused, match="git did not answer"):
            backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)
        assert fake.calls("PATCH") == [], "a push on a blind index is not sent"
        backend.close()


def test_a_stale_project_file_is_gone_before_the_server_is_spawned(tmp_path, monkeypatch):
    """The drop runs before `KiloServer.spawn`, not after the server has read
    the workspace's config and sized every resumed session by a window this
    round never chose."""
    ws = _repo(tmp_path / "wt" / "agent-a")
    project = ws / ".kilo" / "kilo.jsonc"
    project.parent.mkdir()
    project.write_text(AGENT_FILE, encoding="utf-8")

    seen: list = []

    def spawn(binary, *, log_path, **kwargs):
        seen.append(project.exists())
        return _NoServer()

    class _NoServer:
        base_url = "http://127.0.0.1:1"

    monkeypatch.setattr(cli_mod.KiloServer, "spawn", staticmethod(spawn))
    monkeypatch.setattr(cli_mod, "find_kilo_binary", lambda _binary: "/bin/true")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    workspaces = [Workspace(agent="agent-a", path=ws, branch="contest/151/agent-a",
                            base_sha="base", kind="worktree")]
    _server, _make_backend = cli_mod._make_backends(tr.make_config(["agent-a"]), out_dir,
                                                    workspaces=workspaces)
    assert seen == [False], "the stale file is gone before the server reads the workspace"
    assert not project.exists()


@pytest.mark.parametrize("content,kept", [
    # an agent-authored config: no model limit at all
    (AGENT_FILE, True),
    # a limit of its own, for the same model: a different number is not ours
    ('{"provider": {"kenary": {"models": {"agent-a:free": {"limit": {"context": 300000,'
     ' "input": 98777, "output": 20000}}}}}}\n', True),
    # jsonc comments, still the agent's
    ('// the agent\'s own config\n' + AGENT_FILE, True),
    # the window the run pushed, word for word: this one is ours
    ('{"provider": {"kenary": {"models": {"agent-a:free": {"limit": {"context": 131072,'
     ' "input": 98777, "output": 20000}}}}}}\n', False),
])
def test_only_the_file_the_run_pushed_is_dropped(tmp_path, content, kept):
    """`finish` drops by the limit the run pushed, not by "untracked": an
    untracked `.kilo/kilo.jsonc` is not this round's until its content says so."""
    ws = _repo(tmp_path / "ws")
    project = ws / ".kilo" / "kilo.jsonc"
    project.parent.mkdir()
    project.write_text(content, encoding="utf-8")

    assert drop_stale_kilo_file(str(ws), expected_limit=PUSHED_LIMIT) is (not kept)
    assert project.exists() is kept, "an agent-authored file reaches the harvest"
    if kept:
        assert project.read_text(encoding="utf-8") == content

    # a run that pushed nothing keeps every untracked file, unless it is a
    # leftover of an earlier attempt and says so
    project.unlink(missing_ok=True)
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(AGENT_FILE, encoding="utf-8")
    assert drop_stale_kilo_file(str(ws)) is False, "nothing was pushed: nothing is ours"
    assert project.exists()
    assert drop_stale_kilo_file(str(ws), assume_stale=True) is True, \
        "a pre-spawn drop has no window to compare against"
    assert not project.exists()


def test_the_agent_s_own_project_file_reaches_the_harvest(tmp_path):
    """End to end: the run pushes its remembered window and the agent writes its
    own `.kilo/kilo.jsonc` in the same turn. The push went to Kilo, and the
    agent's file is still in the worktree at the end of the run."""
    memory = tow._remembered(tmp_path)

    def agent_writes_it(directory, text):
        worktree = Path(directory)
        (worktree / ".kilo").mkdir(parents=True, exist_ok=True)
        (worktree / ".kilo" / "kilo.jsonc").write_text(AGENT_FILE, encoding="utf-8")
        tr.work_ready(directory, text)

    scenario = {"turns": [{"on_prompt": agent_writes_it, "events": ["busy", "idle"]}]}
    config = tow._declared(ctm._config(tmp_path, memory=memory))
    sb, fake, _harness, run, _aborted = tr._run_one(tmp_path, scenario, config)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(tow._patches(fake)) == 1, "the remembered window was handed to Kilo"
    project = sb.ws("agent-a").path / ".kilo" / "kilo.jsonc"
    assert project.read_text(encoding="utf-8") == AGENT_FILE, \
        "the agent's own file survives to the harvest"


def test_a_push_from_a_linked_worktree_leaves_the_repository_s_exclude_alone(tmp_path):
    """A linked worktree's `--git-path info/exclude` is the repository's common
    file — every worktree's, the operator's own checkout included. A push from a
    worktree leaves it byte-identical, and still hides its own file here."""
    root = _repo(tmp_path / "repo")
    exclude = root / ".git" / "info" / "exclude"
    before = exclude.read_bytes() if exclude.is_file() else b""
    worktree = tmp_path / "wt" / "agent-a"
    _git(root, "worktree", "add", "-q", "-b", "contest/151/agent-a", str(worktree), "HEAD")

    with FakeKiloServer() as fake:
        backend = _backend(fake, str(worktree), str(tmp_path / "out" / "events.jsonl"))
        backend.set_model_limit("kenary", "agent-a:free", PUSHED_LIMIT)
        assert _ignored(worktree, ".kilo/kilo.jsonc") is True, "the push is still hidden here"
        assert _ignored(worktree, ".kilo/rules/x.md") is False
        assert len(fake.calls("PATCH")) == 1
        backend.close()

    assert (exclude.read_bytes() if exclude.is_file() else b"") == before, \
        "the repository's shared exclude is ours for no one"
    # and the worktree's own tree: only the ignore file, which hides itself
    assert _git(worktree, "status", "--porcelain") == ""


# ─────────────────────────────────────────────────────────────────────────────
# Kilo's own `.kilo/.gitignore` (judge, round 151)
# ─────────────────────────────────────────────────────────────────────────────

#: What 7.6.2 writes, byte for byte, when it opens a workspace whose `.kilo/` has
#: no `.gitignore` — checked live: no trailing newline, and neither project file.
KILO_OWN_GITIGNORE = ("node_modules\npackage.json\npackage-lock.json\npnpm-lock.yaml\n"
                      "bun.lock\nyarn.lock\n.gitignore\nagent-manager.json")


def test_kilo_s_own_ignore_file_is_extended_and_the_push_stays_hidden(tmp_path):
    """The agent made `.kilo/rules/` and Kilo wrote its own `.kilo/.gitignore`
    there before the push. That file hides itself and is not tracked, so the
    project names are appended to it: Kilo's lines stay, and the file the
    patch writes is out of `git status`."""
    repo = _repo(tmp_path / "repo")
    (repo / ".kilo" / "rules").mkdir(parents=True)
    (repo / ".kilo" / "rules" / "x.md").write_text("# rule\n", encoding="utf-8")
    ignore = repo / ".kilo" / ".gitignore"
    ignore.write_text(KILO_OWN_GITIGNORE, encoding="utf-8")

    backend_mod._exclude_kilo_dir(str(repo))
    (repo / ".kilo" / "kilo.jsonc").write_text("{}\n", encoding="utf-8")

    lines = ignore.read_text(encoding="utf-8").splitlines()
    assert lines[:8] == KILO_OWN_GITIGNORE.splitlines()
    assert "kilo.jsonc" in lines and "kilo.json" in lines
    assert _ignored(repo, ".kilo/kilo.jsonc") is True
    assert _ignored(repo, ".kilo/rules/x.md") is False
    assert _git(repo, "status", "--porcelain", "-uall") == "?? .kilo/rules/x.md"


def test_a_tracked_ignore_file_of_the_agent_s_is_never_edited(tmp_path, caplog):
    """A `.kilo/.gitignore` the checkout tracks would put any edit in the diff,
    even one that lists itself: it is left byte-identical and warned about."""
    caplog.set_level(logging.WARNING, logger="tools.contest.backend")
    repo = _repo(tmp_path / "repo")
    (repo / ".kilo").mkdir()
    (repo / ".kilo" / ".gitignore").write_text("node_modules\n.gitignore\n", encoding="utf-8")
    _git(repo, "add", "-f", ".kilo/.gitignore")      # it lists itself: only -f tracks it
    _git(repo, "commit", "-q", "-m", "the agent's ignore file")
    before = (repo / ".kilo" / ".gitignore").read_bytes()

    backend_mod._exclude_kilo_dir(str(repo))

    assert (repo / ".kilo" / ".gitignore").read_bytes() == before
    assert any("agent's" in record.getMessage() for record in caplog.records)


def test_the_ignore_is_written_again_after_the_drop_in_the_same_process(tmp_path):
    """A relay's next leg runs in the same process and the same worktrees: the
    drop at the end of one leg takes the ignore file with it, so the next push
    writes it again instead of trusting what it did the first time."""
    repo = _repo(tmp_path / "repo")
    backend_mod._exclude_kilo_dir(str(repo))
    project = repo / ".kilo" / "kilo.jsonc"
    project.write_text(json.dumps({"provider": {"kenary": {"models": {
        "agent-a:free": {"limit": PUSHED_LIMIT}}}}}), encoding="utf-8")
    assert drop_stale_kilo_file(str(repo), PUSHED_LIMIT) is True
    assert not (repo / ".kilo").exists()

    backend_mod._exclude_kilo_dir(str(repo))
    project.write_text("{}\n", encoding="utf-8")
    assert _ignored(repo, ".kilo/kilo.jsonc") is True
    assert _git(repo, "status", "--porcelain", "-uall") == ""


def test_the_ignore_is_written_again_after_the_agent_removed_kilo(tmp_path):
    """The agent's own `git clean -fdx` (or `rm -rf .kilo`) between two pushes:
    no drop ran, and the second push still hides the file it makes."""
    import shutil
    repo = _repo(tmp_path / "repo")
    backend_mod._exclude_kilo_dir(str(repo))
    shutil.rmtree(repo / ".kilo")
    backend_mod._exclude_kilo_dir(str(repo))
    (repo / ".kilo" / "kilo.jsonc").write_text("{}\n", encoding="utf-8")
    assert _ignored(repo, ".kilo/kilo.jsonc") is True
    assert _git(repo, "status", "--porcelain", "-uall") == ""
