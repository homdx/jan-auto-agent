"""tools/arena/judging.py — AR-64: `arena run judge NN[.K]`, the score table and the cross matrix in one command.

By hand the judge was two scripts and four paths: `setup_worktrees.py` turns the
round's `entrants.json` into one worktree per entry, then `judge_epic_round.py
--cross` scores them and runs every entry's tests on every other entry's code.
`run judge` finds the round's folder and base itself, sets the worktrees up in
`<round folder>/wt`, and starts the judge with `--cross`, `--cross-out` pointing at
the folder and one cell at a time.

Nothing is written outside the round's folder, and the entries' trees are only
read: the judge copies a test into a scratch directory, never into a worktree.
An existing `wt/` is reused; only its `ideal` tree is moved, to the ref named now,
so one `wt/` serves the check of a candidate ideal and the check after it was
amended. A cell is timing-sensitive (round 184's own tests assert a three second
budget), so a box already loaded above its core count gets one line saying so.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from tools.arena import output
from tools.arena.rounds import (
    EXIT_FAILED, EXIT_OK, PROC_ROOT, RoundError, _VIEW_ARG, load_config,
    round_alive, round_folder,
)

#: The two scripts the verb drives — paths under the checkout.
SETUP_SCRIPT = Path("contest-bench") / "harness" / "setup_worktrees.py"
JUDGE_SCRIPT = Path("scripts") / "judge_epic_round.py"

#: Runs a command line and returns its exit code — a seam, so tests record it.
RUN: Callable[..., subprocess.CompletedProcess] = subprocess.run

#: `os.getloadavg`, a seam for the load note.
LOADAVG: Callable[[], tuple[float, float, float]] = os.getloadavg


def _git(repo: Path, *words: str) -> str:
    """One `git` call's stdout, stripped; empty when git fails."""
    try:
        done = subprocess.run(["git", "-C", str(repo), *words], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def build_lines(repo: Path, nn: int, folder: Path, base: str,
                args: argparse.Namespace, passthrough: list[str]) -> list[list[str]]:
    """The command lines in order: the worktrees (when needed), then the judge.

    The setup line is present when `wt/` is missing, or when `--ideal` names a
    ref and `wt/ideal` is missing; `plan_ideal_move` handles an `ideal` that is
    there already.
    """
    wt = folder / "wt"
    lines: list[list[str]] = []
    need_setup = not wt.is_dir() or (args.ideal and not (wt / "ideal").exists())
    if need_setup:
        line = [sys.executable, str(repo / SETUP_SCRIPT), str(folder / "entrants.json"),
                "--wt", str(wt), "--repo", str(repo)]
        if args.ideal:
            line += ["--ideal", args.ideal]
        lines.append(line)
    judge = [sys.executable, str(repo / JUDGE_SCRIPT), "--round", str(nn),
             "--base", base, "--runs", str(wt)]
    if not args.no_cross:
        judge += ["--cross", "--jobs", str(args.jobs), "--cross-out", str(folder)]
        if args.ideal:
            judge += ["--ideal", args.ideal]
        if args.cell_timeout is not None:
            judge += ["--cell-timeout", str(args.cell_timeout)]
    lines.append(judge + list(passthrough))
    return lines


def _ideal_refusal(repo: Path, folder: Path, tree: Path) -> Optional[str]:
    """Why an existing `wt/ideal` is unsafe to run `checkout` in, or None.

    A stale plain directory, a symlink to the main checkout or a path that
    redirects outside the round folder would make `git -C wt/ideal checkout`
    walk up and land on another checkout: the main one, or one the round does
    not own. `is-inside-work-tree` does not tell that apart, and
    `resolve(show-toplevel) == resolve(ideal)` passes a symlink to the main
    checkout (both resolve to it), so the resolved top-level is checked against
    the round folder and against the main checkout, and the tree's git metadata
    must be a worktree entry the repo registered (`_unregistered_worktree`): a
    foreign repository at that path, or a `.git` file redirecting to the main
    `.git`, or a symlink to another worktree of the round, is no worktree of
    this round's `ideal`. Refusing is the only safe answer —
    nothing is deleted, recreated or switched.
    """
    if not (tree.exists() or tree.is_symlink()):
        return None
    try:
        tree_res = tree.resolve()
        folder_res = folder.resolve()
        # `wt/ideal` must be the worktree itself, not a way to another one: a link to
        # the round's `wt/base` is registered, inside the round and its own top-level
        # once resolved, yet `checkout` through it would move `base`.
        if tree.is_symlink():
            return f"{tree} is a symlink to {tree_res}, not a worktree of its own"
        if tree.parent.resolve() != folder_res / "wt":
            return f"{tree} does not sit directly in {folder / 'wt'} (its wt is a redirect)"
    except (OSError, RuntimeError, ValueError) as err:
        return f"cannot resolve {tree}: {err}"
    top = _git(tree, "rev-parse", "--show-toplevel")
    if not top:
        return f"{tree} is not a Git worktree"
    try:
        top_res = Path(top).resolve()
    except (OSError, RuntimeError, ValueError) as err:
        return f"cannot resolve Git top-level for {tree}: {err}"
    if top_res != tree_res:
        return f"{tree} is not its own Git top-level (it reports {top})"
    if folder_res != top_res and folder_res not in top_res.parents:
        return f"{tree} is outside the round folder {folder}"
    main = _git(repo, "rev-parse", "--show-toplevel")
    if main:
        try:
            if Path(main).resolve() == top_res:
                return f"{tree} is the main checkout of {repo}"
        except (OSError, RuntimeError, ValueError) as err:
            return f"cannot resolve main checkout for {repo}: {err}"
    return _unregistered_worktree(repo, tree)


def _unregistered_worktree(repo: Path, tree: Path) -> Optional[str]:
    """Why *tree* is not a linked worktree *repo* itself registered, or None.

    Where the tree sits proves nothing: a separate repository created at
    `wt/ideal`, or a `.git` file whose `gitdir:` line names the main `.git`
    (or another worktree's admin dir), passes every location check, and then
    `checkout` moves the main checkout's HEAD and index. So the git metadata is
    read: the tree's common dir must be the repo's, its own git dir must be a
    `worktrees/<name>` entry of that common dir, and that entry's `gitdir`
    back-pointer must name this tree's `.git`.
    """
    own = _git(tree, "rev-parse", "--absolute-git-dir")
    common = _git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    mine = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not (own and common and mine):
        return f"{tree} has no readable Git metadata"
    try:
        own_res, common_res = Path(own).resolve(), Path(common).resolve()
        if common_res != Path(mine).resolve():
            return f"{tree} belongs to another repository ({common}), not {repo}"
        if own_res.parent != common_res / "worktrees":
            return f"{tree} is not a worktree registered in {repo} (its git dir is {own})"
        back = (own_res / "gitdir").read_text(encoding="utf-8").strip()
        # the tree's own path with only its parents resolved: `tree.resolve()` would follow a link
        if Path(back).resolve() != tree.parent.resolve() / tree.name / ".git":
            return f"{tree} points at the worktree entry of {back}, not its own"
    except (OSError, RuntimeError, ValueError) as err:
        return f"cannot verify {tree} against {repo}: {err}"
    return None


def plan_ideal_move(repo: Path, folder: Path, ref: Optional[str]) -> Optional[str]:
    """The commit `wt/ideal` must move to, or None when it is already there.

    `setup_worktrees.py` skips a tree that exists, so a second `--ideal` would
    otherwise be scored against the first one's checkout without a word.
    An existing `wt/ideal` that is not a real detached worktree of *repo* inside
    *folder* — a stale plain directory, a symlink to the main checkout, a path
    redirecting outside the round — is refused: `git -C wt/ideal checkout` would
    walk up and land on another checkout.
    Raises `RoundError` for a ref git cannot resolve, or for such an `ideal`.
    """
    tree = folder / "wt" / "ideal"
    bad = _ideal_refusal(repo, folder, tree)
    if bad:
        raise RoundError(f"--ideal {tree}: {bad}")
    if not ref or not tree.exists():
        return None
    want = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if not want:
        raise RoundError(f"--ideal {ref!r} is not a commit in {repo}")
    have = _git(tree, "rev-parse", "HEAD")
    return None if have == want else want


def run_judge(repo: Path, args: argparse.Namespace) -> int:
    """`arena run judge NN[.K] [--ideal REF] [--jobs N] [--cell-timeout S] [--no-cross] [--dry-run] [-- judge flags]`.

    Every refusal comes before anything is run. Exit: the judge's own, 1 when the
    round has no `entrants.json` or a setup step fails.
    """
    repo = Path(repo)
    match = _VIEW_ARG.match(str(args.run))
    try:
        if not match:
            raise RoundError(f"run judge: {args.run!r} is not NN or NN.K")
        if args.jobs < 1:
            raise RoundError("run judge: --jobs is 1 or more")
        nn = int(match.group(1))
        leg = int(match.group(2)) if match.group(2) is not None else None
        folder = round_folder(repo, load_config(repo), nn, leg=leg)
        entrants = folder / "entrants.json"
        if not entrants.is_file():
            print(output.scrub(f"arena: no entrants.json for round {args.run} "
                               f"(looked in {folder}) — the round has not ended"),
                  file=sys.stderr)
            return EXIT_FAILED
        if round_alive(repo, nn, PROC_ROOT):
            raise RoundError(f"round {nn} is running in {repo} — judge it when it ends")
        try:
            base = str(json.loads(entrants.read_text(encoding="utf-8"))["base"])
        except (OSError, ValueError, KeyError, TypeError) as err:
            raise RoundError(f"{entrants} has no base: {err}") from err
        move = plan_ideal_move(repo, folder, args.ideal)
        lines = build_lines(repo, nn, folder, base, args, args.passthrough)
    except RoundError as err:
        return output.refuse(str(err))

    load, cores = LOADAVG()[0], os.cpu_count() or 1
    if load > cores:
        print(output.scrub(f"arena: note: load {load:.1f} on {cores} cores — a cell is "
                           "timing-sensitive; a lead that vanishes on a quiet box is noise"),
              file=sys.stderr)
    if args.dry_run:
        if move:
            print(f"git -C {folder / 'wt' / 'ideal'} checkout -q --detach {move}")
        for line in lines:
            print(output.scrub(" ".join(line)))
        return EXIT_OK
    if move:
        done = RUN(["git", "-C", str(folder / "wt" / "ideal"), "checkout", "-q", "--detach", move])
        if done.returncode:
            return output.refuse(f"cannot move {folder / 'wt' / 'ideal'} to {move[:12]}")
        print(f"ideal -> {move[:12]}")
    for line in lines:
        sys.stdout.flush()
        code = RUN(line, cwd=repo).returncode
        if code:
            return code
    return EXIT_OK
