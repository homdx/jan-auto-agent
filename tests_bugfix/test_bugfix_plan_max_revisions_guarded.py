"""tests_bugfix/test_bugfix_plan_max_revisions_guarded.py

BUG 1 — unguarded ``plan_max_revisions`` config parse in
``tools/auto/pipeline.py:_run_plan_phase``.

The plan-revision loop is bounded by two ``[architect]`` config knobs::

    _max_rewrites = cfg.getint("architect", "max_rewrites", fallback=1)      # guarded
    _plan_max_rev = cfg.getint("architect", "plan_max_revisions",            # was UNGUARDED
                                fallback=_max_rewrites)

``max_rewrites`` was already wrapped in try/except by an earlier fix
(OPT-1). Its sibling, the OUTER read of ``plan_max_revisions``, was not.
``configparser.getint`` only falls back to ``fallback=`` when the key is
*missing* — a key that is *present but non-numeric* ("3x", "auto",
"not_a_number") raises ``ValueError`` straight out of the call. Nothing on
the path up to the ``--auto`` entrypoint caught it, so a single malformed
``plan_max_revisions`` value crashed the entire plan phase before any task
ran.

The fix wraps the outer read in the same try/except pattern as the inner
one: a malformed value degrades to the already-sanitised
``_max_rewrites`` instead of propagating.

IMPORTANT — this bug only reproduces when a plan validator is active.
Both getint calls live inside ``if _plan_validator is not None:``. A test
that mocks ``_build_plan_validator`` to return ``None`` will skip this
whole block and pass on unfixed code too, giving false confidence. Every
test below supplies a real (mocked) validator object so the guarded code
path is actually exercised.
"""

from __future__ import annotations

import configparser
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.backlog_prioritiser import PrioritisedBacklog
from tools.auto.pipeline import _run_plan_phase


