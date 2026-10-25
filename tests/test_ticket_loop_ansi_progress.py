"""A PROGRESS.csv / findings CSV saved as plain ANSI keeps its progress and is refused, not crashed on."""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HEADER = "ticket,finding,outcome,commit,note\r\n"


def _run(script, *args):
    return subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                          capture_output=True, text=True, cwd=str(REPO))


def _tasks(tmp_path, encoding="cp1251"):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "01-a.md").write_text("# a\n", encoding="utf-8")
    (tasks / "02-b.md").write_text("# b\n", encoding="utf-8")
    progress = tasks / "PROGRESS.csv"
    progress.write_bytes((HEADER + "01-a.md,,FIXED,abc123,исправлено\r\n").encode(encoding))
    return tasks, progress


def test_next_task_keeps_the_progress_of_an_ansi_file_and_says_so(tmp_path):
    tasks, _ = _tasks(tmp_path)
    r = _run("next_task.py", "--tasks", str(tasks))
    assert r.returncode == 0, r.stderr
    assert "progress: 1/2 recorded" in r.stdout          # was 0/2: 01-a.md handed out again
    assert "02-b.md" in r.stdout and "01-a.md  (already" not in r.stdout
    assert "is not UTF-8" in r.stderr and "CSV UTF-8" in r.stderr


def test_a_utf8_file_reads_with_no_warning(tmp_path):
    tasks, _ = _tasks(tmp_path, encoding="utf-8")
    r = _run("next_task.py", "--tasks", str(tasks))
    assert "progress: 1/2 recorded" in r.stdout and r.stderr == ""


def test_append_task_refuses_an_ansi_file_and_leaves_it_alone(tmp_path):
    _, progress = _tasks(tmp_path)
    before = progress.read_bytes()
    for ticket in ("02-b.md", "01-a.md"):                 # a new row, and a duplicate
        r = _run("append_task.py", "--progress", str(progress), "--ticket", ticket,
                 "--outcome", "FIXED", "--commit", "def456")
        assert r.returncode == 3, r.stdout + r.stderr
        assert "Traceback" not in r.stderr and "is not UTF-8" in r.stderr
    assert progress.read_bytes() == before


def test_append_finding_refuses_an_ansi_file(tmp_path):
    out = tmp_path / "validation-v1-x.csv"
    out.write_bytes("file,symbol,verdict\r\nmain.py,f,REAL примечание\r\n".encode("cp1251"))
    before = out.read_bytes()
    r = _run("append_finding.py", "--out", str(out), "--variant", "1", "--task-id", "AUTO-T1",
             "--title", "t", "--file", "main.py", "--symbol", "sym", "--verdict", "FALSE_POSITIVE",
             "--severity", "NONE", "--disproof", "d", "--repo-root", str(REPO))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "Traceback" not in r.stderr and "is not UTF-8" in r.stderr
    assert out.read_bytes() == before
