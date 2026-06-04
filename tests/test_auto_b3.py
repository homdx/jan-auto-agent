"""tests/test_auto_b3.py — Tests for AUTO-B3: Gate 1 false-positive filter.

Covers all ACs from the story:

  AC1 (bogus candidate dropped):
      A candidate that cites a line range or symbol where the stated
      problem is NOT present is rejected with a logged reason.  Tested at
      both the existence stage (symbol genuinely absent) and the presence
      stage (LLM says "rejected").

  AC2 (duplicates merged):
      Two candidates with identical (file, anchor, normalised title)
      fingerprints result in exactly one accepted task; the second is
      recorded as a duplicate in the rejected list.

  Broader coverage:

  ExistenceChecks:
      - Missing file → rejected at existence stage.
      - Symbol cited but not found in file → rejected at existence stage.
      - Line start out of range → rejected at existence stage.
      - Valid symbol → existence passes, code block extracted correctly.
      - Valid line range → existence passes, lines extracted.

  PresenceChecks:
      - LLM returns {"verdict": "confirmed"} → accepted.
      - LLM returns {"verdict": "rejected"}  → rejected at presence stage.
      - LLM returns malformed JSON            → fail-closed (rejected).
      - LLM returns unrecognised verdict      → fail-closed (rejected).
      - LLM raises a network exception        → fail-closed (rejected).
      - <think> blocks in LLM response stripped before JSON parse.
      - Markdown fences stripped before JSON parse.

  StageOrdering:
      - Existence failure stops processing; no LLM call is made.
      - skip_llm=True bypasses Stage B entirely.

  Integration:
      - Mix of accepted/rejected candidates from multiple stages in one call.
      - filter_candidates() convenience factory reads config correctly.

All LLM calls are patched; no network I/O occurs.
"""

from __future__ import annotations

import configparser
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import (
    FilterResult,
    Gate1Filter,
    _fingerprint,
    _location_str,
    _truncate,
    filter_candidates,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
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
        config=minimal_config,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
    )


@pytest.fixture()
def skip_llm_filt(minimal_config: configparser.ConfigParser) -> Gate1Filter:
    """Filter with LLM stage disabled — only runs existence checks."""
    minimal_config.set("gate1", "skip_llm", "true")
    return Gate1Filter(
        config=minimal_config,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        verify_ssl=False,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Minimal fake repo with one Python file that has a real symbol."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "utils.py").write_text(
        textwrap.dedent("""\
            def parse_config(raw):
                # TODO: validate input
                return raw

            def another_func():
                pass
        """),
        encoding="utf-8",
    )
    return tmp_path


# ─── Candidate builder helpers ────────────────────────────────────────────────

def _make_candidate(
    *,
    title: str = "Add input validation",
    instruction: str = "validate that raw is a dict",
    file: str = "tools/utils.py",
    symbol: str | None = "parse_config",
    line_start: int | None = None,
    line_end: int | None = None,
    acceptance_check: str = "python -m pytest tests/ -q",
) -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction=instruction,
        target_files=[file],
        acceptance_check=acceptance_check,
        cited_location=CitedLocation(
            file=file,
            symbol=symbol,
            line_start=line_start,
            line_end=line_end,
        ),
        cluster="agents",
    )


def _confirmed_response(reason: str = "Problem is present") -> str:
    return json.dumps({"verdict": "confirmed", "reason": reason})


def _rejected_response(reason: str = "Problem not found") -> str:
    return json.dumps({"verdict": "rejected", "reason": reason})


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — bogus candidate (problem absent) is dropped with a logged reason
# ─────────────────────────────────────────────────────────────────────────────

