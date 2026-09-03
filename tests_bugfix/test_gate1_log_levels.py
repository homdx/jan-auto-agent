"""tests/test_gate1_log_levels.py — AUTO-LOG-1: Gate 1 log levels and
per-candidate progress output.

Story background
-----------------
Field report: a real --validate-plan run against 31 tasks logged every
single rejection at WARNING — indistinguishable, at a glance, from an
actual technical failure ("JSON decode failed", "LLM call failed").
Concretely raised:

  1. "Reject or Approve for plan is not a state for request in log" —
     a REJECTED verdict is Gate 1 doing its job (the LLM read the code and
     disagreed with the claim), not an anomaly, and shouldn't share a log
     level with a genuine call/parse failure.
  2. Confirmations were entirely silent — no log line at all, asymmetric
     with (over-logged) rejections.
  3. No progress indicator while working through a 31-task, several-
     minutes-long batch — nothing to show which task is currently being
     checked.

Fix: _is_technical_failure() classifies a rejection reason as either a
genuine LLM verdict (INFO — matches the new CONFIRMED level) or an actual
call/parse failure (WARNING — the level now reserved for real anomalies).
filter() also prints a "[i/N] existence/presence check: <title>" line
before each candidate, for both stages.
"""

from __future__ import annotations

import configparser
import json
import logging
import re
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter, _is_technical_failure


# ─────────────────────────────────────────────────────────────────────────────
# _is_technical_failure — pure classifier
# ─────────────────────────────────────────────────────────────────────────────

class TestIsTechnicalFailure:

    def test_llm_call_failed_is_technical(self):
        assert _is_technical_failure("LLM call failed: Connection refused") is True

    def test_json_decode_failed_is_technical(self):
        assert _is_technical_failure(
            "JSON decode failed (Expecting value: line 1 column 1 (char 0)) — failing closed"
        ) is True

    def test_expected_json_object_is_technical(self):
        assert _is_technical_failure(
            "expected JSON object, got list — failing closed"
        ) is True

    def test_unrecognised_verdict_is_technical(self):
        assert _is_technical_failure(
            "unrecognised verdict 'maybe' — failing closed"
        ) is True

    def test_genuine_llm_reason_is_not_technical(self):
        reason = (
            "The code shown is an INI configuration file (agents_4k.ini), "
            "not the Python file tools/collect/cli.py, so the claimed "
            "error-handling problem is not present in this excerpt."
        )
        assert _is_technical_failure(reason) is False

    def test_confirmed_reason_is_not_technical(self):
        assert _is_technical_failure("parse_config lacks input validation") is False

    def test_empty_reason_is_not_technical(self):
        assert _is_technical_failure("") is False


# ─────────────────────────────────────────────────────────────────────────────
# filter() — log levels, end to end
# ─────────────────────────────────────────────────────────────────────────────

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


def _confirmed_for_prompt(url, headers, payload, **kwargs):
    """AUTO-H3-compatible mock: always confirms, grounded in a real line
    pulled from whatever code block Stage B was actually shown — needed
    here because a single fixed evidence string wouldn't match every
    symbol (parse_config vs. stable_func have different bodies)."""
    messages = payload.get("messages", [])
    user = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
    m = re.search(r"```\n(.*?)\n```", user, re.S)
    code = m.group(1) if m else ""
    evidence = next((ln.strip() for ln in code.splitlines() if ln.strip()), "def ")
    return json.dumps({"verdict": "confirmed", "evidence": evidence, "reason": "x"})


class TestExistenceRejectionIsInfoNotWarning:

    def test_missing_symbol_logs_at_info(self, filt, repo, caplog):
        c = _candidate(symbol="nonexistent_function")
        with caplog.at_level(logging.INFO, logger="tools.auto.gate1_filter"):
            filt.filter([c], repo)
        records = [r for r in caplog.records if "nonexistent_function" in r.message]
        assert records, "expected a log record mentioning the missing symbol"
        assert all(r.levelno == logging.INFO for r in records)

    def test_missing_symbol_does_not_log_at_warning(self, filt, repo, caplog):
        c = _candidate(symbol="nonexistent_function")
        with caplog.at_level(logging.WARNING, logger="tools.auto.gate1_filter"):
            filt.filter([c], repo)
        assert not any("nonexistent_function" in r.message for r in caplog.records)


