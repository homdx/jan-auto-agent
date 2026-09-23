"""tests/test_git_run_index_lock.py — FL-2: one held-index retry, used by every git call.

git holds `.git/index.lock` for the whole of any command that writes the index
(`add`, `commit`, `checkout -B`, `clean -fdx`, `worktree add`, and `status` when
it refreshes), and a second git that finds it there does not wait: it exits 128
with `Unable to create '<path>/.git/index.lock': File exists`.

FL-1 (round 84) made `GitManager._run` wait that out — 8 attempts, 0.25 s apart,
only on that one signature — and it fixed the caller the stress run happened to
hit. Every other git call in the tree was written before it and took the first
128 as final:

  * `workspace._git` — a round of N agents runs `worktree add` / `checkout -B` /
    `clean -fdx` / `status` against one repository, and a collision cost an agent
    its workspace before the round even started;
  * `runner._dirty_tree` — `out = r.stdout if r.returncode == 0 else ""`, so a
    failed `git status` read as a clean tree. That word is what the KC-31/KC-41
    harvest is built on: an unreadable tree scored clean, and the work went to
    zero entries;
  * `gates.git` — `check=False` by default, so a collision was an empty diff, an
    empty file list, a gate that passed because it saw nothing.

The ladder now lives in `tools/git_run.py` once. These tests pin the four ways a
reimplementation can quietly break it: it waits a lock that a neighbour releases,
it stops after about two seconds with the same error when nobody will, it
repeats *only* that signature (a bad ref never pays the ladder), and a failed
status is a read error rather than an empty tree.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.contest.runner as runner_mod
from tools import git_run
from tools.collect import manifest
from tools.contest import gates, workspace
from tools.contest.roster import AgentSpec, ContestConfig
from tools.contest.runner import AgentRun, AgentState, RoundState, TreeReadError
from tools.contest.workspace import Workspace, WorkspaceError

AGENT = "agent-a"
BRANCH = "contest/fl2/agent-a"



# ─────────────────────────────────────────────────────────────────────────────
# the harness
# ─────────────────────────────────────────────────────────────────────────────


def _git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _make_repo(path: Path) -> Path:
    """A one-commit repo with a tracked `a.txt`, ready to collide over its index."""
    repo = path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def repo(tmp_path) -> Path:
    return _make_repo(tmp_path)


@pytest.fixture(scope="module")
def lock_text() -> str:
    """git's own held-index message, captured from one real collision.

    Kept from a real run rather than typed out: if git ever reworded the
    message, `LOCK_CONTENTION_RE` would stop matching and the retry would go
    silently quiet — this test is what turns that into a failure.
    """
    with tempfile.TemporaryDirectory() as d:
        return _lock_text(_make_repo(Path(d)))


def _index_lock(repo: Path) -> Path:
    return repo / ".git" / "index.lock"


def _lock_text(repo: Path) -> str:
    """One real collision, and git's message for it. Leaves the index free."""
    lock = _index_lock(repo)
    lock.write_text("", encoding="utf-8")
    proc = subprocess.run(["git", "add", "-u"], cwd=repo, capture_output=True, text=True)
    lock.unlink()
    assert proc.returncode == 128
    return proc.stderr.strip()


@pytest.fixture(autouse=True)
def backoffs(monkeypatch) -> list:
    """Every wait the ladder asks for, recorded instead of slept.

    Counted, not timed (FL-1, round 84): the claims here are "it waited
    once", "it gave up after the whole ladder", "it never waited" — and a
    stopwatch around a subprocess is a bet on the box, not on any of those.
    """
    waits: list = []
    monkeypatch.setattr(git_run, "_backoff", waits.append)
    return waits


def _release_in_backoff(monkeypatch, lock: Path, waits: list) -> None:
    """Another git has the index and lets go of it during the first backoff —
    the ordering "attempt fails, lock clears, attempt succeeds", made exact."""
    lock.write_text("", encoding="utf-8")

    def release(seconds):
        waits.append(seconds)
        if lock.exists():
            lock.unlink()

    monkeypatch.setattr(git_run, "_backoff", release)


def _always_fail(text: str, attempts: list) -> callable:
    """A `subprocess.run` that fails every attempt the way a held index does.

    Used where real git cannot reproduce the collision: this git's `status`
    refreshes the index in memory and never writes it, so it never takes the
    lock at all. The stub keeps the ladder's own code path — 8 attempts, the
    real backoff — and proves every caller routes through it.
    """

    def fake(cmd, *args, **kwargs):
        attempts.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr=text + "\n")

    return fake


def _workspace(repo: Path, base_sha: str = "0" * 40) -> Workspace:
    """A workspace over *repo*; `_dirty_tree` never looks at `base_sha`."""
    return Workspace(agent=AGENT, path=repo, branch="main",
                     base_sha=base_sha, kind="worktree")


class _Verdict:
    """A REWORK verdict, so `_plan` takes the non-READY branch."""

    verdict = "REWORK"
    commit = None


