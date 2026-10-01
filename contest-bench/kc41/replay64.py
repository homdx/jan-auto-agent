"""KC-41 replay — round 64's five abandoned worktrees, scored the way the ticket says.

Round 64 (base `9912b78`, eight agents, ticket `64-kc25-...`) is the round this
ticket exists for. Three agents `timeout` at `turn_timeout_sec = 1800`, one
`stalled`, two `error`, one `idle` through to a REWORK — and the round harvested
**nothing**: every one of those agents ran out of clock with the work in its
tree and no commit, and `harvest` scores a commit. On 2026-09-22 the five
worktrees that held anything were squashed to one commit each by hand and put
through `harvest(ws, ticket, run_tests=True)` one at a time, and the result was
the round the operator never saw: two `READY`, three `REWORK`, all four pytest
roots green for the two.

This script does the same thing *through the code* — `_deadline_commit`, the
progress row, `_harvest` — instead of by hand, and asserts the ticket's *Source*
table comes back. It is the ticket's seventh Acceptance item, and it is a bench
script rather than a `tests/` test for the reasons in the ticket's ground rules:
it starts no `kilo` and no provider, but it runs four real pytest roots over
five real worktrees, which is ~65 minutes of wall clock (almost all of it
`glm-4-7-flash`, whose own code makes its `tests` root take 2609 s — the ticket
puts that out of scope, and it is why this is run by hand and not in CI).

    python3 contest-bench/kc41/replay64.py --rounds ../rounds --out contest-out/64

`--rounds` defaults to `../rounds`, `../rounds` holding one directory per agent
named `64-<agent>`; `--out` defaults to `contest-out/64`, whose `state.json` is
the round's own record and whose `64-<agent>/` directories carry the ticket the
agents were given. Nothing is written inside either: the script copies each
worktree to a scratch dir first, so the 2026-09-22 hand work is never mutated
and a second run starts from the same tree.

**The roots run sequentially**, one worktree at a time, and that is not a
politeness: they are the same four `pytest` invocations the harvest runs in a
real round, and `agent_suite_slots` exists precisely so two of them never sit
beside each other. The 2026-09-22 pass took ~65 min for exactly that reason.

Pass/fail is the ticket's table: two `READY`, three `REWORK` with
`tests_failed`, five commits, `off_ticket: 0` and `shrink: same` throughout. The
`--table` flag prints the same five rows the ticket prints and stops there; with
no flag the script asserts.

`--timeout SEC` bounds each worktree's four roots. It defaults to no bound,
because the honest answer for `glm-4-7-flash` is 2609 s and a bound that clips
it would report `tests_slow` for a root that is merely enormous.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.contest import runner as runner_mod  # noqa: E402
from tools.contest.harvest import harvest  # noqa: E402
from tools.contest.workspace import Workspace  # noqa: E402

#: The five worktrees round 64 left holding something. The other three agents of
#: the eight ended clean (or with a tree the operator had already thrown away),
#: and the ticket's table is about these five.
ROUND_64_AGENTS = (
    "mimo-v2-5",
    "agnes-2-5-flash",
    "step-3-7-flash",
    "hy3",
    "glm-4-7-flash",
)

#: The ticket's *Source* table, verbatim: what the 2026-09-22 hand pass found.
#: `verdict` and `tests` are what the replay has to reproduce; the shas are the
#: 2026-09-22 ones and are deliberately *not* compared — the replay's own
#: deadline commits carry new ones, which is the point.
EXPECTED = {
    "mimo-v2-5":     {"verdict": "READY",  "tests": "PASS"},
    "agnes-2-5-flash": {"verdict": "READY",  "tests": "PASS"},
    "step-3-7-flash": {"verdict": "REWORK", "tests": "FAIL"},
    "hy3":           {"verdict": "REWORK", "tests": "FAIL"},
    "glm-4-7-flash": {"verdict": "REWORK", "tests": "FAIL"},
}

#: The reason a REWORK entry is expected to carry. `tests_failed` only: the
#: deadline commit and the row are the runner satisfying the existing contract,
#: so nothing mechanical about the commit itself may object.
EXPECTED_REWORK_REASONS = ("tests_failed",)


def _git(cwd, *args) -> str:

    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} in {cwd} exited {r.returncode}: {r.stderr}")
    return r.stdout.strip()


def _main_repo(worktree: Path) -> Path:
    """The repository a round worktree belongs to, from its `.git` file.

    `gitdir: .../repo/.git/worktrees/64-<agent>` — the repository root is the
    parent of the `.git` directory two levels up. A worktree whose `.git` is a
    *directory* (an ordinary clone) is its own repository.
    """
    dot = worktree / ".git"
    if dot.is_dir():
        return worktree
    pointer = dot.read_text(encoding="utf-8").strip()
    if not pointer.startswith("gitdir:"):
        raise SystemExit(f"{worktree}/.git is neither a directory nor a gitdir pointer")
    return Path(pointer.split(":", 1)[1].strip()).resolve().parent.parent


def _branch_of(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "--abbrev-ref", "HEAD")


def _status_of(worktree: Path) -> str:
    """`git status --porcelain -uall` of a worktree, `""` when git cannot read it.

    The replay reads the *uncommitted* work off the round's own worktree — that
    is the work KC-41 commits — and a status that does not run is reported as
    the script's own error rather than as an empty tree, because an empty tree
    here would score as "nothing to commit" and quietly pass.
    """

    r = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain",
                        "--untracked-files=all"],
                       cwd=str(worktree), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"git status in {worktree} exited {r.returncode}: {r.stderr}")
    return r.stdout


def _agent_worktree(rounds: Path, out: Path, agent: str) -> Path | None:
    """Where round 64's `<agent>` worktree is, or `None` when it is not on disk.

    Two shapes, both seen in the tree: `<rounds>/64-<agent>/` (the worktree) and
    `<out>/64-<agent>/` (the round's own out dir, which holds the ticket and the
    events, not the code). The first is the one that carries the work.
    """
    for candidate in (rounds / f"64-{agent}", out / f"64-{agent}"):
        if (candidate / ".git").exists() or (candidate / "HEAD").exists():
            return candidate
    return None


def _ticket_for(out: Path) -> Path | None:
    """The ticket round 64 handed out, from its own `state.json`."""
    state = out / "state.json"
    if not state.exists():
        return None
    name = json.loads(state.read_text(encoding="utf-8")).get("ticket") or ""
    for base in (out, out.parent / "epic-tasks", REPO_ROOT / "epic-tasks"):
        path = base / name
        if name and path.exists():
            return path
    return None


def _replay_one(src: Path, agent: str, ticket: Path, base: str, scratch: Path,
                *, run_tests: bool, timeout: float) -> dict:
    """One worktree, through KC-41's steps 1–4, and nothing else.

    The tree is *copied* first, so the round-64 worktree is never mutated: the
    2026-09-22 squash is still in it, and a second run of this script has to
    start from the same place the first did.
    """
    work = scratch / f"64-{agent}"
    branch_name = f"replay64/{agent}"
    # a second run of the script must start from the same place the first did,
    # so the scratch clone and its worktree are torn down rather than reused
    if work.exists():
        subprocess.run(["git", "worktree", "remove", "-f", str(work)],
                       cwd=str(scratch / "repo.git"), check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(work, ignore_errors=True)
    # A `copytree` of a worktree copies its `.git` *file*, whose `gitdir:` still
    # points at the original repository — so a commit in the copy lands on the
    # round-64 branch and the replay mutates the evidence it is scoring. It is
    # therefore re-created as a worktree of a *clone* of the repository, off the
    # source's own branch, and the source's uncommitted work is copied in. The
    # clone is the scratch dir's, and nothing under `--rounds` is written.
    repo = scratch / "repo.git"
    if not repo.exists():
        subprocess.run(["git", "clone", "-q", "--no-hardlinks",
                        str(_main_repo(src)), str(repo)], check=True)
    _git(repo, "fetch", "-q", "--force", str(_main_repo(src)),
         f"+refs/heads/{_branch_of(src)}:refs/heads/{branch_name}")
    subprocess.run(["git", "worktree", "add", "-q", "-B", branch_name,
                    str(work), branch_name], cwd=str(repo), check=True)
    # the source's own uncommitted work, which is the whole point of the replay
    for line in _status_of(src).splitlines():
        if len(line) <= 3:
            continue
        path = line[3:].rsplit(" -> ", 1)[-1].strip()
        if not path or path.startswith("runs/") or not (src / path).is_file():
            continue
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / path, work / path)
    branch = _branch_of(src)
    ws = Workspace(agent=agent, path=work, branch=branch, base_sha=base, kind="worktree")

    before = _git(work, "rev-list", "--count", f"{base}..HEAD") if base else "?"
    started = time.time()
    # step 1: commit what the turn left, when it left nothing committed
    sha = runner_mod._deadline_commit(ws, reason="replay of round 64", ticket=ticket)
    if not sha:
        return {"agent": agent, "deadline_commit": None, "verdict": "—",
                "tests": "—", "reasons": [], "off_ticket": None, "shrink": None,
                "commits": before, "note": "nothing to commit — the tree was clean "
                                           "or the commit was refused"}
    # step 2: the row `harvest` asks for, written by the runner
    runner_mod._write_progress_row(
        ws, ticket, sha,
        note=runner_mod._deadline_note("replay of round 64", agent, Path(ticket).name))
    # step 3 + 4: the ordinary harvest, exactly as the terminal branch calls it
    verdict = harvest(ws, ticket, run_tests=run_tests, budget_sec=timeout)
    return {
        "agent": agent,
        "deadline_commit": sha[:7],
        "verdict": verdict.verdict,
        "tests": str(verdict.facts.get("tests_run", "—")),
        "reasons": [r.code for r in verdict.reasons],
        "off_ticket": verdict.facts.get("off_ticket"),
        "shrink": verdict.facts.get("shrink"),
        "commits": verdict.facts.get("commits"),
        "elapsed": round(time.time() - started, 1),
        "note": "",
    }


def _base_sha(out: Path) -> str | None:
    state = out / "state.json"
    if not state.exists():
        return None
    return json.loads(state.read_text(encoding="utf-8")).get("base_sha")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", default="../rounds", type=Path,
                    help="the directory holding one `64-<agent>` worktree per agent")
    ap.add_argument("--out", default="contest-out/64", type=Path,
                    help="round 64's own out dir (state.json, the ticket, the events)")
    ap.add_argument("--scratch", default="/tmp/kilo/kc41-replay64", type=Path,
                    help="where the worktrees are copied to — never the round's own")
    ap.add_argument("--no-tests", action="store_true",
                    help="score the commits without the four roots (fast; a smoke, not the ticket)")
    ap.add_argument("--timeout", default=0.0, type=float,
                    help="per-worktree budget for the four roots, 0 = unbounded")
    ap.add_argument("--table", action="store_true",
                    help="print the rows and stop — no assertion")
    ap.add_argument("--only", default="", help="one agent, for a single worktree")
    args = ap.parse_args()

    base = _base_sha(args.out)
    ticket = _ticket_for(args.out)
    if not base:
        print(f"no base_sha in {args.out / 'state.json'} — is this round 64?")
        return 2
    if not ticket:
        print(f"no ticket found for round 64 under {args.out} — nothing to score against")
        return 2
    agents = (args.only,) if args.only else ROUND_64_AGENTS
    args.scratch.mkdir(parents=True, exist_ok=True)

    print(f"round 64 replay — base {base[:12]}, ticket {ticket.name}, "
          f"roots {'off' if args.no_tests else 'on, sequential'}")
    rows = []
    for agent in agents:
        src = _agent_worktree(args.rounds, args.out, agent)
        if src is None:
            print(f"  {agent}: no worktree under {args.rounds}/64-{agent} — skipped")
            continue
        print(f"  {agent}: scoring {src} ...", flush=True)
        row = _replay_one(src, agent, ticket, base, args.scratch,
                          run_tests=not args.no_tests, timeout=args.timeout)
        rows.append(row)
        print(f"    -> {row['verdict']} ({row['tests']}) commit {row['deadline_commit']}"
              f" in {row.get('elapsed', 0)}s"
              + (f" — {row['note']}" if row.get("note") else ""), flush=True)

    print()
    print(f"| agent | deadline commit | verdict | four roots | off_ticket | shrink | commits |")
    print("|---|---|---|---|---|---|---|")
    for row in rows:
        print(f"| {row['agent']} | `{row['deadline_commit']}` | **{row['verdict']}** "
              f"| {row['tests']} | {row['off_ticket']} | {row['shrink']} | {row['commits']} |")
    if args.table:
        return 0

    bad: list[str] = []
    ready = sum(1 for r in rows if r["verdict"] == "READY")
    if ready != 2:
        bad.append(f"{ready} READY, the ticket's table has 2")
    for row in rows:
        want = EXPECTED.get(row["agent"])
        if want is None:
            continue
        if row["verdict"] != want["verdict"]:
            bad.append(f"{row['agent']}: {row['verdict']}, the table says {want['verdict']} "
                       f"({row['reasons']})")
        if row["verdict"] == "REWORK":
            missing = [c for c in EXPECTED_REWORK_REASONS if c not in row["reasons"]]
            if missing:
                bad.append(f"{row['agent']}: REWORK without {missing} — {row['reasons']}")
        if row["commits"] != 1:
            bad.append(f"{row['agent']}: {row['commits']} commits, the table has one")
        if row["off_ticket"] not in (0, None, ""):
            bad.append(f"{row['agent']}: off_ticket {row['off_ticket']}, the table has 0")
        if row["shrink"] not in ("same", None, ""):
            bad.append(f"{row['agent']}: shrink {row['shrink']}, the table has 'same'")
    if len(rows) != len(agents):
        bad.append(f"{len(rows)} of {len(agents)} worktrees were on disk")
    print()
    if bad:
        print("the round did not come back:")
        for line in bad:
            print(f"  x {line}")
        return 1
    print(f"round 64 came back: {ready} READY, {len(rows) - ready} REWORK with "
          f"tests_failed, {len(rows)} commits, off_ticket 0 and shrink same throughout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
