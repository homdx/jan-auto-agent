"""tests/test_auto_b4.py — Tests for AUTO-B4: Prioritise + attach acceptance checks.

Covers all ACs from the story:

  AC1 (every autonomous task has a runnable acceptance_check):
      - Tasks with empty or placeholder acceptance checks are moved to
        manual_suggestions and never appear in auto_tasks.
      - Tasks with real shell commands (pytest, python -m pytest, ./run.sh,
        make test, etc.) stay in auto_tasks.

  AC2 (check-less tasks land in IMPROVEMENTS.md under "Manual suggestions"):
      - to_improvements_md() renders manual_suggestions under a dedicated
        heading distinct from the autonomous-tasks section.
      - Autonomous tasks appear in the "Autonomous Tasks" section with their
        task ID, acceptance_check, instruction, and dependencies.

  AC3 (open decision: check-less tasks excluded from auto-run, assumed yes):
      - Confirmed by AC1: non-runnable checks → manual_suggestions only.

  Broader coverage:

  DependencyInference:
      - Same-file linear order: A's line_end < B's line_start → A dep of B.
      - Cross-file symbol reference: B's instruction names A's cited symbol
        and files are distinct → A dep of B.
      - No false dependency when files differ and instruction has no symbol ref.
      - No self-dependency.

  TopologicalSort:
      - Chain A→B→C emerges in correct order.
      - Independent tasks retain Architect (original_index) order.
      - Cycle detected: cycle tasks appended at end, no crash.
      - Empty input → empty output.
      - Single task → returned as-is.

  TaskIDs:
      - IDs follow the configured prefix + 1-based index.
      - Custom prefix respected.

  StateTaskConversion:
      - to_state_tasks() returns schema-valid dicts (passes make_task).
      - Dependencies are written into the task dict.
      - cited_locations list has the correct shape.

  ImprovementsMd:
      - Manual suggestions section present and has the right heading.
      - Autonomous section lists task IDs.
      - Empty manual / empty auto sections render gracefully.

  IsRunnableCheck:
      - Comprehensive fixture-based parametric tests.

  Integration:
      - End-to-end: mixed candidates → correct split, ordering, state dicts.
      - build_backlog() convenience factory works identically to
        BacklogPrioritiser().build().
"""

from __future__ import annotations

import pytest
from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.backlog_prioritiser import (
    BacklogPrioritiser,
    ReadyTask,
    _is_runnable_check,
    _topological_sort,
    build_backlog,
    to_improvements_md,
)
from tools.auto.state import _validate_task_schema


# ─────────────────────────────────────────────────────────────────────────────
# Candidate builder helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cand(
    *,
    title: str = "Some task",
    instruction: str = "Do something useful.",
    file: str = "tools/utils.py",
    symbol: str | None = "my_func",
    line_start: int | None = 1,
    line_end: int | None = 5,
    acceptance_check: str = "python -m pytest tests/ -q",
    cluster: str = "agents",
    target_files: list[str] | None = None,
    new_file: bool = False,
) -> CandidateTask:
    return CandidateTask(
        title            = title,
        instruction      = instruction,
        target_files     = target_files or [file],
        acceptance_check = acceptance_check,
        cited_location   = CitedLocation(
            file       = file,
            symbol     = symbol,
            line_start = line_start,
            line_end   = line_end,
            new_file   = new_file,
        ),
        cluster = cluster,
    )



