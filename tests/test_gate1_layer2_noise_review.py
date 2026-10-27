"""tests/test_gate1_layer2_noise_review.py — layer 2's SUMMARY after review batch m: the baselines' noise line counts on
the candidates both runs judged (№86), and a known-answer set with two candidates of one title is refused (№87)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gate1_vote_layer2_noise", ROOT / "scripts" / "gate1_vote_layer2.py")
l2 = importlib.util.module_from_spec(_spec)
sys.modules["gate1_vote_layer2_noise"] = l2
_spec.loader.exec_module(l2)

IDS = [f"AUTO-T{i}" for i in range(1, 7)]
TRUTH = {"AUTO-T1": "REAL", **{f"AUTO-T{i}": "FALSE" for i in range(2, 7)}}


def _res(name, removed, inconclusive=()):
    return {"variant": name, "rc": 0, "seconds": 5, "voters": [], "accept": "all", "ok": True, "checked": 6,
            "removed": {i: "x" for i in removed}, "removed_n": len(removed), "inconclusive": list(inconclusive)}


def test_the_noise_line_counts_what_both_baselines_judged(tmp_path):
    """`off` could not judge AUTO-T3 and `off-2` kept it: the line said 3 and 4 false positives through, the
    verdicts compared against 3 and 3 (they leave the baselines' unresolved out of both sides)."""
    l2.summary(tmp_path, [_res("off", ["AUTO-T2"], inconclusive=["AUTO-T3"]), _res("off-2", ["AUTO-T2"])],
               IDS, TRUTH, {})
    text = (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "false positives through 3 and 3 (on the 5 both judged)" in text and "disagree on 0 candidate(s)" in text


def test_a_candidate_one_run_could_not_judge_is_no_flip(tmp_path):
    l2.summary(tmp_path, [_res("off", ["AUTO-T2"], inconclusive=["AUTO-T3"]), _res("off-2", ["AUTO-T2", "AUTO-T3"])],
               IDS, TRUTH, {})
    assert "disagree on 0 candidate(s)" in (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")


def test_a_set_with_two_candidates_of_one_title_is_refused(tmp_path, capsys):
    known = tmp_path / "known"
    known.mkdir()
    (known / "plan.json").write_text(json.dumps({"tasks": [{"id": "AUTO-T1", "title": "Fix the login bug"},
                                                           {"id": "AUTO-T2", "title": "Fix the login bug"}]}),
                                     encoding="utf-8")
    (known / "TARGET").write_text("4ff7d141f77c66c67c3d47ca643054feb01c8e4c\n", encoding="utf-8")
    assert l2.main(["--known", str(known), "--dry-run", "--sets", "3"]) == 2
    assert "share a title ('Fix the login bug')" in capsys.readouterr().err
