"""tests/test_coder_context_request.py

Locks in the coder side of the pull model (not covered by the broker/inner-loop
tests): parsing a top-level `context_request` from the LLM response into
CoderResult.missing_context, and injecting prefetched context into the prompt.
"""

import configparser
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.auto.coder as C


def _cfg():
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "true"},
        "api_local": {"base_url": "http://x/v1", "api_key": "k", "model": "m",
                      "api_format": "openai", "num_ctx": "0"},
        "coder": {"temperature": "0.2", "max_tokens": "4096"},
        "loop": {"timeout_seconds": "300"},
    })
    return cfg


_TASK = {"id": "T1", "title": "t", "instruction": "i", "target_files": ["app.py"]}


def _run(monkeypatch, response, prefetched_context=""):
    captured = {}

    def fake(**kw):
        captured["payload"] = kw["payload"]
        return response

    monkeypatch.setattr(C._llm_stream, "request_completion", fake)
    coder = C.make_coder(_cfg())
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "app.py").write_text("old\n")
        result = coder.generate(_TASK, d, prefetched_context=prefetched_context)
    return result, captured.get("payload", {})


def test_context_request_populates_missing_context(monkeypatch):
    resp = '{"files": [{"path": "app.py", "content": "x=1\\n"}], "context_request": ["Config", "_resolve"]}'
    result, _ = _run(monkeypatch, resp)
    assert result.missing_context == ["Config", "_resolve"]


def test_no_context_request_is_empty(monkeypatch):
    resp = '{"files": [{"path": "app.py", "content": "x=1\\n"}]}'
    result, _ = _run(monkeypatch, resp)
    assert result.missing_context == []


def test_context_request_survives_fences(monkeypatch):
    resp = '```json\n{"files": [{"path": "app.py", "content": "x=1\\n"}], "context_request": ["Foo"]}\n```'
    result, _ = _run(monkeypatch, resp)
    assert result.missing_context == ["Foo"]


def test_prefetched_context_injected_into_prompt(monkeypatch):
    resp = '{"files": [{"path": "app.py", "content": "x=1\\n"}]}'
    pre = "PREFETCHED CONTEXT (symbols you requested):\n### Config\nclass Config:\n    X = 1"
    _, payload = _run(monkeypatch, resp, prefetched_context=pre)
    user_msg = payload["messages"][-1]["content"]
    assert "PREFETCHED CONTEXT" in user_msg
    assert "class Config" in user_msg


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
