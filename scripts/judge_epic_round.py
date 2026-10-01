#!/usr/bin/env python3
"""Score one epic round across several agent worktrees, on facts only.

An epic round hands every agent the same ticket and the same starting tree.
This collects what can be checked mechanically, so the judgement call that is
left is about the design, not about whether the suite passes.

    # every worktree under runs/, round 1, no test execution
    python3 scripts/judge_epic_round.py --round 1 --runs runs/ --base main

    # explicit worktrees, and actually run the four pytest roots
    python3 scripts/judge_epic_round.py --round 1 --base competition \\
        --worktree opus=/tmp/wt-opus --worktree sonnet=/tmp/wt-sonnet --tests

Hard gates (a `FAIL` here settles the round regardless of anything else):

  shrink       `CollectBridge._shrink` must be byte-identical to `--base`
  commits      exactly one commit for the ticket
  pushed       nothing may have reached a remote

Everything else is a column, not a verdict. The scorecard is in
`docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`; this script fills in its
mechanical half and prints the questions it cannot answer.

Exit codes: 0 scored · 1 usage / nothing to score.

KC-5: the scoring itself lives in `tools/contest/gates.py` (`judge_worktree`)
so the contest runner can import it; this file is the operator CLI over it and
keeps its CLI and its stdout/CSV bytes.
"""
import argparse
import csv
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.contest.gates import (  # noqa: E402
    judge_worktree,
    ticket_for_round,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, required=True, help="round number, matches NN- prefix")
    ap.add_argument("--tasks", default="epic-tasks", help="folder of NN-*.md tickets")
    ap.add_argument("--runs", default=None, help="folder whose subdirs are one worktree per agent")
    ap.add_argument("--worktree", action="append", default=[], metavar="NAME=PATH")
    ap.add_argument("--base", default="competition", help="the tree every agent started from")
    ap.add_argument("--tests", action="store_true", help="run the four pytest roots in each worktree")
    ap.add_argument("--csv", default=None, help="also write the scorecard here")
    a = ap.parse_args()

    name, title, declared = ticket_for_round(a.tasks, a.round)
    if not name:
        print(f"no ticket numbered {a.round} in {a.tasks}/", file=sys.stderr)
        return 1

    trees = []
    for spec in a.worktree:
        n, _, p = spec.partition("=")
        trees.append((n, p))
    if a.runs and os.path.isdir(a.runs):
        for d in sorted(os.listdir(a.runs)):
            p = os.path.join(a.runs, d)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, ".git")):
                trees.append((d, p))
    if not trees:
        print("nothing to score — pass --runs or --worktree", file=sys.stderr)
        return 1

    print(f"\nRound {a.round}: {title}")
    print(f"ticket: {a.tasks}/{name}")
    print(f"declared files: {', '.join(f'`{d}`' for d in declared) or '—'}")
    print(f"base: {a.base}\n")

    rows = [judge_worktree(n, p, a.base, declared, a.tests) for n, p in trees]

    cols = ["agent", "gate", "shrink", "commits", "files", "+/-", "test_files",
            "test_funcs", "off_ticket", "pushed", "sha"]
    if a.tests:
        cols.append("tests_run")
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    print("  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols))
    for r in rows:
        if r.get("notes"):
            print(f"\n  {r['agent']}: {r['notes']}")
        if r.get("off_ticket_files"):
            print(f"  {r['agent']} touched off-ticket: {r['off_ticket_files']}")

    print("\nWhat this script cannot score — read the diffs for these:")
    print("  1. Does it do what the ticket's Acceptance list says, item by item?")
    print("  2. Is it the simplest thing that does it, or is there a new abstraction")
    print("     the ticket did not ask for?")
    print("  3. Does the test fail without the change? (delete the change, re-run it)")
    print("  4. Does it stay fail-open — absent model, malformed config, broken artifact?")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            wr = csv.DictWriter(fh, fieldnames=cols + ["notes", "off_ticket_files", "path"])
            wr.writeheader()
            for r in rows:
                wr.writerow({k: r.get(k, "") for k in wr.fieldnames})
        print(f"\nscorecard -> {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
