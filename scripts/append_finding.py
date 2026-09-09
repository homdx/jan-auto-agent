#!/usr/bin/env python3
"""Append one validated finding to a reviewer CSV, immediately and safely.

Reviewers lose work two ways: they buffer every finding until the end and then
run out of context, or they hand-format CSV and produce rows with the wrong
column count (observed: one model emitted rows of 12, 13 and 27 columns, fusing
records together and making the whole file unreadable).

Both go away if each finding is written the moment it is decided. This script
takes named arguments, so quoting and column order cannot be got wrong, writes
the header on first use, refuses a duplicate file+symbol, and flushes to disk
before returning. Call it once per finding and move on.

    python3 scripts/append_finding.py --out validation-v1-mymodel.csv \
        --variant 1 --task-id AUTO-T4 --title "get_progress returns a shallow copy" \
        --file tools/auto/state.py --symbol StateStore.get_progress \
        --verdict FALSE_POSITIVE --severity NONE --defect-class mutable-state-leak \
        --caller-mutates NO \
        --impact "none — the values are scalars" \
        --evidence "return dict(self._progress)" \
        --disproof "would be real only if _progress held nested dicts/lists; grepped every write: only str and int" \
        --fix-type none --effort S --confidence HIGH
"""
import argparse
import csv
import os
import sys

COLUMNS = [
    "variant", "task_id", "title", "file", "symbol", "line_start", "line_end",
    "verdict", "severity", "defect_class", "caller_mutates", "impact", "repro",
    "evidence", "disproof", "fix_type", "effort", "confidence", "notes",
]
VERDICTS = {"CONFIRMED", "FALSE_POSITIVE", "ALREADY_FIXED", "UNVERIFIABLE", "OUT_OF_SCOPE"}
SEVERITIES = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"}
TRISTATE = {"YES", "NO", "UNKNOWN"}


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--out", help="CSV path; or use --reviewer to have it built for you")
    ap.add_argument("--reviewer", help="your model name, lowercase (e.g. glm-4.5-flash). "
                                       "Builds validation-v<variant>-<reviewer>.csv")
    for col in COLUMNS:
        ap.add_argument("--" + col.replace("_", "-"), default="")
    ap.add_argument("--allow-duplicate", action="store_true")
    a = vars(ap.parse_args())

    row = {c: str(a.get(c) or "").replace("\n", "; ").strip() for c in COLUMNS}

    out = a.get("out")
    if not out:
        if not (a.get("reviewer") and row["variant"]):
            print("error: pass --out, or --reviewer together with --variant", file=sys.stderr)
            return 1
        out = f"validation-v{row['variant']}-{a['reviewer'].strip().lower()}.csv"

    # One reviewer, one file. A model that writes Kilo.csv on one call and
    # kilo.csv on the next splits its own work in half and defeats the dedupe,
    # which is per-file — so refuse the second spelling rather than silently
    # starting a rival file.
    d, base = os.path.dirname(out) or ".", os.path.basename(out)
    if os.path.isdir(d):
        twins = [f for f in os.listdir(d)
                 if f.lower() == base.lower() and f != base]
        if twins:
            print(f"error: {os.path.join(d, twins[0])} already exists and differs only in "
                  f"case. Keep using that exact filename — one reviewer, one file.",
                  file=sys.stderr)
            return 1

    # --- refuse what the merge step cannot use -------------------------------
    problems = []
    v, s = row["verdict"].upper(), row["severity"].upper()
    if v not in VERDICTS:
        problems.append(f"--verdict must be one of {sorted(VERDICTS)}, got {row['verdict']!r}")
    if s not in SEVERITIES:
        problems.append(f"--severity must be one of {sorted(SEVERITIES)}, got {row['severity']!r}")
    if row["caller_mutates"] and row["caller_mutates"].upper() not in TRISTATE:
        problems.append(f"--caller-mutates must be YES/NO/UNKNOWN, got {row['caller_mutates']!r}")
    if not row["file"]:
        problems.append("--file is required — a finding with no location cannot be checked")
    if v == "CONFIRMED" and not row["evidence"]:
        problems.append("a CONFIRMED verdict needs --evidence quoting the line that proves it")
    if v in {"CONFIRMED", "ALREADY_FIXED"} and not row["disproof"]:
        problems.append("--disproof is required: name the check that would have made this "
                        "a false positive, and what you found when you ran it")
    if v == "CONFIRMED" and s in {"CRITICAL", "HIGH"} and row["caller_mutates"].upper() != "YES":
        problems.append("CRITICAL/HIGH needs --caller-mutates YES: a defect no caller can reach "
                        "today is latent — use MEDIUM or lower and say so in notes")
    if problems:
        print("finding rejected:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1

    row["verdict"], row["severity"] = v, s
    row["caller_mutates"] = row["caller_mutates"].upper()

    # --- dedupe against what is already on disk ------------------------------
    existed = os.path.exists(out) and os.path.getsize(out) > 0
    if existed and not a["allow_duplicate"]:
        with open(out, newline="", encoding="utf-8") as fh:
            for prev in csv.DictReader(fh):
                if (prev.get("file"), prev.get("symbol")) == (row["file"], row["symbol"]):
                    print(f"skipped: {row['file']}::{row['symbol']} is already recorded "
                          f"as {prev.get('verdict')} — move on to the next finding "
                          f"(--allow-duplicate to override)", file=sys.stderr)
                    return 2

    with open(out, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if not existed:
            w.writeheader()
        w.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())

    n = sum(1 for _ in open(out, encoding="utf-8")) - 1
    print(f"recorded #{n}: {row['verdict']}/{row['severity']} "
          f"{row['file']}::{row['symbol'] or '-'} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
