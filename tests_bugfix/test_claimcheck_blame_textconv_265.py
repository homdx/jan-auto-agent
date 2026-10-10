"""Found building CC-3: `git blame` runs the operator's textconv driver by default; both views pass --no-textconv."""
import os
import subprocess

import pytest

from tools.claimcheck import target as T
from tools.claimcheck.anchors import PathRepoView

_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@a",
            GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@a")


def _g(d, *a):
    return subprocess.run(["git", "-C", str(d), *a], env=_ENV, capture_output=True, text=True,
                          check=True).stdout.strip()


@pytest.fixture
def evil(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "main")
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    (repo / "a.py").write_text("A=1\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-qm", "one")
    marker = tmp_path / "RAN"
    _g(repo, "config", "diff.evil.textconv", f"touch {marker}; cat")
    return repo, marker


def test_path_view_blame_runs_no_textconv(evil):
    repo, marker = evil
    assert "A=1" in PathRepoView(repo).git("blame", "a.py")
    assert not marker.exists()


def test_target_view_blame_runs_no_textconv(evil, tmp_path):
    repo, marker = evil
    with T.Target.open(repo, "main", scratch=tmp_path / "s") as t:
        assert "A=1" in t.view().git("blame", "a.py")
    assert not marker.exists()
