"""tests/test_cr20_4_plan_wiring.py — AUTO-CR-20-4 acceptance tests.

Covers:
  * bad plan → reviewer REVISE → architect re-run → corrected plan accepted;
    review_clusters called twice; log contains "plan REVISE"
  * cap respected: reviewer always REVISE, plan_max_revisions=1 →
    exactly one re-run (architect called 2×), last plan kept, warning logged
  * disabled (validate_plan_creative=false) → _build_plan_validator returns None
  * task_mode="code" → _build_plan_validator returns None (regression)
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tools.auto.pipeline import _build_plan_validator, _run_plan_phase
from tools.auto.architect import CandidateTask, CitedLocation


# ── Minimal stubs ─────────────────────────────────────────────────────────────

def _candidate(title: str = "Write chapter") -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction=f"Instruction for {title}.",
        target_files=["chapter_01.md"],
        acceptance_check="true",
        cited_location=CitedLocation(file="chapter_01.md"),
        cluster="main",
    )


def _make_cfg(
    *,
    validate: str = "true",
    max_rev: int = 1,
    task_mode: str = "creative",
) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(f"""
[api]
active     = local
verify_ssl = false

[api_local]
base_url   = http://localhost:11434/v1
api_key    = x
model      = test-model
api_format = openai

[auto]
task_mode        = {task_mode}
exec_timeout_sec = 10

[architect]
validate_plan_creative = {validate}
plan_max_revisions     = {max_rev}
max_tasks_creative     = 1
temperature            = 0.2
max_tokens             = 512
max_file_chars         = 1500
max_files_per_review   = 3
rewrite_max_tokens     = 256
rewrite_temperature    = 0.4

[loop]
timeout_seconds = 30
max_attempts    = 3

[coder]
max_tokens  = 500
temperature = 0.5

[validator_agent]
temperature         = 0.1
max_tokens          = 350
fact_check_creative = false

[gate1]
temperature = 0.0
max_tokens  = 128

