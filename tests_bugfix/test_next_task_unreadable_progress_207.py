"""207 bug 39: an undecodable, NUL-holding or oversized-field PROGRESS.csv reads as empty; the script hands out the ticket."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import next_task  # noqa: E402


@pytest.mark.parametrize("body", [
    b"\xff\xfeticket,outcome\n",
    b"ticket,outcome\nNN\x00,landed\n",
    b"ticket,outcome\n01-a," + b"x" * 200_000 + b"\n",
])
def test_unreadable_file_is_read_as_empty(tmp_path, body):
    path = tmp_path / "PROGRESS.csv"
    path.write_bytes(body)
    assert next_task.recorded(str(path)) == {}


def test_readable_file_still_reads(tmp_path):
    path = tmp_path / "PROGRESS.csv"
    path.write_text("ticket,outcome\n01-a,landed\n")
    assert next_task.recorded(str(path)) == {"01-a": "landed"}


def test_script_hands_out_the_ticket_past_an_undecodable_file(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "01-first.md").write_text("# first\n\n**Status:** open\n")
    (tasks / "PROGRESS.csv").write_bytes(b"\xff\xfe\x00junk")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "next_task.py"), "--tasks", str(tasks)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert "01-first.md" in r.stdout


def test_readable_file_still_skips_the_recorded_ticket(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "01-first.md").write_text("# first\n\n**Status:** open\n")
    (tasks / "02-second.md").write_text("# second\n\n**Status:** open\n")
    (tasks / "PROGRESS.csv").write_text("ticket,outcome\n01-first.md,landed\n")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "next_task.py"), "--tasks", str(tasks)],
                       capture_output=True, text=True)
    assert "02-second.md" in r.stdout