# ─────────────────────────────────────────────────────────────────────────────
# the ladder itself
# ─────────────────────────────────────────────────────────────────────────────


def test_the_ladder_is_the_one_the_ticket_names():
    """8 attempts 0.25 s apart: a stale lock costs two seconds, and only two."""
    assert (git_run.LOCK_RETRIES, git_run.LOCK_BACKOFF_S) == (8, 0.25)
    assert git_run.LOCK_RETRIES * git_run.LOCK_BACKOFF_S == 2.0


def test_the_signature_is_gits_own_message_and_nothing_else(lock_text):
    assert "index.lock" in lock_text and "File exists" in lock_text
    assert git_run.LOCK_CONTENTION_RE.search(lock_text)
    for ordinary in (
        "fatal: bad revision 'no-such-branch-here'",
        "fatal: not a git repository (or any of the parent directories)",
        "error: the index file is too old",
        "fatal: unable to write new index file",
        "warning: LF will be replaced by CRLF",
    ):
        assert not git_run.LOCK_CONTENTION_RE.search(ordinary)


def test_run_git_waits_out_a_lock_that_gets_released(repo, monkeypatch, backoffs):
    """The caller waits while a neighbour has the index, then goes through."""
    _release_in_backoff(monkeypatch, _index_lock(repo), backoffs)
    (repo / "a.txt").write_text("2\n", encoding="utf-8")

    proc = git_run.run_git(["git", "add", "-u"], cwd=repo)

    assert proc.returncode == 0
    assert backoffs == [git_run.LOCK_BACKOFF_S], backoffs


def test_run_git_waits_through_the_callers_own_sleep(repo, monkeypatch, backoffs):
    """*sleep* is how `GitManager` keeps its `_backoff` seam: when given, the
    module's default is never used."""
    lock = _index_lock(repo)
    lock.write_text("", encoding="utf-8")
    own: list = []
    (repo / "a.txt").write_text("2\n", encoding="utf-8")

    proc = git_run.run_git(["git", "add", "-u"], cwd=repo,
                           sleep=lambda s: (own.append(s), lock.unlink()))

    assert proc.returncode == 0
    assert own == [git_run.LOCK_BACKOFF_S] and backoffs == []


def test_run_git_does_not_retry_an_ordinary_git_failure(repo, monkeypatch, backoffs):
    """A bad ref comes back on the first attempt — nobody pays the ladder for it."""
    attempts = []
    real = git_run.subprocess.run

    def counting(cmd, *args, **kwargs):
        attempts.append(list(cmd))
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(git_run.subprocess, "run", counting)

    proc = git_run.run_git(["git", "checkout", "no-such-branch-here"], cwd=repo)

    assert proc.returncode != 0
    assert len(attempts) == 1, "an ordinary failure paid the whole ladder"
    assert backoffs == []


def test_retries_1_is_one_attempt_and_no_waiting(repo, monkeypatch, lock_text, backoffs):
    """`GitManager._run_once` stays the single attempt."""
    attempts = []
    monkeypatch.setattr(git_run.subprocess, "run", _always_fail(lock_text, attempts))

    proc = git_run.run_git(["git", "add", "-u"], cwd=repo, retries=1)
    assert proc.returncode == 128
    assert len(attempts) == 1
    assert backoffs == []


# ─────────────────────────────────────────────────────────────────────────────
# every caller waits the transient out
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("caller", ["workspace", "gates", "manifest"])
def test_a_held_lock_is_waited_out_by_every_caller(repo, monkeypatch, backoffs, caller):
    """Another git has the index and releases it; every index-writing caller goes
    through after exactly one wait.

    Without the retry these die on the first attempt — `WorkspaceError` out of
    `workspace._git`, `RuntimeError` out of `gates.git`, `None` out of
    `manifest._run_git`.
    """
    ws = _workspace(repo)
    _release_in_backoff(monkeypatch, _index_lock(repo), backoffs)
    (repo / "a.txt").write_text("2\n", encoding="utf-8")

    if caller == "workspace":
        assert workspace._git(ws.path, ["checkout", "-B", BRANCH, "HEAD"]).returncode == 0
    elif caller == "gates":
        assert gates.git(ws.path, "add", "a.txt", check=True) == ""
    else:
        assert manifest._run_git(ws.path, "add", "a.txt") == ""

    assert backoffs == [git_run.LOCK_BACKOFF_S], backoffs


def test_the_runner_reads_a_tree_whose_index_is_held(repo, backoffs):
    """`git status` refreshes the index in memory and exits 0 when the lock is
    taken (git 2.34 and later), so the runner's read never needs the ladder —
    but it must still report the work, not an empty tree."""
    ws = _workspace(repo)
    _index_lock(repo).write_text("", encoding="utf-8")
    (repo / "a.txt").write_text("2\n", encoding="utf-8")

    assert runner_mod._dirty_tree(ws).splitlines() == [" M a.txt"]
    assert backoffs == []


