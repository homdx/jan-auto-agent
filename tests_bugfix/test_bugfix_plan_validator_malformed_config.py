"""tests_bugfix/test_bugfix_plan_validator_malformed_config.py

_build_plan_validator() in tools/auto/pipeline.py is documented to
"Return None when the feature is disabled or the mode is not creative,
so the calling code stays a no-op with a simple if guard." A try/except
block catches setup failures (missing [api_*] section, missing keys)
and returns None — but the getboolean("validate_plan_creative") call
at line 92 is OUTSIDE that guard, so a malformed boolean value (e.g.
"maybe") raises ValueError uncaught, crashing the entire --auto run
instead of degrading to "no plan validator".

Similarly, the nested getint for plan_max_revisions (line 284-287)
evaluates cfg.getint("architect", "max_rewrites", fallback=1) eagerly
as the fallback= argument, so a malformed max_rewrites raises
ValueError before the outer getint can use its fallback=1.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.pipeline import _build_plan_validator


def _base_cfg() -> configparser.ConfigParser:
    """A config that would build a plan validator if the feature is on."""
    cfg = configparser.ConfigParser()
    cfg.read_string("""
[api]
active     = local
verify_ssl = false

[api_local]
base_url   = http://localhost:11434/v1
api_key    = x
model      = test-model
api_format = openai

[architect]
validate_plan_creative = true
max_tasks_creative     = 1
temperature            = 0.2
max_tokens             = 512
max_file_chars         = 1500
max_files_per_review   = 3
""")
    return cfg


class TestBuildPlanValidatorMalformedConfig:
    def test_malformed_validate_plan_creative_returns_none(self):
        """A malformed validate_plan_creative value must not crash; it
        must degrade to None (feature disabled) per the docstring."""
        cfg = _base_cfg()
        cfg["architect"]["validate_plan_creative"] = "maybe"
        # Must not raise — must return None
        result = _build_plan_validator(cfg, "creative")
        assert result is None

    def test_missing_architect_section_returns_none(self):
        """A missing [architect] section must not raise NoSectionError;
        it must return None."""
        cfg = configparser.ConfigParser()
        cfg.read_string("""
[api]
active     = local
verify_ssl = false

[api_local]
base_url   = http://localhost:11434/v1
api_key    = x
model      = test-model
api_format = openai
""")
        # No [architect] section at all — getboolean raises NoSectionError
        result = _build_plan_validator(cfg, "creative")
        assert result is None

    def test_malformed_verify_ssl_returns_none(self):
        """A malformed verify_ssl in the [api] section is read inside the
        try/except, so it should degrade to None, not crash."""
        cfg = _base_cfg()
        cfg["api"]["verify_ssl"] = "not_a_bool"
        result = _build_plan_validator(cfg, "creative")
        assert result is None

    def test_valid_config_still_builds_validator(self):
        """Sanity: the happy path must still build a validator."""
        cfg = _base_cfg()
        # The validator is a ClusterReviewer; building it requires an LLM
        # call only at validate_plan time, not at construction time.
        result = _build_plan_validator(cfg, "creative")
        assert result is not None


class TestPlanMaxRevisionsMalformedMaxRewrites:
    """The nested getint for plan_max_revisions eagerly evaluates
    cfg.getint("architect", "max_rewrites", fallback=1) as the fallback
    argument to the outer getint. If max_rewrites is present but
    non-numeric, the inner getint raises ValueError before the outer
    getint can use its fallback.
    """

    def test_malformed_max_rewrites_does_not_crash_pipeline(self, tmp_path):
        """A malformed max_rewrites must not crash _run_plan_phase; the
        outer getint's fallback=1 should be used instead."""
        from tools.auto.pipeline import _run_plan_phase
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        cfg = _base_cfg()
        cfg["architect"]["plan_max_revisions"] = "2"
        cfg["architect"]["max_rewrites"] = "not_a_number"

        # _run_plan_phase needs a controller-like object; we only need
        # to get past the plan_max_revisions read without crashing, so
        # a minimal mock with the attributes _run_plan_phase accesses
        # before that point is enough.
        agent_dir = tmp_path / ".agent"
        agent_dir.mkdir()
        state = MagicMock()
        state.all_tasks.return_value = []
        state.agent_dir = agent_dir
        controller = SimpleNamespace(
            goal="test goal",
            base_dir=str(tmp_path),
            config_path="agents_stub.ini",
            state=state,
            task_mode="creative",
            run_trace=None,
            progress_display=None,
            metrics_stream=None,
            auto_tuner=None,
            dry_run=False,
            git=None,
            _get_collect_bridge=lambda mode: None,
        )

        # The plan validator must return None so the revision loop is
        # skipped — otherwise _run_plan_phase would try to call
        # validate_plan on it and crash for a different reason.
        from tools.auto.backlog_prioritiser import PrioritisedBacklog

        def _noop_backlog(candidates, task_id_prefix="AUTO-T"):
            return PrioritisedBacklog(auto_tasks=[], manual_suggestions=[])

        with (
            patch("tools.auto.pipeline._build_plan_validator", return_value=None),
            patch("tools.auto.pipeline.ingest_repo", return_value=[]),
            patch("tools.auto.pipeline.review_clusters", return_value=[]),
            patch("tools.auto.pipeline.filter_candidates", side_effect=lambda c, *a, **k: ([], [])),
            patch("tools.auto.pipeline.build_backlog", side_effect=_noop_backlog),
            patch("tools.auto.pipeline._emit_without_git"),
        ):
            # Must not raise ValueError from the nested getint
            _run_plan_phase(controller, cfg)
