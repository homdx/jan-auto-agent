"""The runner's abort stops the OpenRouter agent: an `abort` line or a closed stdin ends the turn, and SIGTERM kills its command (AGENT-ABORT, AGENT-TERM)."""
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.contest import backend

REPO = Path(__file__).resolve().parent.parent
BASH_CALL = {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "bash", "arguments": '{"command": "echo hi"}'}}]}


def _run_agent(monkeypatch, tmp_path, stdin_text):
    agent = backend._Agent("http://x.invalid/v1", "", "m", str(tmp_path), max_steps=5)
    calls = []

    def call():
        calls.append(1)
        return dict(BASH_CALL), {"input": 1, "output": 1}
    monkeypatch.setattr(agent, "_call", call)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = agent.run()
    return rc, len(calls), [json.loads(ln) for ln in out.getvalue().splitlines()]


@pytest.mark.parametrize("after_prompt", [json.dumps({"type": "abort"}) + "\n", ""])
def test_an_abort_or_a_closed_stdin_while_a_permission_waits_ends_the_turn(monkeypatch, tmp_path, after_prompt):
    stdin = json.dumps({"type": "prompt", "text": "go"}) + "\n" + after_prompt
    rc, calls, events = _run_agent(monkeypatch, tmp_path, stdin)
    assert rc == 0
    assert calls == 1                                                    # was 5: max_steps paid calls
    assert [e["type"] for e in events] == ["usage", "message", "permission.asked"]


def test_a_reply_still_runs_the_command_and_goes_on(monkeypatch, tmp_path):
    reply = {"type": "permission.reply", "reply": "reject", "message": "no"}
    stdin = json.dumps({"type": "prompt", "text": "go"}) + "\n" + (json.dumps(reply) + "\n") * 2
    rc, calls, events = _run_agent(monkeypatch, tmp_path, stdin)
    assert rc == 0 and calls == 3                                        # the third ask meets the end of stdin
    assert [e["state"]["output"] for e in events if e["type"] == "tool"] == ["no", "no"]


def _wait_for(pid_file):
    deadline = time.monotonic() + 30
    while not (pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()):
        assert time.monotonic() < deadline, "the command never started"
        time.sleep(0.05)
    return int(pid_file.read_text(encoding="utf-8").strip())


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    os.killpg(pid, signal.SIGKILL)
    return False


def test_the_runners_terminate_kills_the_command_the_agent_runs(tmp_path):
    pid_file = tmp_path / "bash.pid"
    code = (
        f"import sys; sys.path.insert(0, {str(REPO)!r})\n"
        "from tools.contest import backend\n"
        "call = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'c1', 'function': {\n"
        f"    'name': 'bash', 'arguments': __import__('json').dumps({{'command': 'echo $$ > {pid_file}; sleep 300'}})}}}}]}}\n"
        "backend._Agent._call = lambda self: (dict(call), {'input': 0, 'output': 0})\n"
        "backend._Agent._say_permission = lambda self, *a: ('once', '')\n"
        f"sys.exit(backend.agent_loop(['--agent-loop', '--base-url', 'http://x.invalid', '--model', 'm', "
        f"'--directory', {str(tmp_path)!r}]))\n"
    )
    agent = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    bash_pid = None
    try:
        agent.stdin.write(json.dumps({"type": "prompt", "text": "go"}) + "\n")
        agent.stdin.flush()
        bash_pid = _wait_for(pid_file)
        agent.terminate()                                                # what _terminate does on abort
        assert agent.wait(timeout=30) == 128 + signal.SIGTERM
        assert _gone(bash_pid)                                           # was left running
    finally:
        if agent.poll() is None:
            agent.kill()
            agent.wait()


def test_a_terminate_while_the_command_starts_still_kills_it(tmp_path):
    pid_file = tmp_path / "bash.pid"
    code = (
        f"import os, signal, sys, time; sys.path.insert(0, {str(REPO)!r})\n"
        "from tools.contest import backend\n"
        "signal.signal(signal.SIGTERM, backend._exit_on_sigterm)\n"
        "real = backend.subprocess.Popen\n"
        "def late(*a, **k):\n"
        "    proc = real(*a, **k)\n"
        f"    while not (os.path.exists({str(pid_file)!r}) and os.path.getsize({str(pid_file)!r})):\n"
        "        time.sleep(0.01)\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "    return proc\n"
        "backend.subprocess.Popen = late\n"
        f"backend._run_bash_group('echo $$ > {pid_file}; sleep 30', {str(tmp_path)!r}, 60)\n"
    )
    agent = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert agent.returncode == 128 + signal.SIGTERM, agent.stderr
    assert _gone(int(pid_file.read_text(encoding="utf-8").strip()))
