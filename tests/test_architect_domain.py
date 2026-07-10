"""tests/test_architect_domain.py — AUTO-DM-2: Architect domain-mode support.

Verifies all acceptance criteria from the EPIC:

  1. CitedLocation.is_valid("docs") returns True for file-only (no anchor).
  2. CitedLocation.is_valid("code") returns False for file-only (no anchor).
  3. task_mode="docs" → architect uses _SYSTEM_PROMPT_DOCS (or system_docs override).
  4. task_mode="creative" → architect uses _SYSTEM_PROMPT_CREATIVE (or system_creative override).
  5. task_mode="code" → behaviour identical to current (_SYSTEM_PROMPT_CODE used).
  6. Docs candidate with file + line_range but no symbol is accepted.
  7. Creative mode accepts empty acceptance_check (defaults to 'true').
  8. review_clusters factory accepts task_mode kwarg.
  9. Backward compat: CitedLocation.is_valid() with zero args still requires anchor.
 10. Backward compat: ClusterReviewer with no task_mode uses code-mode prompt.
 11. Backward compat: existing tests for is_valid() still pass.

All LLM calls are patched; no network I/O.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto.architect import (
    CitedLocation,
    ClusterReviewer,
    _SYSTEM_PROMPT_CODE,
    _SYSTEM_PROMPT_DOCS,
    _SYSTEM_PROMPT_CREATIVE,
    _SYSTEM_PROMPT,        # backward-compat alias
    review_clusters,
)
from tools.auto.repo_ingest import RepoCluster


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def cfg() -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_dict({
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
    return c


def _reviewer(cfg, task_mode="code") -> ClusterReviewer:
    return ClusterReviewer(
        config=cfg,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
        task_mode=task_mode,
    )


def _cluster(tmp_path: Path, content: str = "# doc\n") -> tuple[RepoCluster, Path]:
    f = tmp_path / "README.md"
    f.write_text(content, encoding="utf-8")
    return RepoCluster(name="docs", patterns=["*.md"], files=["README.md"]), tmp_path


def _item(**overrides) -> dict:
    """Minimal valid candidate dict."""
    base = {
        "title": "Fix heading",
        "instruction": "Add a missing introduction section.",
        "target_files": ["README.md"],
        "acceptance_check": "grep -q 'Introduction' README.md",
        "cited_location": {
            "file": "README.md",
            "symbol": None,
            "line_start": 1,
            "line_end": 5,
        },
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# 1 & 2 — CitedLocation.is_valid() with task_mode
# ─────────────────────────────────────────────────────────────────────────────

class TestCitedLocationIsValidMode:
    def test_docs_mode_file_only_is_valid(self):
        loc = CitedLocation(file="README.md", symbol=None, line_start=None)
        assert loc.is_valid("docs") is True

    def test_creative_mode_file_only_is_valid(self):
        loc = CitedLocation(file="story.md", symbol=None, line_start=None)
        assert loc.is_valid("creative") is True

    def test_code_mode_file_only_is_invalid(self):
        loc = CitedLocation(file="main.py", symbol=None, line_start=None)
        assert loc.is_valid("code") is False

    def test_code_mode_file_plus_symbol_is_valid(self):
        assert CitedLocation(file="a.py", symbol="fn").is_valid("code") is True

    def test_code_mode_file_plus_line_is_valid(self):
        assert CitedLocation(file="a.py", line_start=10).is_valid("code") is True

    def test_docs_mode_empty_file_is_invalid(self):
        assert CitedLocation(file="", symbol=None).is_valid("docs") is False

    def test_creative_mode_empty_file_is_invalid(self):
        assert CitedLocation(file="", line_start=1).is_valid("creative") is False

    # ── 9. Backward compat: zero-arg call still enforces anchor ──────────────

    def test_zero_arg_requires_anchor(self):
        """Existing call sites that pass no arg must keep current behaviour."""
        assert not CitedLocation(file="a.py", symbol=None, line_start=None).is_valid()

    def test_zero_arg_with_symbol_ok(self):
        assert CitedLocation(file="a.py", symbol="fn").is_valid()

    def test_zero_arg_with_line_ok(self):
        assert CitedLocation(file="a.py", line_start=5).is_valid()


# ─────────────────────────────────────────────────────────────────────────────
# 3 & 4 & 5 — ClusterReviewer prompt selection
# ─────────────────────────────────────────────────────────────────────────────

class TestClusterReviewerPromptSelection:
    def test_code_mode_uses_code_prompt(self, cfg):
        r = _reviewer(cfg, "code")
        assert r._system == _SYSTEM_PROMPT_CODE

    def test_docs_mode_uses_docs_prompt(self, cfg):
        r = _reviewer(cfg, "docs")
        assert r._system == _SYSTEM_PROMPT_DOCS

    def test_creative_mode_uses_creative_prompt(self, cfg):
        r = _reviewer(cfg, "creative")
        assert r._system == _SYSTEM_PROMPT_CREATIVE

    def test_no_task_mode_defaults_to_code(self, cfg):
        """Backward compat: omitting task_mode gives code-mode prompt."""
        r = ClusterReviewer(
            config=cfg, base_url="http://x", api_key="k",
            model="m", verify_ssl=False,
        )
        assert r._system == _SYSTEM_PROMPT_CODE

    def test_system_docs_ini_override(self, cfg):
        cfg.set("architect", "system_docs", "custom docs prompt")
        r = _reviewer(cfg, "docs")
        assert r._system == "custom docs prompt"

    def test_system_creative_ini_override(self, cfg):
        cfg.set("architect", "system_creative", "custom creative prompt")
        r = _reviewer(cfg, "creative")
        assert r._system == "custom creative prompt"

    def test_legacy_system_key_still_works_in_code_mode(self, cfg):
        cfg.set("architect", "system", "legacy override")
        r = _reviewer(cfg, "code")
        assert r._system == "legacy override"

    def test_system_docs_key_not_used_in_code_mode(self, cfg):
        """system_docs should not bleed into code mode."""
        cfg.set("architect", "system_docs", "docs prompt")
        r = _reviewer(cfg, "code")
        assert r._system == _SYSTEM_PROMPT_CODE


# ─────────────────────────────────────────────────────────────────────────────
# 6 — Docs candidate with file + line_range but no symbol is accepted
# ─────────────────────────────────────────────────────────────────────────────

class TestDocsCandidateAccepted:
    def test_file_and_line_range_no_symbol_accepted_in_docs_mode(self, cfg, tmp_path):
        cluster, base_dir = _cluster(tmp_path)
        item = _item(cited_location={
            "file": "README.md",
            "symbol": None,
            "line_start": 1,
            "line_end": 3,
        })
        payload = json.dumps([item])
        r = _reviewer(cfg, "docs")
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = r.review_clusters([cluster], base_dir, goal="improve docs")
        assert len(results) == 1
        assert results[0].cited_location.symbol is None
        assert results[0].cited_location.line_start == 1

    def test_file_only_no_anchor_accepted_in_docs_mode(self, cfg, tmp_path):
        """File alone (no line range, no symbol) is valid grounding in docs mode."""
        cluster, base_dir = _cluster(tmp_path)
        item = _item(cited_location={
            "file": "README.md",
            "symbol": None,
            "line_start": None,
            "line_end": None,
        })
        payload = json.dumps([item])
        r = _reviewer(cfg, "docs")
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = r.review_clusters([cluster], base_dir, goal="improve docs")
        assert len(results) == 1

    def test_file_only_rejected_in_code_mode(self, cfg, tmp_path):
        """Same candidate is rejected in code mode (original behaviour)."""
        cluster, base_dir = _cluster(tmp_path)
        item = _item(cited_location={
            "file": "README.md",
            "symbol": None,
            "line_start": None,
            "line_end": None,
        })
        payload = json.dumps([item])
        r = _reviewer(cfg, "code")
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = r.review_clusters([cluster], base_dir, goal="improve code")
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# 7 — Creative mode accepts empty acceptance_check (defaults to 'true')
# ─────────────────────────────────────────────────────────────────────────────

class TestCreativeModeAcceptanceCheck:
    def test_empty_acceptance_check_becomes_true_in_creative(self, cfg, tmp_path):
        cluster, base_dir = _cluster(tmp_path, "Once upon a time...\n")
        item = _item(
            acceptance_check="",
            cited_location={"file": "README.md", "symbol": None,
                            "line_start": 1, "line_end": 1},
        )
        payload = json.dumps([item])
        r = _reviewer(cfg, "creative")
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = r.review_clusters([cluster], base_dir, goal="improve story")
        assert len(results) == 1
        assert results[0].acceptance_check == "true"

    def test_empty_acceptance_check_still_rejected_in_code_mode(self, cfg, tmp_path):
        """Empty acceptance_check is not allowed in code mode."""
        cluster, base_dir = _cluster(tmp_path)
        item = _item(
            acceptance_check="",
            cited_location={"file": "README.md", "symbol": "fn",
                            "line_start": 1, "line_end": 1},
        )
        payload = json.dumps([item])
        r = _reviewer(cfg, "code")
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = r.review_clusters([cluster], base_dir)
        assert results == []

    def test_explicit_true_acceptance_check_preserved(self, cfg, tmp_path):
        cluster, base_dir = _cluster(tmp_path, "Story content\n")
        item = _item(
            acceptance_check="true",
            cited_location={"file": "README.md", "symbol": None,
                            "line_start": 1, "line_end": 1},
        )
        payload = json.dumps([item])
        r = _reviewer(cfg, "creative")
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = r.review_clusters([cluster], base_dir, goal="improve story")
        assert results[0].acceptance_check == "true"


# ─────────────────────────────────────────────────────────────────────────────
# 8 — review_clusters factory accepts task_mode kwarg
# ─────────────────────────────────────────────────────────────────────────────

class TestReviewClustersFactory:
    def test_accepts_task_mode_kwarg_docs(self, cfg, tmp_path):
        cluster, base_dir = _cluster(tmp_path)
        item = _item(cited_location={
            "file": "README.md", "symbol": None,
            "line_start": 1, "line_end": 3,
        })
        payload = json.dumps([item])
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = review_clusters(
                [cluster], base_dir, cfg,
                goal="improve docs",
                task_mode="docs",
            )
        assert len(results) == 1

    def test_accepts_task_mode_kwarg_creative(self, cfg, tmp_path):
        cluster, base_dir = _cluster(tmp_path)
        item = _item(cited_location={
            "file": "README.md", "symbol": None,
            "line_start": None, "line_end": None,
        })
        payload = json.dumps([item])
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = review_clusters(
                [cluster], base_dir, cfg,
                goal="polish story",
                task_mode="creative",
            )
        assert len(results) == 1

    def test_no_task_mode_defaults_to_code_behaviour(self, cfg, tmp_path):
        """Omitting task_mode keeps the original code-mode filter."""
        cluster, base_dir = _cluster(tmp_path)
        # File-only cited_location rejected in code mode
        item = _item(cited_location={
            "file": "README.md", "symbol": None,
            "line_start": None, "line_end": None,
        })
        payload = json.dumps([item])
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = review_clusters([cluster], base_dir, cfg)
        assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# 10 & 11 — Backward compat: existing test assertions still hold
# ─────────────────────────────────────────────────────────────────────────────

class TestBackwardCompat:
    def test_system_prompt_alias_equals_code_prompt(self):
        """_SYSTEM_PROMPT backward-compat alias must equal _SYSTEM_PROMPT_CODE."""
        assert _SYSTEM_PROMPT is _SYSTEM_PROMPT_CODE

    def test_is_valid_no_arg_with_symbol(self):
        assert CitedLocation(file="a.py", symbol="func").is_valid()

    def test_is_valid_no_arg_with_line_start(self):
        assert CitedLocation(file="a.py", line_start=10).is_valid()

    def test_is_valid_no_arg_no_anchor_false(self):
        assert not CitedLocation(file="a.py", symbol=None, line_start=None).is_valid()

    def test_is_valid_no_arg_empty_file_false(self):
        assert not CitedLocation(file="", symbol="func").is_valid()

    def test_code_mode_grounded_candidate_still_accepted(self, cfg, tmp_path):
        """Fully grounded code candidate unchanged by DM-2."""
        (tmp_path / "tools").mkdir()
        src = tmp_path / "tools" / "example.py"
        src.write_text("def fn(): pass\n", encoding="utf-8")
        cluster = RepoCluster(name="code", patterns=["tools/*"],
                              files=["tools/example.py"])
        item = {
            "title": "Add validation",
            "instruction": "Validate input.",
            "target_files": ["tools/example.py"],
            "acceptance_check": "pytest tests/",
            "cited_location": {
                "file": "tools/example.py",
                "symbol": "fn",
                "line_start": 1,
                "line_end": 1,
            },
        }
        payload = json.dumps([item])
        r = _reviewer(cfg, "code")
        with patch("tools.llm_stream.request_completion", return_value=payload):
            results = r.review_clusters([cluster], tmp_path, goal="improve code")
        assert len(results) == 1
        assert results[0].cited_location.symbol == "fn"