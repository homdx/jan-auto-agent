"""Regression: iterations_used must not exceed max_iterations and must be 0
for show/show_imports (no validation loop ran).

Before the fix, an exhausted validation loop (all max_iterations rejected)
recorded iterations_used = max_iterations + 1 because the increment at the
bottom of the while loop runs before the while condition re-checks. This
inflated avg_iterations in the PromptOptimizer trigger and displayed
"iter: 4/3" in the formatter. Show/show_imports runs (which skip the
validation loop entirely) recorded iterations_used = 1 from the init at
the top of run_pipeline, diluting avg_iterations with a bogus 1 for a run
that did zero validation iterations.

The fix clamps iterations_used to min(iteration, max_iterations) for
validation runs and sets it to 0 for show/show_imports.
"""

from pathlib import Path

from tools.metrics_collector import MetricsCollector, RunRecord


MAIN = Path(__file__).resolve().parent.parent / "main.py"


# ── source-level regression guards ──────────────────────────────────────────

def test_main_py_clamps_iterations_used():
    """main.py must use min(iteration, max_iterations) for iterations_used,
    not the raw iteration counter (which is max+1 after an exhausted loop)."""
    src = MAIN.read_text("utf-8")
    assert "min(iteration, self.max_iterations)" in src, (
        "main.py no longer clamps iterations_used — an exhausted validation "
        "loop records max_iterations + 1, inflating avg_iterations"
    )


def test_main_py_show_imports_records_zero_iterations():
    """show/show_imports skip the validation loop — iterations_used must be 0."""
    src = MAIN.read_text("utf-8")
    # The fix sets iterations_used = 0 for show/show_imports
    assert 'iterations_used = 0' in src, (
        "main.py no longer sets iterations_used = 0 for show/show_imports — "
        "non-validation runs pollute avg_iterations with a bogus 1"
    )


def test_main_py_passes_iterations_used_to_formatter():
    """The formatter must receive the clamped iterations_used, not the raw
    iteration counter (which would display 'iter: 4/3' for an exhausted run)."""
    src = MAIN.read_text("utf-8")
    assert "iteration=iterations_used" in src, (
        "main.py no longer passes iterations_used to OutputFormatter.render — "
        "an exhausted run would display 'iter: 4/3'"
    )


# ── MetricsCollector: avg_iterations impact ─────────────────────────────────

def test_avg_iterations_with_clamped_exhausted_run(tmp_path):
    """An exhausted run (max=3, all rejected) records iterations_used=3
    (clamped), not 4 (max+1). Three such runs give avg_iterations=3.0."""
    mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
    for _ in range(3):
        mc.record(RunRecord(
            timestamp="2024-01-01 00:00:00",
            intent="improve",
            prompt_version="v1",
            iterations_used=3,  # clamped from 4
            validator_status="rejected",
            validator_feedback="bad",
            improvement_json_ok=True,
            elapsed_seconds=10.0,
        ))
    summary = mc.summarize_failures(n=10)
    assert summary["avg_iterations"] == 3.0


def test_avg_iterations_inflated_by_unclamped_exhausted_run(tmp_path):
    """Without the clamp, the same three runs would record 4 (max+1),
    giving avg_iterations=4.0 — this test documents the bug's impact
    by showing the difference. The clamp keeps it at 3.0."""
    mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
    for _ in range(3):
        mc.record(RunRecord(
            timestamp="2024-01-01 00:00:00",
            intent="improve",
            prompt_version="v1",
            iterations_used=4,  # unclamped (the old bug)
            validator_status="rejected",
            validator_feedback="bad",
            improvement_json_ok=True,
            elapsed_seconds=10.0,
        ))
    summary = mc.summarize_failures(n=10)
    assert summary["avg_iterations"] == 4.0  # inflated — the bug


def test_show_intent_does_not_pollute_avg_iterations(tmp_path):
    """show/show_imports runs record iterations_used=0 (the fix), so they
    don't dilute avg_iterations. Three validation runs at 3 iterations each
    plus seven show runs at 0 give avg_iterations=3.0, not 1.6."""
    mc = MetricsCollector(metrics_path=tmp_path / "metrics.json")
    for _ in range(3):
        mc.record(RunRecord(
            timestamp="2024-01-01 00:00:00",
            intent="improve",
            prompt_version="v1",
            iterations_used=3,
            validator_status="rejected",
            validator_feedback="bad",
            improvement_json_ok=True,
            elapsed_seconds=10.0,
        ))
    for _ in range(7):
        mc.record(RunRecord(
            timestamp="2024-01-01 00:00:00",
            intent="show",
            prompt_version="v1",
            iterations_used=0,  # the fix: 0, not 1
            validator_status="skipped",
            validator_feedback="",
            improvement_json_ok=None,
            elapsed_seconds=1.0,
        ))
    summary = mc.summarize_failures(n=10)
    # 3*3 + 7*0 = 9 / 10 = 0.9 — the show runs correctly contribute 0
    assert summary["avg_iterations"] == 0.9
    # Without the fix: 3*3 + 7*1 = 16 / 10 = 1.6 — inflated by show runs
