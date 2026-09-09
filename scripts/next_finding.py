#!/usr/bin/env python3
"""Hand a reviewer exactly one unrecorded entry from IMPROVEMENTS.md.

Reviewers told to "record each finding as you go" read the whole list, judge
everything in their head, and write nothing — then run out of room and lose it.
Asking harder does not work, so this removes the choice: the queue is derived
from the CSV, and an entry stops being handed out only once it appears there.

Call it, judge the one entry it gives you, record it with append_finding.py,
call it again. Calling twice without recording returns the same entry, so
skipping ahead is not possible.

    python3 scripts/next_finding.py --improvements IMPROVEMENTS.md \
                                    --out validation-v1-mymodel.csv

Exit codes: 0 an entry was printed · 3 the list is finished · 1 usage error.
"""
import argparse
import csv
import os
import re
import sys

HEADING = re.compile(r"^### +([A-Za-z0-9_\-]+): *(.*)$")


def parse_improvements(path):
    """Every '### <id>: <title>' section, in file order."""
    text = open(path, encoding="utf-8", errors="replace").read()
    lines = text.split("\n")
    starts = [(i, m.group(1), m.group(2).strip())
              for i, l in enumerate(lines) if (m := HEADING.match(l))]
    entries = []
    for n, (i, tid, title) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body = "\n".join(lines[i:end]).rstrip()
        # a trailing '---' separator belongs to the entry, not the next one
        body = re.sub(r"\n-{3,}\s*$", "", body)
        # A rendered entry carries Location, Target files, Acceptance check and
        # Instruction. One that has only a heading is not a task a reviewer can
        # judge — it is a truncated file, and handing it over silently produces
        # 53 confident verdicts formed from a title alone. Flag it here rather
        # than letting the run look successful.
        thin = "**Instruction:**" not in body and "**Location:**" not in body
        entries.append({"task_id": tid, "title": title, "body": body, "thin": thin})
    return entries


def recorded_ids(csv_path):
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
        return set()
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return {(r.get("task_id") or "").strip() for r in csv.DictReader(fh)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--improvements", required=True)
    ap.add_argument("--out", required=True, help="the CSV append_finding.py writes to")
    ap.add_argument("--status", action="store_true", help="progress only, hand out nothing")
    ap.add_argument("--allow-thin", action="store_true",
                    help="hand out entries that carry no Location/Instruction anyway")
    a = ap.parse_args()

    if not os.path.exists(a.improvements):
        print(f"error: {a.improvements} not found", file=sys.stderr)
        return 1

    d, base = os.path.dirname(a.out) or ".", os.path.basename(a.out)
    if os.path.isdir(d):
        twins = [f for f in os.listdir(d) if f.lower() == base.lower() and f != base]
        if twins:
            print(f"error: {os.path.join(d, twins[0])} already exists and differs only in "
                  f"case — use that exact filename, or your work lands in two files.",
                  file=sys.stderr)
            return 1

    entries = parse_improvements(a.improvements)
    if not entries:
        print(f"error: no '### <id>: <title>' sections in {a.improvements}", file=sys.stderr)
        return 1

    # A thin entry — heading only, no Location and no Instruction — cannot be
    # judged, and handing one over silently buys a confident verdict formed from
    # a title. Skip those and say so, rather than failing the whole round: one
    # broken entry should not cost the other fifty-two. A file that is entirely
    # thin is a different thing — there is nothing to review at all, so stop.
    thin = [e["task_id"] for e in entries if e["thin"]]
    # --status only reports; it hands nothing over, so it must keep working on a
    # broken file — that is exactly when you want to look at the state.
    if thin and not a.allow_thin and not a.status:
        if len(thin) == len(entries):
            print(f"error: every entry in {a.improvements} ({len(entries)}) is a heading "
                  f"with no Location and no Instruction — there is nothing to review.",
                  file=sys.stderr)
            print(f"  Regenerate it with --auto ... --dry-run, or pass --allow-thin to "
                  f"review titles only and record that the input was thin.", file=sys.stderr)
            return 1
        print(f"warning: skipping {len(thin)} entry/entries with no Location and no "
              f"Instruction — they cannot be judged: {', '.join(thin[:8])}"
              + (" ..." if len(thin) > 8 else ""), file=sys.stderr)
        print(f"  reviewing the remaining {len(entries) - len(thin)}. "
              f"--allow-thin includes them anyway.", file=sys.stderr)
        entries = [e for e in entries if not e["thin"]]

    done = recorded_ids(a.out)
    todo = [e for e in entries if e["task_id"] not in done]
    n_done, n_all = len(entries) - len(todo), len(entries)
    extra = sorted(i for i in done if i.startswith("NEW-"))

    if a.status and thin:
        print(f"note: {len(thin)}/{len(entries)} entries carry no Location and no "
              f"Instruction and would be skipped in a real run", file=sys.stderr)

    if a.status or not todo:
        print(f"progress: {n_done}/{n_all} recorded, {len(todo)} remaining"
              + (f"  (+{len(extra)} NEW-*)" if extra else ""))
        if not todo:
            print("\nThe list is finished. Report your verdict counts and stop.")
            print("Do not re-audit entries; they are already on disk.")
            return 3
        print(f"remaining: {', '.join(e['task_id'] for e in todo[:12])}"
              + (" ..." if len(todo) > 12 else ""))
        return 0

    nxt = todo[0]
    repeat = nxt["task_id"] == getattr(main, "_last", None)
    main._last = nxt["task_id"]

    print(f"progress: {n_done}/{n_all} recorded, {len(todo)} remaining"
          + (f"  (+{len(extra)} NEW-*)" if extra else ""))
    if n_done == 0 and len(entries) > 1:
        print("Judge this one entry, record it with append_finding.py, then run "
              "this command again for the next.")
    print("=" * 72)
    print(nxt["body"])
    print("=" * 72)
    print(f"Record {nxt['task_id']} with scripts/append_finding.py --out {a.out} ...")
    print(f"until you do, this command keeps returning {nxt['task_id']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
