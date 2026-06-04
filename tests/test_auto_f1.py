"""tests/test_auto_f1.py — AUTO-F1: Live progress display.

ACs (from the Jira story):
  * Console + progress.json: ``architecture [x/N]  coding [y/M]``
    plus ``task k · attempt a/5 · round r/10``. Updates after each step.
  * Matches the requested format; readable while running unattended.
  * Dep: AUTO-A2.

Coverage:
  * render_banner() produces the exact canonical string.
  * render_task_line() produces the exact canonical string.
  * tick_arch() increments arch_done and calls refresh().
  * tick_arch() caps at arch_total (no over-counting).
  * tick_code() increments code_done and calls refresh().
  * tick_code() caps at code_total.
  * set_task() updates task_num / attempt / round_num and calls refresh().
  * banner() reflects current counters.
  * task_line() returns empty string before any set_task() call.
  * task_line() reflects current counters after set_task().
  * refresh() writes the banner line to the output sink.
  * refresh() writes the task line when a task is active.
  * refresh() does NOT write a task line before set_task() is called.
  * refresh() persists arch/code counters to progress.json via StateStore.
  * refresh() persists task/attempt/round to progress.json when on a task.
  * refresh() preserves the existing 'status' field in progress.json.
  * refresh() never raises — console errors are swallowed.
  * refresh() never raises — StateStore errors are swallowed.
  * Progress.json values match the display counters at every step.
  * Full flow: arch ticks → coding ticks → task loop → progress.json correct.
  * make_progress_display() factory: reads max_attempts from config.
  * make_progress_display() factory: reads max_rounds from config.
  * make_progress_display() factory: wires arch_total / code_total.
  * make_progress_display() factory: defaults to 0 when totals not passed.
  * make_progress_display() factory: out kwarg is wired through.
"""

from __future__ import annotations

import configparser
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.progress_display import (
    ProgressDisplay,
    make_progress_display,
    render_banner,
    render_task_line,
)
from tools.auto.state import StateStore


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / ".agent")
    store.initialise("test goal", tmp_path)
    return store


def _make_display(
    tmp_path: Path,
    *,
    arch_total: int = 4,
    code_total: int = 8,
    max_attempts: int = 5,
    max_rounds: int = 10,
    out=None,
) -> tuple[ProgressDisplay, StateStore]:
    state = _make_state(tmp_path)
    display = ProgressDisplay(
        state=state,
        arch_total=arch_total,
        code_total=code_total,
        max_attempts=max_attempts,
        max_rounds=max_rounds,
        out=out,
    )
    return display, state


def _make_config(
    max_attempts: int = 5,
    max_rounds: int = 10,
) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["auto"] = {
        "max_attempts_per_task": str(max_attempts),
        "max_rounds_per_task":   str(max_rounds),
    }
    return cfg


# ── render_banner ──────────────────────────────────────────────────────────────

class TestRenderBanner:
    def test_canonical_format(self):
        assert render_banner(2, 4, 3, 8) == "architecture [2/4]  coding [3/8]"

    def test_zeros(self):
        assert render_banner(0, 4, 0, 8) == "architecture [0/4]  coding [0/8]"

    def test_complete(self):
        assert render_banner(4, 4, 8, 8) == "architecture [4/4]  coding [8/8]"

    def test_single_cluster(self):
        assert render_banner(1, 1, 0, 3) == "architecture [1/1]  coding [0/3]"

    def test_double_space_between_sections(self):
        result = render_banner(1, 4, 2, 8)
        # The two sections must be separated by exactly two spaces
        assert "  coding" in result
        assert "architecture" in result

    def test_bracket_format_arch(self):
        result = render_banner(3, 5, 0, 0)
        assert "[3/5]" in result

    def test_bracket_format_coding(self):
        result = render_banner(0, 0, 6, 10)
        assert "[6/10]" in result


# ── render_task_line ───────────────────────────────────────────────────────────

