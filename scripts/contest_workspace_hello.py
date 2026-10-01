#!/usr/bin/env python3
"""contest_workspace_hello.py — the smallest possible tools.contest.workspace
driver: one round, two agents, one crash-and-rerun.

Unlike scripts/kilo_hello.py (KC-1), there is nothing here to stub or run
live against — KC-4 is pure `git` plumbing with no Kilo server, no model, no
network call anywhere in tools/contest/workspace.py. This script is the
worktree-management equivalent: it builds a disposable two-commit sandbox
repo (never this checkout), runs prepare_round once, simulates a crash
(a stray file + a stale PROGRESS.csv) and reruns it to show the reset, then
tears down with remove_round. Standard library plus this repo's own
tools.contest.{roster,workspace} — nothing else.

    python3 scripts/contest_workspace_hello.py
    python3 scripts/contest_workspace_hello.py --keep   # leave the sandbox on disk, print its path
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.contest.roster import AgentSpec, ContestConfig  # noqa: E402
from tools.contest.workspace import prepare_round, remove_round  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"git {' '.join(args)} in {cwd} failed: {proc.stderr.strip()}")
    return proc.stdout


def make_sandbox(root: Path) -> str:
    """Two commits: init, then a committed epic-tasks/ — the base a round runs from."""
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "hello@example.com")
    git(root, "config", "user.name", "contest_workspace_hello")
    (root / "README.md").write_text("sandbox\n")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "init")
    (root / "epic-tasks").mkdir()
    (root / "epic-tasks" / "INDEX.md").write_text("- one ticket\n")
    git(root, "add", "epic-tasks")
    git(root, "commit", "-q", "-m", "epic-tasks")
    return git(root, "rev-parse", "HEAD").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the sandbox on disk")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="contest-workspace-hello-"))
    repo = tmp / "repo"
    print(f"1. sandbox repo: {repo}")
    base = make_sandbox(repo)
    print(f"   base sha: {base}")

    config = ContestConfig(
        rounds_dir=str(tmp / "rounds"),
        agents=(
            AgentSpec(name="alice", provider_id="kenary", model_id="hy3:free"),
            AgentSpec(name="bob", provider_id="kenary", model_id="hy3:free"),
        ),
    )

    print("2. prepare_round(1) — one worktree per agent, at the base")
    wss = prepare_round(repo, config, 1, base)
    for ws in wss:
        print(f"   {ws.agent}: {ws.path}  ({ws.branch})  HEAD={git(ws.path, 'rev-parse', 'HEAD').strip()}")

    alice = next(ws for ws in wss if ws.agent == "alice")
    print("3. simulate a crashed attempt for alice: a stray commit + a stale PROGRESS.csv")
    (alice.path / "scratch.txt").write_text("mid-run garbage\n")
    git(alice.path, "add", "scratch.txt")
    git(alice.path, "commit", "-q", "-m", "stray")
    alice.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    alice.progress_csv.write_text("task1,done\n")

    print("4. prepare_round(1) again — same round number, must land back on the base")
    wss2 = prepare_round(repo, config, 1, base)
    alice2 = next(ws for ws in wss2 if ws.agent == "alice")
    print(f"   alice HEAD now: {git(alice2.path, 'rev-parse', 'HEAD').strip()} (== base: {git(alice2.path, 'rev-parse', 'HEAD').strip() == base})")
    print(f"   stray.txt gone: {not (alice2.path / 'scratch.txt').exists()}")
    print(f"   PROGRESS.csv gone: {not alice2.progress_csv.exists()}")

    print("5. remove_round(1) — cleanup")
    remove_round(repo, config, 1)
    listing = git(repo, "worktree", "list", "--porcelain")
    print(f"   worktrees left: {listing.count('worktree ')} (only the sandbox's own checkout)")

    if args.keep:
        print(f"\n--keep: sandbox left at {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