class TestBogusCandidateDropped:
    """AC1: a deliberately bogus candidate is rejected with a reason."""

    def test_symbol_not_in_file_rejected_at_existence(
        self, skip_llm_filt: Gate1Filter, repo: Path
    ) -> None:
        """Citing a symbol that does not exist → rejected at existence stage."""
        c = _make_candidate(symbol="nonexistent_function")
        accepted, rejected = skip_llm_filt.filter([c], repo)

        assert accepted == []
        assert len(rejected) == 1
        r = rejected[0]
        assert r.stage == "existence"
        assert r.accepted is False
        assert "nonexistent_function" in r.reason

    def test_llm_says_problem_absent_rejected_at_presence(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        """LLM verdict='rejected' → problem not present → dropped with reason."""
        c = _make_candidate(symbol="parse_config")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_rejected_response("The TODO comment is just a note, no bug present"),
        ):
            accepted, rejected = filt.filter([c], repo)

        assert accepted == []
        assert len(rejected) == 1
        r = rejected[0]
        assert r.stage == "presence"
        assert r.accepted is False
        assert "not found" in r.reason or "absent" in r.reason or "TODO" in r.reason

    def test_confirmed_candidate_accepted(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        """LLM verdict='confirmed' → candidate passes Gate 1."""
        c = _make_candidate(symbol="parse_config")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_confirmed_response("parse_config lacks input validation"),
        ):
            accepted, rejected = filt.filter([c], repo)

        assert len(accepted) == 1
        assert accepted[0].title == "Add input validation"
        assert rejected == []

    def test_rejected_reason_logged(
        self, filt: Gate1Filter, repo: Path, caplog
    ) -> None:
        """The rejection reason appears in the log."""
        c = _make_candidate(symbol="nonexistent_function")

        import logging
        with caplog.at_level(logging.WARNING, logger="tools.auto.gate1_filter"):
            accepted, rejected = filt.filter([c], repo)

        assert any("nonexistent_function" in msg for msg in caplog.messages)


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — duplicate candidates are merged
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateMerging:
    """AC2: identical (file, anchor, title) candidates → only one accepted."""

    def test_exact_duplicate_deduplicated(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c1 = _make_candidate(title="Add input validation", symbol="parse_config")
        c2 = _make_candidate(title="Add input validation", symbol="parse_config")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_confirmed_response(),
        ):
            accepted, rejected = filt.filter([c1, c2], repo)

        assert len(accepted) == 1
        dup_rejected = [r for r in rejected if r.stage == "duplicate"]
        assert len(dup_rejected) == 1

    def test_different_titles_not_deduplicated(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c1 = _make_candidate(title="Task Alpha", symbol="parse_config")
        c2 = _make_candidate(title="Task Beta", symbol="parse_config")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_confirmed_response(),
        ):
            accepted, rejected = filt.filter([c1, c2], repo)

        assert len(accepted) == 2
        assert not any(r.stage == "duplicate" for r in rejected)

    def test_different_symbols_not_deduplicated(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c1 = _make_candidate(title="Same title", symbol="parse_config")
        c2 = _make_candidate(title="Same title", symbol="another_func")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_confirmed_response(),
        ):
            accepted, rejected = filt.filter([c1, c2], repo)

        assert len(accepted) == 2

    def test_title_case_insensitive_dedup(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        """Title comparison for dedup is case-insensitive."""
        c1 = _make_candidate(title="Add Input Validation", symbol="parse_config")
        c2 = _make_candidate(title="add input validation", symbol="parse_config")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_confirmed_response(),
        ):
            accepted, rejected = filt.filter([c1, c2], repo)

        assert len(accepted) == 1
        assert len([r for r in rejected if r.stage == "duplicate"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Existence checks
# ─────────────────────────────────────────────────────────────────────────────

class TestExistenceChecks:
    def test_missing_file_rejected(
        self, skip_llm_filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(file="tools/does_not_exist.py", symbol="func")
        accepted, rejected = skip_llm_filt.filter([c], repo)

        assert accepted == []
        r = rejected[0]
        assert r.stage == "existence"
        assert "not found" in r.reason

    def test_valid_symbol_passes(
        self, skip_llm_filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol="parse_config")
        accepted, rejected = skip_llm_filt.filter([c], repo)

        assert len(accepted) == 1
        assert rejected == []

    def test_valid_line_range_passes(
        self, skip_llm_filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol=None, line_start=1, line_end=3)
        accepted, rejected = skip_llm_filt.filter([c], repo)

        assert len(accepted) == 1
        assert rejected == []

    def test_line_start_out_of_range_rejected(
        self, skip_llm_filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol=None, line_start=9999, line_end=10000)
        accepted, rejected = skip_llm_filt.filter([c], repo)

        assert accepted == []
        r = rejected[0]
        assert r.stage == "existence"
        assert "out of range" in r.reason

    def test_no_llm_call_when_existence_fails(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        """Existence failure short-circuits; Stage B LLM is never called."""
        c = _make_candidate(file="tools/missing.py", symbol="func")

        with patch("tools.llm_stream.request_completion") as mock_llm:
            filt.filter([c], repo)

        mock_llm.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Presence checks (LLM stage)
# ─────────────────────────────────────────────────────────────────────────────

class TestPresenceChecks:
    def test_confirmed_verdict_accepted(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol="parse_config")
        with patch(
            "tools.llm_stream.request_completion",
            return_value=_confirmed_response("validation gap confirmed"),
        ):
            accepted, rejected = filt.filter([c], repo)

        assert len(accepted) == 1
        assert rejected == []

    def test_rejected_verdict_rejected(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol="parse_config")
        with patch(
            "tools.llm_stream.request_completion",
            return_value=_rejected_response("already handles dict check"),
        ):
            accepted, rejected = filt.filter([c], repo)

        assert accepted == []
        assert rejected[0].stage == "presence"

    def test_malformed_json_fail_closed(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol="parse_config")
        with patch(
            "tools.llm_stream.request_completion",
            return_value="not json at all",
        ):
            accepted, rejected = filt.filter([c], repo)

        assert accepted == []
        assert "JSON" in rejected[0].reason or "closed" in rejected[0].reason

    def test_unrecognised_verdict_fail_closed(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol="parse_config")
        with patch(
            "tools.llm_stream.request_completion",
            return_value=json.dumps({"verdict": "maybe", "reason": "not sure"}),
        ):
            accepted, rejected = filt.filter([c], repo)

        assert accepted == []
        assert "closed" in rejected[0].reason or "unrecognised" in rejected[0].reason

    def test_network_exception_fail_closed(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol="parse_config")
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=ConnectionError("refused"),
        ):
            accepted, rejected = filt.filter([c], repo)

        assert accepted == []
        assert "refused" in rejected[0].reason or "LLM call failed" in rejected[0].reason

    def test_think_block_stripped_before_parse(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        """<think>…</think> wrapper removed before JSON parsing."""
        c = _make_candidate(symbol="parse_config")
        inner = _confirmed_response("thinking was stripped")
        wrapped = f"<think>Internal reasoning…</think>\n{inner}"

        with patch(
            "tools.llm_stream.request_completion",
            return_value=wrapped,
        ):
            accepted, rejected = filt.filter([c], repo)

        assert len(accepted) == 1

    def test_markdown_fences_stripped_before_parse(
        self, filt: Gate1Filter, repo: Path
    ) -> None:
        c = _make_candidate(symbol="parse_config")
        inner = _confirmed_response("fences removed")
        fenced = f"```json\n{inner}\n```"

        with patch(
            "tools.llm_stream.request_completion",
            return_value=fenced,
        ):
            accepted, rejected = filt.filter([c], repo)

        assert len(accepted) == 1

    def test_skip_llm_bypasses_stage_b(
        self, skip_llm_filt: Gate1Filter, repo: Path
    ) -> None:
        """skip_llm=True: Stage B is skipped; all existence-passing candidates accepted."""
        c = _make_candidate(symbol="parse_config")

        with patch("tools.llm_stream.request_completion") as mock_llm:
            accepted, rejected = skip_llm_filt.filter([c], repo)

        mock_llm.assert_not_called()
        assert len(accepted) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Integration — mixed outcomes
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_mixed_candidates(self, filt: Gate1Filter, repo: Path) -> None:
        """Three candidates: one accepted, one existence-rejected, one presence-rejected."""
        good     = _make_candidate(title="Fix validation",  symbol="parse_config")
        no_file  = _make_candidate(title="Fix missing",     file="tools/gone.py",    symbol="func")
        bad_llm  = _make_candidate(title="Fix another_func", symbol="another_func")

        llm_responses = iter([
            _confirmed_response("validation gap real"),   # for good
            _rejected_response("nothing wrong here"),     # for bad_llm
        ])

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=list(llm_responses),
        ):
            accepted, rejected = filt.filter([good, no_file, bad_llm], repo)

        assert len(accepted) == 1
        assert accepted[0].title == "Fix validation"

        stages = {r.stage for r in rejected}
        assert "existence" in stages
        assert "presence" in stages

    def test_all_candidates_accepted(self, filt: Gate1Filter, repo: Path) -> None:
        c1 = _make_candidate(title="Task One", symbol="parse_config")
        c2 = _make_candidate(title="Task Two", symbol="another_func")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_confirmed_response(),
        ):
            accepted, rejected = filt.filter([c1, c2], repo)

        assert len(accepted) == 2
        assert rejected == []

    def test_empty_input(self, filt: Gate1Filter, repo: Path) -> None:
        accepted, rejected = filt.filter([], repo)
        assert accepted == []
        assert rejected == []


# ─────────────────────────────────────────────────────────────────────────────
# filter_candidates() convenience factory
# ─────────────────────────────────────────────────────────────────────────────

class TestFilterCandidatesFactory:
    def test_factory_reads_config(
        self, minimal_config: configparser.ConfigParser, repo: Path
    ) -> None:
        c = _make_candidate(symbol="parse_config")

        with patch(
            "tools.llm_stream.request_completion",
            return_value=_confirmed_response(),
        ):
            accepted, rejected = filter_candidates([c], repo, minimal_config)

        assert len(accepted) == 1

    def test_factory_skip_llm_via_config(
        self, minimal_config: configparser.ConfigParser, repo: Path
    ) -> None:
        minimal_config.set("gate1", "skip_llm", "true")
        c = _make_candidate(symbol="parse_config")

        with patch("tools.llm_stream.request_completion") as mock_llm:
            accepted, rejected = filter_candidates([c], repo, minimal_config)

        mock_llm.assert_not_called()
        assert len(accepted) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for internal helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestFingerprint:
    def test_same_candidate_same_fingerprint(self) -> None:
        c1 = _make_candidate(title="Task A", symbol="func_x")
        c2 = _make_candidate(title="Task A", symbol="func_x")
        assert _fingerprint(c1) == _fingerprint(c2)

    def test_different_symbol_different_fingerprint(self) -> None:
        c1 = _make_candidate(title="Task A", symbol="func_x")
        c2 = _make_candidate(title="Task A", symbol="func_y")
        assert _fingerprint(c1) != _fingerprint(c2)

    def test_different_title_different_fingerprint(self) -> None:
        c1 = _make_candidate(title="Task A", symbol="func_x")
        c2 = _make_candidate(title="Task B", symbol="func_x")
        assert _fingerprint(c1) != _fingerprint(c2)

    def test_line_range_anchor_used_when_no_symbol(self) -> None:
        c = _make_candidate(symbol=None, line_start=5, line_end=10)
        fp = _fingerprint(c)
        assert "L5-10" in fp


class TestLocationStr:
    def test_symbol_present(self) -> None:
        loc = CitedLocation(file="a.py", symbol="my_func")
        assert "my_func" in _location_str(loc)
        assert "a.py" in _location_str(loc)

    def test_line_range_present(self) -> None:
        loc = CitedLocation(file="a.py", line_start=3, line_end=10)
        s = _location_str(loc)
        assert "3" in s and "10" in s


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello", max_chars=100) == "hello"

    def test_long_text_truncated(self) -> None:
        text = "x" * 200
        result = _truncate(text, max_chars=50)
        assert len(result) > 50          # includes the notice
        assert result.startswith("x" * 50)
        assert "truncated" in result


class TestFilterResultDataclass:
    def test_fields(self) -> None:
        c = _make_candidate()
        r = FilterResult(candidate=c, accepted=True, stage="presence", reason="ok")
        assert r.accepted is True
        assert r.stage == "presence"
        assert r.reason == "ok"
        assert r.candidate is c
