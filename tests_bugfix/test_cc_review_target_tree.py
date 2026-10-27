"""Review of cc-epic-docs: a shared worktree outlives its creator's close, a URL --ref --fetch takes the fetched branch, a live lock is not broken."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.claimcheck import anchors as cc_anchors  # noqa: E402
from tools.claimcheck import target as cc_target  # noqa: E402
from tools.claimcheck.target import Target, TargetError, _Lock  # noqa: E402

ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.py").write_text("def f():\n    return 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "one")
    return r


def test_the_first_close_does_not_remove_a_tree_another_open_uses(repo, tmp_path):
    first = Target.open(str(repo), "HEAD", scratch=tmp_path / "s")
    second = Target.open(str(repo), "HEAD", scratch=tmp_path / "s")
    assert first._created and not second._created
    first.close()
    view = second.view()
    assert view.read("a.py").startswith("def f")
    resolved = cc_anchors.resolve_anchors(cc_anchors.extract_anchors("`f` in a.py returns 1"), view)
    assert all(r.found for r in resolved)
    assert cc_anchors.classify("`f` in a.py returns 1", resolved) != "world"
    second.close()                      # the last one out removes it
    assert not second.tree.exists()
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_an_open_in_another_process_keeps_the_tree(repo, tmp_path):
    code = (f"import sys, time; sys.path.insert(0, {str(ROOT)!r})\n"
            "from tools.claimcheck.target import Target\n"
            f"t = Target.open({str(repo)!r}, 'HEAD', scratch={str(tmp_path / 's')!r})\n"
            "print('open', flush=True); sys.stdin.readline(); t.close()\n")
    child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, env={**os.environ, **ENV})
    try:
        assert child.stdout.readline().strip() == "open"
        mine = Target.open(str(repo), "HEAD", scratch=tmp_path / "s")
        mine.close(remove=True)
        assert mine.tree.exists()       # the child still has it
        child.communicate("\n", timeout=60)
        assert not mine.tree.exists()   # the child closed last and removed it
    finally:
        child.kill()


def test_a_dead_process_entry_does_not_keep_the_tree_forever(repo, tmp_path):
    t = Target.open(str(repo), "HEAD", scratch=tmp_path / "s")
    (t.tree.parent / "users" / "999999999-dead").touch()     # a process that never closed
    t.close()
    assert not t.tree.exists()


def test_a_tree_that_is_gone_is_an_error_not_empty_evidence(repo, tmp_path):
    t = Target.open(str(repo), "HEAD", scratch=tmp_path / "s")
    try:
        git(repo, "worktree", "remove", "--force", str(t.tree))
        with pytest.raises(TargetError, match="is gone"):
            t.view()
    finally:
        t.close()


def test_url_ref_with_fetch_resolves_the_fetched_branch(repo, tmp_path):
    url = "file://" + str(repo)
    with Target.open(url, "main", scratch=tmp_path / "s") as t:
        old = t.sha
    (repo / "a.py").write_text("def f():\n    return 3\n")
    git(repo, "commit", "-qam", "three")
    new = git(repo, "rev-parse", "HEAD")
    with Target.open(url, "main", scratch=tmp_path / "s", fetch=True) as t:
        assert t.sha == new != old
    with Target.open(url, old, scratch=tmp_path / "s") as t:        # a sha is still a sha
        assert t.sha == old


def test_a_local_branch_still_wins_on_a_local_repo(repo, tmp_path):
    """Only a URL target asks origin/<ref> first: the operator's own unpushed main is the main."""
    clone = tmp_path / "c"
    git(tmp_path, "clone", "-q", str(repo), str(clone))
    (clone / "a.py").write_text("x = 1\n")
    git(clone, "commit", "-qam", "local only")
    with Target.open(str(clone), "main", scratch=tmp_path / "s") as t:
        assert t.sha == git(clone, "rev-parse", "main") != git(clone, "rev-parse", "origin/main")


def test_a_lock_with_a_live_owner_is_not_broken_however_old(tmp_path, monkeypatch):
    path = tmp_path / ".lock"
    held = _Lock(path).__enter__()
    old = time.time() - 10 * _Lock.STALE
    os.utime(path, (old, old))
    with pytest.raises(TargetError, match="held for more than"):
        _Lock(path, wait=0.3).__enter__()
    assert (path / "owner").read_text() == held.token
    held.__exit__(None, None, None)
    assert not path.exists()


def test_a_lock_whose_owner_died_is_broken_and_not_removed_by_the_dead_owner(tmp_path):
    path = tmp_path / ".lock"
    path.mkdir()
    (path / "owner").write_text("999999999-gone")
    stale = _Lock(path)
    stale.token = "999999999-gone"
    taker = _Lock(path, wait=5).__enter__()
    stale.__exit__(None, None, None)          # the old owner's late exit leaves the new lock alone
    assert (path / "owner").read_text() == taker.token
    taker.__exit__(None, None, None)
    assert not path.exists()


def test_a_lock_with_no_owner_on_record_falls_back_to_its_age(tmp_path, monkeypatch):
    monkeypatch.setattr(cc_target._Lock, "STALE", 0.1)
    path = tmp_path / ".lock"
    path.mkdir()
    old = time.time() - 1
    os.utime(path, (old, old))
    with _Lock(path, wait=5):
        assert (path / "owner").exists()
