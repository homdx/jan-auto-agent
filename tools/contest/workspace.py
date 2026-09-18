"""tools/contest/workspace.py — KC-4: one worktree (or clone) per agent at the base.

The probe (``scripts/kilo_hello.py``, commit 67e834d; ``docs/kilo-contest/PROBE.md``)
and the runbook (``docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`` §Stage 1) build
the per-agent checkouts by hand and warn, three times, about what must hold:

  * every agent gets its **own** checkout — agents must not share a working tree,
    and a worktree must never be carried across rounds;
  * the boundary the policy (KC-3) treats as "inside" the session is the worktree
    path itself, so the worktree is what the contest provisions;
  * ``epic-tasks/`` is invisible inside a worktree when it is untracked at the
    base commit — the runbook's own trap — so the base must carry it committed.

This module is the operator-facing replacement for that shell loop: it resolves the
base, refuses to reuse a previous round's worktree, empties ``runs/<agent>/`` so a
stale ``PROGRESS.csv`` never makes ``next_task.py`` hand out nothing, and never
touches the repo's own checkout. The runbook's stage-5 cleanup is :func:`remove_round`.

Standard library only (``subprocess``, ``pathlib``, ``shutil``); it shells out to
``git`` exactly the way the runbook does.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tools.contest.roster import ContestConfig

__all__ = [
    "Workspace",
    "WorkspaceError",
    "attach_clone",
    "prepare_round",
    "remove_round",
    "reset_worktree",
]

#: The runbook's reason, repeated verbatim when ``epic-tasks/`` is not clean at
#: the base — an untracked ``epic-tasks/`` is invisible inside a worktree.
_EPIC_TASKS_DIRTY_REASON = (
    "epic-tasks/ has uncommitted or untracked changes at the base commit — an "
    "untracked epic-tasks/ is invisible inside a worktree, so the runbook forbids "
    "it; commit epic-tasks/ at the base before running a round"
)

#: The roster's ``rounds_dir`` is resolved against the repo when relative, the
#: same way the runbook's ``git worktree add ../round-$a`` sits next to the repo.
_KIND_WORKTREE: Literal["worktree", "clone"] = "worktree"
_KIND_CLONE: Literal["worktree", "clone"] = "clone"


class WorkspaceError(RuntimeError):
    """A worktree or clone could not be prepared or removed.

    Raised for an unresolvable base, a dirty ``epic-tasks/`` at the base, a folder
    at a worktree path that is not ours to delete, a clone that lacks the base sha,
    a dirty clone without ``--force``, and any ``git`` failure in between. Always
    says what was attempted.
    """


@dataclass(frozen=True)
class Workspace:
    """One agent's prepared checkout — the unit the runner (KC-6) drives.

    ``path`` is resolved, ``branch`` is ``contest/<NN>/<agent>``, ``base_sha`` is
    the frozen base, ``kind`` is ``"worktree"`` or ``clone"``, and
    ``progress_csv`` is where ``next_task.py`` records that agent's queue.
    """

    agent: str
    path: Path
    branch: str
    base_sha: str
    kind: Literal["worktree", "clone"]

    @property
    def progress_csv(self) -> Path:
        """The agent's queue file: ``<path>/runs/<agent>/PROGRESS.csv``."""
        return self.path / "runs" / self.agent / "PROGRESS.csv"


# ─────────────────────────────────────────────────────────────────────────────
# git plumbing
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_rounds_dir(repo: Path, rounds_dir: str) -> Path:
    """*rounds_dir* as an absolute path, relative to *repo* when not absolute."""
    base = Path(rounds_dir)
    if not base.is_absolute():
        base = (repo / rounds_dir).resolve()
    return base


def _git(cwd: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run ``git`` in *cwd*; raise :class:`WorkspaceError` on a non-zero exit."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} in {cwd} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def _rev_parse(repo: Path, ref: str) -> str | None:
    """The sha *ref* resolves to, or ``None`` when it does not exist."""
    proc = _git(repo, ["rev-parse", "--verify", "--quiet", ref], check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _worktree_paths(repo: Path) -> set[Path]:
    """The resolved worktree paths of *repo* (``git worktree list --porcelain``)."""
    proc = _git(repo, ["worktree", "list", "--porcelain"])
    paths: set[Path] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line[len("worktree "):].strip()).resolve())
    return paths


