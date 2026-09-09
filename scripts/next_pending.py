#!/usr/bin/env python3
"""Hand an adjudicator exactly one un-adjudicated finding from the queue.

Stage 2 asks "is this proposal true?" and produces open questions. Stage 3
settles them. Same mechanic as next_finding.py and for the same reason: an
adjudicator handed the whole queue reads it all, decides in its head, and writes
nothing. The queue is derived from the verdict file, so an unrecorded finding is
handed back and the only way forward is to write.

    python3 scripts/next_pending.py --pending validate1/pending-validation.md \\
                                    --out truth-<yourname>.csv

Exit codes: 0 an entry was printed · 3 the queue is finished · 1 usage error.
"""
import argparse
import csv
import os
import re
import sys

ENTRY = re.compile(r"^## +\d+\. +`([^`]+)`\s*$")


def parse_pending(path):
    lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
    starts = [(i, m.group(1)) for i, l in enumerate(lines) if (m := ENTRY.match(l))]
    out = []
    for n, (i, key) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body = "\n".join(lines[i:end]).rstrip()
        body = re.sub(r"\n-{3,}\s*$", "", body)
        out.append({"finding": key, "body": body})
    return out


def decided(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        return {(r.get("finding") or "").strip() for r in csv.DictReader(fh)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pending", required=True)
    ap.add_argument("--out", required=True, help="the truth CSV append_verdict.py writes")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.pending):
        print(f"error: {a.pending} not found", file=sys.stderr)
        return 1
    entries = parse_pending(a.pending)
    if not entries:
        print(f"error: no '## N. `finding`' sections in {a.pending}", file=sys.stderr)
        return 1

    done = decided(a.out)
    todo = [e for e in entries if e["finding"] not in done]
    n_done, n_all = len(entries) - len(todo), len(entries)

    print(f"progress: {n_done}/{n_all} adjudicated, {len(todo)} remaining")
    if a.status or not todo:
        if not todo:
            print("\nThe queue is finished. Report your REAL/FALSE/FIXED counts and stop.")
        return 3 if not todo else 0

    print("=" * 72)
    print(todo[0]["body"])
    print("=" * 72)
    print(f"Read the code, then record with scripts/append_verdict.py --out {a.out} .")
    print(f"Until you do, this command keeps returning {todo[0]['finding']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
