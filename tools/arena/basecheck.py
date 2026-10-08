"""tools/arena/basecheck.py — AR-25: `arena base check`, the base's own checks on a clean checkout.

Today the base is checked by hand, four commands typed one after another at the
end of every round. A base that is already red makes every entry of the next
round look broken, and the round is scored on a failure nobody wrote.

The check must run on a clean checkout of the commit, not on the operator's
working tree: on the operator's box two tests are red in the working tree and
green on a clean checkout of the same commit, because `contest.local.ini`
(untracked, never committed) overrides the committed defaults they read. An
agent works in a clone of the base, so a clean checkout is the base as the
agents see it. Hence the throwaway detached worktree under
`.arena/base-check/<sha[:12]>`, removed when the check ends, red or green — the
operator's tree, index and HEAD are never read for writing, and no untracked
or ignored file is copied into it.

`.arena/base-check.json` maps a full sha to its pass, so the check is paid once
per commit: a stored pass is printed `cached` and nothing runs. Only a pass is
stored — a red base is run again next time. The cache is fail-open (a missing,
garbage or unwriteable file is an empty cache) and the verdict never is: the
exit code is the steps'.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from tools.arena import gitref, output

# AR-1's table, the only codes `base check` returns: 0 every step passed (also a
# cache hit), 1 a red step, 2 a refusal before anything ran. The 2 comes from
# `output.refuse`, which prints the one line as well as returning it.
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

#: The base's own checks, strictly in this order. Each is its own subprocess in
#: the clean checkout. `-p no:cacheprovider` keeps the throwaway tree free of a
#: `.pytest_cache`; the repository's own `pytest.ini` decides the parallelism,
#: so the check adds no `-n`. `tests` and `tests_bugfix` are never run together.
STEPS: tuple[tuple[str, list[str]], ...] = (
    ("tests", ["python3", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]),
    ("tests_bugfix", ["python3", "-m", "pytest", "tests_bugfix", "-q", "-p", "no:cacheprovider"]),
    ("tiers", ["python3", "scripts/sync_test_tiers.py", "--check"]),
    ("clocks", ["python3", "scripts/check_test_clocks.py", "--check"]),
)

#: The steps that run pytest, and so list their failed tests.
PYTEST_STEPS = frozenset({"tests", "tests_bugfix"})

#: The table columns; the JSON rows carry `sha` and `cached` on top of these.
COLUMNS = ["step", "ok", "seconds", "summary"]

CACHE_FILE = "base-check.json"
WORKTREE_DIR = "base-check"
SHORT = 12
#: `summary` is one line per row.
SUMMARY_LIMIT = 120
FAILED_LINES = 5

#: Runs a command line and returns its exit code — a seam, so tests record it.
RUN: Callable[..., subprocess.CompletedProcess] = subprocess.run


class BaseCheckError(Exception):
    """A refusal before anything ran; the message is the one line the CLI prints."""


@dataclass(frozen=True)
class Step:
    """One step: its name and the command line that runs it."""

    name: str
    command: list[str]
    #: A pytest step: a failed one carries its `FAILED …` lines. Named on the
    #: step, not read off the command line, so a stand-in command is one too.
    pytest: bool = False


def build_steps() -> list[Step]:
    """The four checks, in order, as `STEPS` is right now."""
    return [Step(name, list(command), pytest=name in PYTEST_STEPS) for name, command in STEPS]


def default_ref(profile: Optional[Mapping[str, str]] = None) -> str:
    """The ref to check when `--ref` is not given: the profile's `branch`, else `HEAD`.

    A missing profile and a malformed `branch` key both fall through to `HEAD` —
    the ref must never be empty, so a bad profile setting cannot become an
    unresolved ref.
    """
    branch = (profile or {}).get("branch") if isinstance(profile, Mapping) else None
    if isinstance(branch, str) and branch.strip():
        return branch.strip()
    return "HEAD"


def resolve_sha(repo: Path, ref: str) -> str:
    """The full sha of *ref*, or `BaseCheckError`.

    A directory that is not a git checkout and a ref that does not resolve are
    refusals: both are checked before the worktree and the cache are touched, so
    nothing is created for either.
    """
    repo = Path(repo)
    try:
        inside = gitref.git(repo, "rev-parse", "--is-inside-work-tree")
    except gitref.GitRefError:
        inside = ""
    if inside.strip() != "true":
        raise BaseCheckError(f"{repo} is not a git checkout")
    try:
        sha = gitref.git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    except gitref.GitRefError as err:
        raise BaseCheckError(f"--ref {ref!r} does not resolve to a commit in {repo}") from err
    if not sha:
        raise BaseCheckError(f"--ref {ref!r} does not resolve to a commit in {repo}")
    return sha


def worktree_path(repo: Path, sha: str) -> Path:
    """The throwaway checkout of *sha*: `.arena/base-check/<sha[:12]>`."""
    return Path(repo) / ".arena" / WORKTREE_DIR / sha[:SHORT]


def _registered(repo: Path) -> set[str]:
    """Every worktree `git` knows about, including an entry an earlier crash left."""
    try:
        listed = gitref.git(repo, "worktree", "list", "--porcelain")
    except gitref.GitRefError:
        return set()
    return {line.split(" ", 1)[1] for line in listed.splitlines()
            if line.startswith("worktree ")}


def remove_tree(repo: Path, tree: Path) -> None:
    """Drop the throwaway tree, whatever an earlier crash left behind.

    `remove --force` for a registered tree, `prune` for a registration whose
    directory an earlier run already lost; the directory is deleted by hand as a
    last resort, so a crash mid-`worktree add` cannot leave it either.
    """
    if not tree.exists() and str(tree) not in _registered(repo):
        return
    try:
        gitref.git(repo, "worktree", "remove", "--force", str(tree))
    except gitref.GitRefError:
        pass
    try:
        gitref.git(repo, "worktree", "prune")
    except gitref.GitRefError:
        pass
    if tree.exists():
        shutil.rmtree(tree, ignore_errors=True)


def prepare_tree(repo: Path, tree: Path, sha: str) -> Path:
    """A throwaway detached checkout of *sha* at *tree*, replacing any leftover."""
    remove_tree(repo, tree)
    tree.parent.mkdir(parents=True, exist_ok=True)
    try:
        gitref.git(repo, "worktree", "add", "-q", "--detach", str(tree), sha)
    except gitref.GitRefError as err:
        raise BaseCheckError(f"git worktree add failed: cannot check out {sha[:SHORT]} into {tree}") from err
    return tree


def cache_path(repo: Path) -> Path:
    """`.arena/base-check.json`: the stored passes by full sha."""
    return Path(repo) / ".arena" / CACHE_FILE


def read_cache(repo: Path) -> dict[str, dict]:
    """The stored passes by full sha.

    A file that is missing, unreadable or not a JSON object is an empty cache —
    the cache is fail-open, the verdict never is.
    """
    try:
        data = json.loads(cache_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {sha: entry for sha, entry in data.items()
            if isinstance(sha, str) and isinstance(entry, dict)}


def write_cache(repo: Path, cache: Mapping[str, dict]) -> bool:
    """Write the cache atomically: a temporary file, then a rename.

    `False` when the write fails — a lost cache is the next run's job, not an
    error, and the verdict stands. No `.tmp` survives, either way. Bug 200: the
    temp file is `mkstemp`'s own unique name next to the cache, not one fixed
    name shared by every process — two `base check` runs at once would then
    write into each other's file, or one would lose it before the `replace`.
    """
    path = cache_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                   dir=str(path.parent))
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cache, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _hit(cache: Mapping[str, dict], sha: str) -> Optional[list[dict]]:
    """The stored step rows for *sha*, when it was a pass; else None.

    Only a pass is a hit — a red base is run again next time — and a stored entry
    that is not what was written is no hit at all: fail-open to a real run.
    """
    entry = cache.get(sha)
    if not isinstance(entry, dict) or entry.get("ok") is not True:
        return None
    steps = entry.get("steps")
    if not isinstance(steps, list):
        return None
    rows: list[dict] = []
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("step"), str):
            continue
        try:
            seconds = round(float(step.get("seconds") or 0.0), 1)
        except (TypeError, ValueError):
            seconds = 0.0
        base = str(step.get("summary", "") or "").strip()
        rows.append({
            "step": step["step"],
            "ok": bool(step.get("ok")),
            "seconds": seconds,
            "summary": f"{base} (cached)" if base else "(cached)",
            "sha": sha,
            "cached": True,
        })
    # A hit is a pass, so every stored step must have passed too: a mangled or
    # red entry is no hit at all and is run for real.
    return rows if rows and all(row["ok"] for row in rows) else None


def _summary(step: Step, proc: subprocess.CompletedProcess) -> str:
    """The last non-empty line of the step's *stdout*, cut to 120 characters.

    For a failed pytest step up to five of its `FAILED …` lines are appended
    after it, so one run names the red tests.

    Bug 200: stderr comes in only when stdout is empty — a step that prints its
    pytest summary and then its warnings on stderr showed the warning, not
    `N passed in …s`.
    """
    stdout = str(getattr(proc, "stdout", "") or "")
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        stderr = str(getattr(proc, "stderr", "") or "")
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return ""
    summary = lines[-1][:SUMMARY_LIMIT]
    if proc.returncode != 0 and step.pytest:
        # The last line may be a `FAILED …` line itself: it is not said twice.
        failed = [line for line in lines if line.startswith("FAILED ") and line != lines[-1]]
        if failed:
            summary = f"{summary}; " + "; ".join(failed[:FAILED_LINES])
    return summary


def run_steps(
    worktree: Path,
    steps: Sequence[Step],
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> list[dict]:
    """Run *steps* one after another in *worktree*, each its own subprocess.

    A step that fails does not stop the later ones, so one run names every red
    step; a step that cannot be started is a failed row, not a traceback.
    """
    runner = RUN if runner is None else runner
    rows: list[dict] = []
    for step in steps:
        start = time.monotonic()
        try:
            proc = runner(list(step.command), cwd=str(worktree),
                          capture_output=True, text=True, errors="replace")
        except OSError as err:
            proc = subprocess.CompletedProcess(list(step.command), 127, "", str(err))
        rows.append({
            "step": step.name,
            "ok": proc.returncode == 0,
            "seconds": round(time.monotonic() - start, 1),
            "summary": _summary(step, proc),
        })
    return rows


def verdict(sha: str, rows: Sequence[Mapping[str, object]]) -> str:
    """`base <sha[:12]>: ok`, or the same line naming every red step."""
    red = [str(row.get("step", "?")) for row in rows if not row.get("ok")]
    return (f"base {sha[:SHORT]}: ok" if not red
            else f"base {sha[:SHORT]}: FAILED ({', '.join(red)})")


def _print(rows: Sequence[Mapping[str, object]], sha: str, fmt: str) -> None:
    """The table, then the verdict line — the JSON rows alone."""
    output.emit(rows, COLUMNS, fmt)
    if fmt != "json":
        print(verdict(sha, rows))


def dry_run(sha: str, tree: Path, steps: Sequence[Step]) -> None:
    """The sha, the worktree path and the four commands, one per line."""
    print(sha)
    print(output.scrub(str(tree)))
    for step in steps:
        print(output.scrub(" ".join(step.command)))


def run_base_check(
    repo: Path,
    args: argparse.Namespace,
    profile: Optional[Mapping[str, str]] = None,
    steps: Optional[list[Step]] = None,
) -> int:
    """`arena base check [--ref REF] [--force] [--keep] [--dry-run]`.

    Exit: 0 every step passed (also a cache hit), 1 at least one step failed, 2
    a refusal before anything ran — an unresolved ref, a directory that is not a
    git checkout, `git worktree add` failing. No 3, no 4.
    """
    repo = Path(repo)
    ref = getattr(args, "ref", None) or default_ref(profile)
    if not isinstance(ref, str) or not ref.strip():
        return output.refuse("base check: --ref is empty")
    steps = list(steps) if steps else build_steps()
    fmt = getattr(args, "output", "table")

    try:
        sha = resolve_sha(repo, ref)
    except BaseCheckError as err:
        return output.refuse(str(err))

    tree = worktree_path(repo, sha)
    if getattr(args, "dry_run", False):
        dry_run(sha, tree, steps)
        return EXIT_OK

    if not getattr(args, "force", False):
        hit = _hit(read_cache(repo), sha)
        if hit is not None:
            _print(hit, sha, fmt)
            return EXIT_OK

    try:
        prepare_tree(repo, tree, sha)
    except BaseCheckError as err:
        return output.refuse(str(err))

    try:
        rows = run_steps(tree, steps)
    finally:
        if getattr(args, "keep", False):
            if tree.exists():
                print(str(tree))
        else:
            remove_tree(repo, tree)

    for row in rows:
        row["sha"], row["cached"] = sha, False
    _print(rows, sha, fmt)

    if all(row["ok"] for row in rows):
        cache = read_cache(repo)
        cache[sha] = {
            "at": int(time.time()),
            "ok": True,
            "steps": [{k: row[k] for k in COLUMNS} for row in rows],
        }
        write_cache(repo, cache)
    return EXIT_OK if all(row["ok"] for row in rows) else EXIT_FAILED
