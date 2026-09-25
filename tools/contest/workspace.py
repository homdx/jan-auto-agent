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
touches the repo's own checkout. Since KC-23 it also refuses a reset that would drop
the work a worktree carries — commits above the base, or edits outside
``runs/`` — unless ``--fresh`` says to discard it; the message names ``--resume``
for the other way out. The runbook's stage-5 cleanup is :func:`remove_round`.

KC-59 makes the checkout a fresh local clone by default
(``[contest] workspace_kind = clone``): a worktree is a second working tree of
one repository, and every worktree shares that repository's single ``refs/stash``,
so two agents that both ran "stash my change, run the tests, pop" popped each
other's work (round 103: ``hy3``'s tree was, byte for byte, another agent's file,
and ``hy3``'s own work survived only as an unreachable stash commit). A clone made
by ``git clone --local`` costs about the same — the objects are hard links — and
keeps its own ``refs/stash``, index, ``HEAD``, branches and config, so a ``git
stash``, a ``git checkout`` and a ``git branch -D`` stay inside the agent. The
clone's push URL is cut at the same time, so ``git push origin HEAD`` fails in git
itself rather than in the LLM gate's prompt. The worktree path is unchanged and
selectable with ``workspace_kind = worktree``.

The round's scratch dir is per agent too: ``tmp_roots`` names the shared roots, and
each agent gets ``<tmp_root>/<agent>/``, created at round start by
:func:`ensure_agent_tmp_dirs`. Nothing in the runner names a path — the dir is
derived here from the configured root and the agent's name.

Standard library only (``subprocess``, ``pathlib``, ``shutil``), plus this
repo's own ``tools.git_run`` for the git calls; it shells out to ``git`` exactly
the way the runbook does.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tools.contest.roster import ContestConfig
from tools.git_run import run_git

__all__ = [
    "Workspace",
    "WorkspaceError",
    "agent_tmp_dir",
    "agent_tmp_dirs",
    "agent_tmp_globs",
    "attach_clone",
    "ensure_agent_tmp_dirs",
    "prepare_round",
    "remove_round",
    "reset_clone",
    "reset_worktree",
    "tmp_root_dirs",
]

_log = logging.getLogger(__name__)

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

#: How many dirty paths a refusal names before it says there are more — a
#: worktree can hold a whole round of edits, and the operator only needs enough
#: to see that the reset is not safe.
_DIRTY_NAMED = 5


