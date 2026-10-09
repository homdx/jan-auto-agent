"""Ticket 212 store half: tree fingerprint stability and the round's append-only run cache."""
from __future__ import annotations

import json
import multiprocessing
import subprocess
from pathlib import Path

import pytest

from tools.contest.testcache_store import TestRunCache, tree_fingerprint


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "a.py").write_text("x = 1\n")
    (r / ".gitignore").write_text("ignored.txt\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


def test_fingerprint_stable(repo):
    assert tree_fingerprint(repo) == tree_fingerprint(repo)
    assert tree_fingerprint(repo) is not None


def test_fingerprint_tracked_edit(repo):
    before = tree_fingerprint(repo)
    (repo / "a.py").write_text("x = 2\n")
    assert tree_fingerprint(repo) != before


def test_fingerprint_untracked_file(repo):
    before = tree_fingerprint(repo)
    (repo / "new.py").write_text("y\n")
    mid = tree_fingerprint(repo)
    assert mid != before
    (repo / "new.py").write_text("z\n")
    assert tree_fingerprint(repo) != mid


def test_fingerprint_ignores_runs_and_pycache(repo):
    before = tree_fingerprint(repo)
    (repo / "runs").mkdir()
    (repo / "runs" / "x").write_text("log")
    (repo / "pkg" / "__pycache__").mkdir(parents=True)
    (repo / "pkg" / "__pycache__" / "m.pyc").write_bytes(b"\0\1")
    (repo / ".pytest_cache").mkdir()
    (repo / ".pytest_cache" / "v").write_text("1")
    (repo / "ignored.txt").write_text("gitignored")
    assert tree_fingerprint(repo) == before


def test_fingerprint_ignores_tracked_runs_change(repo):
    (repo / "runs").mkdir()
    (repo / "runs" / "x").write_text("1")
    _git(repo, "add", "runs/x")
    _git(repo, "commit", "-q", "-m", "runs")
    before = tree_fingerprint(repo)
    (repo / "runs" / "x").write_text("2")
    assert tree_fingerprint(repo) == before


def test_fingerprint_none_outside_git(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    assert tree_fingerprint(d) is None
    assert tree_fingerprint(tmp_path / "missing") is None


def test_fingerprint_leaves_status_alone(repo):
    (repo / "a.py").write_text("x = 3\n")
    (repo / "u.py").write_text("u\n")
    before = _git(repo, "status", "--porcelain")
    index = (repo / ".git" / "index").read_bytes()
    tree_fingerprint(repo)
    assert _git(repo, "status", "--porcelain") == before
    assert (repo / ".git" / "index").read_bytes() == index


def test_fingerprint_symlink_hashes_link_text(repo):
    (repo / "l").symlink_to("target-one")
    one = tree_fingerprint(repo)
    (repo / "l").unlink()
    (repo / "l").symlink_to("target-two")
    assert tree_fingerprint(repo) != one


def test_round_trip_and_newest_wins(tmp_path):
    c = TestRunCache(tmp_path / "sub" / "c.jsonl")
    c.record("k", "fp", {"passed": 1}, 0, 1.5, "a1")
    c.record("k", "fp", {"passed": 2}, 1, 2.5, "a2")
    e = c.lookup("k", "fp")
    assert e["summary"] == {"passed": 2} and e["rc"] == 1
    assert e["wall_s"] == 2.5 and e["agent"] == "a2" and e["key"] == "k" and e["t"]


def test_miss_on_other_key_or_fingerprint(tmp_path):
    c = TestRunCache(tmp_path / "c.jsonl")
    c.record("k", "fp", {}, 0, 1.0, "a")
    assert c.lookup("k2", "fp") is None
    assert c.lookup("k", "fp2") is None


def test_missing_file(tmp_path):
    c = TestRunCache(tmp_path / "nope.jsonl")
    assert c.lookup("k", "fp") is None
    assert c.classification("cmd") == (False, None)


def test_torn_last_line_skipped_and_next_append_whole(tmp_path):
    p = tmp_path / "c.jsonl"
    c = TestRunCache(p)
    c.record("k", "fp", {"n": 1}, 0, 1.0, "a")
    with open(p, "ab") as f:
        f.write(b'{"key": "k", "fp": "fp", "summ')
    assert c.lookup("k", "fp")["summary"] == {"n": 1}
    c.record("k", "fp", {"n": 2}, 0, 1.0, "b")
    assert c.lookup("k", "fp")["summary"] == {"n": 2}


def _writer(path: str, i: int) -> None:
    TestRunCache(path).record(f"k{i}", "fp", {"i": i, "pad": "x" * 5000}, 0, 1.0, f"a{i}")


def test_concurrent_appends_ten_whole_lines(tmp_path):
    p = tmp_path / "c.jsonl"
    procs = [multiprocessing.Process(target=_writer, args=(str(p), i)) for i in range(10)]
    for pr in procs:
        pr.start()
    for pr in procs:
        pr.join()
    lines = p.read_text().splitlines()
    assert len(lines) == 10
    assert sorted(json.loads(x)["key"] for x in lines) == sorted(f"k{i}" for i in range(10))


def test_classification_kept_apart(tmp_path):
    c = TestRunCache(tmp_path / "c.jsonl")
    c.record_classification("bash run_tests.sh", "tests|-q")
    c.record_classification("make lint", None)
    c.record("tests|-q", "fp", {}, 0, 1.0, "a")
    assert c.classification("bash run_tests.sh") == (True, "tests|-q")
    assert c.classification("make lint") == (True, None)
    assert c.classification("other") == (False, None)
    assert c.classification("tests|-q") == (False, None)   # a run key is not a command
    assert c.lookup("tests|-q", "fp")["agent"] == "a"
    assert c.lookup("bash run_tests.sh", "fp") is None


def test_unwritable_path_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    c = TestRunCache(blocker / "sub" / "c.jsonl")   # parent is a file
    c.record("k", "fp", {}, 0, 1.0, "a")
    c.record_classification("cmd", None)
    assert c.lookup("k", "fp") is None