def _base_cfg() -> configparser.ConfigParser:
    """A config that enables the plan validator in creative mode."""
    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
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
"""
    )
    return cfg


def _controller(tmp_path: Path) -> SimpleNamespace:
    """Minimal controller-like object accepted by _run_plan_phase."""
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    state = MagicMock()
    state.all_tasks.return_value = []
    state.agent_dir = agent_dir
    return SimpleNamespace(
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


def _run_plan_phase_with(cfg, controller, validator):
    """Drive _run_plan_phase with dependencies stubbed out so only the
    plan_max_revisions / max_rewrites parsing under test is exercised."""

    def _noop_backlog(candidates, task_id_prefix="AUTO-T"):
        return PrioritisedBacklog(auto_tasks=[], manual_suggestions=[])

    with (
        patch("tools.auto.pipeline._build_plan_validator", return_value=validator),
        patch("tools.auto.pipeline.ingest_repo", return_value=[]),
        patch("tools.auto.pipeline.review_clusters", return_value=[]),
        patch(
            "tools.auto.pipeline.filter_candidates",
            side_effect=lambda candidates, *a, **k: ([], []),
        ),
        patch("tools.auto.pipeline.build_backlog", side_effect=_noop_backlog),
        patch("tools.auto.pipeline._emit_without_git"),
    ):
        _run_plan_phase(controller, cfg)


class TestPlanMaxRevisionsMalformed:
    """Core regression coverage: a malformed plan_max_revisions must never
    escape _run_plan_phase as a ValueError."""

    def test_malformed_plan_max_revisions_does_not_crash(self, tmp_path):
        """A present-but-non-numeric plan_max_revisions must degrade to
        the max_rewrites fallback instead of raising."""
        cfg = _base_cfg()
        cfg["architect"]["plan_max_revisions"] = "not_a_number"

        validator = MagicMock()
        validator.validate_plan.return_value = (True, "ok")

        # Must not raise ValueError from the unguarded getint.
        _run_plan_phase_with(cfg, _controller(tmp_path), validator)

    def test_both_knobs_malformed_still_degrades(self, tmp_path):
        """max_rewrites and plan_max_revisions malformed at the same time
        must still degrade to the documented default of 1, not raise."""
        cfg = _base_cfg()
        cfg["architect"]["plan_max_revisions"] = "3x"
        cfg["architect"]["max_rewrites"] = "two"

        validator = MagicMock()
        validator.validate_plan.return_value = (True, "ok")

        _run_plan_phase_with(cfg, _controller(tmp_path), validator)
        # Fallback chain is 1 -> 1: exactly one validation round.
        assert validator.validate_plan.call_count == 1

    def test_malformed_value_with_no_max_rewrites_key(self, tmp_path):
        """A malformed plan_max_revisions with max_rewrites entirely absent
        (fallback chain 1 -> 1) must not raise."""
        cfg = _base_cfg()
        cfg["architect"]["plan_max_revisions"] = "auto"
        # max_rewrites deliberately absent.

        validator = MagicMock()
        validator.validate_plan.return_value = (True, "ok")

        _run_plan_phase_with(cfg, _controller(tmp_path), validator)
        assert validator.validate_plan.call_count == 1


class TestPlanMaxRevisionsFallbackValue:
    """The fallback isn't just "don't crash" — it must actually resolve to
    the sanitised max_rewrites value, and a valid explicit value must still
    be honoured (the fix must not mask real configuration)."""

    def test_malformed_value_falls_back_to_max_rewrites(self, tmp_path):
        """With plan_max_revisions malformed and max_rewrites=3, the
        revision loop must run up to 3 times (until validate_plan
        approves), proving the fallback value — not just 1 — was used."""
        cfg = _base_cfg()
        cfg["architect"]["plan_max_revisions"] = "not_a_number"
        cfg["architect"]["max_rewrites"] = "3"

        validator = MagicMock()
        # Always request a revision so the loop runs to its cap.
        validator.validate_plan.return_value = (False, "needs work")

        _run_plan_phase_with(cfg, _controller(tmp_path), validator)
        assert validator.validate_plan.call_count == 3

    def test_valid_plan_max_revisions_is_honoured(self, tmp_path):
        """Sanity: a valid explicit plan_max_revisions must still bound
        the loop on its own terms, independent of max_rewrites."""
        cfg = _base_cfg()
        cfg["architect"]["plan_max_revisions"] = "2"
        cfg["architect"]["max_rewrites"] = "99"

        validator = MagicMock()
        validator.validate_plan.return_value = (False, "needs work")

        _run_plan_phase_with(cfg, _controller(tmp_path), validator)
        assert validator.validate_plan.call_count == 2

    def test_missing_plan_max_revisions_uses_max_rewrites(self, tmp_path):
        """When the key is simply absent (the normal, non-malformed
        fallback path), behaviour must be unchanged by the fix."""
        cfg = _base_cfg()
        cfg["architect"]["max_rewrites"] = "2"
        # plan_max_revisions deliberately absent.

        validator = MagicMock()
        validator.validate_plan.return_value = (False, "needs work")

        _run_plan_phase_with(cfg, _controller(tmp_path), validator)
        assert validator.validate_plan.call_count == 2


class TestMalformedValueIsReported:
    """The degrade must not be silent.

    A run that quietly ignores a malformed agents.ini key leaves the
    operator with no way to discover the typo -- the plan phase simply
    behaves as if the knob were absent, and the symptom (fewer plan
    revisions than configured) looks like normal operation. Every sibling
    config guard in this batch reports (max_tasks_creative, and each of
    TaskRewriter's five reads); this one was the only guard that degraded
    without a word.
    """

    def test_warning_names_the_offending_key(self, tmp_path, caplog):
        cfg = _base_cfg()
        cfg["architect"]["max_rewrites"] = "3"
        cfg["architect"]["plan_max_revisions"] = "not_a_number"

        validator = MagicMock()
        with caplog.at_level(logging.WARNING, logger="tools.auto.pipeline"):
            _run_plan_phase_with(cfg, _controller(tmp_path), validator)

        assert any(
            "plan_max_revisions" in r.getMessage() for r in caplog.records
        ), "a malformed plan_max_revisions must be reported, not swallowed"

    def test_warning_states_the_value_used_instead(self, tmp_path, caplog):
        """Naming the key is half the message; the operator also needs to
        know what the run actually did, since it keeps going."""
        cfg = _base_cfg()
        cfg["architect"]["max_rewrites"] = "3"
        cfg["architect"]["plan_max_revisions"] = "3x"

        validator = MagicMock()
        with caplog.at_level(logging.WARNING, logger="tools.auto.pipeline"):
            _run_plan_phase_with(cfg, _controller(tmp_path), validator)

        messages = [r.getMessage() for r in caplog.records]
        assert any("plan_max_revisions" in m and "3" in m for m in messages)

    def test_well_formed_value_logs_nothing(self, tmp_path, caplog):
        """The guard must stay quiet on the happy path -- a warning on
        every ordinary run is noise that trains operators to ignore it."""
        cfg = _base_cfg()
        cfg["architect"]["max_rewrites"] = "3"
        cfg["architect"]["plan_max_revisions"] = "2"

        validator = MagicMock()
        with caplog.at_level(logging.WARNING, logger="tools.auto.pipeline"):
            _run_plan_phase_with(cfg, _controller(tmp_path), validator)

        assert not any(
            "plan_max_revisions" in r.getMessage() for r in caplog.records
        )

    def test_missing_key_logs_nothing(self, tmp_path, caplog):
        """An absent key is not an error -- fallback= handles it, and it
        must not be reported as a malformed value."""
        cfg = _base_cfg()
        cfg["architect"]["max_rewrites"] = "3"

        validator = MagicMock()
        with caplog.at_level(logging.WARNING, logger="tools.auto.pipeline"):
            _run_plan_phase_with(cfg, _controller(tmp_path), validator)

        assert not any(
            "plan_max_revisions" in r.getMessage() for r in caplog.records
        )
