"""207 bug 38: a base-check step that prints a non-UTF-8 byte is a row, and the later steps still run."""
import sys

from tools.arena import basecheck

BAD = r"import sys; sys.stdout.buffer.write(b'caf\xe9 ok\n'); sys.stderr.buffer.write(b'\xff\xfe err\n'); sys.exit({rc})"


def _step(name, rc, pytest=False):
    return basecheck.Step(name, [sys.executable, "-c", BAD.format(rc=rc)], pytest=pytest)


def test_green_and_red_steps_with_undecodable_output_are_rows(tmp_path):
    rows = basecheck.run_steps(tmp_path, [_step("one", 0), _step("two", 1, pytest=True), _step("three", 0)])
    assert [r["step"] for r in rows] == ["one", "two", "three"]
    assert [r["ok"] for r in rows] == [True, False, True]
    assert rows[1]["summary"]                      # red keeps a summary
    assert "FAILED (two)" in basecheck.verdict("a" * 40, rows)


def test_a_step_that_cannot_start_is_still_a_failed_row(tmp_path):
    step = basecheck.Step("gone", ["/nonexistent/binary-207"], pytest=False)
    rows = basecheck.run_steps(tmp_path, [step, _step("after", 0)])
    assert rows[0]["ok"] is False
    assert rows[1]["ok"] is True
