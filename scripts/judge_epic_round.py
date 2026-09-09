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
"""
import argparse
import csv
import os
import re
import subprocess
import sys

BRIDGE = "tools/auto/collect_bridge.py"
TEST_ROOTS = ["tests", "tests_bugfix", ".smoke_tests", ".regression_tests"]


def git(cwd, *args, check=False):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {cwd}: {r.stderr.strip()}")
    return r.stdout.strip()


def extract_shrink(cwd, rev):
    """The text of `def _shrink` at `rev`, or None if the file/def is absent."""
    src = subprocess.run(["git", "show", f"{rev}:{BRIDGE}"], cwd=cwd,
                         capture_output=True, text=True)
    if src.returncode:
        return None
    lines = src.stdout.splitlines()
    start = None
    for i, l in enumerate(lines):
        if re.match(r"^\s*def _shrink\b", l):
            start = i
            break
    if start is None:
        return None
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = [lines[start]]
    for l in lines[start + 1:]:
        if l.strip() and (len(l) - len(l.lstrip())) <= indent:
            break
        body.append(l)
    return "\n".join(body).rstrip()


def ticket_for_round(tasks_dir, n):
    for name in sorted(os.listdir(tasks_dir)):
        m = re.match(r"^0*(\d+)-.*\.md$", name)
        if m and int(m.group(1)) == n:
            body = open(os.path.join(tasks_dir, name), encoding="utf-8").read()
            f = re.search(r"^\*\*File:\*\*\s*`?([^`\n]+?)`?\s*$", body, re.M)
            also = re.search(r"^\*\*Also touches:\*\*\s*(.+?)\s*$", body, re.M)
            declared = [f.group(1)] if f else []
            if also:
                declared += re.findall(r"`([^`]+)`", also.group(1))
            title = body.splitlines()[0].lstrip("# ").strip()
            return name, title, [d for d in declared if d != "—"]
    return None, None, []


def run_tests(cwd):
    """Four separate invocations — combining the roots collides in conftest."""
    out = []
    for d in TEST_ROOTS:
        if not os.path.isdir(os.path.join(cwd, d)):
            out.append(f"{d}:absent")
            continue
        r = subprocess.run([sys.executable, "-m", "pytest", d, "-q", "--timeout=180"],
                           cwd=cwd, capture_output=True, text=True)
        tail = (r.stdout or "").strip().splitlines()
        summary = tail[-1] if tail else ""
        nfail = re.search(r"(\d+) failed", summary)
        nerr = re.search(r"(\d+) error", summary)
        bad = int(nfail.group(1) if nfail else 0) + int(nerr.group(1) if nerr else 0)
        out.append(f"{d}:{'PASS' if r.returncode == 0 else f'{bad}✗'}")
    return " ".join(out)


def judge(name, path, base, declared, want_tests):
    row = {"agent": name, "path": path}
    if not os.path.isdir(os.path.join(path, ".git")) and not os.path.exists(os.path.join(path, ".git")):
        row["gate"] = "FAIL"
        row["notes"] = "not a git worktree"
        return row

    merge_base = git(path, "merge-base", base, "HEAD") or base
    commits = [l for l in git(path, "log", "--oneline", f"{merge_base}..HEAD").splitlines() if l]
    row["commits"] = len(commits)
    row["sha"] = commits[0].split()[0] if commits else "—"

    # ── hard gate 1: _shrink byte-identical ──────────────────────────────
    before, after = extract_shrink(path, merge_base), extract_shrink(path, "HEAD")
    if before is None or after is None:
        row["shrink"] = "?" if before is None else "GONE"
    else:
        row["shrink"] = "same" if before == after else "CHANGED"

    # ── hard gate 2: nothing pushed ──────────────────────────────────────
    remotes = git(path, "branch", "-r", "--contains", "HEAD")
    row["pushed"] = "yes" if remotes.strip() else "no"

    # ── diff shape ───────────────────────────────────────────────────────
    stat = git(path, "diff", "--numstat", f"{merge_base}..HEAD")
    files, add, dele = [], 0, 0
    for l in stat.splitlines():
        parts = l.split("\t")
        if len(parts) != 3:
            continue
        a, d, f = parts
        files.append(f)
        add += int(a) if a.isdigit() else 0
        dele += int(d) if d.isdigit() else 0
    row["files"] = len(files)
    row["+/-"] = f"+{add}/-{dele}"

    tests = [f for f in files if re.search(r"(^|/)tests?[_/]|/test_|^\.smoke_tests/|^\.regression_tests/", f)]
    row["test_files"] = len(tests)
    new_tests = 0
    for f in tests:
        blob = subprocess.run(["git", "show", f"HEAD:{f}"], cwd=path,
                              capture_output=True, text=True)
        if blob.returncode == 0:
            new_tests += len(re.findall(r"^\s*def test_", blob.stdout, re.M))
    row["test_funcs"] = new_tests

    if declared:
        outside = [f for f in files if f not in declared and not tests.count(f)]
        row["off_ticket"] = len(outside)
        row["off_ticket_files"] = ";".join(outside[:4])
    else:
        row["off_ticket"] = 0
        row["off_ticket_files"] = ""

    row["tests_run"] = run_tests(path) if want_tests else "—"

    gates = []
    if row["shrink"] == "CHANGED":
        gates.append("_shrink modified")
    if row["commits"] != 1:
        gates.append(f"{row['commits']} commits, expected 1")
    if row["pushed"] == "yes":
        gates.append("reached a remote")
    if row["test_files"] == 0:
        gates.append("no test shipped")
    row["gate"] = "FAIL" if gates else "ok"
    row["notes"] = "; ".join(gates)
    return row


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

    rows = [judge(n, p, a.base, declared, a.tests) for n, p in trees]

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
