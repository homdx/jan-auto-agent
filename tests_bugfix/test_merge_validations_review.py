"""tests_bugfix/test_merge_validations_review.py — scripts/merge_validations.py's on-screen report.

The `--csv` pivot is covered by tests/test_competition_merge_csv.py; the report printed to
the screen had no test, and disagreed with the pivot.
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "merge_validations.py"
sys.path.insert(0, str(ROOT / "scripts"))
import merge_validations as mv  # noqa: E402


def _csv(path: Path, rows: list[dict], width: int | None = None) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(mv.COLUMNS)
        for row in rows:
            full = {col: "" for col in mv.COLUMNS}
            full.update(row)
            values = [full[col] for col in mv.COLUMNS]
            writer.writerow(values[:width] if width else values)
    return path


def _row(task, file, symbol, verdict, severity="LOW"):
    return {"task_id": task, "file": file, "symbol": symbol, "verdict": verdict,
            "severity": severity, "title": f"{symbol} title"}


def _merge(tmp_path: Path, *files: Path) -> tuple[subprocess.CompletedProcess, dict, dict]:
    """The completed run, the screen's sections `{heading: [lines]}`, and the pivot by finding."""
    out = tmp_path / "pivot.csv"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--csv", str(out), *map(str, files)],
                          capture_output=True, text=True)
    sections: dict[str, list[str]] = {}
    current = None
    for line in proc.stdout.splitlines():
        match = re.match(r"^── (SPLIT VERDICTS|AGREED|SEEN BY ONE REVIEWER)", line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    pivot = {}
    if out.exists():
        with open(out, newline="", encoding="utf-8") as fh:
            pivot = {r["finding"]: r for r in csv.DictReader(fh)}
    return proc, sections, pivot


def _where(sections: dict, finding: str) -> str:
    hits = [name for name, lines in sections.items() if any(finding in line for line in lines)]
    assert len(hits) == 1, (finding, hits, sections)
    return hits[0]


SCREEN = {"SPLIT": "SPLIT VERDICTS", "UNANIMOUS": "AGREED", "SOLO": "SEEN BY ONE REVIEWER"}


# ── bug: a finding only one reviewer judged was reported as "AGREED" ─────────
# The screen counted ROWS (`len(rs) == 1`), the pivot counts reviewers. A reviewer who
# judged one symbol under two task-ids made two rows, so the screen printed it as agreed
# — with only the last of her two verdicts, hiding that she contradicted herself — while
# the pivot (and the script's own comments) call it SOLO with `CONFIRMED|FALSE_POSITIVE`.

def _alice_twice(tmp_path):
    alice = _csv(tmp_path / "validation-v1-alice.csv", [
        _row("T1", "x.py", "f", "CONFIRMED", "HIGH"), _row("T2", "x.py", "f", "FALSE_POSITIVE", "LOW"),
        _row("T3", "y.py", "g", "CONFIRMED")])
    bob = _csv(tmp_path / "validation-v1-bob.csv", [_row("T3", "y.py", "g", "CONFIRMED")])
    return alice, bob


def test_a_finding_one_reviewer_judged_twice_is_seen_by_one_reviewer(tmp_path):
    proc, sections, pivot = _merge(tmp_path, *_alice_twice(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert _where(sections, "x.py::f") == "SEEN BY ONE REVIEWER"
    assert pivot["x.py::f"]["agreement"] == "SOLO"


def test_her_two_verdicts_are_both_shown(tmp_path):
    _, sections, _ = _merge(tmp_path, *_alice_twice(tmp_path))
    line = next(l for l in sections["SEEN BY ONE REVIEWER"] if "x.py::f" in l)
    assert "CONFIRMED" in line and "FALSE_POSITIVE" in line
    assert "HIGH" in line                                  # the worst severity of her two rows


def test_the_screen_and_the_pivot_put_every_finding_in_the_same_bucket(tmp_path):
    alice = _csv(tmp_path / "validation-v1-alice.csv", [
        _row("T1", "a.py", "split", "CONFIRMED"), _row("T2", "b.py", "agreed", "CONFIRMED"),
        _row("T3", "c.py", "solo", "CONFIRMED"),
        _row("T4", "d.py", "self", "CONFIRMED"), _row("T5", "d.py", "self", "FALSE_POSITIVE"),
        _row("T6", "e.py", "both_self", "CONFIRMED"), _row("T7", "e.py", "both_self", "FALSE_POSITIVE")])
    bob = _csv(tmp_path / "validation-v1-bob.csv", [
        _row("T1", "a.py", "split", "FALSE_POSITIVE"), _row("T2", "b.py", "agreed", "CONFIRMED"),
        _row("T6", "e.py", "both_self", "CONFIRMED"), _row("T8", "e.py", "both_self", "FALSE_POSITIVE")])
    proc, sections, pivot = _merge(tmp_path, alice, bob)
    assert proc.returncode == 0, proc.stderr
    assert len(pivot) == 5
    for finding, record in pivot.items():
        assert _where(sections, finding) == SCREEN[record["agreement"]], (finding, record["agreement"])
    assert {f: r["agreement"] for f, r in pivot.items()} == {
        "a.py::split": "SPLIT", "b.py::agreed": "UNANIMOUS", "c.py::solo": "SOLO",
        "d.py::self": "SOLO", "e.py::both_self": "SPLIT"}


def test_the_ordinary_cases_read_as_before(tmp_path):
    proc, sections, _ = _merge(tmp_path, *_alice_twice(tmp_path))
    agreed = next(l for l in sections["AGREED"] if "y.py::g" in l)
    assert agreed.split()[0] == "CONFIRMED"


# ── bug: a truncated row crashed the report ──────────────────────────────────
# `csv.DictReader` fills the fields a short row lacks with None, and the report read
# them with `r.get("verdict", "").strip()` — which returns that None. The script is
# meant for CSVs written by models, which truncate rows, and it prints a "schema
# problems" list for them instead of failing.

def test_a_truncated_row_is_counted_not_a_traceback(tmp_path):
    short = _csv(tmp_path / "validation-v1-carol.csv", [_row("T9", "a.py", "f", "CONFIRMED")], width=4)
    proc, _, _ = _merge(tmp_path, short)
    assert proc.returncode == 0, proc.stderr
    assert "Traceback" not in proc.stderr
    assert re.search(r"^\s+carol\s+1\b", proc.stdout, re.M), proc.stdout


def test_a_truncated_row_next_to_a_whole_one(tmp_path):
    whole = _csv(tmp_path / "validation-v1-alice.csv", [_row("T1", "a.py", "f", "CONFIRMED")])
    short = _csv(tmp_path / "validation-v1-carol.csv", [_row("T1", "a.py", "f", "CONFIRMED")], width=5)
    proc, sections, pivot = _merge(tmp_path, whole, short)
    assert proc.returncode == 0, proc.stderr
    assert pivot["a.py::f"]["alice"] == "CONFIRMED"
