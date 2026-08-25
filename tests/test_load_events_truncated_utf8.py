"""tests/test_load_events_truncated_utf8.py — load_events() truncated-UTF-8 regression.

Bug report REPORT20260824.md §2.1 / §2.2.

Both `tools/auto/view_trace.py::load_events` and `analyze_logs.py::load_events`
open a trace file with `encoding="utf-8"` and only catch
`json.JSONDecodeError`. A trace file whose last line was truncated
mid-write **inside a multi-byte UTF-8 character** (e.g. the process was
killed while flushing a line containing non-ASCII text) raised an uncaught
`UnicodeDecodeError`, crashing the whole tool and discarding every event
that had already been read successfully — exactly the "diagnose a crashed
run" scenario these tools exist for.

After the fix, both implementations open the file with
`errors="replace"`, so a broken trailing byte sequence is substituted
rather than raised, and every event written before the truncation point is
still returned.

Covers:
  1. tools/auto/view_trace.py::load_events survives a truncated multi-byte
     UTF-8 tail and still returns the events written before it.
  2. analyze_logs.py::load_events — same, independent implementation.
  3. Both still behave correctly on a clean, well-formed trace file
     (no regression on the happy path).
  4. Both still emit a warning (not a crash) for a merely-invalid JSON line,
     preserving the pre-existing JSONDecodeError handling.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.view_trace import load_events as view_trace_load_events
from analyze_logs import load_events as analyze_logs_load_events


TRUNCATED_MULTIBYTE_TAIL = b'{"event": "caf\xc3'  # 0xC3 starts a 2-byte UTF-8
# sequence ('\xc3\xa9' would be 'é'); truncated here with no continuation byte.


def _write_truncated_trace(tmp_path: Path) -> Path:
    """A trace file with one well-formed line, then a line truncated
    mid multi-byte UTF-8 character (no trailing newline — simulates a
    process killed mid-write)."""
    trace_file = tmp_path / "trace_truncated.jsonl"
    with trace_file.open("wb") as fh:
        fh.write(b'{"event": "ok", "n": 1}\n')
        fh.write(TRUNCATED_MULTIBYTE_TAIL)
    return trace_file


def _write_clean_trace(tmp_path: Path) -> Path:
    trace_file = tmp_path / "trace_clean.jsonl"
    trace_file.write_text(
        '{"event": "ok", "n": 1}\n{"event": "ok", "n": 2}\n',
        encoding="utf-8",
    )
    return trace_file


def _write_bad_json_trace(tmp_path: Path) -> Path:
    trace_file = tmp_path / "trace_bad_json.jsonl"
    trace_file.write_text(
        '{"event": "ok", "n": 1}\nnot json at all\n{"event": "ok", "n": 2}\n',
        encoding="utf-8",
    )
    return trace_file


@pytest.mark.parametrize(
    "load_events",
    [view_trace_load_events, analyze_logs_load_events],
    ids=["view_trace", "analyze_logs"],
)
class TestLoadEventsTruncatedUtf8:
    def test_truncated_multibyte_tail_does_not_crash(self, tmp_path, load_events):
        trace_file = _write_truncated_trace(tmp_path)
        # Must not raise UnicodeDecodeError.
        events = load_events(trace_file)
        # The one well-formed line written before the crash must survive.
        assert events == [{"event": "ok", "n": 1}]

    def test_clean_trace_unaffected(self, tmp_path, load_events):
        trace_file = _write_clean_trace(tmp_path)
        events = load_events(trace_file)
        assert events == [{"event": "ok", "n": 1}, {"event": "ok", "n": 2}]

    def test_invalid_json_line_still_warns_not_crashes(self, tmp_path, load_events, capsys):
        trace_file = _write_bad_json_trace(tmp_path)
        events = load_events(trace_file)
        assert events == [{"event": "ok", "n": 1}, {"event": "ok", "n": 2}]
        captured = capsys.readouterr()
        assert captured.err  # a warning was printed for the bad line