class WorkspaceError(RuntimeError):
    """A worktree or clone could not be prepared or removed.

    Raised for an unresolvable base, a dirty ``epic-tasks/`` at the base, a folder
    at a worktree path that is not ours to delete, a clone that lacks the base sha,
    a dirty clone without ``--force``, a worktree that holds commits or edits
    without ``--fresh`` (KC-23), and any ``git`` failure in between. Always says
    what was attempted.
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
    """Run ``git`` in *cwd*; raise :class:`WorkspaceError` on a non-zero exit.

    Through ``tools.git_run.run_git``: a ``worktree add`` / ``checkout -B`` /
    ``clean -fdx`` that finds the index held by another git waits
    for it instead of taking the first 128 as final (FL-2) — in a round of N
    agents against one repository a collision here used to cost an agent its
    workspace before the round even started.
    """
    proc = run_git(["git", "-C", str(cwd), *args])
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


def _workspace_path(rounds_dir: Path, round_no: int, agent: str) -> Path:
    """``<rounds_dir>/<NN>-<agent>`` — the per-agent checkout path, clone or worktree."""
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


def _commits_above(worktree: Path, base_sha: str) -> int:
    """The commits on the worktree's branch above *base_sha*.

    ``git rev-list --count <base>..HEAD`` is 0 when the branch is at the base or
    behind it — a worktree left by a previous base, after the base moved on — so a
    clean tree on an old base is not carrying work. On a base with no history in
    common it would count the branch's whole length instead, a non-zero number,
    which is the safe way to be wrong here: refusing loses nothing.
    """
    proc = _git(worktree, ["rev-list", "--count", f"{base_sha}..HEAD"], check=False)
    if proc.returncode != 0:
        return 0
    text = proc.stdout.strip()
    return int(text) if text.isdigit() else 0


def _is_runs_scratch(line: str) -> bool:
    """Whether a ``--porcelain`` line is the round's own ``runs/`` scratch.

    The status occupies ``line[:2]``, the path starts at ``line[3:]``; a rename
    names its destination after the arrow, and that destination is what matters.
    """
    path = line[3:].strip()
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1]
    return path == "runs" or path.startswith("runs/")


def _dirty_outside_runs(worktree: Path) -> list[str]:
    """The worktree's dirty ``--porcelain`` lines, minus the ``runs/`` scratch.

    ``runs/<agent>/PROGRESS.csv`` is the round's queue file — KC-4 empties it and
    the ``clean`` below excludes it — so a worktree holding nothing but that is
    clean: the idempotent rerun must not refuse over a file the round writes.
    """
    proc = _git(worktree, ["status", "--porcelain", "--untracked-files=all"], check=False)
    return [line for line in proc.stdout.splitlines() if not _is_runs_scratch(line)]


def _refuse_to_reset(
    path: Path, branch: str, round_no: int, commits: int, dirty: list[str]
) -> WorkspaceError:
    """The :class:`WorkspaceError` for a worktree a reset would wipe out.

    Names the worktree, the commits above the base, the first few dirty files, and
    both ways out — ``--fresh`` to discard the work, ``--resume`` to keep it and
    continue the round. The work is the operator's, not ours to throw away
    silently; ``contest-out/<NN>/state.json`` is where ``--resume`` reads it from.
    """
    bits: list[str] = []
    if commits:
        plural = "" if commits == 1 else "s"
        bits.append(f"{commits} commit{plural} on {branch} above the base")
    if dirty:
        plural = "" if len(dirty) == 1 else "s"
        named = "\n".join("  " + line for line in dirty[:_DIRTY_NAMED])
        hidden = len(dirty) - _DIRTY_NAMED
        if hidden > 0:
            named += f"\n  ... and {hidden} more"
        bits.append(f"{len(dirty)} uncommitted change{plural}:\n{named}")
    return WorkspaceError(
        f"{path} holds work a reset would discard — " + "; ".join(bits)
        + "\npass --fresh to discard it, or --resume to continue the round from "
        + f"contest-out/{round_no:02d}/state.json"
    )


def reset_worktree(
    repo: Path, rounds_dir, round_no: int, agent: str, base_sha: str, *, force: bool = False
) -> Workspace:
    """A fresh, idempotent worktree for *agent* at *base_sha*.

    Path ``<rounds_dir>/<NN>-<agent>``, branch ``contest/<NN>/<agent>``. Creates it
    when absent (pruning a registration whose folder was removed by hand, and
    reusing a stale branch with ``-B``); when present and a worktree of
    *repo*, refuses a reset that would drop work — commits above *base_sha* or
    uncommitted edits outside ``runs/`` — unless *force* (KC-23, the operator's
    ``--fresh``); when present but not ours, raises rather than delete it.
    ``runs/<agent>/`` is always emptied. A clean worktree at the base, and a clean
    one left behind by a moved base, reset as before.
    """
    root = _resolve_rounds_dir(repo, str(rounds_dir))
    path = _workspace_path(root, round_no, agent)
    branch = _branch_name(round_no, agent)
    owned = _worktree_paths(repo)

    if not path.exists():
        if path.resolve() in owned:
            # The folder went away without git being told (``rm -rf`` of a round
            # directory, a wiped scratch disk): the worktree stays registered and
            # ``git worktree add`` refuses with "is a missing but already
            # registered worktree". Prune drops exactly those missing
            # registrations and touches no worktree that still has its folder.
            _git(repo, ["worktree", "prune"])
            owned = _worktree_paths(repo)
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
        commits = _commits_above(path, base_sha)
        dirty = _dirty_outside_runs(path)
        if (commits or dirty) and not force:
            raise _refuse_to_reset(path.resolve(), branch, round_no, commits, dirty)
        # ``--fresh`` has already agreed to drop the edits; without
        # ``-f`` a plain checkout keeps them and refuses outright when the new
        # base changes the same file ("would be overwritten by checkout").
        _git(path, ["checkout", *(["-f"] if force else []), "-B", branch, base_sha])

    _git(path, ["clean", "-fdx", "-e", "runs/"])
    _empty_runs_dir(path, agent)

    return Workspace(
        agent=agent,
        path=path.resolve(),
        branch=branch,
        base_sha=base_sha,
        kind=_KIND_WORKTREE,
    )


def _clone_origin(clone: Path) -> str | None:
    """The clone's ``origin`` URL, or ``None`` when the folder is not a clone.

    ``None`` also for a folder that is not a git repository at all — a foreign
    folder at the checkout path, or an empty one left by a crashed ``clone`` — and
    for a *worktree* folder, whose ``.git`` is a file, not a directory: a worktree
    would answer with its parent repo's ``origin`` and read as a clone.
    """
    if not (clone / ".git").is_dir():
        return None
    proc = _git(clone, ["config", "--get", "remote.origin.url"], check=False)
    if proc.returncode != 0:
        return None
    origin = proc.stdout.strip()
    return origin or None


def _disable_push(clone: Path) -> None:
    """Cut the clone's push URL, so ``git push origin …`` fails in git itself.

    KC-59: today a push is refused only by the gate's prompt — a worktree pushing
    writes the operator's own refs, and so would a clone with a live push URL.
    ``remote.origin.pushurl = DISABLED`` makes the push fail in git itself,
    whatever the policy answers, and leaves the fetch URL alone, so an operator's
    ``git fetch <clone> contest/<NN>/<agent>`` and ``attach_clone``'s own
    ``git fetch --all`` still work. Fail-open: a clone whose ``origin`` is gone
    keeps its own settings rather than raising into a round.
    """
    try:
        _git(clone, ["remote", "set-url", "--push", "origin", "DISABLED"], check=False)
    except Exception as exc:  # noqa: BLE001 — the checkout is the round, not the push URL
        _log.warning("could not disable pushing in %s: %s: %s", clone,
                     type(exc).__name__, exc)


def reset_clone(repo, rounds_dir, round_no: int, agent: str, base_sha: str,
                *, force: bool = False) -> Workspace:
    """A fresh, idempotent local clone for *agent* at *base_sha*.

    Path ``<rounds_dir>/<NN>-<agent>``, branch ``contest/<NN>/<agent>``. Built
    with ``git clone --local --no-checkout`` — the clone hard-links the repo's
    objects, so it costs about what a worktree does — then checked out onto its
    own branch at the base. KC-59: a clone has its own ``refs/stash``, index,
    ``HEAD``, branches and config, so a ``git stash`` in this agent's checkout
    can never pop another agent's work off the repo's shared worktree stack, and
    its push URL is cut while the clone is ours to shape.

    The reuse rules are KC-23's, exactly as for a worktree: a clone that holds
    commits above *base_sha* or uncommitted edits outside ``runs/`` is refused
    unless *force*; a clean clone at the base, and a clean one left behind by a
    moved base, reset as before. ``runs/<agent>/`` is always emptied. A folder at
    the path that is not a git clone, or a clone of a different repo, raises
    rather than being deleted; an empty folder is removed and the clone rebuilt.
    """
    repo_path = Path(repo).resolve()
    root = _resolve_rounds_dir(repo_path, str(rounds_dir))
    path = _workspace_path(root, round_no, agent)
    branch = _branch_name(round_no, agent)

    if path.exists():
        origin = _clone_origin(path)
        if origin is None:
            # not a clone: an empty folder a crashed clone left behind is ours to
            # replace, anything else is refused rather than deleted
            if any(path.iterdir()):
                raise WorkspaceError(
                    f"{path} exists but is not a git clone — refusing to delete a "
                    f"folder this module did not create"
                )
            shutil.rmtree(path)
        else:
            try:
                ours = Path(origin).resolve() == repo_path
            except (OSError, RuntimeError):
                ours = False
            if not ours:
                raise WorkspaceError(
                    f"{path} is a clone of {origin}, not of {repo_path} — refusing "
                    f"to reset a checkout this module did not create"
                )
            # the clone's objects are a copy, not a live link: a base the operator
            # moved on to after the clone was made is not in it yet, so fetch
            # before deciding what the clone carries — ``--local`` hard-links what
            # is already there, so this is a handful of new objects at most
            _git(path, ["fetch", "-q", "origin"])
            if _rev_parse(path, f"{base_sha}^{{commit}}") is None:
                raise WorkspaceError(
                    f"clone {path} does not contain base sha {base_sha}, not even "
                    f"after fetching origin — the base is not on any branch of "
                    f"{repo_path}"
                )
            commits = _commits_above(path, base_sha)
            dirty = _dirty_outside_runs(path)
            if (commits or dirty) and not force:
                raise _refuse_to_reset(path.resolve(), branch, round_no, commits, dirty)
            # ``--fresh`` has already agreed to drop the edits; without ``-f`` a
            # plain checkout keeps them and refuses outright when the new base
            # changes the same file ("would be overwritten by checkout").
            _git(path, ["checkout", *(["-f"] if force else []), "-B", branch, base_sha])
    if not path.exists():
        _git(repo_path, ["clone", "--local", "--no-checkout", "-q", str(repo_path),
                         str(path)])
        _git(path, ["checkout", "-q", "-B", branch, base_sha])

    _disable_push(path)
    _git(path, ["clean", "-fdx", "-e", "runs/"])
    _empty_runs_dir(path, agent)

    return Workspace(
        agent=agent,
        path=path.resolve(),
        branch=branch,
        base_sha=base_sha,
        kind=_KIND_CLONE,
    )


# ─────────────────────────────────────────────────────────────────────────────
# the per-agent scratch dir
# ─────────────────────────────────────────────────────────────────────────────


def tmp_root_dirs(tmp_roots) -> tuple[Path, ...]:
    """The directories the round's scratch globs name: ``/tmp/kilo/*`` -> ``/tmp/kilo``.

    KC-59: the per-agent scratch dir is derived from these and the agent's name,
    so no caller names a path. A trailing ``/*`` (or a bare ``/``) is stripped; a
    value that is not an absolute or ``~``-absolute path is skipped, and
    ``~`` is expanded the way the policy expands it. Fail-open throughout:
    ``None``, an empty tuple or a set of malformed globs is ``()`` — the agents
    then have no scratch dir, which is today's "no collect data" shape.
    """
    roots: list[Path] = []
    for item in tmp_roots or ():
        if not isinstance(item, str):
            continue
        raw = item.strip()
        if not raw or not raw.startswith(("/", "~")):
            continue
        base = raw.removesuffix("/*").rstrip("/") or "/"
        try:
            root = Path(os.path.expanduser(base))
        except (OSError, RuntimeError):
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def agent_tmp_dir(tmp_roots, agent) -> Path | None:
    """``<tmp_root>/<agent>`` from the round's first scratch root, or ``None``.

    *agent* is a roster name, so it matches ``[a-z0-9][a-z0-9_-]*`` and can hold
    no glob metacharacter; anything else — ``None``, empty, not a string — is
    ``None``, never an exception.
    """
    if not isinstance(agent, str) or not agent.strip():
        return None
    roots = tmp_root_dirs(tmp_roots)
    if not roots:
        return None
    return roots[0] / agent.strip()


def agent_tmp_globs(tmp_roots, agent) -> tuple[str, ...]:
    """``<tmp_root>/<agent>/*`` for every scratch root — this agent's own dirs.

    The policy's ``tmp_roots`` for one session (KC-59): the agent's own scratch
    dir is allowed, and another agent's dir is not one of these globs, so it is
    not settled by the mechanical layer and stays a permission event. Empty when
    the round names no scratch root, so the policy falls back to ``contest.ini``
    exactly as it does today.
    """
    globs: list[str] = []
    if not isinstance(agent, str) or not agent.strip():
        return ()
    name = agent.strip()
    for root in tmp_root_dirs(tmp_roots):
        glob = f"{root}/{name}/*"
        if glob not in globs:
            globs.append(glob)
    return tuple(globs)


def agent_tmp_dirs(tmp_roots, agents) -> tuple[Path, ...]:
    """Every roster agent's ``<tmp_root>/<agent>`` dir — the ones to forbid.

    KC-59's half of the rule "the policy allows its own dir and not the other
    agents'": each of these belongs to another agent and is forbidden ground for
    this one. *agents* may hold ``AgentSpec`` or bare names.
    """
    dirs: list[Path] = []
    for agent in agents or ():
        name = getattr(agent, "name", agent)
        path = agent_tmp_dir(tmp_roots, name)
        if path is not None and path not in dirs:
            dirs.append(path)
    return tuple(dirs)


def ensure_agent_tmp_dirs(config: ContestConfig) -> list[Path]:
    """Create every roster agent's scratch dir at round start; the ones created.

    Fail-open: no ``tmp_roots`` key, a malformed glob, or a directory that cannot
    be made degrades to "no scratch dir" and never raises into a round — the
    checkout is the round, the dir is a nicety the prompt names when it exists.
    """
    roots = tmp_root_dirs(getattr(config, "tmp_roots", ()))
    if not roots:
        return []
    made: list[Path] = []
    for agent in tuple(getattr(config, "agents", ()) or ()):
        name = getattr(agent, "name", agent)
        if not isinstance(name, str) or not name.strip():
            continue
        target = roots[0] / name.strip()
        try:
            target.mkdir(parents=True, exist_ok=True)
            made.append(target)
        except OSError as exc:
            _log.warning("could not create the scratch dir %s: %s", target, exc)
    return made


def attach_clone(clone_path, agent: str, base_sha: str, branch: str, *, force: bool = False) -> Workspace:
    """Use an existing *clone_path* as *agent*'s checkout at *base_sha*.

    The clone's remote must contain *base_sha* (``git fetch --all`` first). Dirty
    clones are reset only with ``force=True``; otherwise a :class:`WorkspaceError`
    lists the dirty files. ``runs/<agent>/`` is emptied.
    """
    clone = Path(clone_path).resolve()
    if not (clone / ".git").is_dir():
        # a worktree's ``.git`` is a file, not a directory: attaching one would
        # report ``kind == "clone"`` for a checkout that still shares its repo's
        # single ``refs/stash`` — exactly what KC-59 is about
        raise WorkspaceError(
            f"{clone} is not a git repository (a worktree's .git is a file, and a "
            f"worktree shares its repo's refs/stash — clone the repo instead)"
        )

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

    _git(clone, ["checkout", *(["-f"] if force else []), "-B", branch, base_sha])
    _git(clone, ["clean", "-fdx", "-e", "runs/"])
    _empty_runs_dir(clone, agent)

    return Workspace(
        agent=agent,
        path=clone,
        branch=branch,
        base_sha=base_sha,
        kind=_KIND_CLONE,
    )


def _workspace_kind(config: ContestConfig) -> Literal["clone", "worktree"]:
    """``config.workspace_kind`` as a kind, or ``"clone"`` for anything else.

    KC-59's default is a clone per agent. ``load_roster`` already refuses a
    value that is neither ``clone`` nor ``worktree``, so this is the fail-open
    half for a config built by hand: an absent or malformed key degrades to the
    default, never to an exception into a round.
    """
    kind = getattr(config, "workspace_kind", _KIND_CLONE)
    return _KIND_WORKTREE if kind == _KIND_WORKTREE else _KIND_CLONE


def prepare_round(
    repo,
    config: ContestConfig,
    round_no: int,
    base_ref: str,
    *,
    clones: dict[str, Path] | None = None,
    force_clone: bool = False,
    force: bool = False,
) -> list[Workspace]:
    """Prepare one checkout per roster agent at *base_ref*; return the workspaces.

    Resolves and validates the base (unresolvable or a dirty ``epic-tasks/`` raise),
    then for each agent in roster order attaches its ``clones`` entry or builds its
    own checkout: a fresh local clone when ``[contest] workspace_kind`` is
    ``clone`` (the KC-59 default — ``Workspace.kind == "clone"``, its own
    ``refs/stash``, its push URL cut), a worktree when it is ``worktree``. Each
    checkout that holds commits above the base or edits outside ``runs/`` is
    refused unless *force* (KC-23's ``--fresh``); *force_clone* still governs the
    attached clones alone. Every agent's scratch dir is created first. The repo's
    own checkout is never touched (no ``checkout``/``reset`` in *repo* itself).
    """
    repo_path = Path(repo).resolve()
    base_sha = _check_base_and_epic_tasks(repo_path, base_ref)
    root = _resolve_rounds_dir(repo_path, config.rounds_dir)
    use_worktrees = _workspace_kind(config) == _KIND_WORKTREE
    ensure_agent_tmp_dirs(config)

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
        elif use_worktrees:
            workspaces.append(
                reset_worktree(repo_path, root, round_no, agent.name, base_sha,
                               force=force)
            )
        else:
            workspaces.append(
                reset_clone(repo_path, root, round_no, agent.name, base_sha,
                            force=force)
            )
    return workspaces


def remove_round(repo, config: ContestConfig, round_no: int) -> None:
    """Remove every checkout (and branch) of *round_no*.

    The runbook's stage-5 cleanup, in one call: ``git worktree remove --force`` then
    ``git branch -D`` for each ``contest/<NN>/<agent>`` worktree of the round. KC-59's
    default makes the checkouts clones, which ``git worktree list`` cannot see, so the
    round's clone folders are removed whole too — only when their ``origin`` is *repo*,
    so a folder this module did not create is never deleted. A clone's branches live
    only inside the clone, so dropping the folder drops them.
    """
    repo_path = Path(repo).resolve()
    targets = {_branch_name(round_no, agent.name) for agent in config.agents}
    for path, branch in _worktree_list(repo_path):
        if branch in targets:
            _git(repo_path, ["worktree", "remove", "--force", str(path)])
            _git(repo_path, ["branch", "-D", branch], check=False)

    root = _resolve_rounds_dir(repo_path, config.rounds_dir)
    for agent in config.agents:
        path = _workspace_path(root, round_no, agent.name)
        origin = _clone_origin(path)
        if origin is None:
            continue
        try:
            ours = Path(origin).resolve() == repo_path
        except (OSError, RuntimeError):
            ours = False
        if not ours:
            # a folder this module did not create — leave it for the operator
            _log.warning("leaving %s: a clone of %s, not of %s", path, origin, repo_path)
            continue
        _git(repo_path, ["branch", "-D", _branch_name(round_no, agent.name)], check=False)
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise WorkspaceError(f"could not remove the clone {path}: {exc}") from exc


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
    p_prep.add_argument("--fresh", action="store_true",
                        help="reset a checkout even when it holds commits or edits")

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
        agent.name: _workspace_path(root, args.round, agent.name).exists()
        for agent in cfg.agents
        if agent.name not in clone_map
    }

    workspaces = prepare_round(
        repo_path, cfg, args.round, args.base_ref,
        clones=clone_map, force_clone=args.force_clone, force=args.fresh,
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
