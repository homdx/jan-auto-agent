"""FL-2 acceptance — the judge's own suite, written from the ticket, not from any entry.

Every scenario is one line of `epic-tasks/85-fl-2-*.md` §Acceptance read
literally, through the callers the ticket names — `workspace._git`,
`gates.git`, the runner's dirty-tree reader, the manifest's git helper,
`GitManager._run` — and never through an entry's own shared module, whose name
and signature differ in every entry.

The ticket names two helpers that do not exist at the base: the runner's reader
is `_dirty_tree` (ticket: `_dirty_paths`) and the manifest helper is `_run_git`
(ticket: `_git`). Either name is accepted.

**Counted, not timed** (POSTMORTEM-FL-1 §7 G, §8): no timer thread races the
ladder and no assertion reads a stopwatch. `subprocess.run` is wrapped for the
duration of one test: every git argv is counted, and a "neighbour" releases the
held lock *right after* the first attempt that hit it — so "waited once" is
exactly two attempts, "gave up after the ladder" is exactly eight, and "raised
on the first attempt" is exactly one, on any box under any load. The wrapper is
the only black-box seam every entry shares; the suite runs with `-n0`, one
entry at a time, so the process-wide patch touches nothing else.

`git status` exits 0 on a held `.git/index.lock` (verified on git 2.34: it
refreshes in memory and skips the write), so the index-writing probes use
`checkout -B` and `add -u`, and the runner's reader is checked for *reporting
the work* under a held lock, not for waiting.

Run: `contest-bench/fl2/score_acceptance.sh ../cb-fl2 [entry …]` — it copies
this file into `<worktree>/tests/` so the entry's own `tools/` is imported.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(os.environ.get("FL2_REPO", Path(__file__).resolve().parents[1])).resolve()
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.auto.git_manager import GitError, GitManager  # noqa: E402
from tools.collect import manifest  # noqa: E402
from tools.contest import gates, runner, workspace  # noqa: E402
from tools.contest.workspace import Workspace, WorkspaceError  # noqa: E402

LOCK_TEXT = re.compile(r"index\.lock", re.IGNORECASE)
LOCK_SIGNATURE = re.compile(r"Unable to create '.*\.lock': File exists", re.IGNORECASE)
LADDER = 8          # the ticket's bound: 8 attempts
_REAL_RUN = subprocess.run


def _dirty_reader():
    fn = getattr(runner, "_dirty_tree", None) or getattr(runner, "_dirty_paths", None)
    assert fn is not None, "runner has neither _dirty_tree nor _dirty_paths"
    return fn


def _manifest_git():
    fn = getattr(manifest, "_git", None) or getattr(manifest, "_run_git", None)
    assert fn is not None, "manifest has neither _git nor _run_git"
    return fn


def _sh(cwd, *args):
    return _REAL_RUN(["git", *args], cwd=cwd, check=True,
                     capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _sh(r, "init", "-q")
    _sh(r, "config", "user.email", "t@t")
    _sh(r, "config", "user.name", "t")
    (r / "a.txt").write_text("1\n", encoding="utf-8")
    _sh(r, "add", "a.txt")
    _sh(r, "commit", "-q", "-m", "one")
    return r


def _ws(path: Path, sha: str | None = None) -> Workspace:
    sha = sha or _sh(path, "rev-parse", "HEAD")
    return Workspace(agent="a", path=path, branch="b", base_sha=sha, kind="worktree")


class _Neighbour:
    """Counts every git attempt; optionally lets go of the lock after the first
    attempt that found it held."""

    def __init__(self, lock, release_after_first_hit):
        self.lock = lock
        self.release = release_after_first_hit
        self.attempts = []
        self.hits = 0

    def __call__(self, cmd, *args, **kwargs):
        argv = [str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
        proc = _REAL_RUN(cmd, *args, **kwargs)
        if argv and os.path.basename(argv[0]) == "git":
            self.attempts.append(argv)
            err = proc.stderr if isinstance(proc.stderr, str) else (
                (proc.stderr or b"").decode("utf-8", "replace"))
            if proc.returncode != 0 and LOCK_SIGNATURE.search(err or ""):
                self.hits += 1
                if self.release and self.lock is not None and self.lock.exists():
                    self.lock.unlink()
        return proc


@pytest.fixture
def neighbour(monkeypatch):
    def install(lock=None, *, held=False, release=False):
        if held and lock is not None:
            lock.write_text("", encoding="utf-8")
        n = _Neighbour(lock, release)
        monkeypatch.setattr(subprocess, "run", n)
        return n
    return install


def _read_error(fn, *a):
    """True when *fn* reported a read error: raised, or returned a non-str sentinel."""
    try:
        out = fn(*a)
    except Exception as exc:  # noqa: BLE001 — any raise is "could not read"
        return True, str(exc)
    return (not isinstance(out, str)), repr(out)


# ── A1: one module owns the pattern ─────────────────────────────────────────

def test_a1_the_lock_pattern_lives_in_exactly_one_file():
    out = _REAL_RUN(["grep", "-rlE", "File exists", "tools/", "--include=*.py"],
                    cwd=REPO, capture_output=True, text=True).stdout.split()
    assert len(out) == 1, out


def test_a1b_no_second_ladder_in_git_manager():
    src = (REPO / "tools/auto/git_manager.py").read_text(encoding="utf-8")
    body = src.split("class GitManager", 1)[1].split("\ndef ", 1)[0]
    assert not re.search(r"Unable to create", body), "GitManager still owns the regex"
    assert not re.search(r"for attempt in range\(self\._LOCK_RETRIES\)", body), \
        "GitManager still runs its own ladder"


# ── A2: GitManager unchanged ─────────────────────────────────────────────────

def test_a2_git_manager_waits_out_a_held_lock(repo, neighbour):
    gm = GitManager(str(repo))
    n = neighbour(repo / ".git" / "index.lock", held=True, release=True)
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    gm._run(["git", "add", "-u"], "git add -u failed")
    assert (n.hits, len(n.attempts)) == (1, 2), n.attempts
    assert gm.has_staged_changes() is True


def test_a2_git_manager_stale_lock_raises_the_original_text(repo, neighbour):
    gm = GitManager(str(repo))
    n = neighbour(repo / ".git" / "index.lock", held=True)
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    with pytest.raises(GitError) as exc:
        gm._run(["git", "add", "-u"], "git add -u failed")
    assert "git add -u failed" in str(exc.value) and LOCK_TEXT.search(str(exc.value))
    assert len(n.attempts) == LADDER, len(n.attempts)


def test_a2_git_manager_real_error_first_attempt(repo, neighbour):
    gm = GitManager(str(repo))
    n = neighbour()
    with pytest.raises(GitError):
        gm._run(["git", "checkout", "no-such-branch-here"], "git checkout failed")
    assert len(n.attempts) == 1, n.attempts


# ── A3: a lock the neighbour releases is waited out by every caller ─────────

def test_a3_workspace_checkout_waits(repo, neighbour):
    n = neighbour(repo / ".git" / "index.lock", held=True, release=True)
    workspace._git(repo, ["checkout", "-q", "-B", "fl2-x"])
    assert (n.hits, len(n.attempts)) == (1, 2), n.attempts
    assert _sh(repo, "rev-parse", "--abbrev-ref", "HEAD") == "fl2-x"


def test_a3_gates_git_waits(repo, neighbour):
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    n = neighbour(repo / ".git" / "index.lock", held=True, release=True)
    gates.git(repo, "add", "-u", check=True)
    assert (n.hits, len(n.attempts)) == (1, 2), n.attempts
    assert "a.txt" in _sh(repo, "diff", "--cached", "--name-only")


def test_a3_manifest_git_waits(repo, neighbour):
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    n = neighbour(repo / ".git" / "index.lock", held=True, release=True)
    out = _manifest_git()(repo, "add", "-u")
    assert out is not None
    assert (n.hits, len(n.attempts)) == (1, 2), n.attempts
    assert "a.txt" in _sh(repo, "diff", "--cached", "--name-only")


def test_a3_dirty_reader_reports_the_work_under_a_held_lock(repo, neighbour):
    (repo / "new.py").write_text("x = 1\n", encoding="utf-8")
    neighbour(repo / ".git" / "index.lock", held=True)
    out = _dirty_reader()(_ws(repo))
    assert isinstance(out, str) and "new.py" in out, out


# ── A3': a lock held for the whole ladder raises the original git text ─────

def test_a3s_workspace_stale_lock_raises_git_text(repo, neighbour):
    n = neighbour(repo / ".git" / "index.lock", held=True)
    with pytest.raises(WorkspaceError) as exc:
        workspace._git(repo, ["checkout", "-q", "-B", "fl2-x"])
    assert LOCK_TEXT.search(str(exc.value)), str(exc.value)
    assert len(n.attempts) == LADDER, len(n.attempts)


def test_a3s_gates_git_stale_lock_raises_git_text(repo, neighbour):
    (repo / "a.txt").write_text("2\n", encoding="utf-8")
    n = neighbour(repo / ".git" / "index.lock", held=True)
    with pytest.raises(RuntimeError) as exc:
        gates.git(repo, "add", "-u", check=True)
    assert LOCK_TEXT.search(str(exc.value)), str(exc.value)
    assert len(n.attempts) == LADDER, len(n.attempts)


# ── A4: a non-zero status is not a clean tree ───────────────────────────────

def test_a4_dirty_reader_on_a_non_repository_is_not_clean(tmp_path, neighbour):
    plain = tmp_path / "plain"
    plain.mkdir()
    n = neighbour()
    err, text = _read_error(_dirty_reader(), _ws(plain, "0" * 40))
    assert err, f"a status that exited 128 was reported as a tree: {text}"
    assert len(n.attempts) == 1, n.attempts


def test_a4_dirty_reader_clean_tree_is_empty_string(repo):
    assert _dirty_reader()(_ws(repo)) == ""


# ── A5: an ordinary git failure is raised on the first attempt ──────────────

def test_a5_workspace_real_error_first_attempt(repo, neighbour):
    n = neighbour()
    with pytest.raises(WorkspaceError):
        workspace._git(repo, ["checkout", "no-such-branch-here"])
    assert len(n.attempts) == 1, n.attempts


def test_a5_gates_real_error_first_attempt(repo, neighbour):
    n = neighbour()
    with pytest.raises(RuntimeError):
        gates.git(repo, "checkout", "no-such-branch-here", check=True)
    assert len(n.attempts) == 1, n.attempts


# ── A6: no caller the ticket names still calls subprocess for git itself ────

@pytest.mark.parametrize("path,fn", [
    ("tools/contest/workspace.py", "_git"),
    ("tools/contest/gates.py", "git"),
    ("tools/collect/manifest.py", "_git|_run_git"),
    ("tools/contest/runner.py", "_dirty_tree|_dirty_paths"),
])
def test_a6_named_callers_are_routed(path, fn):
    src = (REPO / path).read_text(encoding="utf-8")
    m = re.search(rf"^def ({fn})\(.*?(?=^def |^class |\Z)", src, re.S | re.M)
    assert m, f"{fn} not found in {path}"
    assert "subprocess.run(" not in m.group(0), f"{path}:{m.group(1)} still calls subprocess.run"
