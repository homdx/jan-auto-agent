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

Writes tasks/PROGRESS.csv (header on first use), refuses a duplicate ticket
(exit 2) and a file that is not UTF-8 (exit 3), flushes to disk before returning.
"""
import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from tools.file_lock import locked  # noqa: E402
except ImportError:
    # GYRO-01: an external --target repo gets this script alone, without tools/file_lock.py
    # (llama.cpp even has a tools/ of its own); the same parent-directory flock, inline.
    import contextlib

    @contextlib.contextmanager
    def locked(path):
        parent = os.path.dirname(os.path.realpath(path))
        os.makedirs(parent, exist_ok=True)
        fd = None
        if os.name != "nt":
            import fcntl
            fd = os.open(parent, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)

COLUMNS = ["ticket", "finding", "outcome", "commit", "note"]
OUTCOMES = {"FIXED", "ALREADY-OK", "SKIPPED"}


def _not_utf8(path) -> str:
    """CSV-ANSI: why *path* is not UTF-8 (a BOM is fine), or "" when it is.

    Excel's plain "CSV" saves the notes as cp1251/cp1252; reading that file as UTF-8 died
    with a traceback before the duplicate check, and appending a UTF-8 row to it would
    leave two encodings in one file. The file is refused instead, with what to do.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return f"{exc.reason} at byte {exc.start}"
    return ""


def _refuse_not_utf8(path, why) -> int:
    print(f"refused: {path} is not UTF-8 ({why}) - a UTF-8 row would mix two encodings in one "
          f"file; re-save it as \"CSV UTF-8\" and record again", file=sys.stderr)
    return 3


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
    # The epic-round prompt (docs/collect-epics/RUN-THE-EPIC-COMPETITION.md) says `--outcome DONE`; it means FIXED. Rejecting it left the ticket unrecorded.
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

    d = os.path.dirname(a.progress)
    if d:
        os.makedirs(d, exist_ok=True)
    # AUDIT3-C4: the duplicate check and the append are one locked step, as in
    # append_verdict.py — two agents recording one ticket both passed the check.
    with locked(a.progress):
        existed = os.path.exists(a.progress) and os.path.getsize(a.progress) > 0
        why = _not_utf8(a.progress) if existed else ""
        if why:
            return _refuse_not_utf8(a.progress, why)
        if existed and not a.allow_duplicate:
            with open(a.progress, newline="", encoding="utf-8-sig") as fh:   # REL-2: a spreadsheet's BOM
                for prev in csv.DictReader(fh):
                    if (prev.get("ticket") or "").strip() == row["ticket"]:
                        print(f"skipped: {row['ticket']} is already recorded as "
                              f"{prev.get('outcome')} — move to the next ticket "
                              f"(--allow-duplicate to override)", file=sys.stderr)
                        return 2

        # csv writes at the end of the file; if the last line has no final newline, the new row would be glued onto it. Use the file's own line ending first.
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
    with open(a.progress, newline="", encoding="utf-8-sig") as fh:
        n = sum(1 for _ in csv.DictReader(fh))
    print(f"recorded #{n}: {row['outcome']} {row['ticket']}"
          + (f" @ {row['commit']}" if row["commit"] else "") + f" -> {a.progress}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
