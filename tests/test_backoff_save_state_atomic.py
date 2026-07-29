"""tests/test_backoff_save_state_atomic.py — save_state must survive a
crash mid-write.

save_state() used to open its target with mode "w", which truncates the
file to zero bytes before writing a single byte of new content. Every
consumer of this checkpoint exists specifically to survive a crash or
interrupt mid-run (tools/collect/summarizer.py's Pass B batch,
tools/faq_agent.py, tools/actions.py) — but a crash during THIS write left
a truncated, unparseable state.json, which load_state()'s existing (and
already-correct) error handling treats as "corrupt" and discards. The
mechanism whose entire purpose is surviving an interruption was itself
destroyed by one:

    saved: real checkpoint on disk
    [write interrupted mid-flight]
    load_state() returns: None -> ALL prior progress lost

Fixed with the same write-to-temp-then-os.replace pattern used throughout
tools/auto/.
"""

from __future__ import annotations

import json

import pytest
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.backoff import load_state, save_state


class TestSaveStateIsAtomic:
    def test_clean_save_round_trips(self, tmp_path):
        p = tmp_path / "state.json"
        state = {"loop": "x", "modules": {"a.py": {"purpose": "p"}}}
        save_state(state, p)
        assert load_state(p) == state

    def test_no_leftover_temp_file_after_clean_save(self, tmp_path):
        p = tmp_path / "state.json"
        save_state({"loop": "x"}, p)
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_second_save_does_not_leave_a_temp_file_either(self, tmp_path):
        p = tmp_path / "state.json"
        save_state({"loop": "x", "n": 1}, p)
        save_state({"loop": "x", "n": 2}, p)
        assert list(tmp_path.glob(".*.tmp")) == []
        assert load_state(p) == {"loop": "x", "n": 2}

    def test_creates_parent_directory_if_missing(self, tmp_path):
        p = tmp_path / "nested" / "dir" / "state.json"
        save_state({"loop": "x"}, p)
        assert load_state(p) == {"loop": "x"}

    def test_original_survives_an_interrupted_write(self, tmp_path, monkeypatch):
        """A crash DURING the write — not before, not after — must never
        touch the original file. Deterministic: the failure point is
        injected directly into the write path rather than raced against a
        real process kill.

        An earlier version of this test used subprocess.Popen + a fixed
        0.01s sleep before SIGKILL, hoping to catch a real writer process
        mid-flight. It passed even against genuinely unfixed code, every
        time (5/5 runs) — the write finished before the timer fired, so the
        subprocess approach never actually exercised the crash window and
        gave false confidence. json.dump is monkeypatched to raise partway
        through here instead, which reproduces "interrupted after the
        target may have been touched, before the write completed" without
        depending on wall-clock timing at all.
        """
        p = tmp_path / "state.json"
        original = {
            "loop": "collect_summarize",
            "modules": {"a.py": {"purpose": "real work already done"}},
        }
        save_state(original, p)

        def boom(*a, **kw):
            raise OSError("simulated crash mid-write")

        monkeypatch.setattr(json, "dump", boom)

        with pytest.raises(OSError):
            save_state({"loop": "x", "modules": {"b.py": {"purpose": "new"}}}, p)

        assert load_state(p) == original, (
            "the ORIGINAL checkpoint was corrupted or lost by a write that "
            "failed partway through"
        )
        # The failed attempt must not leave a stray temp file behind either.
        assert list(tmp_path.glob(".*.tmp")) == []

    def test_wrong_shape_still_treated_as_corrupt(self, tmp_path):
        """Sanity: load_state's pre-existing wrong-shape guard, documented
        in its own 'Hardening:' comment, must be unaffected by this fix."""
        p = tmp_path / "state.json"
        p.write_text("[]", encoding="utf-8")
        assert load_state(p) is None
