"""GYRO-01: append_task.py records a row when copied alone into another repo.

Before: it imported `tools.file_lock`, which an external --target does not have
(llama.cpp has its own `tools/`), so every agent there died on ImportError and
the harvest answered `no_progress_row`.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "append_task.py"


def test_append_task_runs_without_tools_file_lock(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tools").mkdir()          # a foreign tools/ dir, as in llama.cpp
    shutil.copy(SCRIPT, tmp_path / "scripts" / "append_task.py")
    r = subprocess.run([sys.executable, "-I", "scripts/append_task.py",
                        "--progress", "runs/a/PROGRESS.csv", "--ticket", "01-x.md",
                        "--outcome", "DONE", "--commit", "abc1234", "--note", "n"],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    with open(tmp_path / "runs/a/PROGRESS.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [(x["ticket"], x["outcome"], x["commit"]) for x in rows] == [("01-x.md", "FIXED", "abc1234")]