def _worktree_list(repo: Path) -> list[tuple[Path, str | None]]:
    """``(path, branch-without-refs/heads/)`` for every worktree of *repo*."""
    proc = _git(repo, ["worktree", "list", "--porcelain"])
    out: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            if path is not None:
                out.append((path, branch))
            path = Path(line[len("worktree "):].strip()).resolve()
            branch = None
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    if path is not None:
        out.append((path, branch))
    return out


def _branch_name(round_no: int, agent: str) -> str:
    """``contest/<NN>/<agent>`` — the per-agent branch for a round."""
    return f"contest/{round_no:02d}/{agent}"


def _worktree_path(rounds_dir: Path, round_no: int, agent: str) -> Path:
    """``<rounds_dir>/<NN>-<agent>`` — the per-agent worktree path."""
    return rounds_dir / f"{round_no:02d}-{agent}"


def _empty_runs_dir(worktree: Path, agent: str) -> None:
    """Empty ``<worktree>/runs/<agent>/`` but keep the directory (git-ignored)."""
    runs = worktree / "runs" / agent
    if runs.exists():
        for child in runs.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        runs.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# base validation
# ─────────────────────────────────────────────────────────────────────────────


def _check_base_and_epic_tasks(repo: Path, base_ref: str) -> str:
    """Resolve *base_ref* and refuse a dirty ``epic-tasks/``; return the base sha."""
    base_sha = _rev_parse(repo, base_ref)
    if base_sha is None:
        raise WorkspaceError(
            f"base ref {base_ref!r} does not resolve to a commit in {repo}"
        )
    proc = _git(
        repo,
        ["status", "--porcelain", "--untracked-files=all", "--", "epic-tasks"],
        check=False,
    )
    if proc.stdout.strip():
        raise WorkspaceError(_EPIC_TASKS_DIRTY_REASON)
    return base_sha


# ─────────────────────────────────────────────────────────────────────────────
# worktree prep
# ─────────────────────────────────────────────────────────────────────────────


def reset_worktree(
    repo: Path, rounds_dir, round_no: int, agent: str, base_sha: str
) -> Workspace:
    """A fresh, idempotent worktree for *agent* at *base_sha*.

    Path ``<rounds_dir>/<NN>-<agent>``, branch ``contest/<NN>/<agent>``. Creates it
    when absent (reusing a stale branch with ``-B``); when present and a worktree of
    *repo*, resets the branch to the base and ``clean -fdx -e runs/``; when present
    but not ours, raises rather than delete it. ``runs/<agent>/`` is always emptied.
    """
    root = _resolve_rounds_dir(repo, str(rounds_dir))
    path = _worktree_path(root, round_no, agent)
    branch = _branch_name(round_no, agent)
    owned = _worktree_paths(repo)

    if not path.exists():
        if _rev_parse(repo, branch) is not None:
            # A stale branch without its worktree: recreate the worktree, then move
            # the branch onto the base.
            _git(repo, ["worktree", "add", str(path), base_sha])
            _git(path, ["checkout", "-B", branch, base_sha])
        else:
            _git(repo, ["worktree", "add", str(path), "-b", branch, base_sha])
    else:
        if path.resolve() not in owned:
            raise WorkspaceError(
                f"{path} exists but is not a worktree of {repo} — refusing to "
                f"delete a folder this module did not create"
            )
        _git(path, ["checkout", "-B", branch, base_sha])

    _git(path, ["clean", "-fdx", "-e", "runs/"])
    _empty_runs_dir(path, agent)

    return Workspace(
        agent=agent,
        path=path.resolve(),
        branch=branch,
        base_sha=base_sha,
        kind=_KIND_WORKTREE,
    )


def attach_clone(clone_path, agent: str, base_sha: str, branch: str, *, force: bool = False) -> Workspace:
    """Use an existing *clone_path* as *agent*'s checkout at *base_sha*.

    The clone's remote must contain *base_sha* (``git fetch --all`` first). Dirty
    clones are reset only with ``force=True``; otherwise a :class:`WorkspaceError`
    lists the dirty files. ``runs/<agent>/`` is emptied.
    """
    clone = Path(clone_path).resolve()
    if not (clone / ".git").exists():
        raise WorkspaceError(f"{clone} is not a git repository")

    _git(clone, ["fetch", "--all"])
    if _rev_parse(clone, f"{base_sha}^{{commit}}") is None:
        raise WorkspaceError(
            f"clone {clone} does not contain base sha {base_sha} in any remote"
        )

    dirty = _git(clone, ["status", "--porcelain"], check=False).stdout.strip()
    if dirty and not force:
        files = "\n  ".join(dirty.splitlines())
        raise WorkspaceError(
            f"clone {clone} is dirty (pass --force-clone to reset it):\n  {files}"
        )

    _git(clone, ["checkout", "-B", branch, base_sha])
    _git(clone, ["clean", "-fdx", "-e", "runs/"])
    _empty_runs_dir(clone, agent)

    return Workspace(
        agent=agent,
        path=clone,
        branch=branch,
        base_sha=base_sha,
        kind=_KIND_CLONE,
    )