def test_a_stale_lock_still_raises_with_the_original_git_text(repo, backoffs):
    """The ladder is bounded: nobody releases the index, so git's own error wins —
    the same one, which already names a stale `.git/index.lock` as the likely cause."""
    ws = _workspace(repo)
    _index_lock(repo).write_text("", encoding="utf-8")

    with pytest.raises(WorkspaceError) as exc:
        workspace._git(ws.path, ["checkout", "-B", BRANCH, "HEAD"])

    assert "index.lock" in str(exc.value) and "File exists" in str(exc.value)
    # the whole ladder, not a hang and not the first attempt: it tried and gave up
    assert backoffs == [git_run.LOCK_BACKOFF_S] * (git_run.LOCK_RETRIES - 1)

    backoffs.clear()
    with pytest.raises(RuntimeError, match="index.lock"):
        gates.git(ws.path, "add", "a.txt", check=True)
    assert len(backoffs) == git_run.LOCK_RETRIES - 1


# ─────────────────────────────────────────────────────────────────────────────
# a failed read is not an empty tree
# ─────────────────────────────────────────────────────────────────────────────


def test_a_clean_tree_is_still_an_empty_string(repo):
    assert runner_mod._dirty_tree(_workspace(repo)) == ""


def test_a_status_that_cannot_be_read_is_not_a_clean_tree(repo, monkeypatch, lock_text):
    """`git status` that exits 128 raises: the caller can tell "clean" from "unreadable"."""
    attempts = []
    monkeypatch.setattr(git_run.subprocess, "run", _always_fail(lock_text, attempts))

    with pytest.raises(TreeReadError, match="index.lock"):
        runner_mod._dirty_tree(_workspace(repo))

    assert len(attempts) == git_run.LOCK_RETRIES, len(attempts)


def test_a_path_git_cannot_read_at_all_raises(tmp_path):
    """No lock involved — a path that is not a repository is a read error too."""
    ws = Workspace(agent=AGENT, path=tmp_path / "gone", branch="main",
                   base_sha="0" * 40, kind="worktree")
    with pytest.raises(TreeReadError):
        runner_mod._dirty_tree(ws)


def test_the_runner_keeps_going_when_a_tree_cannot_be_read(tmp_path, monkeypatch, caplog):
    """An unreadable tree is a warning, not a crash of the round's plan and not
    `dirty_on_resume`: the resume prompt stays exactly what it was."""
    def unreadable(ws):
        raise TreeReadError(
            "git status in /gone exited 128: "
            "fatal: Unable to create '/gone/.git/index.lock': File exists"
        )

    monkeypatch.setattr(runner_mod, "_dirty_tree", unreadable)
    monkeypatch.setattr(runner_mod, "_harvest",
                        lambda ws, ticket_path, run_tests=False: _Verdict())

    repo = _make_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    ws = _workspace(repo, base_sha=base)
    spec = AgentSpec(name=AGENT, provider_id="kenary", model_id="hy3:free")
    cfg = ContestConfig(rounds_dir=str(tmp_path / "rounds"), agents=(spec,))
    prior = RoundState(round_no=45, ticket="x.md", base_sha=base, started_at=1.0,
                       agents=[AgentRun(agent=spec, workspace=ws,
                                        state=AgentState.WAITING, session_id="ses_gone")])

    with caplog.at_level(logging.WARNING, logger="tools.contest.runner"):
        runs = runner_mod._plan(cfg, [ws], tmp_path / "x.md", prior)

    assert runs == prior.agents
    assert getattr(runs[0], "dirty_on_resume", "") == ""
    assert any("unreadable" in record.message for record in caplog.records)


def test_gates_and_manifest_keep_their_own_failure_after_the_ladder(repo, monkeypatch, lock_text):
    """Each caller still maps the exhausted ladder to its own answer, having tried."""
    ws = _workspace(repo)
    attempts: list = []
    monkeypatch.setattr(git_run.subprocess, "run", _always_fail(lock_text, attempts))

    with pytest.raises(RuntimeError, match="index.lock"):
        gates.git(ws.path, "add", "a.txt", check=True)
    assert len(attempts) == git_run.LOCK_RETRIES

    attempts.clear()
    assert gates.git(ws.path, "add", "a.txt") == ""   # check=False is its contract
    assert len(attempts) == git_run.LOCK_RETRIES

    attempts.clear()
    assert manifest._run_git(ws.path, "status", "--porcelain") is None
    assert len(attempts) == git_run.LOCK_RETRIES


def test_a_git_call_that_never_completes_is_a_read_error(repo, monkeypatch):
    """A timeout is not a contention: it surfaces, it is not retried as one."""
    def hang(cmd, *args, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(git_run.subprocess, "run", hang)
    with pytest.raises(TreeReadError, match="did not run"):
        runner_mod._dirty_tree(_workspace(repo))
