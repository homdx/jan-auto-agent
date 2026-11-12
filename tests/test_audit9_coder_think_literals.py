"""AUDIT-9 Coder must preserve literal reasoning tags in file contents."""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto.coder import Coder


def _coder() -> Coder:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test",
                      "model": "test-model", "api_format": "openai"},
        "loop": {"timeout_seconds": "30"},
        "coder": {"temperature": "0", "max_tokens": "256"},
    })
    return Coder(cfg, base_url="http://localhost:1337/v1", api_key="test", model="test-model")


def _task() -> dict:
    return {"id": "AUDIT9", "title": "preserve", "instruction": "write exact content",
            "target_files": ["x.py"], "cited_locations": []}


def _reply(content: str, fenced: bool = False) -> str:
    value = json.dumps({"files": [{"path": "x.py", "content": content}]})
    return f"```json\n{value}\n```" if fenced else value


@pytest.mark.parametrize("fenced", [False, True])
@pytest.mark.parametrize("content", [
    'import re\nRE = re.compile(r"<think>.*?</think>")\n',
    'OPEN = "<think>"\n',
    'CLOSE = "</think>"\n',
])
def test_structured_json_preserves_literal_tags(tmp_path, fenced, content):
    with patch("tools.llm_stream.request_completion", return_value=_reply(content, fenced)):
        result = _coder().generate(_task(), tmp_path)
    assert result.succeeded
    assert (tmp_path / "x.py").read_text() == content


def test_leading_reasoning_wrapper_is_removed_without_touching_json_literals(tmp_path):
    content = 'OPEN = "<think>"\nCLOSE = "</think>"\n'
    response = "<think>planning…</think>\n" + _reply(content)
    with patch("tools.llm_stream.request_completion", return_value=response):
        result = _coder().generate(_task(), tmp_path)
    assert result.succeeded
    assert (tmp_path / "x.py").read_text() == content


def test_unfinished_reasoning_wrapper_fails_without_overwriting_file(tmp_path):
    target = tmp_path / "x.py"
    target.write_text("before = True\n")
    with patch("tools.llm_stream.request_completion", return_value='prefix <think>unfinished'):
        result = _coder().generate(_task(), tmp_path)
    assert not result.succeeded
    assert target.read_text() == "before = True\n"


def test_context_probe_uses_the_same_literal_preserving_response_path(tmp_path):
    content = 'RE = re.compile(r"<think>.*?</think>")\n'
    first = json.dumps({"files": [], "missing_context": ["module.py"]})
    second = "<think>probe reasoning</think>\n" + _reply(content)
    coder = _coder()
    coder._context_probe_enabled = True
    coder._fetch_needed = lambda *_args, **_kwargs: "fetched context"
    with patch("tools.llm_stream.request_completion", side_effect=[first, second]):
        result = coder.generate(_task(), tmp_path)
    assert result.succeeded
    assert (tmp_path / "x.py").read_text() == content
