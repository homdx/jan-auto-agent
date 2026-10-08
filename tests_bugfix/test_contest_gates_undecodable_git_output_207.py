"""207 bug 37: judge_worktree reads a fixture, a Latin-1 test file and a Latin-1 message without raising."""
import subprocess

import pytest

from tools.contest import gates


def _git(cwd, *args, env=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": __import__("os").environ["PATH"], "HOME": str(cwd)})


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "tests" / "fixtures").mkdir(parents=True)
    (tmp_path / "tests" / "fixtures" / "sample.png").write_bytes(b"\x89PNG\r\n\xff\xfe\x00\x80")
    (tmp_path / "tests" / "test_a.py").write_bytes(
        b"# caf\xe9\ndef test_one():\n    pass\n\ndef test_two():\n    pass\n")
    _git(tmp_path, "add", "-A")
    (tmp_path / "msg").write_bytes(b"agent work caf\xe9\n")
    _git(tmp_path, "commit", "-q", "-F", "msg")
    return tmp_path


def test_judge_worktree_survives_a_fixture_and_latin1_bytes(repo):
    row = gates.judge_worktree("a", str(repo), "HEAD~1", [], False)
    assert row["commits"] == 1
    assert row["test_files"] == 2          # the fixture is a file in the row
    assert row["test_funcs"] == 2          # the Latin-1 test file still counts its tests


def test_git_helper_reads_a_latin1_commit_message(repo):
    out = gates.git(repo, "log", "--format=%B", "-1")
    assert "agent work caf" in out


def test_extract_shrink_reads_a_bridge_file_with_a_latin1_byte(repo):
    bridge = repo / gates.BRIDGE
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.write_bytes(b"# caf\xe9\ndef _shrink(x):\n    return x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bridge")
    assert "def _shrink" in gates.extract_shrink(repo, "HEAD")


def test_absent_bridge_is_still_none(repo):
    assert gates.extract_shrink(repo, "HEAD") is None
