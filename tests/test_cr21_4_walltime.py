"""tests/test_cr21_4_walltime.py — AUTO-CR-21-4 acceptance tests.

Validates the independent per-task wall-clock guard in InnerLoop:

  - test_walltime_exceeded_stops_loop
      A fake clock that reports elapsed time > max_task_seconds before the
      first attempt → loop stops immediately, returns passed=False, and
      logs a warning.

  - test_walltime_disabled_is_regression_safe
      max_task_seconds = 0 → guard disabled; loop behaves exactly as before
      (runs to completion / exhausts attempts normally).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace


from tools.auto.inner_loop import InnerLoop


def _approved_coder_result(files_written=None):
    return SimpleNamespace(
        succeeded=True,
        files_written=files_written or [],
        missing_context=[],
        context_satisfied=True,
        error=None,
    )


def _passed_exec_result():
    return SimpleNamespace(
        passed=True, exit_code=0, stdout="ok", stderr="",
        traceback="", timed_out=False, command="",
    )


class _AlwaysSucceedCoder:
    def generate(self, task, base_dir, prior_feedback=None, prefetched_context=""):
        return _approved_coder_result(files_written=[])


class _AlwaysPassExecutor:
    def run(self, task):
        return _passed_exec_result()


class _AlwaysApproveValidator:
    last_missing_context: list = []

    def approve(self, task, exec_result, coder_result, *, base_dir=None):
        return True, ""


class _FakeClock:
    """Monotonically increasing fake clock, advancing by `step` each call."""

    def __init__(self, start: float = 0.0, step: float = 0.0):
        self._t = start
        self._step = step

    def __call__(self) -> float:
        val = self._t
        self._t += self._step
        return val


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestWalltimeExceeded:
    def test_walltime_exceeded_stops_loop(self, tmp_path, monkeypatch, caplog):
        """Clock jumps past the limit before attempt 1 → loop stops, not passed."""
        import tools.auto.inner_loop as inner_loop_mod

        # First call establishes _start_time = 0; second call (top of attempt 1)
        # reports elapsed = 5000s, well past max_task_seconds=10.
        clock = _FakeClock(start=0.0, step=5000.0)
        monkeypatch.setattr(inner_loop_mod.time, "monotonic", clock)

        loop = InnerLoop(
            coder=_AlwaysSucceedCoder(),
            executor=_AlwaysPassExecutor(),
            validator=_AlwaysApproveValidator(),
            max_attempts=5,
            task_mode="creative",
            max_task_seconds=10,
        )
        task = {"id": "t1", "target_files": [], "goal": "x", "instruction": "y"}

        with caplog.at_level(logging.WARNING, logger="tools.auto.inner_loop"):
            result = loop.run_task(task, tmp_path)

        assert result.passed is False
        assert result.attempts_used == 0
        warning_text = " ".join(caplog.messages).lower()
        assert "wall-clock" in warning_text or "walltime" in warning_text or "stopping" in warning_text

    def test_walltime_exceeded_mid_run(self, tmp_path, monkeypatch):
        """Limit exceeded partway through (after a couple of successful-looking
        steps) still stops the loop without raising."""
        import tools.auto.inner_loop as inner_loop_mod

        # Step large enough that by the 2nd attempt's guard check we're over.
        clock = _FakeClock(start=0.0, step=20.0)
        monkeypatch.setattr(inner_loop_mod.time, "monotonic", clock)

        loop = InnerLoop(
            coder=_AlwaysSucceedCoder(),
            executor=_AlwaysPassExecutor(),
            validator=_AlwaysApproveValidator(),
            max_attempts=10,
            task_mode="creative",
            max_task_seconds=15,
        )
        task = {"id": "t1", "target_files": [], "goal": "x", "instruction": "y"}

        result = loop.run_task(task, tmp_path)
        assert result.passed is False
        assert result.attempts_used < 10


class TestWalltimeDisabledRegression:
    def test_zero_disables_guard(self, tmp_path, monkeypatch):
        """max_task_seconds=0 → guard never fires, even with a huge fake clock step."""
        import tools.auto.inner_loop as inner_loop_mod

        clock = _FakeClock(start=0.0, step=999999.0)
        monkeypatch.setattr(inner_loop_mod.time, "monotonic", clock)

        loop = InnerLoop(
            coder=_AlwaysSucceedCoder(),
            executor=_AlwaysPassExecutor(),
            validator=_AlwaysApproveValidator(),
            max_attempts=3,
            task_mode="creative",
            max_task_seconds=0,
        )
        task = {"id": "t1", "target_files": [], "goal": "x", "instruction": "y"}

        result = loop.run_task(task, tmp_path)
        # No guard ⇒ normal Gate-2 success on attempt 1.
        assert result.passed is True
        assert result.attempts_used == 1

    def test_default_param_is_disabled(self, tmp_path):
        """InnerLoop() with no max_task_seconds kwarg defaults to disabled (0)."""
        loop = InnerLoop(
            coder=_AlwaysSucceedCoder(),
            executor=_AlwaysPassExecutor(),
            validator=_AlwaysApproveValidator(),
            max_attempts=3,
            task_mode="code",
        )
        assert loop.max_task_seconds == 0
        task = {"id": "t1", "target_files": [], "goal": "x", "instruction": "y"}
        result = loop.run_task(task, tmp_path)
        assert result.passed is True


class TestMakeInnerLoopConfig:
    def test_reads_max_task_seconds_from_config(self, tmp_path):
        import configparser
        from tools.auto.inner_loop import make_inner_loop

        cfg = configparser.ConfigParser()
        cfg.read_dict({"auto": {"max_task_seconds": "42"}})

        loop = make_inner_loop(
            cfg, tmp_path,
            coder=_AlwaysSucceedCoder(),
            executor=_AlwaysPassExecutor(),
            validator=_AlwaysApproveValidator(),
            task_mode="code",
        )
        assert loop.max_task_seconds == 42

    def test_fallback_default_is_1800(self, tmp_path):
        import configparser
        from tools.auto.inner_loop import make_inner_loop

        cfg = configparser.ConfigParser()
        cfg.read_dict({"auto": {}})

        loop = make_inner_loop(
            cfg, tmp_path,
            coder=_AlwaysSucceedCoder(),
            executor=_AlwaysPassExecutor(),
            validator=_AlwaysApproveValidator(),
            task_mode="code",
        )
        assert loop.max_task_seconds == 1800
