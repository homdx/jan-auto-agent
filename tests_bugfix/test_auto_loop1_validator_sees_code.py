"""tests/test_auto_loop1_validator_sees_code.py

Regression guard for the "validator flies blind" bug: the LOOP-1 Gate-2
validator's system prompt promises the model "the generated code" and asks for
line/pattern-specific hints, so the actual changed file content MUST appear in
the user prompt — not just the file names. A mocked-approval suite cannot catch
this, so we assert on the prompt the validator builds.
"""

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.inner_loop import LLMGate2Validator


@dataclass
class FakeCoderResult:
    succeeded: bool = True
    files_written: list = field(default_factory=lambda: ["v2/angie_ops.py"])
    error: str = ""


@dataclass
class FakeExecResult:
    passed: bool = True
    exit_code: int = 0
    stdout: str = "1 passed"
    stderr: str = ""
    traceback: str = ""
    timed_out: bool = False


TASK = {"id": "AUTO-T1", "title": "add timeout",
        "instruction": "add a timeout to fetch_json",
        "acceptance_check": "grep -q timeout v2/angie_ops.py"}

_CODE = ("def fetch_json(url, timeout=30):\n"
         "    return requests.get(url, timeout=timeout).json()\n")


def _capture_prompt(monkeypatch_target_value):
    """Patch the (locally-imported) request_completion and capture the user
    prompt the validator sends."""
    captured = {}

    def _fake(url, headers, payload, **kwargs):
        captured["user"] = payload["messages"][-1]["content"]
        return monkeypatch_target_value

    # approve() does `from tools.llm_stream import request_completion` at call
    # time, so patching the module attribute is what takes effect.
    return captured, patch("tools.llm_stream.request_completion", side_effect=_fake)


class TestValidatorSeesCode:
    def test_prompt_contains_actual_changed_code(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "v2").mkdir()
            (d / "v2/angie_ops.py").write_text(_CODE, encoding="utf-8")

            v = LLMGate2Validator(base_url="http://x/v1", model="m",
                                  base_dir=str(d))
            captured, p = _capture_prompt('{"approved": true, "feedback": ""}')
            with p:
                approved, _ = v.approve(TASK, FakeExecResult(), FakeCoderResult())

            assert approved is True
            # the REAL code must be in the prompt, not just the filename
            assert "def fetch_json(url, timeout=30)" in captured["user"]
            assert "--- v2/angie_ops.py ---" in captured["user"]

    def test_missing_file_is_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as d:
            v = LLMGate2Validator(base_url="http://x/v1", model="m", base_dir=d)
            captured, p = _capture_prompt('{"approved": false, "feedback": "x"}')
            with p:
                v.approve(TASK, FakeExecResult(),
                          FakeCoderResult(files_written=["does/not/exist.py"]))
            assert "could not read" in captured["user"]

    def test_no_files_written_is_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            v = LLMGate2Validator(base_url="http://x/v1", model="m", base_dir=d)
            captured, p = _capture_prompt('{"approved": false, "feedback": "x"}')
            with p:
                v.approve(TASK, FakeExecResult(),
                          FakeCoderResult(files_written=[]))
            assert "NO files written" in captured["user"]

    def test_fail_closed_on_network_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "v2").mkdir()
            (Path(d) / "v2/angie_ops.py").write_text(_CODE, encoding="utf-8")
            v = LLMGate2Validator(base_url="http://x/v1", model="m", base_dir=d)
            with patch("tools.llm_stream.request_completion",
                       side_effect=RuntimeError("conn refused")):
                approved, fb = v.approve(TASK, FakeExecResult(), FakeCoderResult())
            assert approved is False
            assert "validator unavailable" in fb


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
