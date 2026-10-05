"""tests_bugfix/test_arena_rounds_robustness_177_178.py — pins 177 (run list on unpadded folders) and 178 (rerun exit on coarse mtimes)."""

from __future__ import annotations

import json
import math
import os
import types
from pathlib import Path

from tools.arena import rounds


# ── 177: run list on unpadded round folders ─────────────────────────────────
def _cfg():
    return types.SimpleNamespace(out_dir="contest-out")


def test_177_unpadded_folder_is_a_row_not_a_traceback(tmp_path):
    (tmp_path / "contest-out" / "7").mkdir(parents=True)
    rows = rounds.list_rows(tmp_path, _cfg(), proc_root=str(tmp_path))
    assert [(r["RUN"], r["STATE"]) for r in rows] == [(7, "?")]


def test_177_unpadded_leg_folder_reads_its_state(tmp_path):
    leg = tmp_path / "contest-out" / "8.1"
    leg.mkdir(parents=True)
    (leg / "state.json").write_text(json.dumps(
        {"agents": [{"state": "READY"}, {"state": "STALLED"}]}), encoding="utf-8")
    rows = rounds.list_rows(tmp_path, _cfg(), proc_root=str(tmp_path))
    assert rows == [{"RUN": 8, "LEGS": "1/1", "STATE": "done", "AGE": rows[0]["AGE"],
                     "READY/TOTAL": "1/2"}]


def test_177_padded_round_unchanged(tmp_path):
    folder = tmp_path / "contest-out" / "09"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"agents": [{"state": "READY"}]}),
                                       encoding="utf-8")
    rows = rounds.list_rows(tmp_path, _cfg(), proc_root=str(tmp_path))
    assert [(r["RUN"], r["STATE"], r["READY/TOTAL"]) for r in rows] == [(9, "done", "1/1")]


def test_177_three_digit_rounds_and_a_leg_still_pick_the_base_or_the_last_leg(tmp_path):
    """The rounds on this disk are `153`, `148.1`: a base with a state wins, else the highest leg."""
    out = tmp_path / "contest-out"
    for name, agents in (("153", [{"state": "READY"}]), ("148.1", [{"state": "READY"}] * 2)):
        (out / name).mkdir(parents=True)
        (out / name / "state.json").write_text(json.dumps({"agents": agents}), encoding="utf-8")
    rows = {r["RUN"]: r["READY/TOTAL"] for r in rounds.list_rows(tmp_path, _cfg(), proc_root=str(tmp_path))}
    assert rows == {153: "1/1", 148: "2/2"}


# ── 178: a rewrite in the same mtime tick is still the runner's ─────────────
def _same_tick(path: Path, body: bytes) -> float:
    path.write_bytes(body)
    tick = float(int(os.stat(path).st_mtime))
    os.utime(path, (tick, tick))
    return tick


def test_178_rewritten_state_in_the_same_tick_is_no_agent_ready(tmp_path):
    state = tmp_path / "state.json"
    tick = _same_tick(state, b"arena wrote this")
    started = math.nextafter(tick, math.inf)
    _same_tick(state, b"the runner rewrote it")  # same whole-second mtime
    assert rounds._map_exit(2, state, started, written=b"arena wrote this") == rounds.EXIT_NO_READY


def test_178_untouched_state_is_a_plain_failure(tmp_path):
    state = tmp_path / "state.json"
    tick = _same_tick(state, b"arena wrote this")
    started = math.nextafter(tick, math.inf)
    assert rounds._map_exit(2, state, started, written=b"arena wrote this") == rounds.EXIT_FAILED
    assert rounds._map_exit(2, state, started) == rounds.EXIT_FAILED
