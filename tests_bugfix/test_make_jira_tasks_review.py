"""tests_bugfix/test_make_jira_tasks_review.py — scripts/make_jira_tasks.py files one ticket per REAL finding.

The script had no test.
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_jira_tasks.py"


def _csv(path: Path, fields: list[str], rows: list[dict]) -> Path:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({**{field: "" for field in fields}, **row})
    return path


def _truth(tmp_path: Path, rows: list[dict]) -> Path:
    return _csv(tmp_path / "truth.csv", ["finding", "truth", "checked_by", "how", "duplicate_of"],
                [{"truth": "REAL", "checked_by": "x", "how": "ran it", **row} for row in rows])


def _verdicts(tmp_path: Path, name: str, rows: list[dict]) -> Path:
    return _csv(tmp_path / name, ["finding", "truth", "severity", "title", "consequence"], rows)


def _run(tmp_path: Path, truth: Path, *verdicts: Path) -> tuple[list[tuple[str, str, str]], dict]:
    """The INDEX rows `(severity, finding, ticket)` in order, and the tickets by file name."""
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(SCRIPT), "--truth", str(truth), "--out", str(out),
                           "--verdicts", *map(str, verdicts)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    # nothing to file writes no INDEX.md: that is an empty list, and the test says so
    index = (out / "INDEX.md").read_text(encoding="utf-8") if (out / "INDEX.md").exists() else ""
    rows = [(m.group(1), m.group(2), m.group(3)) for m in
            re.finditer(r"^\| \d+ \| (\w+) \| `([^`]+)` \| \[([^\]]+)\]", index, re.M)]
    tickets = {p.name: p.read_text(encoding="utf-8") for p in sorted(out.glob("[0-9]*.md"))}
    return rows, tickets


# ── bug: the severity was the longest word, not the most severe ──────────────
# `longest()` picks the longest *string*: for HIGH and MEDIUM it gave MEDIUM, in the
# ticket and in the order of the index ("highest severity first").

def test_the_ticket_carries_the_most_severe_rating(tmp_path):
    truth = _truth(tmp_path, [{"finding": "a.py::sev"}])
    one = _verdicts(tmp_path, "truth-a.csv", [{"finding": "a.py::sev", "severity": "HIGH"}])
    two = _verdicts(tmp_path, "truth-b.csv", [{"finding": "a.py::sev", "severity": "MEDIUM"}])
    rows, tickets = _run(tmp_path, truth, one, two)
    assert rows == [("HIGH", "a.py::sev", "01-sev.md")]
    assert "**Severity:** HIGH" in tickets["01-sev.md"]


def test_a_finding_rated_high_and_medium_sorts_before_one_rated_only_medium(tmp_path):
    truth = _truth(tmp_path, [{"finding": "a.py::only_medium"}, {"finding": "b.py::high_and_medium"}])
    one = _verdicts(tmp_path, "truth-a.csv", [{"finding": "a.py::only_medium", "severity": "MEDIUM"},
                                              {"finding": "b.py::high_and_medium", "severity": "HIGH"}])
    two = _verdicts(tmp_path, "truth-b.csv", [{"finding": "b.py::high_and_medium", "severity": "MEDIUM"}])
    rows, _ = _run(tmp_path, truth, one, two)
    assert [row[1] for row in rows] == ["b.py::high_and_medium", "a.py::only_medium"]


# ── bug: an unrated finding was shown as MEDIUM but sorted as the least severe ─
# The ticket's severity defaults to MEDIUM, the sort key to "unrated" (last), so an
# index that says "highest severity first" listed a MEDIUM after a LOW.

def test_the_index_order_agrees_with_the_severity_it_shows(tmp_path):
    truth = _truth(tmp_path, [{"finding": "a.py::low"}, {"finding": "b.py::unrated"},
                              {"finding": "c.py::high"}])
    verdicts = _verdicts(tmp_path, "truth-a.csv", [{"finding": "a.py::low", "severity": "LOW"},
                                                   {"finding": "c.py::high", "severity": "HIGH"}])
    rows, _ = _run(tmp_path, truth, verdicts)
    assert [(sev, finding) for sev, finding, _ in rows] == \
        [("HIGH", "c.py::high"), ("MEDIUM", "b.py::unrated"), ("LOW", "a.py::low")]


# ── bug: a REAL finding whose `duplicate_of` names no REAL finding got no ticket ──
# Aliases were only folded into a canonical row that is itself REAL; one pointing at a
# missing, mistyped or non-REAL row was neither folded nor filed — the defect was lost.

def test_a_real_finding_whose_canonical_is_missing_is_filed_on_its_own(tmp_path):
    truth = _truth(tmp_path, [{"finding": "d.py::alias", "duplicate_of": "zzz.py::missing"}])
    rows, tickets = _run(tmp_path, truth)
    assert [row[1] for row in rows] == ["d.py::alias"]
    assert list(tickets) == ["01-alias.md"]


def test_a_real_finding_whose_canonical_is_not_real_is_filed_on_its_own(tmp_path):
    truth = _truth(tmp_path, [{"finding": "d.py::alias", "duplicate_of": "e.py::canon"},
                              {"finding": "e.py::canon", "truth": "FALSE"}])
    rows, _ = _run(tmp_path, truth)
    assert [row[1] for row in rows] == ["d.py::alias"]


def test_two_aliases_of_a_missing_canonical_are_one_ticket(tmp_path):
    truth = _truth(tmp_path, [{"finding": "d.py::one", "duplicate_of": "zzz.py::missing"},
                              {"finding": "d.py::two", "duplicate_of": "zzz.py::missing"}])
    rows, tickets = _run(tmp_path, truth)
    assert [row[1] for row in rows] == ["d.py::one"]
    assert "**Also reported as:** `d.py::two`" in tickets["01-one.md"]


def test_a_chain_of_duplicates_ends_in_one_ticket_for_the_last_canonical(tmp_path):
    truth = _truth(tmp_path, [{"finding": "a.py::a", "duplicate_of": "b.py::b"},
                              {"finding": "b.py::b", "duplicate_of": "c.py::c"},
                              {"finding": "c.py::c"}])
    rows, tickets = _run(tmp_path, truth)
    assert [row[1] for row in rows] == ["c.py::c"]
    text = tickets["01-c.md"]
    assert "`a.py::a`" in text and "`b.py::b`" in text


def test_a_cycle_of_duplicates_still_files_one_ticket(tmp_path):
    truth = _truth(tmp_path, [{"finding": "a.py::a", "duplicate_of": "b.py::b"},
                              {"finding": "b.py::b", "duplicate_of": "a.py::a"}])
    rows, _ = _run(tmp_path, truth)
    assert len(rows) == 1


def test_an_alias_of_a_real_canonical_is_still_folded(tmp_path):
    truth = _truth(tmp_path, [{"finding": "a.py::canon"},
                              {"finding": "b.py::alias", "duplicate_of": "a.py::canon"}])
    rows, tickets = _run(tmp_path, truth)
    assert [row[1] for row in rows] == ["a.py::canon"]
    assert "**Also reported as:** `b.py::alias`" in tickets["01-canon.md"]
