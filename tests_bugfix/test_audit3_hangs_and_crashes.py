"""tests_bugfix/test_audit3_hangs_and_crashes.py — AUDIT3 group B.

Hangs, crashes and lost paid calls:

* AUDIT3-9b  the OpenRouter agent's stderr was a pipe nobody read (deadlock at ~64 KiB).
* AUDIT3-10b a streamed chunk with a non-object message/delta raised AttributeError.
* AUDIT3-11  a retried stream inherited the failed attempt's finish_reason.
* AUDIT3-12  each outer retry re-granted _open's whole retry budget.
* AUDIT3-13  "{}" / "{0}" in a prompt template raised IndexError out of validate().
* AUDIT3-14  any exception from one REPL query ended the interactive session.
* AUDIT3-15  attach_clone read a failed `git status` as "clean", then ran clean -fdx.
* AUDIT3-17  gates' git and tier check had no timeout under the round-wide lock.
* AUDIT3-18  UnicodeEncodeError on a non-UTF-8 stdout killed a streaming call.
* AUDIT3-19  append_finding/append_verdict glued a row onto a last line without a newline.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from tools import llm_stream, ui
from tools.contest import backend, gates, workspace
from tools.prompt_evaluator import _validate_candidate_placeholders
from tools.validator_agent import ValidatorAgent

REPO = Path(__file__).resolve().parents[1]


# ── AUDIT3-9b ───────────────────────────────────────────────────────────────

def test_agent_stderr_is_drained_and_kept(tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text(
        "import sys\n"
        "sys.stderr.write(('x' * 1000 + '\\n') * 300)\n"      # ~300 KiB, far past a pipe
        "sys.stderr.flush()\n"
        "print('{\"type\": \"idle\"}', flush=True)\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.PIPE, text=True)
    record = backend._AgentRecord(str(tmp_path), "s", "p", "m", None, proc, proc.stdin, proc.stdout)
    import threading
    threading.Thread(target=backend.OpenRouterBackend._drain_stderr,
                     args=(record, proc.stderr), daemon=True).start()
    try:
        assert proc.stdout.readline().strip() == '{"type": "idle"}'
        proc.wait(timeout=20)
    finally:
        proc.kill()
    deadline = time.monotonic() + 5
    while len(record.stderr_tail) < 200 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(record.stderr_tail) == 200 and record.stderr_tail[-1] == "x" * 1000


# ── AUDIT3-10b / 11 / 12: request_completion against a scripted urlopen ──────

class _Resp:
    def __init__(self, lines):
        self._lines = [l.encode() + b"\n" for l in lines]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self, *a):
        return b"".join(self._lines)


def _complete(responses, **kw):
    it = iter(responses)

    def fake_urlopen(req, timeout=None, context=None):
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        return item

    with patch("urllib.request.urlopen", fake_urlopen):
        return llm_stream.request_completion(
            "http://x/v1/chat/completions", {}, {"model": "m", "messages": []}, 5,
            stream=kw.pop("stream", True), api_format=kw.pop("api_format", "openai"),
            error_retries=kw.pop("error_retries", 2), error_retry_wait_sec=0,
            _sleep_fn=lambda s: None, **kw)


def test_a_non_object_delta_or_message_carries_no_token():
    sse = ['data: {"choices": [{"delta": "oops"}]}',
           'data: {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}',
           "data: [DONE]"]
    assert _complete([_Resp(sse)]) == "hi"
    nd = [json.dumps({"message": "oops"}),
          json.dumps({"message": {"content": "ok"}, "done": True})]
    assert _complete([_Resp(nd)], api_format="ollama") == "ok"


def test_a_retried_stream_does_not_inherit_the_failed_attempts_finish_reason():
    # attempt 1: a finish_reason, then the connection drops before any token
    class _Dropping(_Resp):
        def __iter__(self):
            yield b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n'
            raise ConnectionResetError("reset")

    # attempt 2: content only, no completion signal at all — a truncation
    truncated = _Resp(['data: {"choices": [{"delta": {"content": "half"}}]}'])
    with pytest.raises(RuntimeError, match="ended mid-reply"):
        _complete([_Dropping([]), truncated])


def test_the_open_retry_budget_is_shared_across_outer_retries():
    """Each outer read retry used to re-enter _open with a fresh budget: two 503s
    then a body that drops, three times over, made 9 requests at error_retries=2."""
    calls = {"n": 0}

    class _DroppingBody:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            raise ConnectionResetError("reset")

    def fake_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        if calls["n"] % 3:
            raise urllib.error.HTTPError("http://x", 503, "busy", {}, io.BytesIO(b""))
        return _DroppingBody()

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(Exception):
            llm_stream.request_completion(
                "http://x/v1/chat/completions", {}, {"model": "m", "messages": []}, 5,
                stream=False, api_format="openai", error_retries=2,
                error_retry_wait_sec=0, _sleep_fn=lambda s: None)
    assert calls["n"] <= 5, calls


# ── AUDIT3-13 ───────────────────────────────────────────────────────────────

def test_a_positional_placeholder_falls_back_instead_of_crashing():
    store = type("S", (), {"get_current": lambda self, name: "{} {0} {task}"})()
    agent = ValidatorAgent(model="m", base_url="http://fake", api_key="x",
                           timeout=5, max_iter=3, prompt_store=store)
    raw = json.dumps({"status": "approved", "feedback": "", "suggested_searches": []})
    payload = {"task": "t", "iteration": 1, "target_block": "", "imports": "",
               "related_code": {}, "missing_refs": "None"}
    with patch("tools.validator_agent.request_completion", return_value=raw):
        assert agent.validate(payload)["status"] == "approved"


def test_a_positional_candidate_is_rejected_before_promotion():
    from tools import prompt_evaluator
    required = " ".join("{%s}" % p for p in prompt_evaluator._REQUIRED_PLACEHOLDERS)
    assert _validate_candidate_placeholders(required) is None
    assert "positional" in _validate_candidate_placeholders(required + " {}")
    assert "positional" in _validate_candidate_placeholders(required + " {0}")


# ── AUDIT3-14 ───────────────────────────────────────────────────────────────

def test_the_repl_survives_a_failing_query():
    src = (REPO / "main.py").read_text(encoding="utf-8")
    loop = src[src.index("orchestrator.run_pipeline(user_input, base_dir)") - 400:
               src.index("orchestrator.run_pipeline(user_input, base_dir)") + 400]
    assert "except Exception" in loop


# ── AUDIT3-15 ───────────────────────────────────────────────────────────────

def test_attach_clone_refuses_an_unreadable_status(tmp_path, monkeypatch):
    def run(*args, cwd=None):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       cwd=cwd, check=True, capture_output=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    run("init", "-q", str(origin))
    (origin / "a").write_text("a")
    run("add", ".", cwd=origin)
    run("commit", "-qm", "c", cwd=origin)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True,
                         text=True, check=True).stdout.strip()
    clone = tmp_path / "clone"
    run("clone", "-q", str(origin), str(clone))
    keep = clone / "untracked_work.txt"
    keep.write_text("precious")
    real_git = workspace._git

    def git(cwd, args, *, check=True):
        if args[:1] == ["status"]:
            return subprocess.CompletedProcess(args, 128, "", "fatal: index file corrupt")
        return real_git(cwd, args, check=check)

    monkeypatch.setattr(workspace, "_git", git)
    with pytest.raises(workspace.WorkspaceError, match="git status failed"):
        workspace.attach_clone(clone, "a1", sha, "b1")
    assert keep.read_text() == "precious"


# ── AUDIT3-17 ───────────────────────────────────────────────────────────────

def test_gates_git_times_out_to_empty(monkeypatch):
    def slow(*a, **k):
        assert k.get("timeout") == gates.GIT_TIMEOUT_S
        raise subprocess.TimeoutExpired("git", k["timeout"])

    monkeypatch.setattr(gates, "run_git", slow)
    assert gates.git(".", "log") == ""
    with pytest.raises(RuntimeError):
        gates.git(".", "log", check=True)


# ── AUDIT3-18 ───────────────────────────────────────────────────────────────

class _AsciiOut(io.StringIO):
    encoding = "ascii"

    def write(self, s):
        s.encode("ascii")
        return super().write(s)


def test_on_token_survives_a_non_utf8_stdout(monkeypatch):
    out = _AsciiOut()
    monkeypatch.setattr(sys, "stdout", out)
    on_token, _stats = ui.stream_tracker()
    on_token("привет ok")
    assert "ok" in out.getvalue()


def test_the_spinner_survives_a_non_utf8_stdout(monkeypatch):
    out = _AsciiOut()
    monkeypatch.setattr(sys, "stdout", out)
    sp = ui.Spinner("work")
    sp.spin()
    sp.spin()
    assert "work" in out.getvalue()


# ── AUDIT3-19 ───────────────────────────────────────────────────────────────

def test_append_finding_starts_a_new_line_after_an_unterminated_row(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import append_finding
    finally:
        sys.path.remove(str(REPO / "scripts"))
    out = tmp_path / "v.csv"
    header = ",".join(append_finding.COLUMNS)
    other = ",".join("g.py" if c == "file" else "T" if c == "symbol" else "x"
                     for c in append_finding.COLUMNS)
    out.write_text(header + "\r\n" + other, encoding="utf-8")       # no final newline
    (tmp_path / "f.py").write_text("x = 1\n")
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "append_finding.py"),
                        "--out", str(out), "--repo-root", str(tmp_path), "--variant", "1",
                        "--file", "f.py", "--symbol", "S", "--verdict", "FALSE_POSITIVE",
                        "--severity", "NONE"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    rows = list(csv.reader(out.open(encoding="utf-8", newline="")))
    assert len(rows) == 3 and all(len(row) == len(append_finding.COLUMNS) for row in rows)