def _rt(
    task_id: str,
    *,
    title: str = "t",
    file: str = "f.py",
    symbol: str | None = "fn",
    line_start: int | None = 1,
    line_end: int | None = 5,
    instruction: str = "do it",
    acceptance_check: str = "pytest",
    dependencies: list[str] | None = None,
    original_index: int = 0,
    cluster: str = "agents",
) -> ReadyTask:
    """Minimal ReadyTask factory for topology tests."""
    return ReadyTask(
        task_id        = task_id,
        candidate      = _cand(
            title            = title,
            file             = file,
            symbol           = symbol,
            line_start       = line_start,
            line_end         = line_end,
            instruction      = instruction,
            acceptance_check = acceptance_check,
            cluster          = cluster,
        ),
        dependencies   = dependencies or [],
        original_index = original_index,
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — every auto task has a runnable acceptance_check
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoTasksHaveRunnableChecks:
    def test_real_check_stays_in_auto(self) -> None:
        c = _cand(acceptance_check="python -m pytest tests/ -q")
        result = build_backlog([c])
        assert len(result.auto_tasks) == 1
        assert result.manual_suggestions == []

    def test_empty_check_goes_to_manual(self) -> None:
        c = _cand(acceptance_check="")
        result = build_backlog([c])
        assert result.auto_tasks == []
        assert len(result.manual_suggestions) == 1

    @pytest.mark.parametrize("check", [
        "N/A", "n/a", "None", "none", "TBD", "tbd",
        "manual review", "Manual Review", "MANUAL REVIEW",
        "code review", "human review", "peer review",
        "review manually", "no automated check", "not applicable",
        "unknown", "placeholder", "TODO",
    ])
    def test_placeholder_check_goes_to_manual(self, check: str) -> None:
        c = _cand(acceptance_check=check)
        result = build_backlog([c])
        assert result.auto_tasks == []
        assert len(result.manual_suggestions) == 1

    @pytest.mark.parametrize("check", [
        "pytest",
        "python -m pytest tests/ -q",
        "./run_tests.sh",
        "make test",
        "go test ./...",
        "cargo test",
        "npm test",
        "python -c 'import mymod; mymod.fn()'",
        "bash scripts/validate.sh",
    ])
    def test_real_check_stays_in_auto_parametric(self, check: str) -> None:
        c = _cand(acceptance_check=check)
        result = build_backlog([c])
        assert len(result.auto_tasks) == 1
        assert result.auto_tasks[0].acceptance_check == check

    def test_whitespace_only_check_goes_to_manual(self) -> None:
        c = _cand(acceptance_check="   ")
        result = build_backlog([c])
        assert result.auto_tasks == []
        assert len(result.manual_suggestions) == 1

    def test_mixed_split(self) -> None:
        auto = _cand(title="Auto task",   acceptance_check="pytest")
        manual = _cand(title="Manual task", acceptance_check="manual review")
        result = build_backlog([auto, manual])
        assert len(result.auto_tasks) == 1
        assert len(result.manual_suggestions) == 1
        assert result.auto_tasks[0].title == "Auto task"
        assert result.manual_suggestions[0].title == "Manual task"


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — check-less tasks land in IMPROVEMENTS.md under "Manual suggestions"
# ─────────────────────────────────────────────────────────────────────────────

class TestImprovementsMd:
    def test_manual_section_heading_present(self) -> None:
        auto   = _cand(title="Auto one",   acceptance_check="pytest")
        manual = _cand(title="Manual one", acceptance_check="manual review")
        backlog = build_backlog([auto, manual])
        md = to_improvements_md(backlog)
        assert "## Manual Suggestions" in md
        assert "Manual one" in md

    def test_auto_section_heading_present(self) -> None:
        c = _cand(title="Fix it", acceptance_check="pytest")
        backlog = build_backlog([c])
        md = to_improvements_md(backlog)
        assert "## Autonomous Tasks" in md
        assert "Fix it" in md

    def test_task_id_appears_in_auto_section(self) -> None:
        c = _cand(acceptance_check="pytest")
        backlog = build_backlog([c], task_id_prefix="AUTO-T")
        md = to_improvements_md(backlog)
        assert "AUTO-T1" in md

    def test_acceptance_check_in_auto_section(self) -> None:
        c = _cand(acceptance_check="make test")
        backlog = build_backlog([c])
        md = to_improvements_md(backlog)
        assert "make test" in md

    def test_manual_task_not_in_auto_section(self) -> None:
        manual = _cand(title="Refactor comments", acceptance_check="none")
        backlog = build_backlog([manual])
        md = to_improvements_md(backlog)
        assert "Refactor comments" in md
        # Should NOT appear as an autonomous task
        assert "AUTO-T" not in md  # no auto tasks → no IDs

    def test_empty_auto_section(self) -> None:
        manual = _cand(acceptance_check="tbd")
        backlog = build_backlog([manual])
        md = to_improvements_md(backlog)
        assert "No autonomous tasks" in md

    def test_empty_manual_section(self) -> None:
        auto = _cand(acceptance_check="pytest")
        backlog = build_backlog([auto])
        md = to_improvements_md(backlog)
        assert "No manual suggestions" in md

    def test_dependencies_in_md(self) -> None:
        a = _cand(title="Task A", file="tools/a.py", symbol="fn_a",
                  line_start=1, line_end=5, acceptance_check="pytest")
        b = _cand(title="Task B", file="tools/a.py", symbol="fn_b",
                  line_start=10, line_end=15, acceptance_check="pytest")
        backlog = build_backlog([a, b])
        md = to_improvements_md(backlog)
        # Task B should list Task A as a dependency
        assert "AUTO-T1" in md  # dependency reference in task B's entry

    def test_md_is_string(self) -> None:
        backlog = build_backlog([])
        assert isinstance(to_improvements_md(backlog), str)

    def test_manual_note_present(self) -> None:
        manual = _cand(acceptance_check="none")
        backlog = build_backlog([manual])
        md = to_improvements_md(backlog)
        # The note about exclusion from auto-run must be present
        assert "will not" in md.lower() or "not" in md.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Dependency inference — same-file linear order
# ─────────────────────────────────────────────────────────────────────────────

class TestSameFileLinearDependency:
    def test_earlier_lines_become_dependency(self) -> None:
        """Task at lines 1-5 must complete before task at lines 10-20."""
        a = _cand(title="Task A", file="tools/m.py", symbol="fn_a",
                  line_start=1,  line_end=5,  acceptance_check="pytest")
        b = _cand(title="Task B", file="tools/m.py", symbol="fn_b",
                  line_start=10, line_end=20, acceptance_check="pytest")
        backlog = build_backlog([a, b])
        task_a = backlog.auto_tasks[0]
        task_b = backlog.auto_tasks[1]
        assert task_a.task_id in task_b.dependencies

    def test_later_task_does_not_depend_on_earlier(self) -> None:
        """The dependency is directional: A→B, not B→A."""
        a = _cand(title="Task A", file="tools/m.py", line_start=1,  line_end=5,  acceptance_check="pytest")
        b = _cand(title="Task B", file="tools/m.py", line_start=10, line_end=20, acceptance_check="pytest")
        backlog = build_backlog([a, b])
        task_a = next(t for t in backlog.auto_tasks if t.title == "Task A")
        assert not task_a.dependencies

    def test_different_files_no_same_file_dep(self) -> None:
        a = _cand(title="Task A", file="tools/a.py", line_start=1, line_end=5,  acceptance_check="pytest")
        b = _cand(title="Task B", file="tools/b.py", line_start=1, line_end=5,  acceptance_check="pytest")
        backlog = build_backlog([a, b])
        for t in backlog.auto_tasks:
            assert not t.dependencies


# ─────────────────────────────────────────────────────────────────────────────
# Dependency inference — cross-file symbol reference
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossFileSymbolDependency:
    def test_instruction_mentioning_symbol_adds_dep(self) -> None:
        """B's instruction references A's cited symbol → A is a dep of B."""
        a = _cand(
            title="Define parse_config", file="tools/config.py",
            symbol="parse_config", line_start=1, line_end=10,
            acceptance_check="pytest",
        )
        b = _cand(
            title="Call parse_config from main", file="main.py",
            symbol="run", line_start=5, line_end=15,
            instruction="Update run() to call parse_config with the new schema.",
            acceptance_check="pytest",
        )
        backlog = build_backlog([a, b])
        task_a = next(t for t in backlog.auto_tasks if t.title == "Define parse_config")
        task_b = next(t for t in backlog.auto_tasks if t.title == "Call parse_config from main")
        assert task_a.task_id in task_b.dependencies

    def test_no_dep_when_files_overlap(self) -> None:
        """Cross-file rule doesn't fire when both tasks touch the same file."""
        a = _cand(
            title="Define helper",
            file="tools/utils.py", symbol="helper_fn",
            line_start=1, line_end=5, acceptance_check="pytest",
        )
        b = _cand(
            title="Use helper",
            file="tools/utils.py", symbol="consumer",
            line_start=20, line_end=30,
            instruction="Call helper_fn from consumer.",
            acceptance_check="pytest",
        )
        backlog = build_backlog([a, b])
        task_b = next(t for t in backlog.auto_tasks if t.title == "Use helper")
        # same-file dep may be added, but not a *cross-file* dep
        # (both tasks on the same file so cross-file rule must not fire)
        # We just check no *double* dependency is created
        assert task_b.dependencies.count(backlog.auto_tasks[0].task_id) <= 1

    def test_no_dep_without_symbol_in_instruction(self) -> None:
        """No dep added when instruction doesn't mention A's symbol."""
        a = _cand(title="Task A", file="tools/a.py", symbol="very_unique_func_xyz",
                  acceptance_check="pytest")
        b = _cand(title="Task B", file="main.py", symbol="main",
                  instruction="Improve error handling in main.",
                  acceptance_check="pytest")
        backlog = build_backlog([a, b])
        task_b = next(t for t in backlog.auto_tasks if t.title == "Task B")
        assert not task_b.dependencies


# ─────────────────────────────────────────────────────────────────────────────
# Topological sort
# ─────────────────────────────────────────────────────────────────────────────

class TestTopologicalSort:
    def test_chain_ordered_correctly(self) -> None:
        """A→B→C must appear in that order."""
        a = _rt("A", original_index=0)
        b = _rt("B", original_index=1, dependencies=["A"])
        c = _rt("C", original_index=2, dependencies=["B"])
        ordered = _topological_sort([a, b, c])
        ids = [t.task_id for t in ordered]
        assert ids.index("A") < ids.index("B") < ids.index("C")

    def test_independent_tasks_preserve_original_order(self) -> None:
        tasks = [_rt(f"T{i}", original_index=i) for i in range(5)]
        ordered = _topological_sort(tasks)
        assert [t.task_id for t in ordered] == [f"T{i}" for i in range(5)]

    def test_diamond_dep(self) -> None:
        """A→B, A→C, B+C→D.  A must come first, D must come last."""
        a = _rt("A", original_index=0)
        b = _rt("B", original_index=1, dependencies=["A"])
        c = _rt("C", original_index=2, dependencies=["A"])
        d = _rt("D", original_index=3, dependencies=["B", "C"])
        ordered = _topological_sort([a, b, c, d])
        ids = [t.task_id for t in ordered]
        assert ids[0] == "A"
        assert ids[-1] == "D"

    def test_cycle_does_not_crash(self) -> None:
        """Cyclic deps produce a warning but return all tasks."""
        a = _rt("A", original_index=0, dependencies=["B"])
        b = _rt("B", original_index=1, dependencies=["A"])
        ordered = _topological_sort([a, b])
        assert len(ordered) == 2

    def test_empty_input(self) -> None:
        assert _topological_sort([]) == []

    def test_single_task(self) -> None:
        t = _rt("X")
        assert _topological_sort([t]) == [t]

    def test_unknown_dep_ignored(self) -> None:
        """A dep referencing a non-existent task ID is silently ignored."""
        t = _rt("A", dependencies=["GHOST"])
        ordered = _topological_sort([t])
        assert len(ordered) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Task IDs
# ─────────────────────────────────────────────────────────────────────────────

class TestTaskIDs:
    def test_default_prefix(self) -> None:
        result = build_backlog([_cand(acceptance_check="pytest")])
        assert result.auto_tasks[0].task_id == "AUTO-T1"

    def test_sequential_ids(self) -> None:
        candidates = [_cand(title=f"Task {i}", acceptance_check="pytest") for i in range(4)]
        result = build_backlog(candidates)
        ids = [t.task_id for t in result.auto_tasks]
        assert ids == ["AUTO-T1", "AUTO-T2", "AUTO-T3", "AUTO-T4"]

    def test_custom_prefix(self) -> None:
        result = build_backlog(
            [_cand(acceptance_check="pytest")],
            task_id_prefix="PROJ-",
        )
        assert result.auto_tasks[0].task_id == "PROJ-1"

    def test_ids_skip_manual_tasks(self) -> None:
        """Manual tasks don't consume an ID slot; auto IDs remain dense."""
        auto   = _cand(title="A", acceptance_check="pytest")
        manual = _cand(title="M", acceptance_check="none")
        result = build_backlog([manual, auto])
        # Only one auto task — gets ID 1
        assert result.auto_tasks[0].task_id == "AUTO-T1"


# ─────────────────────────────────────────────────────────────────────────────
# State task conversion
# ─────────────────────────────────────────────────────────────────────────────

class TestStateTaskConversion:
    def test_schema_valid(self) -> None:
        c = _cand(acceptance_check="pytest")
        backlog = build_backlog([c])
        state_tasks = backlog.to_state_tasks()
        for t in state_tasks:
            _validate_task_schema(t)  # raises if invalid

    def test_fields_populated(self) -> None:
        c = _cand(
            title="Fix it",
            instruction="Add validation.",
            file="tools/a.py",
            symbol="fn",
            line_start=3,
            line_end=7,
            acceptance_check="make test",
        )
        backlog = build_backlog([c])
        t = backlog.to_state_tasks()[0]
        assert t["title"] == "Fix it"
        assert t["instruction"] == "Add validation."
        assert t["acceptance_check"] == "make test"
        assert t["target_files"] == ["tools/a.py"]
        assert t["cited_locations"][0]["symbol"] == "fn"
        assert t["cited_locations"][0]["line_start"] == 3

    def test_dependencies_in_state_task(self) -> None:
        a = _cand(title="Task A", file="f.py", line_start=1,  line_end=5,  acceptance_check="pytest")
        b = _cand(title="Task B", file="f.py", line_start=10, line_end=20, acceptance_check="pytest")
        backlog = build_backlog([a, b])
        state_tasks = backlog.to_state_tasks()
        task_b_dict = next(t for t in state_tasks if t["title"] == "Task B")
        assert len(task_b_dict["dependencies"]) == 1

    def test_empty_backlog(self) -> None:
        backlog = build_backlog([])
        assert backlog.to_state_tasks() == []

    def test_custom_status(self) -> None:
        c = _cand(acceptance_check="pytest")
        backlog = build_backlog([c])
        t = backlog.to_state_tasks(status="in_progress")[0]
        assert t["status"] == "in_progress"

    # Bugfix regression: new_file used to be silently dropped when a
    # candidate was converted into a persisted task dict.
    def test_new_file_flag_preserved(self) -> None:
        c = _cand(file="tools/brand_new.py", symbol=None, line_start=None,
                   line_end=None, acceptance_check="pytest", new_file=True)
        backlog = build_backlog([c])
        t = backlog.to_state_tasks()[0]
        assert t["cited_locations"][0]["new_file"] is True

    def test_new_file_flag_defaults_false(self) -> None:
        c = _cand(acceptance_check="pytest")  # new_file not passed -> False
        backlog = build_backlog([c])
        t = backlog.to_state_tasks()[0]
        assert t["cited_locations"][0]["new_file"] is False


# ─────────────────────────────────────────────────────────────────────────────
# _is_runnable_check unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestIsRunnableCheck:
    @pytest.mark.parametrize("check,expected", [
        ("",                    False),
        ("   ",                 False),
        ("N/A",                 False),
        ("n/a",                 False),
        ("None",                False),
        ("NONE",                False),
        ("tbd",                 False),
        ("TBD",                 False),
        ("TODO",                False),
        ("manual review",       False),
        ("Manual Review",       False),
        ("code review",         False),
        ("human review",        False),
        ("peer review",         False),
        ("review manually",     False),
        ("no automated check",  False),
        ("not applicable",      False),
        ("unknown",             False),
        ("placeholder",         False),
        ("pytest",              True),
        ("python -m pytest",    True),
        ("make test",           True),
        ("./run.sh",            True),
        ("go test ./...",       True),
        ("echo ok",             True),
        ("bash ci.sh",          True),
        # Substring: "manual review" inside a real command is still runnable
        ("run_manual_review_tool.py", True),
    ])
    def test_runnable(self, check: str, expected: bool) -> None:
        assert _is_runnable_check(check) is expected


# ─────────────────────────────────────────────────────────────────────────────
# PrioritisedBacklog.summary()
# ─────────────────────────────────────────────────────────────────────────────

class TestBacklogSummary:
    def test_summary_counts(self) -> None:
        auto   = _cand(acceptance_check="pytest")
        manual = _cand(acceptance_check="none")
        backlog = build_backlog([auto, manual])
        summary = backlog.summary()
        assert "1" in summary  # 1 auto
        assert "1" in summary  # 1 manual

    def test_summary_zero_counts(self) -> None:
        backlog = build_backlog([])
        assert "0" in backlog.summary()


# ─────────────────────────────────────────────────────────────────────────────
# ReadyTask property pass-throughs
# ─────────────────────────────────────────────────────────────────────────────

class TestReadyTaskProperties:
    def test_passthrough_properties(self) -> None:
        c = _cand(
            title="T", instruction="I", file="f.py",
            acceptance_check="pytest", cluster="agents",
        )
        rt = ReadyTask(task_id="X1", candidate=c)
        assert rt.title == "T"
        assert rt.instruction == "I"
        assert rt.target_files == ["f.py"]
        assert rt.acceptance_check == "pytest"
        assert rt.cluster == "agents"
        assert rt.cited_location is c.cited_location


# ─────────────────────────────────────────────────────────────────────────────
# Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_end_to_end_ordering_and_split(self) -> None:
        """Full pipeline: mixed candidates, same-file dep, manual task."""
        task_a = _cand(
            title="Fix parse_config validation",
            file="tools/config.py", symbol="parse_config",
            line_start=1, line_end=10, acceptance_check="pytest",
        )
        task_b = _cand(
            title="Fix load_config after parse_config",
            file="tools/config.py", symbol="load_config",
            line_start=20, line_end=35, acceptance_check="pytest",
        )
        task_manual = _cand(
            title="Add docstrings to utils",
            file="tools/utils.py", symbol="helper",
            line_start=1, line_end=5, acceptance_check="manual review",
        )

        backlog = build_backlog([task_a, task_b, task_manual])

        # Two auto tasks, one manual
        assert len(backlog.auto_tasks) == 2
        assert len(backlog.manual_suggestions) == 1

        # task_a (lines 1-10) should come before task_b (lines 20-35) in same file
        titles = [t.title for t in backlog.auto_tasks]
        assert titles.index("Fix parse_config validation") < titles.index("Fix load_config after parse_config")

        # task_b depends on task_a
        task_b_rt = next(t for t in backlog.auto_tasks if "load_config" in t.title)
        assert len(task_b_rt.dependencies) == 1

        # State tasks are schema-valid
        for st in backlog.to_state_tasks():
            _validate_task_schema(st)

        # IMPROVEMENTS.md has both sections and the manual task
        md = to_improvements_md(backlog)
        assert "## Autonomous Tasks" in md
        assert "## Manual Suggestions" in md
        assert "Add docstrings to utils" in md

    def test_build_backlog_factory_matches_class(self) -> None:
        """build_backlog() and BacklogPrioritiser().build() produce same structure."""
        c = _cand(acceptance_check="pytest")
        r1 = build_backlog([c])
        r2 = BacklogPrioritiser().build([c])
        assert len(r1.auto_tasks) == len(r2.auto_tasks)
        assert len(r1.manual_suggestions) == len(r2.manual_suggestions)

    def test_all_manual(self) -> None:
        candidates = [_cand(acceptance_check="none") for _ in range(3)]
        backlog = build_backlog(candidates)
        assert backlog.auto_tasks == []
        assert len(backlog.manual_suggestions) == 3
        assert backlog.to_state_tasks() == []

    def test_all_auto(self) -> None:
        candidates = [
            _cand(title=f"T{i}", file=f"tools/{i}.py", acceptance_check="pytest")
            for i in range(4)
        ]
        backlog = build_backlog(candidates)
        assert len(backlog.auto_tasks) == 4
        assert backlog.manual_suggestions == []