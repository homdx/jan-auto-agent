"""CC-2: Target — a pinned read-only tree at an exact sha, a cached collect model, parse_report_ref."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from tools.claimcheck import target as tgt
from tools.claimcheck.model import RepoView
from tools.claimcheck.target import RefHint, Target, TargetError, parse_report_ref

FIXTURES = Path(__file__).parent / "fixtures" / "claimcheck"

_IDENT = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for key, value in _IDENT.items():
        monkeypatch.setenv(key, value)
    # the test's own `git status` must not rewrite the index it is about to compare
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(repo: Path, files: dict, message: str = "c") -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    return path


class _Repo(type(Path())):
    """A Path that also carries the shas of the two commits the fixture made."""


@pytest.fixture
def repo(tmp_path):
    root = _Repo(make_repo(tmp_path / "op"))
    sha1 = commit(root, {"a.py": "A = 1\n", "docs/n.md": "one\n"}, "first")
    sha2 = commit(root, {"a.py": "A = 2\n", "b.py": "B = 1\n"}, "second")
    root.sha1, root.sha2 = sha1, sha2
    return root


def snapshot(repo: Path) -> dict:
    """What an open must not change. `git status` refreshes the index itself, so it runs
    once before the bytes and the mtime are read: the test must not be the one that moves them."""
    status = git(repo, "status", "--porcelain")
    index = repo / ".git" / "index"
    return {
        "status": status,
        "head": git(repo, "rev-parse", "HEAD"),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "index": index.read_bytes(),
        "mtime": index.stat().st_mtime_ns,
    }


# ── parse_report_ref ──────────────────────────────────────────────────────────

def test_parse_report_ref_table():
    rows = json.loads((FIXTURES / "ref_hints.json").read_text(encoding="utf-8"))
    assert len(rows) >= 20
    for row in rows:
        want = [RefHint(ref, sha, source) for ref, sha, source in row["hints"]]
        assert parse_report_ref(row["text"]) == want, row["text"]


def test_parse_report_ref_never_raises():
    for junk in (None, 5, b"commit 7b4e5f9", ["x"], "\x00" * 10, "@" * 5000, "commit " + "a1" * 5000):
        assert isinstance(parse_report_ref(junk), list)


# ── open ──────────────────────────────────────────────────────────────────────

def test_open_at_sha_gives_that_tree(repo, tmp_path):
    with Target.open(repo, repo.sha1, scratch=tmp_path / "s") as t:
        assert t.sha == repo.sha1
        assert (t.tree / "a.py").read_text() == "A = 1\n"
        assert not (t.tree / "b.py").exists()
    assert (repo / "a.py").read_text() == "A = 2\n"


def test_operators_checkout_is_untouched(repo, tmp_path):
    (repo / "a.py").write_text("A = 99  # dirty\n")
    (repo / "scratch.txt").write_text("untracked\n")
    before = snapshot(repo)
    with Target.open(repo, repo.sha1, scratch=tmp_path / "s") as t:
        t.collect()
        view = t.view()
        view.read("a.py")
        view.git("log", "-1")
    assert snapshot(repo) == before
    assert (repo / "a.py").read_text() == "A = 99  # dirty\n"


def test_branch_ref_is_pinned_to_a_sha(repo, tmp_path):
    git(repo, "branch", "feat", repo.sha1)
    with Target.open(repo, "feat", scratch=tmp_path / "s") as t:
        pinned = t.sha
        commit_on_feat = None
        git(repo, "checkout", "-q", "feat")
        commit_on_feat = commit(repo, {"a.py": "A = 3\n"}, "moves feat")
        git(repo, "checkout", "-q", "main")
        assert pinned == repo.sha1 != commit_on_feat
        assert t.sha == repo.sha1
        assert (t.tree / "a.py").read_text() == "A = 1\n"


def test_annotated_tag_is_peeled_to_the_commit(repo, tmp_path):
    git(repo, "tag", "-a", "v1", "-m", "release", repo.sha1)
    with Target.open(repo, "v1", scratch=tmp_path / "s") as t:
        assert t.sha == repo.sha1


def test_ref_with_a_slash(repo, tmp_path):
    git(repo, "branch", "release/1.0", repo.sha1)
    with Target.open(repo, "release/1.0", scratch=tmp_path / "s") as t:
        assert t.sha == repo.sha1


def test_report_sha_mismatch_is_a_warning(repo, tmp_path):
    with Target.open(repo, "main", scratch=tmp_path / "s", expect_sha=repo.sha2[:7]) as t:
        assert t.warnings == []
    with Target.open(repo, "main", scratch=tmp_path / "s", expect_sha="afa53f1") as t:
        assert t.sha == repo.sha2
        assert t.warnings == [f"the report says `afa53f1`, `main` is `{repo.sha2[:7]}`"]
    # when ref is itself a sha, it wins: no warning
    with Target.open(repo, repo.sha1, scratch=tmp_path / "s", expect_sha="afa53f1") as t:
        assert t.warnings == []


def test_unknown_ref_is_a_one_line_error(repo, tmp_path):
    with pytest.raises(TargetError) as err:
        Target.open(repo, "no-such-branch", scratch=tmp_path / "s")
    message = str(err.value)
    assert "\n" not in message
    assert "rev-parse" in message and "no-such-branch" in message
    assert "fatal" in message or "Needed a single revision" in message


def test_short_sha_is_refused(repo, tmp_path):
    with pytest.raises(TargetError, match="shorter than 7"):
        Target.open(repo, repo.sha1[:6], scratch=tmp_path / "s")


def test_ref_that_looks_like_an_option_is_refused(repo, tmp_path):
    with pytest.raises(TargetError):
        Target.open(repo, "--output=/tmp/x", scratch=tmp_path / "s")


def test_not_a_repository_is_an_error(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(TargetError):
        Target.open(tmp_path / "plain", "HEAD", scratch=tmp_path / "s")
    with pytest.raises(TargetError):
        Target.open(tmp_path / "missing", "HEAD", scratch=tmp_path / "s")


def test_scratch_that_cannot_be_written_is_a_one_line_error(repo, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    with pytest.raises(TargetError) as err:
        Target.open(repo, "main", scratch=blocker / "inside")
    assert "\n" not in str(err.value)


def test_relative_scratch_and_bare_repository(repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with Target.open(repo, "main", scratch="rel/scratch") as t:
        assert t.tree.is_absolute() and t.tree.is_relative_to(tmp_path / "rel" / "scratch")
        assert (t.tree / "b.py").exists()
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(bare)], check=True, capture_output=True)
    with Target.open(bare, "main", scratch=tmp_path / "s") as t:
        assert t.sha == repo.sha2 and (t.tree / "a.py").read_text() == "A = 2\n"


def test_view_git_keeps_crlf_bytes(repo, tmp_path):
    (repo / "crlf.txt").write_bytes(b"x\r\ny\r\n")
    commit(repo, {}, "crlf")
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        assert t.view().git("show", "HEAD:crlf.txt") == "x\r\ny\r\n"


def test_scratch_path_with_spaces_and_unicode(repo, tmp_path):
    with Target.open(repo, "main", scratch=tmp_path / "scratch ключ ✓") as t:
        assert (t.tree / "b.py").read_text() == "B = 1\n"


# ── a remote, a shallow clone, fetch ─────────────────────────────────────────

@pytest.fixture
def remote_pair(tmp_path):
    """`origin` (a repository reached by file://) and a local clone of it, then origin moves on."""
    origin = make_repo(tmp_path / "origin")
    first = commit(origin, {"a.py": "A = 1\n"}, "first")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", f"file://{origin}", str(local)], check=True, capture_output=True)
    second = commit(origin, {"a.py": "A = 2\n"}, "second")
    return origin, local, first, second


def test_missing_object_says_fetch_would_help(remote_pair, tmp_path):
    origin, local, first, second = remote_pair
    with pytest.raises(TargetError) as err:
        Target.open(local, second, scratch=tmp_path / "s")
    message = str(err.value)
    assert "\n" not in message
    assert second in message and "fetch=True may bring it" in message
    # a repository with no remote: fetch would not help, and the message says so
    with pytest.raises(TargetError, match="would not help"):
        Target.open(origin, "f" * 40, scratch=tmp_path / "s")


def test_fetch_brings_new_commits_and_nothing_else(remote_pair, tmp_path):
    origin, local, first, second = remote_pair
    git(origin, "tag", "v2", second)
    before = snapshot(local)
    local_heads = git(local, "for-each-ref", "refs/heads")
    with Target.open(local, second, scratch=tmp_path / "s", fetch=True) as t:
        assert t.sha == second
    with Target.open(local, "origin/main", scratch=tmp_path / "s", fetch=True) as t:
        assert t.sha == second
    with Target.open(local, "v2", scratch=tmp_path / "s", fetch=True) as t:
        assert t.sha == second
    assert snapshot(local) == before
    assert git(local, "for-each-ref", "refs/heads") == local_heads
    assert git(local, "rev-list", "--merges", "--all") == ""
    assert git(local, "rev-parse", "main") == first   # no pull, no merge, no reset


def test_fetch_on_a_repository_without_remote_is_a_no_op(repo, tmp_path):
    with Target.open(repo, "main", scratch=tmp_path / "s", fetch=True) as t:
        assert t.sha == repo.sha2


def test_url_is_cloned_once_and_reused(remote_pair, tmp_path, monkeypatch):
    origin, _, _, second = remote_pair
    calls = []
    real = tgt._run_git

    def counting(cwd, *args):
        calls.append(args)
        return real(cwd, *args)

    monkeypatch.setattr(tgt, "_run_git", counting)
    url = f"file://{origin}"
    with Target.open(url, "origin/main", scratch=tmp_path / "s") as t:
        assert t.sha == second
        assert (t.tree / "a.py").read_text() == "A = 2\n"
    with Target.open(url, "main", scratch=tmp_path / "s") as t:
        assert t.sha == second
    assert sum(1 for a in calls if a and a[0] == "clone") == 1
    clones = list((tmp_path / "s" / "claimcheck" / "clones").iterdir())
    assert len([c for c in clones if (c / ".git").exists()]) == 1


# ── reuse, concurrency, close ────────────────────────────────────────────────

def test_second_open_reuses_the_tree(repo, tmp_path, monkeypatch):
    first = Target.open(repo, "main", scratch=tmp_path / "s")
    calls = []
    real = tgt._run_git
    monkeypatch.setattr(tgt, "_run_git", lambda cwd, *a: (calls.append(a), real(cwd, *a))[1])
    second = Target.open(repo, "main", scratch=tmp_path / "s")
    assert second.tree == first.tree
    assert not any(a[:2] == ("worktree", "add") for a in calls)
    assert second._created is False


def test_changed_tree_is_rebuilt(repo, tmp_path):
    first = Target.open(repo, "main", scratch=tmp_path / "s")
    (first.tree / "a.py").write_text("A = 'someone edited the pinned tree'\n")
    second = Target.open(repo, "main", scratch=tmp_path / "s")
    assert (second.tree / "a.py").read_text() == "A = 2\n"
    second.close()


def test_concurrent_opens_of_one_sha(repo, tmp_path):
    results, errors = [], []
    barrier = threading.Barrier(2)

    def work():
        try:
            barrier.wait()
            results.append(Target.open(repo, "main", scratch=tmp_path / "s"))
        except Exception as err:  # noqa: BLE001
            errors.append(err)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert len(results) == 2 and results[0].tree == results[1].tree
    listed = git(repo, "worktree", "list", "--porcelain")
    assert listed.count("worktree ") == 2   # the operator's checkout and one tree
    assert sum(t._created for t in results) == 1
    results[0].close(remove=True)


def test_close_removes_what_it_made(repo, tmp_path):
    first = Target.open(repo, "main", scratch=tmp_path / "s")
    tree = first.tree
    first.close()
    assert not tree.exists()
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    first.close()   # twice is harmless

    made = Target.open(repo, "main", scratch=tmp_path / "s")
    reused = Target.open(repo, "main", scratch=tmp_path / "s")
    reused.close()
    assert made.tree.exists()                       # a reused tree stays
    reused.close(remove=True)
    assert not made.tree.exists()
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_context_manager_closes(repo, tmp_path):
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        tree = t.tree
        assert tree.exists()
    assert not tree.exists()


def test_no_network_without_a_url_or_fetch(repo, tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the network was touched")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        assert t.sha == repo.sha2


def test_hooks_of_the_operators_repo_do_not_run(repo, tmp_path):
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/hook-ran\n")
    hook.chmod(0o755)
    with Target.open(repo, repo.sha1, scratch=tmp_path / "s"):
        pass
    assert not (tmp_path / "hook-ran").exists()


def test_submodules_are_gitlinks_only(repo, tmp_path):
    sub = make_repo(tmp_path / "sub")
    sub_sha = commit(sub, {"x.py": "X = 1\n"})
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    commit(repo, {}, "adds a submodule")
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        assert not (t.tree / "vendor" / "x.py").exists()
        assert "vendor" in git(t.tree, "ls-tree", "HEAD")
        assert sub_sha in git(t.tree, "ls-tree", "HEAD", "vendor")


# ── view ─────────────────────────────────────────────────────────────────────

def test_view_methods(repo, tmp_path):
    (repo / "epic-tasks").mkdir()
    commit(repo, {"epic-tasks/07-thing.md": "# 07\n"}, "ticket")
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        view = t.view()
        assert isinstance(view, RepoView)
        assert view.exists("a.py") and not view.exists("nope.py") and not view.exists("../op/a.py")
        assert view.read("a.py") == "A = 2\n"
        with pytest.raises(FileNotFoundError):
            view.read("nope.py")
        files = view.files()
        assert files == sorted(files) == ["a.py", "b.py", "docs/n.md", "epic-tasks/07-thing.md"]
        assert not any(f.startswith(".git") for f in files)
        assert view.rev_parse(repo.sha1[:7]) == repo.sha1
        assert view.rev_parse("nonsense") is None
        assert view.ticket_file("07") == "epic-tasks/07-thing.md"
        assert view.ticket_file("99") is None
        assert view.git("log", "-1", "--format=%s").strip() == "ticket"
        assert view.git("show", f"{repo.sha1}:a.py") == "A = 1\n"


def test_view_files_ignore_what_collect_wrote(repo, tmp_path):
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        (t.tree / ".collect").mkdir()
        (t.tree / ".collect" / "artifact.json").write_text("{}")
        assert ".collect/artifact.json" not in t.view().files()


def test_view_refuses_writing_git(repo, tmp_path):
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        view = t.view()
        for args in (("checkout", repo.sha1), ("reset", "--hard", repo.sha1), ("commit", "-m", "x"),
                     ("-c", "core.editor=true", "log"), ("log", "-c", "core.editor=true"),
                     ("log", "--output=/tmp/x"), ("diff", "--output", "/tmp/x"),
                     ("grep", "-Oless", "A"), ("log", "--ext-diff"), (), ("add", "-A"),
                     ("branch", "-D", "main"), ("fetch",), ("worktree", "prune"), ("config", "x", "y")):
            with pytest.raises(TargetError):
                view.git(*args)
        assert git(repo, "rev-parse", "main") == repo.sha2
        assert view.git("log", "-c", "-1")   # `-c` alone is git's own combined-diff flag


def test_view_follows_no_symlink_out_of_the_tree(repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    (repo / "link").symlink_to(outside)
    commit(repo, {}, "a symlink")
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        assert not t.view().exists("link")
        with pytest.raises(FileNotFoundError):
            t.view().read("link")


# ── collect ──────────────────────────────────────────────────────────────────

def test_collect_is_cached_by_sha(repo, tmp_path, monkeypatch):
    calls = []
    real = tgt.collect_cli.action_collect

    def counting(root, **kw):
        calls.append(Path(root))
        return real(root, **kw)

    monkeypatch.setattr(tgt.collect_cli, "action_collect", counting)
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        first = t.collect()
        second = t.collect()
        assert first is second
        assert len(calls) == 1 and calls[0] == t.tree
        assert (t.tree / ".collect").is_dir()
    # a new Target on the same (reused) tree finds the model fresh and does not build
    t1 = Target.open(repo, "main", scratch=tmp_path / "s")
    t2 = Target.open(repo, "main", scratch=tmp_path / "s")
    t1.collect()
    t2.collect()
    assert len(calls) == 2   # the first test block built it once; one rebuild for the fresh tree, none after
    t2.close(remove=True)


def test_collect_failure_is_a_warning(repo, tmp_path, monkeypatch):
    def boom(root, **kw):
        raise RuntimeError("parse error in a.py\nsecond line")

    monkeypatch.setattr(tgt.collect_cli, "action_collect", boom)
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        model = t.collect()
        assert model.status == "absent"
        assert model.module("a.py") is None
        assert t.warnings == ["collect failed: parse error in a.py"]
        t.collect()
        assert len(t.warnings) == 1   # asked twice, said once


def test_collect_timeout_is_a_warning(repo, tmp_path, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(tgt.collect_cli, "action_collect", lambda root, **kw: release.wait(10))
    monkeypatch.setattr(tgt, "COLLECT_TIMEOUT", 0.2)
    with Target.open(repo, "main", scratch=tmp_path / "s") as t:
        assert t.collect().status == "absent"
        assert "did not finish" in t.warnings[0]
    release.set()


def test_collect_builds_a_model_with_modules(tmp_path):
    root = make_repo(tmp_path / "py")
    commit(root, {"pipeline.py": "def run():\n    return 1\n", "util.py": "import pipeline\n"})
    with Target.open(root, "HEAD", scratch=tmp_path / "s") as t:
        model = t.collect()
        assert model.status == "fresh"
        assert model.module("pipeline.py") is not None
