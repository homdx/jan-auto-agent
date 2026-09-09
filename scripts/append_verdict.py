#!/usr/bin/env python3
"""Record one adjudication: is this reported defect real?

Stage 2 reviewers judged a *proposal*. You are judging the *code*, and your
answer becomes ground truth that scores every reviewer and every future run — so
the bar is higher: an opinion is not admissible, only something you can point at.

    python3 scripts/append_verdict.py --out truth-<yourname>.csv \\
        --finding "tools/search_agent.py::_DEFAULT_SKIP_DIRS" \\
        --truth REAL --confidence HIGH \\
        --how "identity check: SearchAgent().skip_dirs is _DEFAULT_SKIP_DIRS -> True" \\
        --evidence "self.skip_dirs = _DEFAULT_SKIP_DIRS if skip_dirs is None else skip_dirs" \\
        --consequence "one append poisons every SearchAgent built later" \\
        --severity LOW --fix "bind list(_DEFAULT_SKIP_DIRS)"
"""
import argparse
import csv
import os
import sys

COLUMNS = ["finding", "truth", "checked_by", "confidence", "how", "evidence",
           "consequence", "severity", "fix", "notes"]
TRUTH = {"REAL", "FALSE", "FIXED", "UNDECIDED"}
SEV = {"CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE", ""}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    for c in COLUMNS:
        ap.add_argument("--" + c.replace("_", "-"), default="")
    a = vars(ap.parse_args())
    row = {c: str(a.get(c) or "").replace("\n", "; ").strip() for c in COLUMNS}
    out, t = a["out"], row["truth"].upper()

    problems = []
    if not row["finding"]:
        problems.append("--finding is required (copy the `file::symbol` from the queue)")
    if t not in TRUTH:
        problems.append(f"--truth must be one of {sorted(TRUTH)}, got {row['truth']!r}")
    if row["severity"].upper() not in SEV:
        problems.append(f"--severity must be one of {sorted(SEV - {''})} or omitted")
    if t in {"REAL", "FALSE", "FIXED"} and not row["how"]:
        problems.append("--how is required: the check you ran, not the conclusion you "
                        "reached. 'looks wrong' is not a check")
    if t == "REAL":
        if not row["evidence"]:
            problems.append("--evidence is required for REAL: quote the line that proves it")
        if not row["consequence"]:
            problems.append("--consequence is required for REAL: what goes wrong, and when. "
                            "A defect nobody can reach is real code and a NONE severity, "
                            "so say which")
    if t == "FIXED" and not row["evidence"]:
        problems.append("--evidence is required for FIXED: quote the guard that closes it")
    if problems:
        print("verdict rejected:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1

    row["truth"], row["severity"] = t, row["severity"].upper()

    d, base = os.path.dirname(out) or ".", os.path.basename(out)
    if os.path.isdir(d):
        twins = [f for f in os.listdir(d) if f.lower() == base.lower() and f != base]
        if twins:
            print(f"error: {os.path.join(d, twins[0])} differs only in case — use it.",
                  file=sys.stderr)
            return 1

    existed = os.path.exists(out) and os.path.getsize(out) > 0
    if existed:
        with open(out, newline="", encoding="utf-8") as fh:
            for prev in csv.DictReader(fh):
                if (prev.get("finding") or "").strip() == row["finding"]:
                    print(f"skipped: {row['finding']} is already decided as "
                          f"{prev.get('truth')} — move on.", file=sys.stderr)
                    return 2

    with open(out, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        if not existed:
            w.writeheader()
        w.writerow(row)
        fh.flush()
        os.fsync(fh.fileno())
    n = sum(1 for _ in open(out, encoding="utf-8")) - 1
    print(f"decided #{n}: {row['truth']} {row['finding']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
