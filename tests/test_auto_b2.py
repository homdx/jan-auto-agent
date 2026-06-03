"""tests/test_auto_b2.py — Tests for AUTO-B2: Cluster review → candidate tasks.

Covers all ACs from the story:

  AC1: Candidates without a concrete cited_location (missing file, or missing
       both symbol and line_start) are rejected at parse time — zero candidates
       come back for a cluster whose LLM response is entirely ungrounded.

  AC2: strip_think is applied before JSON parsing — <think>…</think> blocks in
       the model output are silently removed and parsing still succeeds.

  AC3: JSON parse failures are fail-closed — a cluster returning non-JSON
       (or a network error) produces zero candidates and does NOT raise.

  AC4: A well-formed, fully-grounded LLM response produces the expected
       CandidateTask objects with all fields populated correctly.

  AC5: Markdown code fences (```json … ```) are stripped before parsing.

  AC6: review_clusters skips empty clusters (no LLM call, no candidates).

All LLM calls are patched; no network I/O occurs.
"""

from __future__ import annotations

import configparser
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.auto.architect import (
    CandidateTask,
    CitedLocation,
    ClusterReviewer,
    _to_int_or_none,
)
from tools.auto.repo_ingest import RepoCluster


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
        "architect": {"temperature": "0.2", "max_tokens": "512"},
        "loop":      {"timeout_seconds": "10"},
    })
    return cfg


@pytest.fixture()
def reviewer(minimal_config: configparser.ConfigParser) -> ClusterReviewer:
    return ClusterReviewer(
        config=minimal_config,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
    )


@pytest.fixture()
def single_file_cluster(tmp_path: Path) -> tuple[RepoCluster, Path]:
    """A cluster with one real file in a temp directory."""
    (tmp_path / "tools").mkdir()
    src = tmp_path / "tools" / "example.py"
    src.write_text(
        textwrap.dedent("""\
            def parse_config(raw):
                return raw  # TODO: validate
        """),
        encoding="utf-8",
    )
    cluster = RepoCluster(name="agents", patterns=["tools/*"], files=["tools/example.py"])
    return cluster, tmp_path


def _make_llm_response(items: list[dict]) -> str:
    """Return the JSON array string the mock LLM would produce."""
    return json.dumps(items)


