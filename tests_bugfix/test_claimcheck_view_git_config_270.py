"""CC-1/CC-2 fix found building CC-4 (ticket 270): both views' git read the operator's global config and had no time limit.

`color.ui=always` put escape codes into `git show` and `diff.noprefix` dropped `a/`/`b/` from the
headers — the text a voter is shown depended on the operator's ~/.gitconfig. Both views now run
`view.git` with GIT_CONFIG_GLOBAL=/dev/null and GIT_CONFIG_NOSYSTEM=1, and kill a call after a
timeout (a hung git raised nothing and hung the claim).
"""
import os
import subprocess

import pytest

from tools.claimcheck import anchors, target
from tools.claimcheck.anchors import PathRepoView
from tools.claimcheck.target import Target, TargetError
from tools.arena.gitref import GitRefError

_ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="a",
            GIT_AUTHOR_EMAIL="a@a", GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@a")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(r), *a], env=_ENV, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (r / "a.py").write_text("A = 1\n")
    run("add", "-A")
    run("commit", "-qm", "one")
    (r / "a.py").write_text("A = 2\n")
    run("commit", "-qam", "two")
    return r


@pytest.fixture
def hostile(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[color]\n\tui = always\n[diff]\n\tnoprefix = true\n\tcontext = 0\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))


def _plain(out):
    assert "\x1b[" not in out
    assert "diff --git a/a.py b/a.py" in out
    assert "+A = 2" in out


def test_path_view_ignores_the_operators_global_config(repo, hostile):
    _plain(PathRepoView(repo).git("show", "HEAD"))


def test_target_view_ignores_the_operators_global_config(repo, hostile, tmp_path):
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        _plain(t.view().git("show", "HEAD"))


def test_path_view_git_has_a_time_limit(repo, monkeypatch):
    monkeypatch.setattr(anchors, "GIT_TIMEOUT", 1e-6)
    with pytest.raises(GitRefError, match="timed out"):
        PathRepoView(repo).git("log", "-p")


def test_target_view_git_has_a_time_limit(repo, monkeypatch, tmp_path):
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        monkeypatch.setattr(target, "VIEW_GIT_TIMEOUT", 1e-6)
        with pytest.raises(TargetError, match="timed out"):
            t.view().git("log", "-p")
