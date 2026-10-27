"""CC-1 fix found building CC-3: PathRepoView.git runs no textconv / external diff of the operator's config and reads no outside file."""
import os
import subprocess

import pytest

from tools.claimcheck.anchors import PathRepoView

_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@a",
            GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@a")


def _g(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], env=_ENV, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def evil(tmp_path):
    """A checkout whose config names a textconv and an external diff that leave a marker file."""
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


@pytest.mark.parametrize("args", [("show", "HEAD"), ("diff", "HEAD~1", "HEAD"), ("log", "-p"),
                                  ("diff-tree", "-p", "HEAD~1", "HEAD")])
def test_git_runs_no_driver_of_the_operators_config(evil, args):
    repo, marker = evil
    out = PathRepoView(repo).git(*args)
    assert "A=2" in out
    assert not marker.exists()


@pytest.mark.parametrize("args", [
    ("blame", "--contents", "OUTSIDE", "a.py"),
    ("show", "--textconv", "HEAD:a.py"),
    ("log", "-c", "core.pager=touch x", "-1"),
    ("log", "--git-dir=/elsewhere"),
    ("grep", "-Otouch", "A"),
])
def test_git_refuses_options_that_reach_outside(evil, tmp_path, args):
    repo, marker = evil
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    args = tuple(str(outside) if a == "OUTSIDE" else a for a in args)
    with pytest.raises(ValueError, match="refused"):
        PathRepoView(repo).git(*args)
    assert not marker.exists()