def prepare_round(
    repo,
    config: ContestConfig,
    round_no: int,
    base_ref: str,
    *,
    clones: dict[str, Path] | None = None,
    force_clone: bool = False,
) -> list[Workspace]:
    """Prepare one checkout per roster agent at *base_ref*; return the workspaces.

    Resolves and validates the base (unresolvable or a dirty ``epic-tasks/`` raise),
    then for each agent in roster order attaches its ``clones`` entry or builds a
    worktree. The repo's own checkout is never touched (no ``checkout``/``reset`` in
    *repo* itself).
    """
    repo_path = Path(repo).resolve()
    base_sha = _check_base_and_epic_tasks(repo_path, base_ref)
    root = _resolve_rounds_dir(repo_path, config.rounds_dir)

    clone_map = {name: Path(p) for name, p in (clones or {}).items()}
    workspaces: list[Workspace] = []
    for agent in config.agents:
        if agent.name in clone_map:
            workspaces.append(
                attach_clone(
                    clone_map[agent.name],
                    agent.name,
                    base_sha,
                    _branch_name(round_no, agent.name),
                    force=force_clone,
                )
            )
        else:
            workspaces.append(
                reset_worktree(repo_path, root, round_no, agent.name, base_sha)
            )
    return workspaces


def remove_round(repo, config: ContestConfig, round_no: int) -> None:
    """Remove every worktree (and branch) of *round_no*; leave clones alone.

    The runbook's stage-5 cleanup, in one call: ``git worktree remove --force`` then
    ``git branch -D`` for each ``contest/<NN>/<agent>`` worktree of the round.
    """
    repo_path = Path(repo).resolve()
    targets = {_branch_name(round_no, agent.name) for agent in config.agents}
    for path, branch in _worktree_list(repo_path):
        if branch in targets:
            _git(repo_path, ["worktree", "remove", "--force", str(path)])
            _git(repo_path, ["branch", "-D", branch], check=False)


# ─────────────────────────────────────────────────────────────────────────────
# operator CLI
# ─────────────────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    """``python3 -m tools.contest.workspace {prepare|remove} …`` for the operator."""
    import argparse

    from tools.contest.roster import load_roster

    parser = argparse.ArgumentParser(prog="tools.contest.workspace")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare", help="prepare one checkout per agent")
    p_prep.add_argument("--repo", default=".")
    p_prep.add_argument("--config", default="contest.ini")
    p_prep.add_argument("--round", type=int, required=True)
    p_prep.add_argument("base_ref", nargs="?", default="HEAD")
    p_prep.add_argument("--clone", action="append", default=[],
                        metavar="name=path", help="use an existing clone for <name>")
    p_prep.add_argument("--force-clone", action="store_true")

    p_rem = sub.add_parser("remove", help="remove a round's worktrees and branches")
    p_rem.add_argument("--repo", default=".")
    p_rem.add_argument("--config", default="contest.ini")
    p_rem.add_argument("--round", type=int, required=True)

    args = parser.parse_args(argv)

    cfg = load_roster(args.config)
    repo_path = Path(args.repo).resolve()

    if args.cmd == "remove":
        remove_round(repo_path, cfg, args.round)
        return 0

    clone_map: dict[str, Path] = {}
    for spec in args.clone:
        name, sep, cpath = spec.partition("=")
        if not sep:
            raise SystemExit(f"--clone must be name=path, got {spec!r}")
        clone_map[name] = Path(cpath)
    root = _resolve_rounds_dir(repo_path, cfg.rounds_dir)
    before = {
        agent.name: _worktree_path(root, args.round, agent.name).exists()
        for agent in cfg.agents
        if agent.name not in clone_map
    }

    workspaces = prepare_round(
        repo_path, cfg, args.round, args.base_ref,
        clones=clone_map, force_clone=args.force_clone,
    )
    for ws in workspaces:
        if ws.kind == _KIND_CLONE:
            fresh = "clone"
        else:
            fresh = "fresh" if not before.get(ws.agent, False) else "reset"
        print(f"{ws.agent}  {ws.path}  {ws.branch}  @ {args.base_ref}  ({ws.kind}, {fresh})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
