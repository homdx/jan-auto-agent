"""tests/test_coder_num_ctx.py

Regression guard for audit finding #1 (coder leg): the coder must honor
[api_<profile>] num_ctx on Ollama, otherwise its large prompt + up-to-16384
output tokens get silently truncated to the model's default context window
(2048/4096) — the "Unterminated string" coder-truncation failure.

This asserts on the payload the coder actually builds, since a mocked-LLM
suite cannot otherwise detect a missing context-window setting.
"""

import configparser
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.auto.coder as C


def _cfg(num_ctx, api_format="ollama"):
    cfg = configparser.ConfigParser()
    cfg["api"] = {"active": "local", "verify_ssl": "true"}
    cfg["api_local"] = {
        "base_url": "http://x:11434", "api_key": "k",
        "model": "qwen2.5-coder:14b", "api_format": api_format,
        "num_ctx": str(num_ctx),
    }
    cfg["coder"] = {"temperature": "0.2", "max_tokens": "16384"}
    cfg["loop"] = {"timeout_seconds": "300"}
    return cfg


def _capture_payload(coder):
    cap = {}

    def _fake(url, headers, payload, **kwargs):
        cap["payload"] = payload
        return '{"files": []}'

    C._llm_stream.request_completion = _fake
    with tempfile.TemporaryDirectory() as d:
        try:
            coder.generate({"instruction": "x", "title": "t", "target_files": []}, d)
        except Exception:
            pass  # empty fake response → parse no-op; we only want the payload
    return cap.get("payload", {})


def test_coder_reads_num_ctx_from_config():
    coder = C.make_coder(_cfg(8192))
    assert coder._num_ctx == 8192


def test_ollama_payload_carries_num_ctx():
    coder = C.make_coder(_cfg(8192))
    payload = _capture_payload(coder)
    opts = payload.get("options", {})
    assert opts.get("num_ctx") == 8192
    assert opts.get("num_predict") == 16384


def test_num_ctx_zero_is_omitted():
    # 0 means "use server default" — must NOT be sent.
    coder = C.make_coder(_cfg(0))
    payload = _capture_payload(coder)
    assert "num_ctx" not in payload.get("options", {})


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
