"""tests_bugfix/test_arena_output_basecheck_review.py — AR-14's refusal block and AR-25's step runner."""

from __future__ import annotations

import io
import json
import sys

import pytest

from tools.arena import basecheck, output


def _text(**kwargs) -> tuple[int, list[str]]:
    buf = io.StringIO()
    code = output.refuse_ctx("boom", stream=buf, **kwargs)
    return code, buf.getvalue().splitlines()


# ── bug: the labels printed as `where::` and `ticket::` ──────────────────────
# `_wrap` is handed a label that already ends in a colon and adds `: ` itself. The
# spec (epic-tasks/150, §7) shows one colon and the three values in one column:
#   where:  …
#   ticket: …
#   flow:   …

FULL = dict(where="checkout /x on arena (clean)", ticket="148 (AR-7) epic-tasks/148.md",
            flow=[("branch found", "ok", ""), ("intake", "fail", "why"), ("start", "todo", "")],
            hints=[{"why": "park it", "command": "arena issue queue 145"}])


def test_the_labels_have_one_colon():
    code, lines = _text(**FULL)
    assert code == 2
    assert not any("::" in line for line in lines), lines
    assert lines[1].startswith("  where:  checkout")
    assert lines[2].startswith("  ticket: 148")
    assert lines[3].startswith("  flow:   [✓] branch found")


def test_the_three_values_start_in_one_column():
    _, lines = _text(**FULL)
    columns = {lines[i].index(lines[i].split(":", 1)[1].lstrip()) for i in (1, 2, 3)}
    assert columns == {10}


def test_a_wrapped_value_continues_under_the_value_not_the_label():
    where = " ".join(["segment"] * 30)
    _, lines = _text(**{**FULL, "where": where})
    start = lines.index(next(line for line in lines if line.startswith("  where:")))
    cont = lines[start + 1]
    assert cont.startswith(" " * 10 + "segment"), lines
    assert not cont.startswith(" " * 11), lines


# ── bug: `refuse_ctx` raised TypeError when it was given no `flow` ───────────
# `flow=None` is its default, the JSON branch handles it, and the text branch
# iterated it. The docstring: a field not reached is `?`, never dropped.

def test_a_refusal_without_a_flow_prints_a_question_mark_not_a_traceback():
    code, lines = _text(where="here", ticket="there")
    assert code == 2
    assert lines[0] == "arena: boom"
    assert any(line.startswith("  flow:") and line.rstrip().endswith("?") for line in lines), lines


def test_a_refusal_with_nothing_at_all_prints_every_field():
    code, lines = _text()
    assert code == 2
    assert [line.split(":")[0].strip() for line in lines] == ["arena", "where", "ticket", "flow"]


def test_the_json_refusal_without_a_flow_is_unchanged():
    buf = io.StringIO()
    assert output.refuse_ctx("boom", fmt="json", stream=buf) == 2
    assert json.loads(buf.getvalue()) == {"error": "boom", "where": "?", "ticket": "?",
                                          "flow": [], "hints": []}


# ── bug: a step whose output is not UTF-8 crashed `arena base check` ──────────
# `run_steps` read each step with `text=True` and strict decoding and caught only
# OSError: a test that printed a byte that is not UTF-8 ended the whole check in a
# UnicodeDecodeError traceback instead of a row — and the verdict is never allowed
# to fail open. `gitref.git` already decodes with a handler for the same reason.

def _step(code: str, name: str = "tests", pytest_step: bool = True) -> basecheck.Step:
    return basecheck.Step(name, [sys.executable, "-c", code], pytest=pytest_step)


def test_a_passing_step_with_undecodable_output_is_a_green_row(tmp_path):
    step = _step("import sys; sys.stdout.buffer.write(b'1 passed caf\\xff\\xfe\\n')")
    [row] = basecheck.run_steps(tmp_path, [step])
    assert row["ok"] is True
    assert row["step"] == "tests"
    assert "1 passed" in row["summary"]


def test_a_failing_step_with_undecodable_output_is_a_red_row_and_the_next_step_runs(tmp_path):
    bad = _step("import sys; sys.stdout.buffer.write(b'FAILED t.py::t - caf\\xff\\n'); "
                "sys.stderr.buffer.write(b'x\\xfe\\n'); sys.exit(1)")
    good = _step("print('2 passed')", name="tiers", pytest_step=False)
    rows = basecheck.run_steps(tmp_path, [bad, good])
    assert [(row["step"], row["ok"]) for row in rows] == [("tests", False), ("tiers", True)]


def test_plain_output_is_summarised_as_before(tmp_path):
    [row] = basecheck.run_steps(tmp_path, [_step("print('noise'); print('5 passed in 0.1s')")])
    assert row["ok"] is True and row["summary"] == "5 passed in 0.1s"