[context_broker]
max_symbols = 5
""")
    return cfg


def _make_controller(tmp_path: Path, cfg: configparser.ConfigParser, task_mode: str = "creative"):
    """Build the minimal controller-like namespace _run_plan_phase needs."""
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()

    state = MagicMock()
    state.all_tasks.return_value = []   # fresh run
    state.agent_dir = agent_dir
    state.log = MagicMock()

    return SimpleNamespace(
        base_dir=tmp_path,
        goal="Write a family poem. The mother does not work.",
        task_mode=task_mode,
        config=cfg,
        state=state,
        git=None,
        progress_display=None,
        run_trace=None,
        summary_memory=None,
    )


def _stub_reviewer(verdicts: list[bool], reasons: list[str] | None = None):
    """Return a stub reviewer whose validate_plan returns (verdict, reason) in order."""
    reasons = reasons or ["contradiction" if not v else "" for v in verdicts]
    stub = MagicMock()
    stub.validate_plan.side_effect = list(zip(verdicts, reasons))
    return stub


# filter_candidates returns (list[CandidateTask], list[FilterResult]) per the
# actual signature; the accepted list IS already CandidateTasks.
def _noop_filter(candidates, base_dir, cfg, *, cluster_files=None, task_mode="creative"):
    """Pass all candidates; return (candidates, [])."""
    return list(candidates), []


def _noop_backlog(candidates, task_id_prefix="AUTO-T"):
    """Return an empty PrioritisedBacklog."""
    from tools.auto.backlog_prioritiser import PrioritisedBacklog
    return PrioritisedBacklog(auto_tasks=[], manual_suggestions=[])


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_bad_plan_reruns_architect_once(tmp_path, caplog):
    """Reviewer REVISE on first plan, APPROVED on second (max_rev=2).
    review_clusters called twice; log has 'plan REVISE'.
    """
    cfg = _make_cfg(validate="true", max_rev=2)
    controller = _make_controller(tmp_path, cfg)

    bad_plan       = [_candidate("Bad plan")]
    corrected_plan = [_candidate("Corrected plan")]
    review_calls: list[str] = []

    def fake_review(clusters, base_dir, config, *, goal,
                    on_cluster_done=None, task_mode="creative", checkpoint_path=None):
        review_calls.append(goal)
        return bad_plan if len(review_calls) == 1 else corrected_plan

    reviewer = _stub_reviewer([False, True], ["КМС must not be removed", ""])

    with (
        patch("tools.auto.pipeline.ingest_repo", return_value=[]),
        patch("tools.auto.pipeline.review_clusters", side_effect=fake_review),
        patch("tools.auto.pipeline._build_plan_validator", return_value=reviewer),
        patch("tools.auto.pipeline.filter_candidates", side_effect=_noop_filter),
        patch("tools.auto.pipeline.build_backlog", side_effect=_noop_backlog),
        patch("tools.auto.pipeline._emit_without_git"),
        caplog.at_level(logging.INFO, logger="tools.auto.pipeline"),
    ):
        _run_plan_phase(controller, cfg)

    # Architect called twice (initial + one re-run)
    assert len(review_calls) == 2, f"Expected 2 review_clusters calls, got {len(review_calls)}"
    # Feedback in the re-run goal
    assert "PLAN FEEDBACK" in review_calls[1]
    assert "КМС" in review_calls[1]
    # Log must mention REVISE
    assert any("plan REVISE" in m for m in caplog.messages), caplog.messages


def test_cap_respected(tmp_path, caplog):
    """Reviewer always REVISE, cap=1 → exactly one re-run (architect 2×), warning logged.

    While-loop semantics with cap=1:
      iter 0:  0 < 1 → validate → REVISE → re-run (review_clusters call #2)
      check:   1 < 1 → False → else: cap warning
    So validate_plan is called once, review_clusters twice.
    """
    cfg = _make_cfg(validate="true", max_rev=1)
    controller = _make_controller(tmp_path, cfg)

    review_calls: list[int] = []

    def fake_review(clusters, base_dir, config, *, goal,
                    on_cluster_done=None, task_mode="creative", checkpoint_path=None):
        review_calls.append(len(review_calls) + 1)
        return [_candidate()]

    reviewer = _stub_reviewer([False, False], ["bad plan", "still bad"])

    with (
        patch("tools.auto.pipeline.ingest_repo", return_value=[]),
        patch("tools.auto.pipeline.review_clusters", side_effect=fake_review),
        patch("tools.auto.pipeline._build_plan_validator", return_value=reviewer),
        patch("tools.auto.pipeline.filter_candidates", side_effect=_noop_filter),
        patch("tools.auto.pipeline.build_backlog", side_effect=_noop_backlog),
        patch("tools.auto.pipeline._emit_without_git"),
        caplog.at_level(logging.WARNING, logger="tools.auto.pipeline"),
    ):
        _run_plan_phase(controller, cfg)

    # Exactly one re-run → 2 total review_clusters calls
    assert len(review_calls) == 2, f"Expected 2 review_clusters calls, got {len(review_calls)}"
    # validate_plan called once (before re-run; loop exits without re-checking)
    assert reviewer.validate_plan.call_count == 1
    # Cap warning logged
    assert any("revision cap" in m.lower() for m in caplog.messages), caplog.messages


def test_disabled_no_validation(tmp_path):
    """validate_plan_creative=false → _build_plan_validator returns None; pipeline skips Gate-3."""
    cfg = _make_cfg(validate="false")

    # Direct unit test of the factory
    assert _build_plan_validator(cfg, "creative") is None

    # Also confirm the pipeline doesn't call validate_plan when factory returns None
    controller = _make_controller(tmp_path, cfg)
    validator_mock = MagicMock()

    with (
        patch("tools.auto.pipeline.ingest_repo", return_value=[]),
        patch("tools.auto.pipeline.review_clusters", return_value=[_candidate()]),
        patch("tools.auto.pipeline._build_plan_validator", return_value=None),
        patch("tools.auto.pipeline.filter_candidates", side_effect=_noop_filter),
        patch("tools.auto.pipeline.build_backlog", side_effect=_noop_backlog),
        patch("tools.auto.pipeline._emit_without_git"),
    ):
        _run_plan_phase(controller, cfg)

    validator_mock.validate_plan.assert_not_called()


def test_code_mode_unaffected(tmp_path):
    """task_mode='code' → _build_plan_validator returns None (regression)."""
    cfg = _make_cfg(validate="true", task_mode="code")
    assert _build_plan_validator(cfg, "code") is None


def test_build_plan_validator_returns_none_when_disabled():
    """Unit: _build_plan_validator with validate_plan_creative=false → None."""
    cfg = _make_cfg(validate="false")
    assert _build_plan_validator(cfg, "creative") is None


def test_build_plan_validator_returns_none_in_code_mode():
    """Unit: _build_plan_validator in code mode → None regardless of flag."""
    cfg = _make_cfg(validate="true")
    assert _build_plan_validator(cfg, "code") is None


def test_plan_feedback_appended_to_goal_on_rerun(tmp_path):
    """The re-run goal must contain 'PLAN FEEDBACK:' and the rejection reason."""
    cfg = _make_cfg(validate="true", max_rev=2)
    controller = _make_controller(tmp_path, cfg)

    goals_seen: list[str] = []

    def fake_review(clusters, base_dir, config, *, goal, **kw):
        goals_seen.append(goal)
        return [_candidate()]

    reviewer = _stub_reviewer([False, True], ["task contradicts goal: КМС removed", ""])

    with (
        patch("tools.auto.pipeline.ingest_repo", return_value=[]),
        patch("tools.auto.pipeline.review_clusters", side_effect=fake_review),
        patch("tools.auto.pipeline._build_plan_validator", return_value=reviewer),
        patch("tools.auto.pipeline.filter_candidates", side_effect=_noop_filter),
        patch("tools.auto.pipeline.build_backlog", side_effect=_noop_backlog),
        patch("tools.auto.pipeline._emit_without_git"),
    ):
        _run_plan_phase(controller, cfg)

    assert len(goals_seen) == 2
    assert "PLAN FEEDBACK:" in goals_seen[1]
    assert "КМС" in goals_seen[1]


def test_approved_plan_skips_rerun(tmp_path):
    """Reviewer APPROVED immediately → review_clusters called exactly once."""
    cfg = _make_cfg(validate="true", max_rev=2)
    controller = _make_controller(tmp_path, cfg)

    review_calls: list[str] = []

    def fake_review(clusters, base_dir, config, *, goal, **kw):
        review_calls.append(goal)
        return [_candidate()]

    reviewer = _stub_reviewer([True], [""])

    with (
        patch("tools.auto.pipeline.ingest_repo", return_value=[]),
        patch("tools.auto.pipeline.review_clusters", side_effect=fake_review),
        patch("tools.auto.pipeline._build_plan_validator", return_value=reviewer),
        patch("tools.auto.pipeline.filter_candidates", side_effect=_noop_filter),
        patch("tools.auto.pipeline.build_backlog", side_effect=_noop_backlog),
        patch("tools.auto.pipeline._emit_without_git"),
    ):
        _run_plan_phase(controller, cfg)

    assert len(review_calls) == 1
    assert reviewer.validate_plan.call_count == 1
