"""tests_bugfix/test_truth_consensus_review.py — scripts/truth_consensus.py merges adjudicator verdicts.

The script had no test. It merges `truth-<name>.csv` files into a settled ground truth and
a report; exit 4 means something is left open for a human.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "truth_consensus.py"
FIELDS = ["finding", "truth", "checked_by", "confidence", "how", "evidence",
          "consequence", "severity", "fix", "notes"]


def _csv(path: Path, rows: list[dict]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{field: "" for field in FIELDS}, **row})
    return path


def _real(finding: str, severity: str) -> dict:
    return {"finding": finding, "truth": "REAL", "how": "ran it", "evidence": "line",
            "consequence": "breaks", "severity": severity}


def _run(tmp_path: Path, *files: Path, extra=()) -> tuple[int, str, str]:
    report, truth = tmp_path / "report.md", tmp_path / "truth.csv"
    proc = subprocess.run([sys.executable, str(SCRIPT), *map(str, files), "--report", str(report),
                           "--truth", str(truth), *extra], capture_output=True, text=True)
    return proc.returncode, (report.read_text(encoding="utf-8") if report.exists() else ""), \
        (truth.read_text(encoding="utf-8") if truth.exists() else "")


# ── bug: a finding every adjudicator left UNDECIDED vanished ─────────────────
# `distinct` (the votes that are not UNDECIDED) is empty, so the finding was neither
# settled nor disputed: not in the report, not in the truth file, counted in
# "findings" only, and the exit code said nothing was left open.

def test_a_finding_everyone_left_undecided_is_reported_and_leaves_the_run_open(tmp_path):
    a = _csv(tmp_path / "truth-alice.csv", [{"finding": "a.py::open", "truth": "UNDECIDED", "how": "unsure"}])
    b = _csv(tmp_path / "truth-bob.csv", [{"finding": "a.py::open", "truth": "UNDECIDED", "how": "unsure"}])
    code, report, truth = _run(tmp_path, a, b)
    assert "a.py::open" in report
    assert "a.py::open" not in truth                   # nothing was settled
    assert code == 4                                   # something is left for a human


def test_a_finding_one_adjudicator_left_undecided_is_still_settled_by_the_other(tmp_path):
    a = _csv(tmp_path / "truth-alice.csv", [{"finding": "a.py::x", "truth": "UNDECIDED", "how": "unsure"}])
    b = _csv(tmp_path / "truth-bob.csv", [_real("a.py::x", "LOW")])
    code, _, truth = _run(tmp_path, a, b)
    assert code == 0
    assert "a.py::x,REAL" in truth


def test_the_counts_add_up(tmp_path):
    a = _csv(tmp_path / "truth-alice.csv", [{"finding": "u", "truth": "UNDECIDED", "how": "?"},
                                            _real("r", "LOW")])
    b = _csv(tmp_path / "truth-bob.csv", [{"finding": "u", "truth": "UNDECIDED", "how": "?"},
                                          _real("r", "LOW")])
    _, report, _ = _run(tmp_path, a, b)
    assert "2 findings · 1 settled · 1 disputed" in report


# ── bug: `--arbiter` was compared with the lower-cased adjudicator names ──────
# Adjudicators are named from the file stem, lower-cased (`truth-Bob.csv` → `bob`), but
# the arbiter was compared as typed: `--arbiter Bob` never matched and the split stayed
# open without a word.

def _split(tmp_path: Path) -> tuple[Path, Path]:
    a = _csv(tmp_path / "truth-alice.csv", [_real("b.py::split", "HIGH")])
    b = _csv(tmp_path / "truth-Bob.csv", [{"finding": "b.py::split", "truth": "FALSE", "how": "ran it"}])
    return a, b


def test_the_arbiter_is_matched_however_it_is_spelled(tmp_path):
    for spelling in ("bob", "Bob", "BOB"):
        out = tmp_path / spelling
        out.mkdir()
        code, report, truth = _run(out, *_split(tmp_path), extra=("--arbiter", spelling))
        assert code == 0, spelling
        assert "Settled by the arbiter (1)" in report, spelling
        assert "b.py::split,FALSE" in truth, spelling


def test_an_arbiter_who_did_not_vote_leaves_the_split_open(tmp_path):
    code, report, _ = _run(tmp_path, *_split(tmp_path), extra=("--arbiter", "carol"))
    assert code == 4
    assert "Disputed (1)" in report


def test_without_an_arbiter_a_split_stays_open(tmp_path):
    code, report, truth = _run(tmp_path, *_split(tmp_path))
    assert code == 4 and "Disputed (1)" in report and "b.py::split" not in truth


# ── bug: the reported severity was the longest word, not the most severe ─────
# `longest()` is for the text fields (consequence, evidence, how); it picked the
# severity too, so MEDIUM (6 letters) beat HIGH (4), and the "Confirmed real" list,
# which is sorted by severity, ranked the finding by the lower rating.

def test_the_most_severe_rating_is_the_one_reported(tmp_path):
    a = _csv(tmp_path / "truth-alice.csv", [_real("c.py::sev", "HIGH")])
    b = _csv(tmp_path / "truth-bob.csv", [_real("c.py::sev", "MEDIUM")])
    _, report, _ = _run(tmp_path, a, b)
    assert "### `c.py::sev` — HIGH" in report


def test_the_rating_does_not_depend_on_the_order_of_the_files(tmp_path):
    a = _csv(tmp_path / "truth-alice.csv", [_real("c.py::sev", "LOW")])
    b = _csv(tmp_path / "truth-bob.csv", [_real("c.py::sev", "NONE")])
    _, forward, _ = _run(tmp_path, a, b)
    out = tmp_path / "rev"
    out.mkdir()
    _, backward, _ = _run(out, b, a)
    assert "### `c.py::sev` — LOW" in forward
    assert "### `c.py::sev` — LOW" in backward


def test_a_blank_rating_is_beaten_by_any_rating(tmp_path):
    a = _csv(tmp_path / "truth-alice.csv", [_real("c.py::sev", "")])
    b = _csv(tmp_path / "truth-bob.csv", [_real("c.py::sev", "LOW")])
    _, report, _ = _run(tmp_path, a, b)
    assert "### `c.py::sev` — LOW" in report


def test_confirmed_real_is_ordered_by_the_most_severe_rating(tmp_path):
    a = _csv(tmp_path / "truth-alice.csv", [_real("a.py::only_medium", "MEDIUM"),
                                            _real("b.py::high_and_medium", "HIGH")])
    b = _csv(tmp_path / "truth-bob.csv", [_real("a.py::only_medium", "MEDIUM"),
                                          _real("b.py::high_and_medium", "MEDIUM")])
    _, report, _ = _run(tmp_path, a, b)
    assert report.index("b.py::high_and_medium") < report.index("a.py::only_medium")


def test_the_text_fields_still_report_the_longest_text(tmp_path):
    a = _csv(tmp_path / "truth-alice.csv", [{**_real("c.py::t", "LOW"), "consequence": "short"}])
    b = _csv(tmp_path / "truth-bob.csv", [{**_real("c.py::t", "LOW"), "consequence": "a longer consequence"}])
    _, report, _ = _run(tmp_path, a, b)
    assert "**consequence:** a longer consequence" in report
