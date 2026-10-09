#!/usr/bin/env python3
"""Record the outcome of one tasks/ ticket, immediately and safely.

The stage-5 counterpart of append_finding.py. next_task.py keeps handing out a
ticket until its outcome is written here, so this is the only way to advance the
loop — and the reason a context blow-up mid-round is a resume rather than lost
work.

    python3 scripts/append_task.py --progress tasks/PROGRESS.csv \
        --ticket 02-list-py-files.md --outcome FIXED --commit 1a2b3c4 \
        --note "list() copy on both branches + regression test in tests_bugfix/"

--outcome:
    FIXED       code changed, one local commit, regression test added  (--commit required)
    ALREADY-OK  verified against live code, no defect / already handled  (no commit)
    SKIPPED     deliberately deferred (out of scope, latent-only, blocked)  (--note required)

Writes tasks/PROGRESS.csv (header on first use), refuses a duplicate ticket,
flushes to disk before returning.
"""
import argparse
import csv
import os
import sys

COLUMNS = ["ticket", "finding", "outcome", "commit", "note"]
OUTCOMES = {"FIXED", "ALREADY-OK", "SKIPPED"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--progress", default="tasks/PROGRESS.csv")
    ap.add_argument("--ticket", required=True, help="the NN-*.md filename")
    ap.add_argument("--outcome", required=True)
    ap.add_argument("--commit", default="", help="local commit sha for a FIXED ticket")
    ap.add_argument("--finding", default="", help="file::symbol (informational)")
    ap.add_argument("--note", default="")
    ap.add_argument("--allow-duplicate", action="store_true")
    a = ap.parse_args()

    outcome = a.outcome.strip().upper()
    # The epic-round prompt (docs/collect-epics/RUN-THE-EPIC-COMPETITION.md)
    # says `--outcome DONE`; it means FIXED. Rejecting it left the agent's
    # ticket unrecorded and next_task.py handing it out again.
    if outcome == "DONE":
        outcome = "FIXED"
    row = {
        "ticket": os.path.basename(a.ticket.strip()),
        "finding": a.finding.strip(),
        "outcome": outcome,
        "commit": a.commit.strip(),
        "note": a.note.replace("\n", "; ").strip(),
    }

    problems = []
    if outcome not in OUTCOMES:
        problems.append(f"--outcome must be one of {sorted(OUTCOMES)}, got {a.outcome!r}")
    if outcome == "FIXED" and not row["commit"]:
        problems.append("a FIXED ticket needs --commit <sha> — the loop's rule is one "
                        "local commit per bug")
    if outcome == "SKIPPED" and not row["note"]:
        problems.append("a SKIPPED ticket needs --note saying why it was deferred")
    if problems:
        print("outcome rejected:", file=sys.stderr)
        for p in problems:
            print(f"  x {p}", file=sys.stderr)
        return 1

    existed = os.path.exists(a.progress) and os.path.getsize(a.progress) > 0
    if existed and not a.allow_duplicate:
        with open(a.progress, newline="", encoding="utf-8") as fh:
            for prev in csv.DictReader(fh):
                if (prev.get("ticket") or "").strip() == row["ticket"]:
                    print(f"skipped: {row['ticket']} is already recorded as "
                          f"{prev.get('outcome')} — move to the next ticket "
                          f"(--allow-duplicate to override)", file=sys.stderr)
                    return 2

    d = os.path.dirname(a.progress)
    if d:
        os.makedirs(d, exist_ok=True)
    # csv writes where the file ends: a PROGRESS.csv an editor or a hand left
    # without its final newline got the new row glued onto its last line, so
    # next_task.py read one row and handed the new ticket out again. The file's
    # own line ending ends that line first, and the row keeps that ending too.
    eol, tail = "\r\n", ""
    if existed:
        with open(a.progress, "rb") as fh:
            data = fh.read()
        eol = "\r\n" if b"\r\n" in data else "\n"
        if not data.endswith((b"\n", b"\r")):
            tail = eol
    with open(a.progress, "a", newline="", encoding="utf-8") as fh:
        fh.write(tail)
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator=eol)
        if not existed:
            w.writeheader()
        w.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())

    # rows, not lines: a note may hold a quoted line break
    with open(a.progress, newline="", encoding="utf-8") as fh:
        n = sum(1 for _ in csv.DictReader(fh))
    print(f"recorded #{n}: {row['outcome']} {row['ticket']}"
          + (f" @ {row['commit']}" if row["commit"] else "") + f" -> {a.progress}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
