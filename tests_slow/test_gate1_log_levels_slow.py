"""tests_slow/test_gate1_log_levels_slow.py — relocated out of
tests/test_gate1_log_levels.py.

Contains exactly one test: test_technical_failure_still_logs_at_warning.
It's real (unmocked) time.sleep() in Gate1Filter's retry backoff
(AUTO-RETRY-BACKOFF-1, tools/auto/gate1_filter.py) that made it take
~180s (llm_call_retry_max=3 x llm_call_retry_wait_sec=60) — by itself
almost the entire runtime of `pytest tests`. filter() has no _sleep_fn
passthrough to stub the wait, so rather than patch production code this
test was moved here unchanged so the main suite stays fast.

Run just this file:
    pytest tests_slow -q

Everything else is unchanged and still lives in
tests/test_gate1_log_levels.py.
"""

from __future__ import annotations

import configparser
import logging
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter


@pytest.fixture()
def minimal_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "gate1": {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false"},
        "loop":  {"timeout_seconds": "10"},
    })
    return cfg


@pytest.fixture()
def filt(minimal_config: configparser.ConfigParser) -> Gate1Filter:
    return Gate1Filter(
        config=minimal_config, base_url="http://localhost:1337/v1",
        api_key="test", model="test-model", api_format="openai", verify_ssl=False,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "utils.py").write_text(
        textwrap.dedent("""\
            def parse_config(raw):
                return raw

            def stable_func():
                return 42
        """),
        encoding="utf-8",
    )
    return tmp_path


def _candidate(*, symbol: str, title: str = "Add input validation") -> CandidateTask:
    return CandidateTask(
        title=title, instruction="validate that raw is a dict",
        target_files=["tools/utils.py"], acceptance_check="true",
        cited_location=CitedLocation(file="tools/utils.py", symbol=symbol),
    )


class TestPresenceRejectionLevelDependsOnCause:

    def test_technical_failure_still_logs_at_warning(self, filt, repo, caplog):
        """A real call failure must NOT be downgraded to INFO — this is
        the actual anomaly WARNING exists for."""
        c = _candidate(symbol="stable_func")
        with caplog.at_level(logging.WARNING, logger="tools.auto.gate1_filter"):
            with patch(
                "tools.llm_stream.request_completion",
                side_effect=RuntimeError("HTTP 500 from server"),
            ):
                accepted, rejected = filt.filter([c], repo)
        assert accepted == []
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("REJECTED" in r.message for r in warning_records)
