"""tests/test_backoff_hardening.py

tools/backoff.py had ZERO dedicated test coverage. Two defensive hardening
fixes, each with a test that FAILS on unfixed code:

1. backoff_seconds() clamped the index only on the HIGH side
   (``min(idx, len-1)``). A negative index — e.g. a caller that computes
   ``count - 1`` before incrementing ``count`` — hit Python's negative
   indexing and returned ``BACKOFF_SERIES[-1]`` == 1024 (the CAP) as the
   very first wait instead of 1 s, turning an off-by-one at a call site into
   a ~17-minute stall. All current callers increment first, so this is
   defensive, not a live production stall.

2. load_state() returned whatever valid JSON the checkpoint file held. A file
   that parses cleanly but is NOT a dict (a JSON list / scalar / null from a
   hand-edited or truncated-then-rewritten file) was returned as-is, and
   every consumer immediately calls ``.get(...)`` on it — so main.py's resume
   path would raise AttributeError instead of honouring the documented
   contract ("None if the file is absent / corrupt").
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import backoff  # noqa: E402


class TestBackoffSecondsClamp:
    def test_first_error_is_one_second(self):
        assert backoff.backoff_seconds(0) == 1

    def test_cap_at_high_index(self):
        assert backoff.backoff_seconds(10) == 1024
        assert backoff.backoff_seconds(999) == 1024

    def test_negative_index_returns_minimum_not_cap(self):
        # FAILS on unfixed code: BACKOFF_SERIES[-1] == 1024.
        assert backoff.backoff_seconds(-1) == 1
        assert backoff.backoff_seconds(-5) == 1

    def test_full_series_mapping(self):
        assert [backoff.backoff_seconds(i) for i in range(11)] == \
            [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]


class TestLoadStateNonDict:
    def test_valid_dict_state_is_returned(self, tmp_path):
        p = tmp_path / "pipeline_state.json"
        p.write_text(json.dumps({"loop": "run_edit", "iteration": 3}), encoding="utf-8")
        assert backoff.load_state(p) == {"loop": "run_edit", "iteration": 3}

    def test_absent_file_returns_none(self, tmp_path):
        assert backoff.load_state(tmp_path / "nope.json") is None

    def test_corrupt_json_returns_none(self, tmp_path):
        p = tmp_path / "pipeline_state.json"
        p.write_text("{not valid json", encoding="utf-8")
        assert backoff.load_state(p) is None

    def test_json_list_returns_none_not_a_list(self, tmp_path):
        # FAILS on unfixed code: returns [1, 2, 3], which later .get() crashes on.
        p = tmp_path / "pipeline_state.json"
        p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert backoff.load_state(p) is None

    def test_json_scalar_returns_none(self, tmp_path):
        p = tmp_path / "pipeline_state.json"
        p.write_text(json.dumps("just a string"), encoding="utf-8")
        assert backoff.load_state(p) is None

    def test_json_null_returns_none(self, tmp_path):
        p = tmp_path / "pipeline_state.json"
        p.write_text("null", encoding="utf-8")
        assert backoff.load_state(p) is None