class TestRenderTaskLine:
    def test_canonical_format(self):
        assert render_task_line(1, 2, 5, 1, 10) == "task 1 · attempt 2/5 · round 1/10"

    def test_first_attempt_first_round(self):
        assert render_task_line(1, 1, 5, 1, 10) == "task 1 · attempt 1/5 · round 1/10"

    def test_last_attempt_last_round(self):
        assert render_task_line(3, 5, 5, 10, 10) == "task 3 · attempt 5/5 · round 10/10"

    def test_separator_between_parts(self):
        result = render_task_line(2, 3, 5, 4, 10)
        assert " · " in result
        parts = result.split(" · ")
        assert len(parts) == 3

    def test_task_num_in_line(self):
        result = render_task_line(7, 1, 5, 1, 10)
        assert "task 7" in result

    def test_attempt_fraction(self):
        result = render_task_line(1, 3, 5, 1, 10)
        assert "attempt 3/5" in result

    def test_round_fraction(self):
        result = render_task_line(1, 1, 5, 6, 10)
        assert "round 6/10" in result


# ── ProgressDisplay: construction ──────────────────────────────────────────────

class TestConstruction:
    def test_initial_arch_done_zero(self, tmp_path):
        display, _ = _make_display(tmp_path)
        assert display.arch_done == 0

    def test_initial_code_done_zero(self, tmp_path):
        display, _ = _make_display(tmp_path)
        assert display.code_done == 0

    def test_initial_task_num_zero(self, tmp_path):
        display, _ = _make_display(tmp_path)
        assert display.task_num == 0

    def test_totals_stored(self, tmp_path):
        display, _ = _make_display(tmp_path, arch_total=6, code_total=12)
        assert display.arch_total == 6
        assert display.code_total == 12

    def test_max_attempts_stored(self, tmp_path):
        display, _ = _make_display(tmp_path, max_attempts=3)
        assert display.max_attempts == 3

    def test_max_rounds_stored(self, tmp_path):
        display, _ = _make_display(tmp_path, max_rounds=7)
        assert display.max_rounds == 7


# ── banner() and task_line() ───────────────────────────────────────────────────