class TestPresenceConfirmedIsLoggedAtInfo:

    def test_confirmed_candidate_logs_at_info(self, filt, repo, caplog):
        c = _candidate(symbol="parse_config")
        with caplog.at_level(logging.INFO, logger="tools.auto.gate1_filter"):
            with patch(
                "tools.llm_stream.request_completion",
                return_value='{"verdict": "confirmed", "evidence": "return raw", '
                             '"reason": "still missing validation"}',
            ):
                accepted, rejected = filt.filter([c], repo)
        assert len(accepted) == 1
        confirmed_records = [r for r in caplog.records if "CONFIRMED" in r.message]
        assert confirmed_records, "expected a CONFIRMED log line — confirmations must not be silent"
        assert confirmed_records[0].levelno == logging.INFO

    def test_confirmed_candidate_does_not_log_at_warning(self, filt, repo, caplog):
        c = _candidate(symbol="parse_config")
        with caplog.at_level(logging.WARNING, logger="tools.auto.gate1_filter"):
            with patch(
                "tools.llm_stream.request_completion",
                return_value='{"verdict": "confirmed", "evidence": "return raw", '
                             '"reason": "still missing validation"}',
            ):
                filt.filter([c], repo)
        assert not any("CONFIRMED" in r.message for r in caplog.records)


class TestPresenceRejectionLevelDependsOnCause:

    def test_genuine_llm_rejection_logs_at_info(self, filt, repo, caplog):
        c = _candidate(symbol="stable_func")
        with caplog.at_level(logging.INFO, logger="tools.auto.gate1_filter"):
            with patch(
                "tools.llm_stream.request_completion",
                return_value='{"verdict": "rejected", "reason": "already has a docstring"}',
            ):
                accepted, rejected = filt.filter([c], repo)
        assert accepted == []
        rejected_records = [r for r in caplog.records if "REJECTED" in r.message]
        assert rejected_records
        assert rejected_records[0].levelno == logging.INFO

    def test_genuine_llm_rejection_does_not_log_at_warning(self, filt, repo, caplog):
        c = _candidate(symbol="stable_func")
        with caplog.at_level(logging.WARNING, logger="tools.auto.gate1_filter"):
            with patch(
                "tools.llm_stream.request_completion",
                return_value='{"verdict": "rejected", "reason": "already has a docstring"}',
            ):
                filt.filter([c], repo)
        assert not any("Gate1[presence] REJECTED" in r.message for r in caplog.records)

    def test_technical_failure_still_logs_at_warning(self, filt, repo, caplog):
        """A real call failure must NOT be downgraded to INFO — this is
        the actual anomaly WARNING exists for."""
        c = _candidate(symbol="stable_func")
        # Default retry config is 3 retries * 60s real time.sleep() between
        # them (see Gate1Filter._check_presence) -- this test only checks
        # the WARNING-level logging outcome, not retry timing, so skip the
        # wait. Was split into tests_slow/test_gate1_log_levels_slow.py
        # while this ~180s cost was unavoidable; merged back now that it
        # isn't (2026-09-03, same fix as tests/test_auto_b3.py).
        filt._llm_call_retry_wait_sec = 0
        with caplog.at_level(logging.WARNING, logger="tools.auto.gate1_filter"):
            with patch(
                "tools.llm_stream.request_completion",
                side_effect=RuntimeError("HTTP 500 from server"),
            ):
                accepted, rejected = filt.filter([c], repo)
        assert accepted == []
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("REJECTED" in r.message for r in warning_records)


# ─────────────────────────────────────────────────────────────────────────────
# filter() — per-candidate progress output
# ─────────────────────────────────────────────────────────────────────────────

class TestProgressOutput:

    def test_existence_stage_prints_progress(self, filt, repo, capsys):
        c1 = _candidate(symbol="parse_config", title="Task One")
        c2 = _candidate(symbol="stable_func", title="Task Two")
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=_confirmed_for_prompt,
        ):
            filt.filter([c1, c2], repo)
        out = capsys.readouterr().out
        assert "[1/2] existence check: Task One" in out
        assert "[2/2] existence check: Task Two" in out

    def test_presence_stage_prints_progress(self, filt, repo, capsys):
        c1 = _candidate(symbol="parse_config", title="Task One")
        c2 = _candidate(symbol="stable_func", title="Task Two")
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=_confirmed_for_prompt,
        ):
            filt.filter([c1, c2], repo)
        out = capsys.readouterr().out
        assert "[1/2] presence check: Task One" in out
        assert "[2/2] presence check: Task Two" in out

    def test_progress_denominator_shrinks_after_existence_drops(self, filt, repo, capsys):
        """A candidate dropped at existence never reaches the presence
        stage, so the presence-stage progress denominator reflects only
        the survivors, not the original candidate count."""
        c1 = _candidate(symbol="nonexistent_function", title="Gone")
        c2 = _candidate(symbol="parse_config", title="Real")
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=_confirmed_for_prompt,
        ):
            filt.filter([c1, c2], repo)
        out = capsys.readouterr().out
        assert "[1/2] existence check: Gone" in out
        assert "[2/2] existence check: Real" in out
        # Only "Real" survived existence, so presence stage is [1/1], not [1/2].
        assert "[1/1] presence check: Real" in out
        assert "presence check: Gone" not in out
