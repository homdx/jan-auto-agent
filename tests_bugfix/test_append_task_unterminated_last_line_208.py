"""Bug 40 (round 208): a row appended after an unterminated last line was glued onto it.

csv writes where the file ends, so a PROGRESS.csv left without its final newline
got the new row on its last line, and next_task.py handed that ticket out again.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "append_task.py"
sys.path.insert(0, str(ROOT / "scripts"))
import next_task as nt  # noqa: E402

HEADER = "ticket,finding,outcome,commit,note"


def _append(progress, ticket, commit):
    return subprocess.run([sys.executable, str(SCRIPT), "--progress", str(progress),
                           "--ticket", ticket, "--outcome", "FIXED", "--commit", commit],
                          capture_output=True, text=True)


def test_a_row_after_an_unterminated_line_is_its_own_row(tmp_path):
    p = tmp_path / "P.csv"
    p.write_bytes(f"{HEADER}\n01-a.md,,FIXED,abc,".encode())
    r = _append(p, "02-b.md", "def")
    assert r.returncode == 0, r.stderr
    assert "recorded #2" in r.stdout
    assert p.read_bytes() == f"{HEADER}\n01-a.md,,FIXED,abc,\n02-b.md,,FIXED,def,\n".encode()
    assert nt.recorded(str(p)) == {"01-a.md": "FIXED", "02-b.md": "FIXED"}


def test_a_crlf_file_keeps_crlf(tmp_path):
    p = tmp_path / "P.csv"
    p.write_bytes(f"{HEADER}\r\n01-a.md,,FIXED,abc,".encode())
    assert _append(p, "02-b.md", "def").returncode == 0
    assert p.read_bytes() == f"{HEADER}\r\n01-a.md,,FIXED,abc,\r\n02-b.md,,FIXED,def,\r\n".encode()


def test_a_terminated_file_gets_no_blank_line(tmp_path):
    p = tmp_path / "P.csv"
    p.write_bytes(f"{HEADER}\n01-a.md,,FIXED,abc,\n".encode())
    r = _append(p, "02-b.md", "def")
    assert "recorded #2" in r.stdout
    assert p.read_bytes() == f"{HEADER}\n01-a.md,,FIXED,abc,\n02-b.md,,FIXED,def,\n".encode()


def test_a_new_file_gets_its_header_and_first_row(tmp_path):
    p = tmp_path / "sub" / "P.csv"
    r = _append(p, "01-a.md", "abc")
    assert "recorded #1" in r.stdout
    assert p.read_text(encoding="utf-8").splitlines() == [HEADER, "01-a.md,,FIXED,abc,"]
    assert _append(p, "02-b.md", "def").returncode == 0
    assert nt.recorded(str(p)) == {"01-a.md": "FIXED", "02-b.md": "FIXED"}