class TestBannerAndTaskLine:
    def test_banner_initial(self, tmp_path):
        display, _ = _make_display(tmp_path, arch_total=4, code_total=8)
        assert display.banner() == "architecture [0/4]  coding [0/8]"

    def test_banner_after_ticks(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, code_total=8, out=buf)
        display.arch_done = 2
        display.code_done = 3
        assert display.banner() == "architecture [2/4]  coding [3/8]"

    def test_task_line_empty_before_set_task(self, tmp_path):
        display, _ = _make_display(tmp_path)
        assert display.task_line() == ""

    def test_task_line_after_set_task(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, max_attempts=5, max_rounds=10, out=buf)
        display.set_task(task_num=2, attempt=1, round_num=1)
        assert display.task_line() == "task 2 · attempt 1/5 · round 1/10"

    def test_task_line_updates_on_set_task(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        display.set_task(task_num=1, attempt=1, round_num=1)
        display.set_task(task_num=1, attempt=3, round_num=2)
        assert display.task_line() == "task 1 · attempt 3/5 · round 2/10"


# ── tick_arch ─────────────────────────────────────────────────────────────────

class TestTickArch:
    def test_increments_arch_done(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, out=buf)
        display.tick_arch()
        assert display.arch_done == 1

    def test_successive_ticks(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, out=buf)
        display.tick_arch()
        display.tick_arch()
        assert display.arch_done == 2

    def test_caps_at_total(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=2, out=buf)
        display.tick_arch()
        display.tick_arch()
        display.tick_arch()   # over-tick
        assert display.arch_done == 2

    def test_calls_refresh(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, out=buf)
        with patch.object(display, "refresh") as mock_refresh:
            display.tick_arch()
        mock_refresh.assert_called_once()

    def test_banner_reflects_after_tick(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, out=buf)
        display.tick_arch()
        display.tick_arch()
        assert "architecture [2/4]" in display.banner()


# ── tick_code ─────────────────────────────────────────────────────────────────

class TestTickCode:
    def test_increments_code_done(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, code_total=8, out=buf)
        display.tick_code()
        assert display.code_done == 1

    def test_successive_ticks(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, code_total=8, out=buf)
        display.tick_code()
        display.tick_code()
        display.tick_code()
        assert display.code_done == 3

    def test_caps_at_total(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, code_total=2, out=buf)
        display.tick_code()
        display.tick_code()
        display.tick_code()   # over-tick
        assert display.code_done == 2

    def test_calls_refresh(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, code_total=8, out=buf)
        with patch.object(display, "refresh") as mock_refresh:
            display.tick_code()
        mock_refresh.assert_called_once()

    def test_banner_reflects_after_tick(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, code_total=8, out=buf)
        display.tick_code()
        display.tick_code()
        assert "coding [2/8]" in display.banner()


# ── set_task ──────────────────────────────────────────────────────────────────

class TestSetTask:
    def test_sets_task_num(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        display.set_task(task_num=3, attempt=1, round_num=1)
        assert display.task_num == 3

    def test_sets_attempt(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        display.set_task(task_num=1, attempt=4, round_num=2)
        assert display.attempt == 4

    def test_sets_round(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        display.set_task(task_num=1, attempt=1, round_num=7)
        assert display.round_num == 7

    def test_calls_refresh(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        with patch.object(display, "refresh") as mock_refresh:
            display.set_task(task_num=1, attempt=1, round_num=1)
        mock_refresh.assert_called_once()

    def test_updates_idempotently(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        display.set_task(task_num=1, attempt=1, round_num=1)
        display.set_task(task_num=1, attempt=2, round_num=1)
        assert display.attempt == 2
        assert display.round_num == 1


# ── refresh() console output ──────────────────────────────────────────────────

class TestRefreshConsole:
    def test_banner_line_written_to_sink(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, code_total=8, out=buf)
        display.refresh()
        output = buf.getvalue()
        assert "architecture [0/4]  coding [0/8]" in output

    def test_task_line_written_when_task_active(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        display.set_task(task_num=1, attempt=1, round_num=1)
        buf.truncate(0); buf.seek(0)
        display.refresh()
        output = buf.getvalue()
        assert "task 1 · attempt 1/5 · round 1/10" in output

    def test_no_task_line_before_set_task(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        display.refresh()
        output = buf.getvalue()
        assert "task" not in output

    def test_both_lines_present_when_task_active(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, code_total=8, out=buf)
        display.set_task(task_num=2, attempt=3, round_num=2)
        buf.truncate(0); buf.seek(0)
        display.refresh()
        output = buf.getvalue()
        assert "architecture [0/4]  coding [0/8]" in output
        assert "task 2 · attempt 3/5 · round 2/10" in output

    def test_refresh_called_by_tick_arch_writes_output(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, code_total=8, out=buf)
        display.tick_arch()
        output = buf.getvalue()
        assert "architecture [1/4]" in output

    def test_refresh_called_by_tick_code_writes_output(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, code_total=8, out=buf)
        display.tick_code()
        output = buf.getvalue()
        assert "coding [1/8]" in output

    def test_multiple_refreshes_append_lines(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, arch_total=4, code_total=8, out=buf)
        display.tick_arch()
        display.tick_arch()
        output = buf.getvalue()
        lines = [l for l in output.splitlines() if l.strip()]
        assert len(lines) >= 2

    def test_banner_newline_terminated(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(tmp_path, out=buf)
        display.refresh()
        assert buf.getvalue().endswith("\n")


# ── refresh() → progress.json ─────────────────────────────────────────────────

class TestRefreshPersists:
    def test_arch_done_written_to_progress(self, tmp_path):
        buf = io.StringIO()
        display, state = _make_display(tmp_path, arch_total=4, out=buf)
        display.tick_arch()
        display.tick_arch()
        prog = state.get_progress()
        assert prog["arch_done"] == 2

    def test_arch_total_written_to_progress(self, tmp_path):
        buf = io.StringIO()
        display, state = _make_display(tmp_path, arch_total=4, out=buf)
        display.refresh()
        prog = state.get_progress()
        assert prog["arch_total"] == 4

    def test_code_done_written_to_progress(self, tmp_path):
        buf = io.StringIO()
        display, state = _make_display(tmp_path, code_total=8, out=buf)
        display.tick_code()
        display.tick_code()
        display.tick_code()
        prog = state.get_progress()
        assert prog["code_done"] == 3

    def test_code_total_written_to_progress(self, tmp_path):
        buf = io.StringIO()
        display, state = _make_display(tmp_path, code_total=8, out=buf)
        display.refresh()
        prog = state.get_progress()
        assert prog["code_total"] == 8

    def test_task_fields_written_when_set_task_called(self, tmp_path):
        buf = io.StringIO()
        display, state = _make_display(tmp_path, out=buf)
        display.set_task(task_num=3, attempt=2, round_num=4)
        prog = state.get_progress()
        assert prog["current_task_num"] == 3
        assert prog["current_attempt"] == 2
        assert prog["current_round"] == 4

    def test_task_fields_absent_before_set_task(self, tmp_path):
        buf = io.StringIO()
        display, state = _make_display(tmp_path, out=buf)
        display.refresh()
        prog = state.get_progress()
        assert "current_task_num" not in prog

    def test_preserves_status_field(self, tmp_path):
        buf = io.StringIO()
        display, state = _make_display(tmp_path, out=buf)
        state.update_progress("running")
        display.refresh()
        prog = state.get_progress()
        assert prog["status"] == "running"

    def test_progress_json_file_updated(self, tmp_path):
        buf = io.StringIO()
        display, state = _make_display(tmp_path, arch_total=4, out=buf)
        display.tick_arch()
        import json
        prog_path = tmp_path / ".agent" / "progress.json"
        data = json.loads(prog_path.read_text())
        assert data["arch_done"] == 1

    def test_counters_match_display_state(self, tmp_path):
        """All progress.json values must mirror the display object's state."""
        buf = io.StringIO()
        display, state = _make_display(
            tmp_path, arch_total=4, code_total=8,
            max_attempts=5, max_rounds=10, out=buf,
        )
        display.tick_arch()
        display.tick_arch()
        display.tick_code()
        display.set_task(task_num=1, attempt=2, round_num=1)

        prog = state.get_progress()
        assert prog["arch_done"]         == display.arch_done
        assert prog["arch_total"]        == display.arch_total
        assert prog["code_done"]         == display.code_done
        assert prog["code_total"]        == display.code_total
        assert prog["current_task_num"]  == display.task_num
        assert prog["current_attempt"]   == display.attempt
        assert prog["current_round"]     == display.round_num


# ── refresh() error resilience ────────────────────────────────────────────────

class TestRefreshRobust:
    def test_bad_out_does_not_raise(self, tmp_path):
        bad_out = MagicMock()
        bad_out.write.side_effect = OSError("broken pipe")
        state = _make_state(tmp_path)
        display = ProgressDisplay(state=state, arch_total=4, code_total=8, out=bad_out)
        display.refresh()  # must not raise

    def test_state_error_does_not_raise(self, tmp_path):
        buf = io.StringIO()
        state = _make_state(tmp_path)
        display = ProgressDisplay(state=state, arch_total=4, code_total=8, out=buf)
        with patch.object(state, "update_progress", side_effect=RuntimeError("disk full")):
            display.refresh()  # must not raise

    def test_console_error_still_persists(self, tmp_path):
        """If console write fails, progress.json should still be updated."""
        bad_out = MagicMock()
        bad_out.write.side_effect = OSError("broken pipe")
        state = _make_state(tmp_path)
        display = ProgressDisplay(state=state, arch_total=4, code_total=8, out=bad_out)
        display.arch_done = 2
        display.refresh()
        prog = state.get_progress()
        assert prog.get("arch_done") == 2

    def test_tick_arch_does_not_raise_on_bad_state(self, tmp_path):
        buf = io.StringIO()
        state = _make_state(tmp_path)
        display = ProgressDisplay(state=state, arch_total=4, code_total=8, out=buf)
        with patch.object(state, "update_progress", side_effect=IOError("gone")):
            display.tick_arch()  # must not raise


# ── Full flow ─────────────────────────────────────────────────────────────────

class TestFullFlow:
    def test_architecture_phase_then_coding_phase(self, tmp_path):
        """Simulate a complete run: 4 clusters reviewed, 3 tasks coded."""
        buf = io.StringIO()
        display, state = _make_display(
            tmp_path, arch_total=4, code_total=3,
            max_attempts=5, max_rounds=10, out=buf,
        )

        # Architecture phase
        for _ in range(4):
            display.tick_arch()

        assert display.arch_done == 4

        # Coding phase: task 1
        display.set_task(task_num=1, attempt=1, round_num=1)
        display.set_task(task_num=1, attempt=2, round_num=1)
        display.tick_code()

        # Coding phase: task 2 (fails round 1, passes round 2)
        display.set_task(task_num=2, attempt=1, round_num=1)
        display.set_task(task_num=2, attempt=1, round_num=2)
        display.tick_code()

        # Coding phase: task 3
        display.set_task(task_num=3, attempt=1, round_num=1)
        display.tick_code()

        prog = state.get_progress()
        assert prog["arch_done"] == 4
        assert prog["code_done"] == 3
        assert prog["current_task_num"] == 3
        assert prog["current_round"] == 1

    def test_output_lines_contain_expected_format(self, tmp_path):
        buf = io.StringIO()
        display, _ = _make_display(
            tmp_path, arch_total=4, code_total=8, out=buf,
        )
        display.tick_arch()
        display.tick_arch()
        display.tick_code()
        display.set_task(task_num=1, attempt=3, round_num=2)

        output = buf.getvalue()
        # Check that the canonical formats appear somewhere in the output
        assert "architecture [2/4]  coding [1/8]" in output
        assert "task 1 · attempt 3/5 · round 2/10" in output

    def test_progress_json_tracks_entire_run(self, tmp_path):
        import json
        buf = io.StringIO()
        display, state = _make_display(
            tmp_path, arch_total=2, code_total=2, out=buf,
        )
        state.update_progress("running")

        display.tick_arch()
        display.tick_arch()
        display.set_task(task_num=1, attempt=1, round_num=1)
        display.tick_code()
        display.set_task(task_num=2, attempt=1, round_num=1)
        display.tick_code()

        prog_path = tmp_path / ".agent" / "progress.json"
        data = json.loads(prog_path.read_text())
        assert data["arch_done"]  == 2
        assert data["code_done"]  == 2
        assert data["status"]     == "running"


# ── make_progress_display factory ────────────────────────────────────────────

class TestFactory:
    def test_returns_progress_display_instance(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _make_config()
        display = make_progress_display(state, cfg)
        assert isinstance(display, ProgressDisplay)

    def test_max_attempts_from_config(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _make_config(max_attempts=3)
        display = make_progress_display(state, cfg)
        assert display.max_attempts == 3

    def test_max_rounds_from_config(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _make_config(max_rounds=7)
        display = make_progress_display(state, cfg)
        assert display.max_rounds == 7

    def test_arch_total_wired(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _make_config()
        display = make_progress_display(state, cfg, arch_total=6)
        assert display.arch_total == 6

    def test_code_total_wired(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _make_config()
        display = make_progress_display(state, cfg, code_total=12)
        assert display.code_total == 12

    def test_defaults_to_zero_totals(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _make_config()
        display = make_progress_display(state, cfg)
        assert display.arch_total == 0
        assert display.code_total == 0

    def test_out_kwarg_wired(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _make_config()
        buf = io.StringIO()
        display = make_progress_display(state, cfg, out=buf)
        assert display._out is buf

    def test_state_stored(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = _make_config()
        display = make_progress_display(state, cfg)
        assert display._state is state

    def test_missing_auto_section_uses_defaults(self, tmp_path):
        state = _make_state(tmp_path)
        cfg = configparser.ConfigParser()   # no [auto] section at all
        display = make_progress_display(state, cfg)
        assert display.max_attempts == 5
        assert display.max_rounds   == 10


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
