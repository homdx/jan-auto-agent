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

KC-59: the KC-4/KC-23 coverage below pins ``workspace_kind = worktree`` — the
pre-KC-59 path — on purpose, and the last section of this file pins the new
default: a fresh local clone per agent, with its own ``refs/stash`` (so one
agent's ``git stash pop`` cannot take another agent's work), its push URL cut,
KC-23's reuse rules, the round's cleanup, a ``<tmp_root>/<agent>/`` scratch dir,
and the harvest and the ``format-patch`` of a clone matching those of a worktree
at the same commit.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(REPO_ROOT))

from tools.contest.harvest import harvest
from tools.contest.roster import ContestConfig
from tools.contest.workspace import (
    Workspace,
    WorkspaceError,
    agent_tmp_dir,
    agent_tmp_dirs,
    agent_tmp_globs,
    attach_clone,
    prepare_round,
    remove_round,
    reset_clone,
    reset_worktree,
    tmp_root_dirs,
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


def _cfg(tmp_path: Path, **over) -> ContestConfig:
    """Two agents against a fresh rounds_dir; KC-59's clone default unless overridden."""
    from tools.contest.roster import AgentSpec

    rounds = tmp_path / "rounds"
    rounds.mkdir(exist_ok=True)
    kw = dict(
        rounds_dir=str(rounds),
        agents=(
            AgentSpec(name="laguna", provider_id="kenary", model_id="hy3:free"),
            AgentSpec(name="hy3", provider_id="kenary", model_id="hy3:free"),
        ),
    )
    kw.update(over)
    return ContestConfig(**kw)


@pytest.fixture
def config(tmp_path):
    """Config whose rounds_dir is a fresh dir under tmp_path, two agents.

    KC-59: ``workspace_kind`` defaults to ``clone``, so the KC-4/KC-23 coverage
    below names ``worktree`` — the pre-KC-59 path — on purpose; the clone path
    is the tests at the end of this file."""
    return _cfg(tmp_path, workspace_kind="worktree")


@pytest.fixture
def clone_config(tmp_path):
    """The KC-59 default: a clone per agent, scratch dirs under ``tmp_path/tmp``."""
    return _cfg(tmp_path, tmp_roots=(str(tmp_path / "tmp") + "/*",))


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


def test_prepare_round_force_resets_an_edit_the_new_base_also_changes(repo, config):
    """Round 86: ``--fresh`` over a worktree whose uncommitted edit is to
    a file the *new* base changes too. A plain ``checkout -B`` refuses ("would
    be overwritten by checkout") and the whole round stops at intake."""
    old_base = _base_sha(repo)
    new_base = _git(repo, "rev-parse", "HEAD").strip()  # changes readme.txt
    laguna = next(ws for ws in prepare_round(repo, config, 40, old_base)
                  if ws.agent == "laguna")
    (laguna.path / "readme.txt").write_text("the agent's edit\n")

    wss = prepare_round(repo, config, 40, new_base, force=True)
    laguna2 = next(ws for ws in wss if ws.agent == "laguna")

    assert _git(laguna2.path, "rev-parse", "HEAD").strip() == new_base
    assert (laguna2.path / "readme.txt").read_text() == "r1\n"
    assert _git(laguna2.path, "status", "--porcelain").strip() == ""


def test_prepare_round_without_force_still_refuses_that_edit(repo, config):
    old_base = _base_sha(repo)
    new_base = _git(repo, "rev-parse", "HEAD").strip()
    laguna = next(ws for ws in prepare_round(repo, config, 40, old_base)
                  if ws.agent == "laguna")
    (laguna.path / "readme.txt").write_text("the agent's edit\n")

    with pytest.raises(WorkspaceError, match="--fresh"):
        prepare_round(repo, config, 40, new_base)
    assert (laguna.path / "readme.txt").read_text() == "the agent's edit\n"


def test_attach_clone_force_resets_an_edit_the_base_also_changes(repo, tmp_path):
    base = _base_sha(repo)
    clone = tmp_path / "clone-hy3"
    _git(repo, "clone", "-q", str(repo), str(clone))
    (clone / "readme.txt").write_text("uncommitted\n")  # HEAD r1, base r0

    attach_clone(clone, "hy3", base, "contest/40/hy3", force=True)

    assert _git(clone, "rev-parse", "HEAD").strip() == base
    assert (clone / "readme.txt").read_text() == "r0\n"


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


# ─────────────────────────────────────────────────────────────────────────────
# KC-59: a clone per agent, and a scratch dir per agent
# ─────────────────────────────────────────────────────────────────────────────

_BRIDGE_SRC = (
    "class CollectBridge:\n"
    "    def _shrink(self, text):\n"
    "        return text[:10]\n"
)

_TICKET = (
    "# R1 — probe the bridge\n"
    "\n"
    "**File:** `tools/auto/probe.py`\n"
    "\n"
    "**Also touches:** `tests/test_probe.py`\n"
)


def _harvest_repo(tmp_path: Path) -> tuple[Path, str, Path]:
    """A temp repo with the bridge, a probe module, a test and one open ticket."""
    repo = tmp_path / "hrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "kc59@example.invalid")
    _git(repo, "config", "user.name", "KC59")
    (repo / "tools" / "auto").mkdir(parents=True)
    (repo / "tools" / "auto" / "collect_bridge.py").write_text(_BRIDGE_SRC, encoding="utf-8")
    (repo / "tools" / "auto" / "probe.py").write_text("PROBE = 1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text(
        "def test_base():\n    assert True\n", encoding="utf-8")
    (repo / "epic-tasks").mkdir()
    (repo / "epic-tasks" / "01-r1.md").write_text(_TICKET, encoding="utf-8")
    (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").strip(), repo / "epic-tasks" / "01-r1.md"


def _record(ws, ticket: str, commit: str, outcome: str = "FIXED") -> None:
    """Append one PROGRESS.csv row, the way `append_task.py` does."""
    import csv

    ws.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(ws.progress_csv, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["ticket", "finding", "outcome", "commit", "note"])
        if ws.progress_csv.stat().st_size == 0:
            writer.writeheader()
        writer.writerow({"ticket": ticket, "finding": "", "outcome": outcome,
                         "commit": commit, "note": ""})


def _format_patch(path: Path, base: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "format-patch", "--stdout", f"{base}..HEAD"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_prepare_round_defaults_to_a_clone_per_agent(repo, clone_config):
    """KC-59: the default is a fresh local clone, so the repo's worktree list and
    the repo's own refs are untouched by the round."""
    base = _base_sha(repo)
    wss = prepare_round(repo, clone_config, 40, base)

    assert len(wss) == 2
    for ws in wss:
        assert ws.kind == "clone"
        assert (ws.path / ".git").is_dir()          # a real clone, not a worktree
        assert _git(ws.path, "rev-parse", "HEAD").strip() == base
        assert _git(ws.path, "rev-parse", "--abbrev-ref", "HEAD").strip() == ws.branch
        assert ws.branch == f"contest/40/{ws.agent}"
        assert _git(ws.path, "status", "--porcelain").strip() == ""
        assert not list((ws.path / "runs" / ws.agent).iterdir())
    # the repo holds no worktree of the round and no branch of it
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert _git(repo, "branch", "--list", "contest/40/*").strip() == ""


def test_two_clones_have_independent_stash_stacks(repo, clone_config):
    """KC-59, the round 103 failure. A worktree pair shares the repo's single
    ``refs/stash``, so a ``git stash push`` in one and a ``git stash pop`` in the
    other pop the first agent's work off into the second's tree. Two clones each
    have their own stack, so the second clone has nothing to pop."""
    base = _base_sha(repo)
    laguna, hy3 = prepare_round(repo, clone_config, 40, base)

    (laguna.path / "readme.txt").write_text("laguna's wip\n")
    _git(laguna.path, "stash", "push", "-m", "laguna-wip", "--", "readme.txt")
    laguna_stash = _git(laguna.path, "stash", "list")
    assert "laguna-wip" in laguna_stash
    assert _git(laguna.path, "status", "--porcelain").strip() == ""

    pop = subprocess.run(["git", "-C", str(hy3.path), "stash", "pop"],
                         capture_output=True, text=True)
    assert pop.returncode != 0
    assert "No stash entries found" in (pop.stdout + pop.stderr)
    # laguna's entry is still on its own stack, and hy3's tree is untouched
    assert "laguna-wip" in _git(laguna.path, "stash", "list")
    assert _git(hy3.path, "status", "--porcelain").strip() == ""
    assert _git(hy3.path, "rev-parse", "HEAD").strip() == base


def test_clone_push_url_is_disabled_and_nothing_reaches_the_origin(repo, clone_config):
    """KC-59: a push fails in git itself, whatever the policy answers, and the
    operator's repo gains no ref. The fetch URL is untouched, so the operator
    still lands with ``git fetch <clone> contest/<NN>/<agent>``."""
    base = _base_sha(repo)
    laguna, _ = prepare_round(repo, clone_config, 40, base)

    assert _git(laguna.path, "config", "--get",
                "remote.origin.pushurl").strip() == "DISABLED"
    assert _git(laguna.path, "config", "--get", "remote.origin.url").strip() == str(repo.resolve())

    (laguna.path / "thing.py").write_text("42\n")
    _git(laguna.path, "add", "thing.py")
    _git(laguna.path, "commit", "-q", "-m", "KC-59: thing")
    branch_sha = _git(laguna.path, "rev-parse", "HEAD")

    push = subprocess.run(["git", "-C", str(laguna.path), "push", "origin", "HEAD"],
                          capture_output=True, text=True)
    assert push.returncode != 0
    assert "DISABLED" in (push.stdout + push.stderr)
    # the operator's repo has no ref to the branch and no commit but its own
    check = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet",
                            "refs/heads/contest/40/laguna"],
                           capture_output=True, text=True)
    assert check.returncode != 0 and check.stdout.strip() == ""
    assert branch_sha not in _git(repo, "rev-list", "--all")


def test_workspace_kind_worktree_keeps_a_worktree(repo, clone_config):
    """KC-59's escape hatch: ``workspace_kind = worktree`` is the pre-KC-59
    path — a worktree registered with the repo, its branch in the repo."""
    cfg = _cfg(Path(clone_config.rounds_dir).parent, workspace_kind="worktree")
    base = _base_sha(repo)
    wss = prepare_round(repo, cfg, 41, base)

    for ws in wss:
        assert ws.kind == "worktree"
        assert not (ws.path / ".git").is_dir()
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert listed.count("worktree ") == 3
    assert _git(repo, "branch", "--list", "contest/41/*").strip() != ""
    # a worktree keeps no push URL of its own — KC-59's clone is what cuts it
    assert _git(repo, "branch", "-r", "--list").strip() == ""


def test_prepare_round_refuses_a_clone_with_a_commit_above_the_base(repo, clone_config):
    """KC-23's rules apply to a clone: a reset that would drop the commit is
    refused without ``force`` and the operator's ``--fresh`` discards it."""
    base = _base_sha(repo)
    laguna = next(ws for ws in prepare_round(repo, clone_config, 40, base)
                  if ws.agent == "laguna")
    (laguna.path / "thing.py").write_text("42\n")
    _git(laguna.path, "add", "thing.py")
    _git(laguna.path, "commit", "-q", "-m", "KC-23: thing")
    assert _git(laguna.path, "rev-list", "--count", f"{base}..HEAD").strip() == "1"

    with pytest.raises(WorkspaceError) as excinfo:
        prepare_round(repo, clone_config, 40, base)
    message = str(excinfo.value)
    assert str(laguna.path) in message
    assert "1 commit" in message
    assert "--fresh" in message and "--resume" in message
    assert "contest-out/40/state.json" in message
    assert _git(laguna.path, "rev-list", "--count", f"{base}..HEAD").strip() == "1"

    wss = prepare_round(repo, clone_config, 40, base, force=True)
    laguna2 = next(ws for ws in wss if ws.agent == "laguna")
    assert laguna2.kind == "clone"
    assert _git(laguna2.path, "rev-parse", "HEAD").strip() == base
    assert not (laguna2.path / "thing.py").exists()
    assert _git(laguna2.path, "status", "--porcelain").strip() == ""
    assert _git(laguna2.path, "config", "--get",
                "remote.origin.pushurl").strip() == "DISABLED"


def test_prepare_round_resets_a_clone_from_a_previous_base(repo, clone_config):
    """The base moved on after the clone was made: the clone's objects are a copy,
    not a live link, so the reset fetches them and still lands back on the base."""
    base = _base_sha(repo)
    prepare_round(repo, clone_config, 40, base)

    (repo / "readme.txt").write_text("r2\n")
    _git(repo, "add", "readme.txt")
    _git(repo, "commit", "-q", "-m", "r2")
    new_base = _git(repo, "rev-parse", "HEAD").strip()

    wss = prepare_round(repo, clone_config, 40, new_base)
    for ws in wss:
        assert _git(ws.path, "rev-parse", "HEAD").strip() == new_base
        assert (ws.path / "readme.txt").read_text() == "r2\n"
        assert _git(ws.path, "status", "--porcelain").strip() == ""


def test_prepare_round_refuses_a_clone_it_does_not_own(repo, clone_config):
    """A folder at the checkout path that is not ours is refused and left
    untouched, and an empty folder a crashed clone left behind is replaced."""
    base = _base_sha(repo)
    foreign = Path(clone_config.rounds_dir) / "40-laguna"
    foreign.mkdir(parents=True)
    foreign.joinpath("i-was-here.txt").write_text("mine\n")

    with pytest.raises(WorkspaceError, match="not a git clone"):
        prepare_round(repo, clone_config, 40, base)
    assert foreign.joinpath("i-was-here.txt").exists()

    # the empty-folder half, straight on reset_clone: the folder goes, the clone comes back
    shutil.rmtree(foreign)
    foreign.mkdir()
    ws = reset_clone(repo, clone_config.rounds_dir, 40, "laguna", base)
    assert ws.kind == "clone"
    assert _git(ws.path, "rev-parse", "HEAD").strip() == base


def test_remove_round_removes_the_rounds_clones(repo, clone_config):
    """KC-59: ``git worktree remove`` cannot see a clone, so the round's cleanup
    takes the folder — a clone whose origin is not the repo would be left alone."""
    base = _base_sha(repo)
    wss = prepare_round(repo, clone_config, 40, base)
    assert all(ws.path.is_dir() for ws in wss)

    remove_round(repo, clone_config, 40)
    for ws in wss:
        assert not ws.path.exists()
    assert _git(repo, "branch", "--list", "contest/40/*").strip() == ""


def test_prepare_round_makes_a_scratch_dir_per_agent(repo, clone_config):
    """KC-59: the scratch dir is ``<tmp_root>/<agent>/``, created at round start,
    one per agent, and the policy's globs for one agent are its own only."""
    base = _base_sha(repo)
    prepare_round(repo, clone_config, 40, base)

    laguna_dir = agent_tmp_dir(clone_config.tmp_roots, "laguna")
    hy3_dir = agent_tmp_dir(clone_config.tmp_roots, "hy3")
    assert laguna_dir != hy3_dir
    assert laguna_dir.is_dir() and hy3_dir.is_dir()
    assert str(laguna_dir.parent) in clone_config.tmp_roots[0]
    assert agent_tmp_globs(clone_config.tmp_roots, "laguna") == (str(laguna_dir) + "/*",)


def test_scratch_helpers_fail_open(tmp_path):
    """No ``tmp_roots``, a malformed glob or a non-string agent degrades to "no
    scratch dir" and never raises — an absent config key is not a round."""
    assert tmp_root_dirs(()) == ()
    assert tmp_root_dirs(None) == ()
    assert tmp_root_dirs(("not-a-path", 42, None, "", "   ")) == ()
    assert agent_tmp_dir((), "laguna") is None
    assert agent_tmp_globs((), "laguna") == ()
    assert agent_tmp_dirs((), ("laguna",)) == ()
    assert agent_tmp_dir(("not-a-path",), "laguna") is None
    assert agent_tmp_dir(("  ",), "") is None and agent_tmp_dir(("  ",), None) is None
    roots = tmp_root_dirs((str(tmp_path / "tmp") + "/*",))
    assert roots == (tmp_path / "tmp",)
    # only an absolute or ~-absolute root derives a scratch dir: a relative one
    # would make the agent's dir depend on the runner's cwd
    assert tmp_root_dirs(("scratch/*", "./scratch/*", "../scratch/*")) == ()
    assert agent_tmp_globs(("scratch/*",), "laguna") == ()
    assert tmp_root_dirs(("~/scratch/*",)) == (Path.home() / "scratch",)
    assert agent_tmp_dir(("~/scratch/*",), "laguna") == Path.home() / "scratch" / "laguna"


def test_harvest_and_format_patch_of_a_clone_match_a_worktree_at_the_same_commit(tmp_path):
    """KC-59: the harvest and the export read only the workspace path, so a clone
    and a worktree at the same commit get the same verdict, the same reasons and
    the same patch."""
    repo, base, ticket = _harvest_repo(tmp_path)
    rounds = tmp_path / "rounds"
    branch = "contest/40/laguna"

    # the round's own default: the commit is made in a clone
    clone = prepare_round(repo, _cfg(tmp_path, rounds_dir=str(rounds)), 40, base)[0]
    assert clone.kind == "clone"
    (clone.path / "tools" / "auto" / "probe.py").write_text("PROBE = 2\n")
    (clone.path / "tests" / "test_probe.py").write_text("def test_probe():\n    assert True\n")
    _git(clone.path, "add", "-A")
    _git(clone.path, "commit", "-q", "-m", "KC-59: probe")
    sha = _git(clone.path, "rev-parse", "HEAD").strip()
    _record(clone, "01-r1.md", sha)

    # the same commit in a worktree, landed the way an operator fetches it
    _git(repo, "fetch", "-q", str(clone.path), f"refs/heads/{branch}")
    wt_dir = rounds / "wt-laguna"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt_dir), sha)
    wt = Workspace(agent="laguna", path=wt_dir.resolve(), branch=branch,
                   base_sha=base, kind="worktree")
    _record(wt, "01-r1.md", sha)

    h_clone = harvest(clone, ticket)
    h_worktree = harvest(wt, ticket)
    assert h_clone.verdict == "READY" == h_worktree.verdict
    assert [r.code for r in h_clone.reasons] == [r.code for r in h_worktree.reasons]
    assert h_clone.commit == sha
    assert _format_patch(clone.path, base) == _format_patch(wt.path, base)
    assert _format_patch(clone.path, base).startswith("From " + sha)
