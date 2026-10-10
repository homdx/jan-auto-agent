"""CC-2 judging fix: the target's git view runs no textconv / external diff and reads no outside file."""
import os
import subprocess

import pytest

from tools.claimcheck import target as T

_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@a",
            GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@a")


def _g(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], env=_ENV, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def evil(tmp_path):
    """A repo whose config defines a textconv and an external diff that leave a marker file."""
    repo = tmp_path / "r"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("A=1\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "one")
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    (repo / "a.py").write_text("A=2\n")
    marker = tmp_path / "RAN"
    _g(repo, "config", "diff.evil.textconv", f"touch {marker}; cat")
    _g(repo, "config", "diff.evil.command", f"touch {marker}; true")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "two")
    return repo, marker


@pytest.mark.parametrize("args", [("show", "HEAD"), ("diff", "HEAD~1", "HEAD"), ("log", "-p")])
def test_view_git_runs_no_driver_of_the_operators_config(evil, tmp_path, args):
    repo, marker = evil
    with T.Target.open(repo, "main", scratch=tmp_path / "s") as t:
        out = t.view().git(*args)
    assert "A=2" in out or "a.py" in out
    assert not marker.exists()


def test_view_git_refuses_blame_contents_of_an_outside_file(evil, tmp_path):
    repo, _ = evil
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    with T.Target.open(repo, "main", scratch=tmp_path / "s") as t:
        with pytest.raises(T.TargetError):
            t.view().git("blame", "--contents", str(outside), "a.py")


def test_view_does_not_see_what_collect_wrote(evil, tmp_path):
    repo, _ = evil
    with T.Target.open(repo, "main", scratch=tmp_path / "s") as t:
        t.collect()
        v = t.view()
        assert (t.tree / ".collect").is_dir()
        assert not v.exists(".collect") and not v.exists(".collect/MODULE_MAP.md")
        assert not v.exists("./.collect/MODULE_MAP.md")
        with pytest.raises(FileNotFoundError):
            v.read(".collect/MODULE_MAP.md")
        assert not [p for p in v.files() if p.startswith(".collect")]
        assert v.exists("a.py") and v.read("a.py") == "A=2\n"
