"""RUN-12: run_flows.sh with no task runs 1-3 as RUNBOOK.md says; task 4 only when named."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _summary(*args):
    out = subprocess.run(["bash", str(ROOT / "scripts" / "run_flows.sh"), "--check-only",
                          "--config", "agents_128k.ini", *args],
                         cwd=ROOT, capture_output=True, text=True, timeout=120).stdout
    return re.findall(r"^\s*task (\d): run=", out, re.M)


def test_no_task_named_runs_the_three_that_need_one_provider():
    assert _summary() == ["1", "2", "3"]


def test_task_four_runs_when_named():
    assert _summary("4") == ["4"]
