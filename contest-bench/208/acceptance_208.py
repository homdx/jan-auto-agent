"""Black-box bench for ticket 208, written from the ticket only.

Run from a checkout root: python3 -m pytest contest-bench/208/acceptance_208.py -q
"""
import csv, subprocess, sys
from pathlib import Path

ROOT = Path.cwd()
APPEND = ROOT / "scripts" / "append_task.py"
STATUS = ROOT / "scripts" / "ticket_status.py"
HDR = "ticket,finding,outcome,commit,note"


def append(p, ticket, outcome="FIXED", commit="def", note=""):
    cmd = [sys.executable, str(APPEND), "--progress", str(p), "--ticket", ticket, "--outcome", outcome]
    if commit:
        cmd += ["--commit", commit]
    if note:
        cmd += ["--note", note]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)


def rows(p):
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_unterminated_lf():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "P.csv"
    p.write_bytes(f"{HDR}\n01-a.md,,FIXED,abc,".encode())
    r = append(p, "02-b.md")
    assert r.returncode == 0 and "recorded #2" in r.stdout
    assert [x["ticket"] for x in rows(p)] == ["01-a.md", "02-b.md"]
    assert p.read_bytes().count(b"\n") == 3


def test_unterminated_crlf_keeps_crlf():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "P.csv"
    p.write_bytes(f"{HDR}\r\n01-a.md,,FIXED,abc,".encode())
    assert append(p, "02-b.md").returncode == 0
    b = p.read_bytes()
    assert b.count(b"\r\n") == 3 and b.replace(b"\r\n", b"").count(b"\n") == 0


def test_lf_file_stays_lf():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "P.csv"
    p.write_bytes(f"{HDR}\n01-a.md,,FIXED,abc,".encode())
    append(p, "02-b.md")
    assert b"\r" not in p.read_bytes()


def test_terminated_no_blank_line():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "P.csv"
    p.write_bytes(f"{HDR}\n01-a.md,,FIXED,abc,\n".encode())
    r = append(p, "02-b.md")
    assert "recorded #2" in r.stdout
    assert b"\n\n" not in p.read_bytes() and len(rows(p)) == 2


def test_new_file_header_and_row():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "sub" / "P.csv"
    r = append(p, "01-a.md")
    assert "recorded #1" in r.stdout
    assert p.read_text().splitlines() == [HDR, "01-a.md,,FIXED,abc,"] or \
        p.read_text().splitlines()[0] == HDR
    assert len(rows(p)) == 1


def test_three_appends_to_unterminated():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "P.csv"
    p.write_bytes(f"{HDR}\n01-a.md,,FIXED,abc,".encode())
    for i in (2, 3, 4):
        r = append(p, f"0{i}-x.md")
        assert f"recorded #{i}" in r.stdout
    assert len(rows(p)) == 4


def test_next_task_sees_both():
    import tempfile
    sys.path.insert(0, str(ROOT / "scripts"))
    import next_task as nt
    p = Path(tempfile.mkdtemp()) / "P.csv"
    p.write_bytes(f"{HDR}\n01-a.md,,FIXED,abc,".encode())
    append(p, "02-b.md")
    assert set(nt.recorded(str(p))) == {"01-a.md", "02-b.md"}


def test_skipped_note_and_duplicate_still_work():
    import tempfile
    p = Path(tempfile.mkdtemp()) / "P.csv"
    assert append(p, "01-a.md", "SKIPPED", "", "why").returncode == 0
    assert append(p, "01-a.md", "SKIPPED", "", "why").returncode == 2
    assert append(p, "02-b.md", "FIXED", "").returncode == 1


# ---- bug 41 ----
def git(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout


def repo(tmp, files):
    git(tmp, "init", "-q", "-b", "arena")
    git(tmp, "config", "user.email", "t@t")
    git(tmp, "config", "user.name", "t")
    d = Path(tmp) / "epic-tasks"
    d.mkdir()
    for n, t in files.items():
        (d / n).write_text(t)
    git(tmp, "add", ".")
    git(tmp, "commit", "-q", "-m", "i")


def land(tmp, n):
    return subprocess.run([sys.executable, str(STATUS), "landed", str(n), "--sha", "abc1234",
                           "--repo", str(tmp)], capture_output=True, text=True)


def test_landed_number_heading(tmp_path):
    repo(tmp_path, {"150-x.md": "# 150 — x\n\n**Status:** open\n",
                    "151-y.md": "# 151 — y\n\n**Status:** queued\n",
                    "48-k.md": "# KC-9 k\n\n**Status:** queued\n"})
    r = land(tmp_path, 150)
    assert r.returncode == 0 and "Traceback" not in r.stderr
    assert "git push" in r.stdout and "151" in r.stdout and "KC-9" in r.stdout
    assert "landed `abc1234`" in git(tmp_path, "show", "HEAD:epic-tasks/150-x.md")
    assert git(tmp_path, "status", "--porcelain") == ""


def test_landed_no_heading(tmp_path):
    repo(tmp_path, {"150-x.md": "**Status:** open\n",
                    "151-y.md": "# 151 — y\n\n**Status:** queued\n"})
    r = land(tmp_path, 150)
    assert r.returncode == 0 and "Traceback" not in r.stderr
    assert "git push" in r.stdout and "151" in r.stdout


def test_landed_family_first(tmp_path):
    repo(tmp_path, {"48-k.md": "# KC-9 k\n\n**Status:** queued\n",
                    "49-k.md": "# KC-10 k\n\n**Status:** queued\n",
                    "151-y.md": "# 151 — y\n\n**Status:** queued\n"})
    r = land(tmp_path, 48)
    assert r.returncode == 0
    line = next(l for l in r.stdout.splitlines() if "not landed yet" in l)
    assert "KC-10" in line and "151" not in line


def test_landed_family_with_nothing_left_lists_others(tmp_path):
    repo(tmp_path, {"48-k.md": "# KC-9 k\n\n**Status:** queued\n",
                    "151-y.md": "# 151 — y\n\n**Status:** queued\n"})
    r = land(tmp_path, 48)
    assert r.returncode == 0 and "151" in r.stdout
