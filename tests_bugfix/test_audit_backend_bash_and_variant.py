"""tests_bugfix/test_audit_backend_bash_and_variant.py — AUDIT-6, AUDIT-18.

AUDIT-6: the openrouter agent's bash tool used subprocess.run(capture_output,
timeout). A background child kept the pipes open, so the call waited for that
child — past the timeout, or with no timeout hit at all.

AUDIT-18: the session's reasoning variant was kept on the SessionRef only and
never reached the model request.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import time
from unittest.mock import patch

import pytest

from tools.contest import backend


# ── AUDIT-6 ─────────────────────────────────────────────────────────────────

def test_a_background_child_does_not_hold_the_call(tmp_path):
    t0 = time.monotonic()
    code, out, err = backend._run_bash_group(
        "echo hi; sleep 30 & echo $! > child.pid", str(tmp_path), 20)
    try:
        assert time.monotonic() - t0 < 10, "the call waited for the background child"
        assert code == 0 and out.strip() == "hi"
    finally:
        # the child outlives the call by design; the test does not leave it behind
        # (CC review fixes 49: each run used to leave a `sleep 30` running)
        try:
            os.kill(int((tmp_path / "child.pid").read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_timeout_kills_the_whole_group_and_returns_on_time(tmp_path):
    marker = tmp_path / "survived"
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        backend._run_bash_group(f"(sleep 3; touch {marker}) & sleep 30", str(tmp_path), 1)
    assert time.monotonic() - t0 < 8
    time.sleep(3.5)
    assert not marker.exists(), "a child of the timed-out command kept running"


def test_tool_result_reports_output_exit_and_timeout(tmp_path, monkeypatch):
    agent = backend._Agent("http://x", "", "m", str(tmp_path))
    monkeypatch.setattr(agent, "_say_permission", lambda *a, **k: ("once", ""))
    status, text = agent._tool_result("bash", "c1", {"command": "echo out; echo err >&2; exit 3"})
    assert status == "error"
    assert text.startswith("exit 3") and "out" in text and "stderr:\nerr" in text
    monkeypatch.setattr(backend, "_bash_timeout", lambda: 1.0)
    status, text = agent._tool_result("bash", "c2", {"command": "sleep 20 & wait"})
    assert status == "error" and text.startswith("timed out after 1s")


# ── AUDIT-18 ────────────────────────────────────────────────────────────────

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _payload_for(variant):
    agent = backend._Agent("http://x", "", "m", "/tmp", variant=variant)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["payload"] = json.loads(req.data.decode())
        return _Resp(json.dumps({"choices": [{"message": {"role": "assistant",
                                                          "content": "ok"}}]}).encode())

    with patch("urllib.request.urlopen", fake_urlopen):
        agent._call()
    return seen["payload"]


def test_variant_reaches_the_model_request():
    assert _payload_for("high")["reasoning"] == {"effort": "high"}
    assert _payload_for("max")["reasoning"] == {"effort": "xhigh"}


def test_no_variant_sends_no_reasoning(capsys):
    assert "reasoning" not in _payload_for(None)
    assert "reasoning" not in _payload_for("highest")
    assert "reasoning" not in _payload_for("weird")
    assert "weird" in capsys.readouterr().err


def test_backend_passes_the_variant_to_the_agent_subprocess(tmp_path):
    seen = {}

    class _Proc:
        stdin = io.StringIO()
        stdout = io.StringIO()

        def poll(self):
            return None

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return _Proc()

    be = backend.OpenRouterBackend("k", "http://x", str(tmp_path))
    with patch.object(backend.subprocess, "Popen", fake_popen):
        ref = be.create_session("openrouter", "m1", rules=[], title="t", variant="low")
    assert ref.variant == "low"
    argv = seen["argv"]
    assert argv[argv.index("--variant") + 1] == "low"
