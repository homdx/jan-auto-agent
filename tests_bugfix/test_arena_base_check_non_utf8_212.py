"""tests_bugfix/test_arena_base_check_non_utf8_212.py — bug 212: a step's output that is not UTF-8 ends `arena base check` in a traceback.

Bug: `basecheck.run_steps` runs each of the four checks with `text=True`, which
decodes its output strictly, and it caught only `OSError` ("a step that cannot be
started is a failed row, not a traceback"). A step that prints one byte that is not
UTF-8 — a test dumping raw bytes, a file name in latin-1 in a `FAILED …` line —
raised `UnicodeDecodeError` out of `subprocess.run` AFTER the step had run: the
traceback ended the whole check, the steps after it never ran, and the verdict a
long suite had earned was lost. The output is decoded with `errors="replace"`: the
summary line may hold a `�`, the verdict never goes missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tools.arena import basecheck

NOISE = "import sys; sys.stdout.buffer.write(b'caf\\xe9 \\xff\\n'); sys.exit({code})"


def _steps(code: int) -> list[basecheck.Step]:
    return [
        basecheck.Step("first", [sys.executable, "-c", "print('ok')"]),
        basecheck.Step("noisy", [sys.executable, "-c", NOISE.format(code=code)], pytest=True),
        basecheck.Step("last", [sys.executable, "-c", "print('fine')"]),
    ]


def test_a_step_with_non_utf8_output_is_a_row_not_a_traceback(tmp_path):
    rows = basecheck.run_steps(Path(tmp_path), _steps(1))
    assert [r["step"] for r in rows] == ["first", "noisy", "last"], "the later steps must still run"
    assert [r["ok"] for r in rows] == [True, False, True]
    assert rows[1]["summary"].startswith("caf")


def test_a_green_step_with_non_utf8_output_is_green(tmp_path):
    rows = basecheck.run_steps(Path(tmp_path), _steps(0))
    assert all(r["ok"] for r in rows)
    assert basecheck.verdict("a" * 40, rows) == "base aaaaaaaaaaaa: ok"


def test_ordinary_output_is_read_as_before(tmp_path):
    rows = basecheck.run_steps(Path(tmp_path), [basecheck.Step("x", [sys.executable, "-c", "print('héllo ✓')"])])
    assert rows[0]["summary"] == "héllo ✓"
