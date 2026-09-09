#!/usr/bin/env python3
"""Hand a coder exactly one unfinished ticket from the tasks/ folder.

Stage 4 writes one ticket per confirmed defect into tasks/. This is the stage-5
loop: same mechanic as next_finding.py / next_pending.py. The coder never gets
the whole folder, only the next ticket whose outcome is not yet recorded in the
progress file. Fix it, commit it, record it with append_task.py, call this
again. Calling twice without recording returns the same ticket, so a context
blow-up mid-round is a resume, not a data loss: re-run the same command and it
continues from the first unrecorded ticket.

    python3 scripts/next_task.py --tasks tasks/

    # after committing the fix:
    python3 scripts/append_task.py --progress tasks/PROGRESS.csv \
        --ticket 02-list-py-files.md --outcome FIXED --commit <sha>

Exit codes: 0 a ticket was printed · 3 the folder is finished · 1 usage error.
"""
import argparse
import csv
import os
import re
import sys

TICKET_RE = re.compile(r"^(\d+)-.*\.md$")


def load_tickets(tasks_dir):
    """Every NN-*.md in tasks/, in numeric order, with its finding key."""
    out = []
    for name in sorted(os.listdir(tasks_dir)):
        m = TICKET_RE.match(name)
        if not m:
            continue
        body = open(os.path.join(tasks_dir, name), encoding="utf-8",
                    errors="replace").read()
        file_ = _field(body, "File")
        symbol = _field(body, "Symbol")
        sev = _field(body, "Severity")
        finding = f"{file_}::{symbol}" if file_ and symbol and symbol != "—" else file_
        out.append({"num": int(m.group(1)), "name": name, "finding": finding,
                    "severity": sev, "body": body})
    return out


def _field(body, label):
    m = re.search(rf"^\*\*{label}:\*\*\s*`?([^`\n]+?)`?\s*$", body, re.MULTILINE)
    return m.group(1).strip() if m else ""


def recorded(progress_path):
    if not os.path.exists(progress_path) or os.path.getsize(progress_path) == 0:
        return {}
    with open(progress_path, newline="", encoding="utf-8") as fh:
        return {(r.get("ticket") or "").strip(): (r.get("outcome") or "").strip()
                for r in csv.DictReader(fh)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="tasks", help="folder of NN-*.md tickets")
    ap.add_argument("--progress", default=None,
                    help="progress CSV append_task.py writes (default: <tasks>/PROGRESS.csv)")
    ap.add_argument("--status", action="store_true", help="progress only, hand out nothing")
    a = ap.parse_args()

    if not os.path.isdir(a.tasks):
        print(f"error: {a.tasks} is not a folder", file=sys.stderr)
        return 1
    progress = a.progress or os.path.join(a.tasks, "PROGRESS.csv")

    tickets = load_tickets(a.tasks)
    if not tickets:
        print(f"error: no NN-*.md tickets in {a.tasks} — run scripts/make_jira_tasks.py first",
              file=sys.stderr)
        return 1

    done = recorded(progress)
    todo = [t for t in tickets if t["name"] not in done]
    n_all, n_done = len(tickets), len(tickets) - len(todo)

    print(f"progress: {n_done}/{n_all} recorded, {len(todo)} remaining")
    if done:
        tally = {}
        for o in done.values():
            tally[o] = tally.get(o, 0) + 1
        print("  so far: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))

    if a.status or not todo:
        if not todo:
            print("\nEvery ticket is recorded. Report your outcome counts and stop.")
            print("Then sync FIXED rows into validate1/truth.csv and GROUND-competition.md.")
            return 3
        print("remaining: " + ", ".join(t["name"] for t in todo))
        return 0

    nxt = todo[0]
    if n_done == 0 and n_all > 1:
        print("Fix this one ticket, commit it, record it with append_task.py, "
              "then run this command again for the next.")
    print("=" * 72)
    print(nxt["body"].rstrip())
    print("=" * 72)
    print(f"When the fix is committed, record it:")
    print(f"  python3 scripts/append_task.py --progress {progress} \\")
    print(f"      --ticket {nxt['name']} --outcome FIXED --commit <sha> \\")
    print(f"      --finding {nxt['finding'] or '-'}")
    print(f"Until you do, this command keeps returning {nxt['name']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
