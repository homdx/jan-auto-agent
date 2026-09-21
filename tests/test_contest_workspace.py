"""tests/test_contest_workspace.py — KC-4: one worktree/clone per agent, idempotent.

The probe (``scripts/kilo_hello.py``, 67e834d; ``docs/kilo-contest/PROBE.md``) and the
runbook (``docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`` §Stage 1) build per-agent
checkouts by hand and warn three times: agents must not share a checkout, a worktree
must never be carried across rounds, and an untracked ``epic-tasks/`` is invisible
inside a worktree. Before ``tools/contest/workspace.py`` existed there was no code
enforcing any of it — a rerun after a crash meant removing everything by hand, and a
previous round's worktree could be silently reused (so ``next_task.py`` would hand out
nothing because its stale ``PROGRESS.csv`` was still there).

This test pins ``prepare_round``, ``reset_worktree``, ``attach_clone`` and
``remove_round`` against temp repos only (never this checkout), per KC-4's acceptance
list. It also asserts the repo's own checkout is never modified by any call. KC-23
adds the refusal: a worktree holding commits above the base or edits outside
``runs/`` is not reset unless ``force=True``, and the message names ``--fresh`` and
``--resume``.

Knowledge label: KC-4 regression test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(REPO_ROOT))

from tools.contest.roster import ContestConfig
from tools.contest.workspace import (
    WorkspaceError,
    attach_clone,
    prepare_round,
    remove_round,
    reset_worktree,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _make_repo(tmp_path: Path, *, with_epic: bool = True) -> Path:
    """A temp repo with two commits and a committed ``epic-tasks/`` (when asked)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    # first commit
    (repo / "readme.txt").write_text("r0\n")
    _git(repo, "add", "readme.txt")
    _git(repo, "commit", "-q", "-m", "r0")
    if with_epic:
        (repo / "epic-tasks").mkdir()
        (repo / "epic-tasks" / "a.md").write_text("task\n")
        _git(repo, "add", "epic-tasks")
        _git(repo, "commit", "-q", "-m", "epic")
    # second commit, so HEAD != base
    (repo / "readme.txt").write_text("r1\n")
    _git(repo, "add", "readme.txt")
    _git(repo, "commit", "-q", "-m", "r1")
    return repo


@pytest.fixture
def repo(tmp_path):
    """A temp repo with two commits and a committed epic-tasks/."""
    return _make_repo(tmp_path)


@pytest.fixture
def config(tmp_path):
    """Config whose rounds_dir is a fresh dir under tmp_path, two agents."""
    rounds = tmp_path / "rounds"
    rounds.mkdir()

    from tools.contest.roster import AgentSpec

    return ContestConfig(
        rounds_dir=str(rounds),
        agents=(
            AgentSpec(name="laguna", provider_id="kenary", model_id="hy3:free"),
            AgentSpec(name="hy3", provider_id="kenary", model_id="hy3:free"),
        ),
    )


def _base_sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD~1").strip()


