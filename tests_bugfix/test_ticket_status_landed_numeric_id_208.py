"""Bug 41 (round 208): `ticket_status.py landed NN` raised for a ticket whose id is not letter-led.

The family match was None for `# 150 — …` or a heading-less ticket, and `.group`
raised after the status was already committed; now every ticket still to land is listed.
"""
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ticket_status.py"


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout


def _repo(tmp_path, landing_text):
    _git(tmp_path, "init", "-q", "-b", "arena")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    tasks = tmp_path / "epic-tasks"
    tasks.mkdir()
    (tasks / "150-x.md").write_text(landing_text, encoding="utf-8")
    (tasks / "151-y.md").write_text("# 151 — y\n\n**Status:** queued\n", encoding="utf-8")
    (tasks / "48-k.md").write_text("# KC-9 k\n\n**Status:** queued\n", encoding="utf-8")
    (tasks / "49-k.md").write_text("# KC-10 k\n\n**Status:** queued\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _land(repo, n):
    return subprocess.run([sys.executable, str(SCRIPT), "landed", str(n), "--sha", "abc1234",
                           "--repo", str(repo)], capture_output=True, text=True)


def _check_landed(repo, r):
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
    assert "git push" in r.stdout
    assert "landed `abc1234`" in _git(repo, "show", "HEAD:epic-tasks/150-x.md")
    assert _git(repo, "status", "--porcelain") == ""


def test_a_heading_that_starts_with_a_number(tmp_path):
    repo = _repo(tmp_path, "# 150 — x\n\n**Status:** open\n")
    r = _land(repo, 150)
    _check_landed(repo, r)
    for t in ("151 (151)", "KC-9 (48)", "KC-10 (49)"):
        assert t in r.stdout


def test_a_ticket_without_a_heading(tmp_path):
    repo = _repo(tmp_path, "**Status:** open\n")
    r = _land(repo, 150)
    _check_landed(repo, r)
    assert "151 (151)" in r.stdout and "KC-9 (48)" in r.stdout


def test_a_ticket_with_a_family_lists_its_own_epic_first(tmp_path):
    repo = _repo(tmp_path, "# 150 — x\n\n**Status:** queued\n")
    r = _land(repo, 48)
    assert r.returncode == 0, r.stderr
    line = next(l for l in r.stdout.splitlines() if "not landed yet" in l)
    assert "KC-10 (49)" in line and "151" not in line
