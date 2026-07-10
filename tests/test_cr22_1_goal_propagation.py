"""tests/test_cr22_1_goal_propagation.py — AUTO-CR-22-1 acceptance tests.

The fact gate (CR-20) and the prosody gate (CR-21) both key their
keyword/fact detection off ``task.get("goal", "")``, but production never
populates ``task["goal"]`` — the architect only emits ``title``/
``instruction`` per task. AUTO-CR-22-1 makes InnerLoop propagate the
run-level goal into the task dict it hands to each gate, whenever the task
doesn't already carry its own ``goal``.

Covers:
  * the fact gate receives the run goal when the task has none
  * the prosody gate activates from the run goal alone (no keyword in the
    per-task instruction)
  * a task that already has its own ``goal`` is never overwritten
  * an empty ``run_goal`` is a no-op (regression — behaves as before CR-22-1)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from tools.auto.fact_validator import FactVerdict
from tools.auto.inner_loop import InnerLoop
from tools.auto.prosody import ProsodyValidator


# ── Shared stubs ──────────────────────────────────────────────────────────────

class _OkCoder:
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        return SimpleNamespace(succeeded=True, files_written=["chapter_01.md"])


class _OkExecutor:
    def run(self, task):
        return SimpleNamespace(
            passed=True, exit_code=0, stdout="", stderr="", traceback=""
        )


class _OkGate2Validator:
    last_missing_context: list = []

    def approve(self, task, exec_result, coder_result, base_dir=None):
        return True, ""


def _write_chapter(tmp_path, name="chapter_01.md", text="Просто текст.\n"):
    (tmp_path / name).write_text(text, encoding="utf-8")


def _make_loop(tmp_path, *, run_goal="", fact_validator=None, prosody_validator=None):
    return InnerLoop(
        coder=_OkCoder(),
        executor=_OkExecutor(),
        validator=_OkGate2Validator(),
        fact_validator=fact_validator,
        prosody_validator=prosody_validator,
        task_mode="creative",
        run_goal=run_goal,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_goal_injected_into_fact_check(tmp_path):
    """run_goal carries the fact into the dict passed to fact_validator.check,
    even though the task's own instruction omits it entirely."""
    _write_chapter(tmp_path)

    fact_validator = MagicMock()
    fact_validator.max_fact_revisions = 1
    fact_validator.check.return_value = FactVerdict(approved=True, reason="", unparseable=False)

    loop = _make_loop(
        tmp_path,
        run_goal="мама не работает",
        fact_validator=fact_validator,
    )
    task = {
        "id": "t1",
        "instruction": "Напиши главу про прогулку в парке.",
        "target_files": ["chapter_01.md"],
    }

    result = loop.run_task(task, tmp_path)

    assert result.passed is True
    fact_validator.check.assert_called_once()
    seen_task, seen_text = fact_validator.check.call_args[0]
    assert seen_task.get("goal") == "мама не работает"
    # original task dict must not be mutated
    assert "goal" not in task


def test_goal_injected_into_prosody_check(tmp_path):
    """The real ProsodyValidator activates from the run goal alone — the
    per-task instruction has no ритм/рифм keyword of its own."""
    _write_chapter(tmp_path, text="Один два три четыре\nПять шесть семь восемь\n")

    prosody_validator = ProsodyValidator()
    loop = _make_loop(
        tmp_path,
        run_goal="напиши стихи с ритмом и рифмой",
        prosody_validator=prosody_validator,
    )
    task = {
        "id": "t2",
        "instruction": "Глава 1.",
        "target_files": ["chapter_01.md"],
    }

    # Spy on check() to confirm it actually evaluated prosody (i.e. the gate
    # was active) rather than short-circuiting as a non-verse no-op.
    real_check = prosody_validator.check
    calls = []

    def _spy(task_arg, text_arg):
        calls.append(task_arg)
        return real_check(task_arg, text_arg)

    prosody_validator.check = _spy

    loop.run_task(task, tmp_path)

    assert calls, "prosody_validator.check was never called"
    assert "ритм" in calls[0].get("goal", "")


def test_existing_task_goal_not_overwritten(tmp_path):
    """A task that already carries its own goal keeps it — run_goal never
    clobbers an explicit per-task goal."""
    _write_chapter(tmp_path)

    fact_validator = MagicMock()
    fact_validator.max_fact_revisions = 1
    fact_validator.check.return_value = FactVerdict(approved=True, reason="", unparseable=False)

    loop = _make_loop(
        tmp_path,
        run_goal="run-level goal",
        fact_validator=fact_validator,
    )
    task = {
        "id": "t3",
        "goal": "task-level goal",
        "instruction": "Напиши главу.",
        "target_files": ["chapter_01.md"],
    }

    loop.run_task(task, tmp_path)

    seen_task, _ = fact_validator.check.call_args[0]
    assert seen_task.get("goal") == "task-level goal"


def test_no_run_goal_is_safe(tmp_path):
    """run_goal="" (default) is a no-op — behaves exactly as before CR-22-1:
    a task with no goal of its own is still checked with goal="" and the
    fact gate's verdict is unaffected."""
    _write_chapter(tmp_path)

    fact_validator = MagicMock()
    fact_validator.max_fact_revisions = 1
    fact_validator.check.return_value = FactVerdict(approved=True, reason="", unparseable=False)

    loop = _make_loop(tmp_path, run_goal="", fact_validator=fact_validator)
    task = {
        "id": "t4",
        "instruction": "Напиши главу.",
        "target_files": ["chapter_01.md"],
    }

    result = loop.run_task(task, tmp_path)

    assert result.passed is True
    seen_task, _ = fact_validator.check.call_args[0]
    assert seen_task.get("goal", "") == ""
