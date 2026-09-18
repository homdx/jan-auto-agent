"""next_task.py hands out only tickets whose **Status:** is neither
`landed` nor `queued`, and append_task.py accepts the runbook's
`--outcome DONE` as FIXED.

Before: a fresh per-agent PROGRESS.csv made next_task.py hand out ticket
01 (landed months ago) instead of the round's open ticket, and the
prompt's `--outcome DONE` was rejected by append_task.py, so the agent's
ticket stayed unrecorded and was handed out again.
"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEXT = ROOT / "scripts" / "next_task.py"
APPEND = ROOT / "scripts" / "append_task.py"


def _ticket(path: Path, title: str, status: str | None) -> None:
    lines = [f"# {title}", ""]
    if status is not None:
        lines.append(f"**Status:** {status}  ")
    lines += ["**Severity:** HIGH  ", "**File:** `tools/x.py`  ", "**Symbol:** `f`  ", "", "body"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], capture_output=True, text=True)


def _folder(tmp_path: Path) -> Path:
    d = tmp_path / "epic"
    d.mkdir()
    _ticket(d / "01-landed.md", "L1", "landed — `8212df1`")
    _ticket(d / "02-queued.md", "Q1", "queued — open, not on offer until INDEX.md reaches it")
    _ticket(d / "03-open.md", "RUN-7", "open (base: `96d6d35`)")
    _ticket(d / "04-nostatus.md", "T4", None)
    return d


def test_landed_and_queued_are_not_offered(tmp_path):
    d = _folder(tmp_path)
    r = _run(str(NEXT), "--tasks", str(d), "--progress", str(tmp_path / "P.csv"))
    assert r.returncode == 0, r.stderr
    assert "# RUN-7" in r.stdout
    assert "# L1" not in r.stdout and "# Q1" not in r.stdout
    assert "0/2 recorded, 2 remaining (2 landed/queued, not on offer)" in r.stdout


def test_status_line_with_backticks_and_dashes_still_parses(tmp_path):
    d = tmp_path / "epic"
    d.mkdir()
    _ticket(d / "01-a.md", "A", "landed — the commit after `24d9028` (RUN-5; base `ade28e6`)")
    _ticket(d / "02-b.md", "B", "open")
    r = _run(str(NEXT), "--tasks", str(d), "--progress", str(tmp_path / "P.csv"))
    assert "# B" in r.stdout and "# A" not in r.stdout


def test_recorded_ticket_is_not_offered_again_and_done_means_fixed(tmp_path):
    d = _folder(tmp_path)
    progress = tmp_path / "P.csv"
    r = _run(str(APPEND), "--progress", str(progress), "--ticket", "03-open.md",
             "--outcome", "DONE", "--commit", "deadbeef")
    assert r.returncode == 0, r.stderr
    with progress.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows[0]["outcome"] == "FIXED"

    r = _run(str(NEXT), "--tasks", str(d), "--progress", str(progress))
    assert "# RUN-7" not in r.stdout
    assert "# T4" in r.stdout          # no Status line at all → offered (tasks/ tickets)

    r = _run(str(APPEND), "--progress", str(progress), "--ticket", "04-nostatus.md",
             "--outcome", "FIXED", "--commit", "cafe")
    r = _run(str(NEXT), "--tasks", str(d), "--progress", str(progress))
    assert r.returncode == 3
    assert "Every ticket is recorded" in r.stdout


def test_nothing_on_offer_exits_3(tmp_path):
    d = tmp_path / "epic"
    d.mkdir()
    _ticket(d / "01-a.md", "A", "landed — `x`")
    r = _run(str(NEXT), "--tasks", str(d), "--progress", str(tmp_path / "P.csv"))
    assert r.returncode == 3
    assert "Nothing on offer" in r.stdout