def test_prepare_round_creates_worktrees_at_base_with_empty_runs(repo, config):
    base = _base_sha(repo)
    wss = prepare_round(repo, config, 40, base)

    assert len(wss) == 2
    for ws in wss:
        assert ws.kind == "worktree"
        assert ws.path.is_dir()
        assert _git(ws.path, "rev-parse", "HEAD").strip() == base
        assert _git(ws.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == ws.branch
        assert ws.branch == f"contest/40/{ws.agent}"
        # runs/<agent>/ exists and is empty
        assert (ws.path / "runs" / ws.agent).is_dir()
        assert not list((ws.path / "runs" / ws.agent).iterdir())
        # clean checkout, no stray status noise
        assert _git(ws.path, "status", "--porcelain").strip() == ""


def test_prepare_round_is_idempotent_after_progress_and_commits(repo, config):
    base = _base_sha(repo)
    prepare_round(repo, config, 40, base)
    laguna = next(ws for ws in prepare_round(repo, config, 40, base)
                  if ws.agent == "laguna")

    # simulate a crashed attempt: a file, a commit, and a stale PROGRESS.csv
    (laguna.path / "stray.txt").write_text("leftover\n")
    _git(laguna.path, "add", "stray.txt")
    _git(laguna.path, "commit", "-q", "-m", "stray")
    laguna.path.joinpath("runs", "laguna", "PROGRESS.csv").write_text("done\n")

    # rerun — KC-23: a rerun over commits needs force=True (the operator's
    # --fresh) on purpose; without it the refusal is the whole point
    wss2 = prepare_round(repo, config, 40, base, force=True)
    laguna2 = next(ws for ws in wss2 if ws.agent == "laguna")

    assert _git(laguna2.path, "rev-parse", "HEAD").strip() == base
    assert not (laguna2.path / "stray.txt").exists()
    assert _git(laguna2.path, "status", "--porcelain").strip() == ""
    # the stale PROGRESS.csv is gone so next_task.py hands out again
    assert not laguna2.progress_csv.exists()
    assert (laguna2.path / "runs" / "laguna").is_dir()
    assert not list((laguna2.path / "runs" / "laguna").iterdir())


def test_prepare_round_refuses_a_worktree_with_a_commit_above_the_base(repo, config):
    """KC-23: a rerun must not wipe a branch that holds a commit. The message
    names the worktree, the count and both ways out — ``--fresh`` to discard
    the work, ``--resume`` to keep it — and leaves the worktree untouched."""
    base = _base_sha(repo)
    laguna = next(ws for ws in prepare_round(repo, config, 40, base)
                  if ws.agent == "laguna")

    (laguna.path / "thing.py").write_text("42\n")
    _git(laguna.path, "add", "thing.py")
    _git(laguna.path, "commit", "-q", "-m", "KC-23: thing")
    assert _git(laguna.path, "rev-list", "--count", f"{base}..HEAD").strip() == "1"

    with pytest.raises(WorkspaceError) as excinfo:
        prepare_round(repo, config, 40, base)

    message = str(excinfo.value)
    assert str(laguna.path) in message
    assert "1 commit" in message
    assert "--fresh" in message
    assert "--resume" in message
    assert "contest-out/40/state.json" in message
    # the refusal must not have touched the worktree
    assert _git(laguna.path, "rev-parse", "HEAD").strip() != base
    assert (laguna.path / "thing.py").exists()


def test_prepare_round_refuses_a_worktree_with_uncommitted_edits(repo, config):
    """No commit, only an edit: refused too, and the edit is named."""
    base = _base_sha(repo)
    laguna = next(ws for ws in prepare_round(repo, config, 40, base)
                  if ws.agent == "laguna")

    (laguna.path / "thing.py").write_text("42\n")

    with pytest.raises(WorkspaceError) as excinfo:
        prepare_round(repo, config, 40, base)

    message = str(excinfo.value)
    assert str(laguna.path) in message
    assert "thing.py" in message
    assert "--fresh" in message
    assert "--resume" in message
    assert _git(laguna.path, "rev-parse", "HEAD").strip() == base


def test_prepare_round_force_resets_a_worktree_that_carries_work(repo, config):
    """``force=True`` is the operator's ``--fresh``: the work is discarded, the
    branch lands back on the base, the scratch goes with it."""
    base = _base_sha(repo)
    laguna = next(ws for ws in prepare_round(repo, config, 40, base)
                  if ws.agent == "laguna")

    (laguna.path / "thing.py").write_text("42\n")
    _git(laguna.path, "add", "thing.py")
    _git(laguna.path, "commit", "-q", "-m", "KC-23: thing")
    laguna.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    laguna.progress_csv.write_text("task1,done\n")

    wss = prepare_round(repo, config, 40, base, force=True)
    laguna2 = next(ws for ws in wss if ws.agent == "laguna")

    assert _git(laguna2.path, "rev-parse", "HEAD").strip() == base
    assert not (laguna2.path / "thing.py").exists()
    assert _git(laguna2.path, "status", "--porcelain").strip() == ""
    assert not laguna2.progress_csv.exists()


def test_prepare_round_resets_a_clean_worktree_at_the_base(repo, config):
    """Nothing on the branch, only the round's own ``runs/`` scratch: the
    idempotent rerun resets as before — the scratch is not work to refuse over,
    so ``next_task.py`` still gets the queue emptied."""
    base = _base_sha(repo)
    prepare_round(repo, config, 40, base)
    laguna = next(ws for ws in prepare_round(repo, config, 40, base)
                  if ws.agent == "laguna")
    laguna.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    laguna.progress_csv.write_text("task1,done\n")

    wss = prepare_round(repo, config, 40, base)
    laguna2 = next(ws for ws in wss if ws.agent == "laguna")

    assert _git(laguna2.path, "rev-parse", "HEAD").strip() == base
    assert not laguna2.progress_csv.exists()
    assert _git(laguna2.path, "status", "--porcelain").strip() == ""


def test_prepare_round_resets_a_worktree_from_a_previous_base(repo, config):
    """A base that moved on: the worktree's branch sits behind the new base with
    no commits above it, so it is not carrying work and resets silently."""
    base = _base_sha(repo)
    prepare_round(repo, config, 40, base)

    (repo / "readme.txt").write_text("r2\n")
    _git(repo, "add", "readme.txt")
    _git(repo, "commit", "-q", "-m", "r2")
    new_base = _git(repo, "rev-parse", "HEAD").strip()
    assert new_base != base

    wss = prepare_round(repo, config, 40, new_base)
    for ws in wss:
        assert _git(ws.path, "rev-parse", "HEAD").strip() == new_base
        assert _git(ws.path, "status", "--porcelain").strip() == ""


def test_reset_worktree_recreates_stale_branch_without_its_worktree(repo, config):
    """KC-4 contest-bench round 2: a worktree removed externally (e.g. by hand,
    or ``remove_round`` on a round the operator re-numbers) leaves its branch
    behind. ``git worktree add <path> -b <branch> ...`` then fails outright —
    ``-b`` refuses a branch that already exists — so the next ``reset_worktree``
    for that same round/agent must reuse the stale branch, not error.

    Two of nine KC-4 contest entries broke exactly here: one always used ``-b``
    with no stale-branch check (crashed); the other tried to move the branch
    onto the base with ``git branch -M <branch> <base_sha>`` before creating the
    worktree — ``-M`` is a *rename*, so it tried (and failed) to rename the
    branch to the literal string ``<base_sha>``, raising from the ``except``
    around it instead of resetting anything.
    """
    base = _base_sha(repo)
    ws1 = reset_worktree(repo, config.rounds_dir, 41, "laguna", base)
    branch = ws1.branch
    _git(repo, "worktree", "remove", "--force", str(ws1.path))
    assert _git(repo, "branch", "--list", branch).strip() != ""  # branch survives

    ws2 = reset_worktree(repo, config.rounds_dir, 41, "laguna", base)
    assert ws2.branch == branch
    assert _git(ws2.path, "rev-parse", "HEAD").strip() == base
    assert _git(ws2.path, "status", "--porcelain").strip() == ""


def test_prepare_round_rejects_worktree_of_another_repo(repo, config, tmp_path):
    """The path check must be "is this a worktree of *this* repo", not just "is
    this a worktree of anything" — a path that is a real, valid worktree but of
    an unrelated repository must still be refused, and left untouched."""
    other_repo = tmp_path / "unrelated"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    _git(other_repo, "config", "user.email", "test@example.com")
    _git(other_repo, "config", "user.name", "Test")
    (other_repo / "f.txt").write_text("f\n")
    _git(other_repo, "add", "f.txt")
    _git(other_repo, "commit", "-q", "-m", "unrelated init")

    foreign = Path(config.rounds_dir) / "42-laguna"
    _git(other_repo, "worktree", "add", "-q", "-b", "unrelated-branch", str(foreign))

    base = _base_sha(repo)
    with pytest.raises(WorkspaceError):
        prepare_round(repo, config, 42, base)
    # still a valid worktree of the OTHER repo — never touched
    assert _git(foreign, "rev-parse", "HEAD").strip() != ""


def test_prepare_round_rejects_non_worktree_folder(repo, config, tmp_path):
    base = _base_sha(repo)
    # drop a foreign folder exactly where laguna's worktree would be created
    foreign = Path(config.rounds_dir) / "40-laguna"
    foreign.mkdir(parents=True)
    foreign.joinpath("i-was-here.txt").write_text("mine\n")

    with pytest.raises(WorkspaceError):
        prepare_round(repo, config, 40, base)
    # the foreign folder is untouched
    assert foreign.joinpath("i-was-here.txt").exists()


def test_prepare_round_rejects_untracked_epic_tasks(repo, config, tmp_path):
    base = _base_sha(repo)
    # an untracked file under epic-tasks/ at the base tree is the runbook's trap
    (repo / "epic-tasks" / "untracked.md").write_text("oops\n")
    with pytest.raises(WorkspaceError, match="epic-tasks"):
        prepare_round(repo, config, 40, base)
    (repo / "epic-tasks" / "untracked.md").unlink()


def test_prepare_round_rejects_unresolvable_base(repo, config):
    with pytest.raises(WorkspaceError, match="does not resolve"):
        prepare_round(repo, config, 40, "no-such-ref-xyz")


def test_attach_clone_attaches_and_resets_dirty(repo, config, tmp_path):
    base = _base_sha(repo)
    clone = tmp_path / "clone-hy3"
    _git(repo, "clone", str(repo), str(clone))
    _git(clone, "remote", "set-url", "origin", str(repo))

    # dirty the clone first (a change that would have to be reset to attach)
    (clone / "dirty.txt").write_text("uncommitted\n")

    # without force: dirty clone must raise listing the file
    with pytest.raises(WorkspaceError, match="dirty"):
        attach_clone(clone, "hy3", base, "contest/40/hy3")

    # with force: it attaches at base and the dirty file is gone
    ws = attach_clone(clone, "hy3", base, "contest/40/hy3", force=True)
    assert ws.kind == "clone"
    assert _git(clone, "rev-parse", "HEAD").strip() == base
    assert not (clone / "dirty.txt").exists()
    assert not list((clone / "runs" / "hy3").iterdir())


def test_attach_clone_rejects_missing_base_sha(repo, config, tmp_path):
    clone = tmp_path / "clone-hy3"
    _git(repo, "clone", str(repo), str(clone))
    _git(clone, "remote", "set-url", "origin", str(clone))  # no base anywhere
    with pytest.raises(WorkspaceError, match="does not contain base sha"):
        attach_clone(clone, "hy3", "0" * 40, "contest/40/hy3")


def test_remove_round_cleans_worktrees_and_branches(repo, config):
    base = _base_sha(repo)
    prepare_round(repo, config, 40, base)
    remove_round(repo, config, 40)

    # only the main checkout remains
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert listed.count("worktree ") == 1
    branches = _git(repo, "branch", "--list", "contest/40/*").strip()
    assert branches == ""


def test_repo_checkout_is_never_modified(repo, config):
    """Every call leaves the operator's own checkout HEAD and status intact."""
    base = _base_sha(repo)
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    status_before = _git(repo, "status", "--porcelain").strip()

    prepare_round(repo, config, 40, base)
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
    assert _git(repo, "status", "--porcelain").strip() == status_before

    remove_round(repo, config, 40)
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
    assert _git(repo, "status", "--porcelain").strip() == status_before


def test_reset_worktree_prunes_a_registration_whose_folder_was_removed(repo, config):
    """Round 64 on the operator's machine: the eight ``rounds/64-*`` folders were
    deleted by hand between two attempts, so ``git worktree list`` still carried
    them (marked ``prunable``) and every slot died in intake with

        fatal: '.../rounds/64-agnes-2-5-flash' is a missing but already
        registered worktree; use 'add -f' to override, or 'prune' or 'remove'

    ``git worktree remove`` deregisters, a plain ``rm -rf`` does not — so the
    stale-branch path above is not reached and ``add`` fails outright. The reset
    must prune the missing registration and rebuild the worktree.
    """
    base = _base_sha(repo)
    ws1 = reset_worktree(repo, config.rounds_dir, 64, "agnes-2-5-flash", base)
    shutil.rmtree(ws1.path)  # the folder only — git is not told
    assert "prunable" in _git(repo, "worktree", "list")

    ws2 = reset_worktree(repo, config.rounds_dir, 64, "agnes-2-5-flash", base)
    assert ws2.path == ws1.path
    assert ws2.branch == ws1.branch
    assert _git(ws2.path, "rev-parse", "HEAD").strip() == base
    assert _git(ws2.path, "status", "--porcelain").strip() == ""
    assert "prunable" not in _git(repo, "worktree", "list")
