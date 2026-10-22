"""tests/test_review49_audit_runtime_leftovers.py — CC review fixes 49: what the audit fixes broke at run time.

Reproduced on the three audit patches (AUDIT-1..23, MIMO-1..5, AUDIT3):

* AUDIT3-1 refused a reply that deleted or renamed a symbol the model WAS shown, on
  any file over ``max_file_chars`` — "remove the dead helper" could never land.
* AUDIT-5 took any live pid for the round's runner: a lock naming a pid since reused
  by another program refused every start as "already running" while
  ``round_alive`` said the round was not running.
* AUDIT-6 put the agent's bash in its own session and killed it on a timeout only:
  a Ctrl-C ended the agent and left its command running, with no timeout left.
* AUDIT-10 skipped everything under ``workspace_root``, so a root above ``base_dir``
  (its parent) mirrored nothing at all.
* MIMO-1 searched the whole appended kilo log, so an earlier attempt's EADDRINUSE
  made a later attempt's other failure look like a lost port and retried it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.arena import rounds
from tools.auto.coder import Coder
from tools.auto.executor import Executor
from tools.contest import kilo_client

REPO = Path(__file__).resolve().parents[1]


# ── AUDIT3-1: a symbol the model saw may go ────────────────────────────────

_DEAD = "def dead_helper(a, b):\n    return a - b\n"


def _module() -> str:
    funcs = "".join(f"def f{i}(x):\n    return x + {i}  # {'pad ' * 10}\n\n\n" for i in range(12))
    return "import os\n\n\n" + funcs + _DEAD


def _shown(tmp_path: Path, src: str, cited: str) -> Coder:
    (tmp_path / "m.py").write_text(src, encoding="utf-8")
    coder = Coder.__new__(Coder)
    coder._max_file_chars = 300
    view = coder._read_file_contents(["m.py"], tmp_path, cited_symbol=cited)
    assert "not included" in view and f"def {cited}(" in view, "the file must be shown in part"
    return coder


def test_a_shown_symbol_may_be_deleted(tmp_path):
    src = _module()
    coder = _shown(tmp_path, src, "dead_helper")
    assert coder._partial_view_error("m.py", src.replace(_DEAD, ""), tmp_path / "m.py") == ""


def test_a_shown_symbol_may_be_renamed(tmp_path):
    src = _module()
    coder = _shown(tmp_path, src, "f5")
    reply = src.replace("def f5(", "def f5_plus(")
    assert coder._partial_view_error("m.py", reply, tmp_path / "m.py") == ""


def test_a_symbol_the_model_never_saw_still_may_not_go(tmp_path):
    src = _module()
    coder = _shown(tmp_path, src, "f5")
    err = coder._partial_view_error("m.py", src.replace("def f11(", "def f11_gone("),
                                    tmp_path / "m.py")
    assert err.startswith("[SAFETY]") and "f11" in err


def test_dropping_a_large_shown_function_is_an_edit_not_a_loss(tmp_path):
    big = "def big(x):\n" + "".join(f"    x += {i}\n" for i in range(400)) + "    return x\n\n\n"
    rest = "".join(f"def g{i}():\n    return {i}\n\n\n" for i in range(3))
    src = "import os\n\n\n" + big + rest
    coder = _shown(tmp_path, src, "big")
    reply = src.replace(big, "")
    assert len(reply) * 2 < len(src), "the reply is under half the file"
    assert coder._partial_view_error("m.py", reply, tmp_path / "m.py") == ""


# ── AUDIT-5: a pid is the lock's owner only while it is a runner ───────────

def _locks(tmp_path: Path) -> Path:
    locks = tmp_path / ".arena" / "locks"
    locks.mkdir(parents=True)
    return locks / "7.pid"


def test_a_lock_naming_a_reused_pid_is_taken_over(tmp_path):
    lock = _locks(tmp_path)
    other = subprocess.Popen(["sleep", "30"])
    try:
        lock.write_text(str(other.pid), encoding="utf-8")
        assert rounds.round_alive(tmp_path, 7) is False
        assert rounds._claim_lock(lock) is True
        assert lock.read_text(encoding="utf-8") == str(os.getpid())
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize("words", [
    ["tools.contest", "run", "--ticket", "7"],      # the runner, after the pid swap
    ["tools.arena", "run", "start", "7"],           # the starter, before it
])
def test_a_live_runner_or_starter_still_holds_it(tmp_path, words):
    lock = _locks(tmp_path)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *words])
    try:
        lock.write_text(str(holder.pid), encoding="utf-8")
        assert rounds._claim_lock(lock) is False
        assert lock.read_text(encoding="utf-8") == str(holder.pid)
    finally:
        holder.kill()
        holder.wait()


def test_with_no_proc_a_live_pid_is_taken_at_its_word(tmp_path, monkeypatch):
    lock = _locks(tmp_path)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    other = subprocess.Popen(["sleep", "30"])
    try:
        lock.write_text(str(other.pid), encoding="utf-8")
        assert rounds._claim_lock(lock) is False
    finally:
        other.kill()
        other.wait()


# ── AUDIT-6: a Ctrl-C ends the agent's command too ─────────────────────────

def test_ctrl_c_in_the_agent_kills_its_command(tmp_path):
    pid_file = tmp_path / "bash.pid"
    code = (
        f"import sys; sys.path.insert(0, {str(REPO)!r})\n"
        "from tools.contest import backend\n"
        f"backend._run_bash_group('echo $$ > {pid_file}; sleep 30', {str(tmp_path)!r}, 60)\n"
    )
    agent = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bash_pid = None
    try:
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()):
            assert time.monotonic() < deadline, "the command never started"
            time.sleep(0.05)
        bash_pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(agent.pid, signal.SIGINT)
        agent.wait(timeout=30)
        # the agent kills and reaps the group before it exits: no race left here
        with pytest.raises(ProcessLookupError):
            os.kill(bash_pid, 0)
    finally:
        if agent.poll() is None:
            agent.kill()
            agent.wait()
        if bash_pid is not None:
            try:
                os.killpg(bash_pid, signal.SIGKILL)
            except OSError:
                pass


# ── AUDIT-10: a workspace root above the repo still mirrors it ─────────────

def test_a_workspace_root_above_the_repo_mirrors_the_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "setup.cfg").write_text("[x]\n", encoding="utf-8")
    workspace = Executor(repo, workspace_root=tmp_path)._prepare_workspace("T1", [])
    files = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file())
    assert files == ["pkg/a.py", "setup.cfg"]


# ── MIMO-1: only this attempt's log decides a lost port ────────────────────

def test_an_earlier_lost_port_does_not_retry_another_failure(tmp_path, monkeypatch):
    log = tmp_path / "kilo.log"
    calls = []

    def once(cls, binary, *, log_path, **kw):
        calls.append(1)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("Error: listen EADDRINUSE 127.0.0.1:4242\n" if len(calls) == 1
                     else "Error: invalid config\n")
        raise kilo_client.KiloServerError("exited", log_tail=kilo_client._log_tail(log_path))

    monkeypatch.setattr(kilo_client.KiloServer, "_spawn_once", classmethod(once))
    with pytest.raises(kilo_client.KiloServerError, match="invalid config"):
        kilo_client.KiloServer.spawn("kilo", log_path=str(log))
    assert len(calls) == 2, "the config error was retried as a lost port"
