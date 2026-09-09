"""scripts/append_finding.py must refuse a --file that is not on disk.

A non-empty --file was the only bar, and a reviewer that never opened the file
clears it by typing something. On the validate1 run, glm-4.5-flash filed 13 of
its 45 rows against ``tools/auto/x.py``, a path that does not exist. Those rows
grouped under a key nobody else used, so the merge read them as solo
discoveries instead of merging them with the other six reviewers' verdicts on
the same symbols — one lazy field silently corrupted the comparison the whole
harness exists to produce.
"""
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "append_finding.py"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run(out, file_arg, *extra, repo_root=None):
    cmd = [sys.executable, str(SCRIPT), "--out", str(out),
           "--variant", "1", "--task-id", "AUTO-T1", "--title", "t",
           "--file", file_arg, "--symbol", "sym",
           "--verdict", "FALSE_POSITIVE", "--severity", "NONE",
           "--disproof", "d", "--repo-root", str(repo_root or ROOT), *extra]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_missing_file_is_refused(tmp_path):
    out = tmp_path / "validation-v1-x.csv"
    r = _run(out, "tools/auto/x.py")
    assert r.returncode == 1
    assert "does not exist" in r.stderr
    assert not out.exists(), "a rejected finding must not reach the CSV"


def test_existing_file_is_accepted(tmp_path):
    out = tmp_path / "validation-v1-x.csv"
    r = _run(out, "scripts/append_finding.py")
    assert r.returncode == 0, r.stderr
    assert out.exists()


def test_allow_missing_file_overrides(tmp_path):
    out = tmp_path / "validation-v1-x.csv"
    r = _run(out, "tools/auto/x.py", "--allow-missing-file")
    assert r.returncode == 0, r.stderr
    assert "tools/auto/x.py" in out.read_text(encoding="utf-8")


def test_empty_file_still_refused_with_its_own_message(tmp_path):
    out = tmp_path / "validation-v1-x.csv"
    r = _run(out, "")
    assert r.returncode == 1
    assert "--file is required" in r.stderr
    assert "does not exist" not in r.stderr, "one problem, not two"
