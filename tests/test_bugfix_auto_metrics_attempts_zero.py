"""tests/test_bugfix_auto_metrics_attempts_zero.py

AutoMetricsStream.record_gate2 uses ``attempts_used if attempts_used else
attempts`` to pick the effective attempt count.  The ``if attempts_used``
check is truthy, not ``is not None`` — so ``attempts_used=0`` (a valid
value meaning "zero attempts were used") falls through to the legacy
``attempts`` parameter.  When a caller explicitly passes
``attempts_used=0`` alongside a non-zero ``attempts``, the recorded
metric gets the wrong count.

The fix: use ``is not None`` so 0 is kept as 0, matching the
docstring's "Use attempts_used=N" contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.auto_metrics import AutoMetricsStream


def _stream(tmp_path: Path) -> AutoMetricsStream:
    return AutoMetricsStream(tmp_path / ".agent")


class TestRecordGate2AttemptsZero:
    def test_attempts_used_zero_is_kept_not_replaced_by_attempts(self, tmp_path):
        """attempts_used=0 must be recorded as 0, not replaced by the
        legacy ``attempts`` parameter."""
        s = _stream(tmp_path)
        s.record_gate2("T1", approved=True, feedback="ok",
                       attempts_used=0, attempts=5)
        records = s.collector.load_recent(10)
        assert len(records) == 1
        assert records[0].iterations_used == 0, (
            "attempts_used=0 was overridden by attempts=5 — "
            f"got {records[0].iterations_used}"
        )

    def test_attempts_used_none_falls_back_to_attempts(self, tmp_path):
        """When attempts_used is not passed (defaults to 0), the legacy
        ``attempts`` parameter is used, matching the pre-fix behaviour."""
        s = _stream(tmp_path)
        s.record_gate2("T1", approved=True, feedback="ok", attempts=3)
        records = s.collector.load_recent(10)
        assert len(records) == 1
        assert records[0].iterations_used == 3

    def test_attempts_used_nonzero_still_wins(self, tmp_path):
        """Sanity: the normal case (attempts_used > 0) is unchanged."""
        s = _stream(tmp_path)
        s.record_gate2("T1", approved=False, feedback="bad",
                       attempts_used=2, attempts=5)
        records = s.collector.load_recent(10)
        assert len(records) == 1
        assert records[0].iterations_used == 2
