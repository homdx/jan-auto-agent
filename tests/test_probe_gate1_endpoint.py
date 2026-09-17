"""tests/test_probe_gate1_endpoint.py — RUN-9: the endpoint readiness probe.

``scripts/probe_gate1_endpoint.py`` makes two presence-shaped calls against
the configured Gate-1 profile and reports what a RUN-9 run will find there:
usage chunk, ``stream_options`` acceptance, visible reasoning, ``think=off``
honoured. These tests fake ``urllib.request.urlopen`` — the layer every call
goes through — and pin the request shape (profile resolution, ``stream`` +
``stream_options``, the think toggle) and the wording of the report the user
reads before starting a multi-hour run.
"""
from __future__ import annotations

import configparser
import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as L  # noqa: E402
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import probe_gate1_endpoint as probe  # noqa: E402

URL = "http://stub.local:1337/v1/chat/completions"
MODEL = "presence-model"


def _chunk(delta: dict, finish_reason=None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


def _usage(n: int) -> dict:
    return {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": n}}


def _sse(chunks) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


VERDICT = '{"verdict": "rejected", "evidence": "", "reason": "add returns a + b"}'
THINKING = _sse([_chunk({"role": "assistant"}), _chunk({"reasoning_content": "the code adds"}),
                 _chunk({"content": VERDICT}), _chunk({}, "stop"), _usage(40)])
PLAIN = _sse([_chunk({"role": "assistant"}), _chunk({"content": VERDICT}),
              _chunk({}, "stop"), _usage(30)])
THINKING_NO_USAGE = _sse([_chunk({"role": "assistant"}), _chunk({"reasoning_content": "the code adds"}),
                          _chunk({"content": VERDICT}), _chunk({}, "stop")])
PLAIN_NO_USAGE = _sse([_chunk({"role": "assistant"}), _chunk({"content": VERDICT}),
                       _chunk({}, "stop")])
ROLE_ONLY = _sse([_chunk({"role": "assistant"}), _chunk({}, "stop"), _usage(0)])


class _Response:
    def __init__(self, body: bytes):
        self._lines = body.splitlines(keepends=True)
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


class FakeUrlopen:
    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.sent: list[dict] = []
        self.auth: list[str] = []

    def __call__(self, req, timeout=None, context=None):
        self.sent.append(json.loads(req.data.decode("utf-8")))
        self.auth.append(req.get_header("Authorization") or "")
        body = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        if isinstance(body, tuple):
            status, text = body
            raise urllib.error.HTTPError(req.full_url, status, f"HTTP {status}", {},
                                         io.BytesIO(text.encode()))
        return _Response(body)


@pytest.fixture
def fake(monkeypatch):
    def _install(*bodies):
        f = FakeUrlopen(*bodies)
        monkeypatch.setattr(urllib.request, "urlopen", f)
        return f
    return _install


@pytest.fixture(autouse=True)
def _clean_memories(monkeypatch):
    L._STREAM_OPTIONS_UNSUPPORTED_KEYS.clear()
    L._RESPONSE_FORMAT_UNSUPPORTED_KEYS.clear()
    monkeypatch.setattr(L.time, "sleep", lambda *_: None)
    yield
    L._STREAM_OPTIONS_UNSUPPORTED_KEYS.clear()
    L._RESPONSE_FORMAT_UNSUPPORTED_KEYS.clear()


@pytest.fixture
def ini(tmp_path) -> Path:
    """A config shaped like agents_128k.ini's Gate-1 part: shared provider
    with think=false, a [gate1_llm] profile with think=true and its own
    key/model — the probe must call the PROFILE, never the shared provider."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "remote", "verify_ssl": "false", "response_format": "true",
                "think_effort_enabled": "true", "think_effort": "high"},
        "api_remote": {"base_url": "http://shared.local:9999/v1", "api_key": "SHARED-KEY",
                       "model": "shared-model", "api_format": "openai", "think": "false"},
        "gate1": {"temperature": "0.0", "max_tokens": "512", "think": "false",
                  "skip_llm": "false", "llm_call_retry_wait_sec": "0",
                  "llm_call_retry_max": "1", "unparseable_retry_mode": "fast",
                  "unparseable_learn": "false", "presence_workers": "1",
                  "presence_empty_retries": "2", "presence_llm_profile": "gate1_llm"},
        "gate1_llm": {"base_url": "http://stub.local:1337/v1", "api_key": "PROFILE-SECRET",
                      "model": MODEL, "think": "true", "temperature": "0.0",
                      "max_tokens": "32768", "num_ctx": "512000"},
        "loop": {"timeout_seconds": "10"},
    })
    p = tmp_path / "agents_probe.ini"
    with p.open("w", encoding="utf-8") as fh:
        cfg.write(fh)
    return p


def _run(argv, capsys) -> tuple[int, str]:
    rc = probe.main(argv)
    return rc, capsys.readouterr().out


class TestRequestShape:
    def test_calls_the_presence_profile_with_the_gate1_prompt(self, fake, ini, capsys):
        f = fake(THINKING, PLAIN)
        rc, out = _run(["--config", str(ini)], capsys)
        assert rc == 0
        assert len(f.sent) == 2
        assert all(p["model"] == MODEL for p in f.sent)
        assert all("PROFILE-SECRET" in a for a in f.auth)
        sysmsg = f.sent[0]["messages"][0]["content"]
        assert f.sent[0]["messages"][0]["role"] == "system" and sysmsg
        user = f.sent[0]["messages"][1]["content"]
        assert "Claimed problem: The function `add`" in user
        assert "def add(a, b):" in user

    def test_stream_with_usage_and_response_format(self, fake, ini, capsys):
        f = fake(THINKING, PLAIN)
        _run(["--config", str(ini)], capsys)
        for p in f.sent:
            assert p["stream"] is True
            assert p["stream_options"] == {"include_usage": True}
            assert p["response_format"] == {"type": "json_object"}

    def test_first_call_thinks_second_does_not(self, fake, ini, capsys):
        f = fake(THINKING, PLAIN)
        _run(["--config", str(ini)], capsys)
        assert f.sent[0]["reasoning_effort"] == "high"
        assert "reasoning_effort" not in f.sent[1]
        assert f.sent[1]["reasoning"] == {"effort": "low", "exclude": True}

    def test_calls_selector(self, fake, ini, capsys):
        f = fake(PLAIN)
        _run(["--config", str(ini), "--calls", "nothink"], capsys)
        assert len(f.sent) == 1 and "reasoning_effort" not in f.sent[0]

    def test_no_secret_in_the_report(self, fake, ini, capsys):
        fake(THINKING, PLAIN)
        _, out = _run(["--config", str(ini)], capsys)
        assert "PROFILE-SECRET" not in out and "SHARED-KEY" not in out


class TestReport:
    def test_ready(self, fake, ini, capsys):
        fake(THINKING, PLAIN)
        rc, out = _run(["--config", str(ini)], capsys)
        assert rc == 0
        assert "verdict: READY" in out and "degraded" not in out
        assert "usage chunk" in out and "completion_tokens=40, 30" in out
        assert "stream_options ....... accepted" in out
        assert "reasoning visible" in out and "13 chars with think=on" in out
        assert "think=off honoured" in out and "no-think rung is real" in out
        assert "call 1 (think=True)" in out and "verdict=rejected" in out
        assert "call 2 (think=False)" in out
        assert f"model={MODEL}" in out and "profile=[gate1_llm]" in out
        assert "presence_empty_retries=2" in out

    def test_stream_options_rejected_is_degraded_not_fatal(self, fake, ini, capsys):
        f = fake((400, '{"error": "unknown field: stream_options"}'), THINKING_NO_USAGE,
                 PLAIN_NO_USAGE)
        rc, out = _run(["--config", str(ini)], capsys)
        assert rc == 0
        assert "stream_options" not in f.sent[1]
        assert "usage chunk .......... NO" in out
        assert "stream_options ....... rejected with HTTP 400" in out
        assert "verdict: READY (degraded: no usage chunk)" in out

    def test_think_off_ignored_is_reported(self, fake, ini, capsys):
        fake(THINKING, THINKING)
        rc, out = _run(["--config", str(ini)], capsys)
        assert rc == 0
        assert "think=off honoured ... NO" in out
        assert "presence_nothink_ignored = 1" in out
        assert "degraded: think=off ignored" in out

    def test_no_reasoning_with_think_on_is_reported(self, fake, ini, capsys):
        fake(PLAIN, PLAIN)
        rc, out = _run(["--config", str(ini)], capsys)
        assert rc == 0
        assert "reasoning visible .... no reasoning text seen" in out
        assert "degraded: reasoning not visible" in out

    def test_empty_reply_is_classified_like_the_run_would(self, fake, ini, capsys):
        fake(ROLE_ONLY, PLAIN)
        rc, out = _run(["--config", str(ini)], capsys)
        assert rc == 0
        assert "EMPTY -> classified transport" in out
        assert "1 of 2 EMPTY" in out and "empty t/x" in out

    def test_transport_failure_is_not_ready(self, fake, ini, capsys):
        fake((503, "upstream down"))
        rc, out = _run(["--config", str(ini), "--error-retries", "1"], capsys)
        assert rc == 1
        assert "call 1 (think=True):   FAILED  RuntimeError: HTTP 503" in out
        assert "verdict: NOT READY" in out

    def test_missing_config(self, capsys, tmp_path):
        assert probe.main(["--config", str(tmp_path / "nope.ini")]) == 2