def _grounded_item(**overrides) -> dict:
    """Return a valid, fully-grounded candidate dict."""
    base = {
        "title": "Add input validation to parse_config",
        "instruction": "Validate that raw is a dict before accessing keys.",
        "target_files": ["tools/example.py"],
        "acceptance_check": "python -m pytest tests/ -q",
        "cited_location": {
            "file": "tools/example.py",
            "symbol": "parse_config",
            "line_start": 1,
            "line_end": 2,
        },
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — well-formed response → correct CandidateTask objects
# ─────────────────────────────────────────────────────────────────────────────

class TestWellFormedResponse:
    def test_returns_candidate_task(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        cluster, base_dir = single_file_cluster
        payload = _make_llm_response([_grounded_item()])

        with patch("tools.auto.architect.request_completion", return_value=payload):
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert len(results) == 1
        c = results[0]
        assert isinstance(c, CandidateTask)
        assert c.title == "Add input validation to parse_config"
        assert c.cluster == "agents"
        assert c.target_files == ["tools/example.py"]
        assert c.acceptance_check == "python -m pytest tests/ -q"

    def test_cited_location_populated(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        cluster, base_dir = single_file_cluster
        payload = _make_llm_response([_grounded_item()])

        with patch("tools.auto.architect.request_completion", return_value=payload):
            results = reviewer.review_clusters([cluster], base_dir)

        loc = results[0].cited_location
        assert isinstance(loc, CitedLocation)
        assert loc.file == "tools/example.py"
        assert loc.symbol == "parse_config"
        assert loc.line_start == 1
        assert loc.line_end == 2

    def test_multiple_candidates_all_returned(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        cluster, base_dir = single_file_cluster
        items = [
            _grounded_item(title="Task A"),
            _grounded_item(title="Task B", cited_location={
                "file": "tools/example.py", "symbol": None,
                "line_start": 1, "line_end": 1,
            }),
        ]
        payload = _make_llm_response(items)

        with patch("tools.auto.architect.request_completion", return_value=payload):
            results = reviewer.review_clusters([cluster], base_dir)

        assert len(results) == 2
        assert {r.title for r in results} == {"Task A", "Task B"}


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — ungrounded candidates rejected at parse time
# ─────────────────────────────────────────────────────────────────────────────

class TestGroundingRejection:
    def test_missing_cited_location_entirely(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        """A candidate with no cited_location key is dropped."""
        bad = _grounded_item()
        del bad["cited_location"]
        payload = _make_llm_response([bad])

        with patch("tools.auto.architect.request_completion", return_value=payload):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results == []

    def test_cited_location_missing_file(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        """A cited_location without a file path is rejected."""
        bad = _grounded_item(cited_location={
            "file": "", "symbol": "parse_config", "line_start": 1, "line_end": 2,
        })
        payload = _make_llm_response([bad])

        with patch("tools.auto.architect.request_completion", return_value=payload):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results == []

    def test_cited_location_no_anchor(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        """A cited_location with a file but no symbol AND no line_start is rejected."""
        bad = _grounded_item(cited_location={
            "file": "tools/example.py",
            "symbol": None,
            "line_start": None,
            "line_end": None,
        })
        payload = _make_llm_response([bad])

        with patch("tools.auto.architect.request_completion", return_value=payload):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results == []

    def test_mix_grounded_and_ungrounded(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        """Only grounded candidates survive; ungrounded ones are silently dropped."""
        items = [
            _grounded_item(title="Valid task"),                     # grounded ✓
            _grounded_item(title="Bogus", cited_location={          # no anchor ✗
                "file": "tools/example.py", "symbol": None,
                "line_start": None, "line_end": None,
            }),
        ]
        payload = _make_llm_response(items)

        with patch("tools.auto.architect.request_completion", return_value=payload):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert len(results) == 1
        assert results[0].title == "Valid task"


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — strip_think applied before JSON parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestStripThink:
    def test_think_block_stripped(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        """<think>…</think> wrapper is silently removed before parsing."""
        inner = _make_llm_response([_grounded_item()])
        wrapped = f"<think>Let me reason about this cluster…</think>\n{inner}"

        with patch("tools.auto.architect.request_completion", return_value=wrapped):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert len(results) == 1

    def test_multiline_think_block(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        inner = _make_llm_response([_grounded_item(title="After think")])
        wrapped = "<think>\nLine 1\nLine 2\n</think>" + inner

        with patch("tools.auto.architect.request_completion", return_value=wrapped):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results[0].title == "After think"


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — fail-closed on bad output and network errors
# ─────────────────────────────────────────────────────────────────────────────

class TestFailClosed:
    def test_invalid_json_returns_empty(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        with patch("tools.auto.architect.request_completion", return_value="not json at all"):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results == []

    def test_json_object_not_array_returns_empty(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        with patch("tools.auto.architect.request_completion", return_value='{"oops": true}'):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results == []

    def test_network_exception_returns_empty(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        with patch(
            "tools.auto.architect.request_completion",
            side_effect=ConnectionError("refused"),
        ):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results == []

    def test_missing_required_fields_returns_empty(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        """A candidate missing 'instruction' is dropped."""
        bad = _grounded_item()
        del bad["instruction"]
        payload = _make_llm_response([bad])

        with patch("tools.auto.architect.request_completion", return_value=payload):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — markdown code fences stripped before parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkdownFenceStripping:
    def test_json_fenced_response(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        inner = _make_llm_response([_grounded_item()])
        fenced = f"```json\n{inner}\n```"

        with patch("tools.auto.architect.request_completion", return_value=fenced):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert len(results) == 1

    def test_plain_fenced_response(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        inner = _make_llm_response([_grounded_item(title="Fenced")])
        fenced = f"```\n{inner}\n```"

        with patch("tools.auto.architect.request_completion", return_value=fenced):
            results = reviewer.review_clusters([single_file_cluster[0]], single_file_cluster[1])

        assert results[0].title == "Fenced"


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — empty clusters are skipped (no LLM call)
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyClusterSkipped:
    def test_empty_cluster_produces_no_call(
        self, reviewer: ClusterReviewer, tmp_path: Path
    ) -> None:
        empty_cluster = RepoCluster(name="support", patterns=["*"], files=[])

        with patch(
            "tools.auto.architect.request_completion"
        ) as mock_llm:
            results = reviewer.review_clusters([empty_cluster], tmp_path)

        mock_llm.assert_not_called()
        assert results == []

    def test_mixed_empty_and_nonempty(
        self, reviewer: ClusterReviewer, single_file_cluster
    ) -> None:
        cluster, base_dir = single_file_cluster
        empty = RepoCluster(name="support", patterns=["*"], files=[])
        payload = _make_llm_response([_grounded_item()])

        with patch(
            "tools.auto.architect.request_completion", return_value=payload
        ) as mock_llm:
            results = reviewer.review_clusters([empty, cluster], base_dir)

        # LLM called exactly once (for the non-empty cluster only)
        assert mock_llm.call_count == 1
        assert len(results) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for CitedLocation.is_valid()
# ─────────────────────────────────────────────────────────────────────────────

class TestCitedLocationIsValid:
    def test_valid_with_symbol(self) -> None:
        assert CitedLocation(file="a.py", symbol="func").is_valid()

    def test_valid_with_line_start(self) -> None:
        assert CitedLocation(file="a.py", line_start=10).is_valid()

    def test_valid_with_both(self) -> None:
        assert CitedLocation(file="a.py", symbol="cls", line_start=1, line_end=20).is_valid()

    def test_invalid_empty_file(self) -> None:
        assert not CitedLocation(file="", symbol="func").is_valid()

    def test_invalid_no_anchor(self) -> None:
        assert not CitedLocation(file="a.py", symbol=None, line_start=None).is_valid()


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for _to_int_or_none helper
# ─────────────────────────────────────────────────────────────────────────────

class TestToIntOrNone:
    def test_int(self) -> None:
        assert _to_int_or_none(5) == 5

    def test_string_int(self) -> None:
        assert _to_int_or_none("10") == 10

    def test_none(self) -> None:
        assert _to_int_or_none(None) is None

    def test_invalid_string(self) -> None:
        assert _to_int_or_none("abc") is None

    def test_float_truncated(self) -> None:
        assert _to_int_or_none(3.9) == 3