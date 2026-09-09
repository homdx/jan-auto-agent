#!/usr/bin/env python3
"""Merge reviewer CSVs from VALIDATE-jan-findings.md and compare them.

Each reviewer model returns one CSV per variant. This groups rows by the thing
they describe (file + symbol, falling back to task_id) and shows where the
reviewers agree, where they split, and what only one of them saw.

The split rows are the point. Unanimous agreement mostly tells you the finding
was easy; a disagreement means one reviewer read the code and another read the
proposal, and you learn something either way.

Usage:
    merge_validations.py validation-v1-*.csv
    merge_validations.py --csv merged.csv validation-v1-*.csv

Pass the CSV paths unquoted so the shell expands the glob — this script does not
expand globs itself. --csv OUT writes a spreadsheet-style pivot: one row per
finding, one column per reviewer holding that reviewer's verdict, with
severity / agreement / vote-tally columns in front. See write_merged_csv below
for the full column list.
"""
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

COLUMNS = [
    "variant", "task_id", "title", "file", "symbol", "line_start", "line_end",
    "verdict", "severity", "defect_class", "caller_mutates", "impact", "repro",
    "evidence", "disproof", "fix_type", "effort", "confidence", "notes",
]
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4, "": 5}
VERDICTS = ["CONFIRMED", "ALREADY_FIXED", "FALSE_POSITIVE", "UNVERIFIABLE", "OUT_OF_SCOPE"]


def reviewer_name(path):
    """validation-v1-laguna.csv -> laguna"""
    base = os.path.basename(path).rsplit(".", 1)[0].lower()
    parts = base.split("-")
    return "-".join(parts[2:]) if len(parts) > 2 else base


def load(paths):
    rows, problems = [], []
    for p in paths:
        with open(p, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                problems.append(f"{os.path.basename(p)}: missing column(s) {missing}")
            for r in reader:
                r["_reviewer"] = reviewer_name(p)
                rows.append(r)
    return rows, problems


def key_of(r):
    """Group by what the row describes, not by the id the agent assigned."""
    f, sym = (r.get("file") or "").strip(), (r.get("symbol") or "").strip()
    if f:
        return f"{f}::{sym}" if sym else f
    return (r.get("task_id") or "?").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvs", nargs="+")
    ap.add_argument("--csv", metavar="OUT",
                    help="write a pivot: one row per finding, one column per "
                         "reviewer holding its verdict, plus severity / agreement / "
                         "confirmed / dismissed / fixed / task_ids / title")
    args = ap.parse_args()

    rows, problems = load(args.csvs)
    if problems:
        print("schema problems:")
        for p in problems:
            print(f"  ! {p}")
        print()
    if not rows:
        print("no rows")
        return 1

    reviewers = sorted({r["_reviewer"] for r in rows})
    groups = defaultdict(list)
    for r in rows:
        groups[key_of(r)].append(r)

    # ── per-reviewer behaviour ───────────────────────────────────────────────
    print(f"reviewers: {len(reviewers)}   findings: {len(groups)}   rows: {len(rows)}\n")
    print("Reviewer behaviour")
    print(f"  {'reviewer':22} {'rows':>5} " + " ".join(f"{v[:9]:>10}" for v in VERDICTS)
          + f" {'no-evidence':>12} {'no-disproof':>12}")
    for rv in reviewers:
        mine = [r for r in rows if r["_reviewer"] == rv]
        c = Counter(r.get("verdict", "").strip().upper() for r in mine)
        confirmed = [r for r in mine if r.get("verdict", "").strip().upper() == "CONFIRMED"]
        blind = sum(1 for r in confirmed if not (r.get("evidence") or "").strip())
        nodis = sum(1 for r in mine
                    if r.get("verdict", "").strip().upper() in ("CONFIRMED", "ALREADY_FIXED")
                    and not (r.get("disproof") or "").strip())
        print(f"  {rv:22} {len(mine):>5} "
              + " ".join(f"{c.get(v, 0):>10}" for v in VERDICTS)
              + f" {blind:>12} {nodis:>12}")
    print("\n  A reviewer whose verdicts are all CONFIRMED did not verify.")
    print("  no-evidence  : CONFIRMED rows with no quoted code.")
    print("  no-disproof  : rows where the reviewer never tried to falsify its own")
    print("                 verdict — the check that catches pattern-matching.\n")

    # ── agreement per finding ────────────────────────────────────────────────
    unanimous, split, singleton = [], [], []
    for k, rs in groups.items():
        verdicts = {r["_reviewer"]: (r.get("verdict") or "").strip().upper() for r in rs}
        distinct = set(verdicts.values())
        if len(rs) == 1 and len(reviewers) > 1:
            singleton.append((k, rs[0]))
        elif len(distinct) == 1:
            unanimous.append((k, rs, distinct.pop()))
        else:
            split.append((k, rs, verdicts))

    def worst_sev(rs):
        sevs = [(r.get("severity") or "").strip().upper() for r in rs]
        return sorted(sevs, key=lambda s: SEVERITY_ORDER.get(s, 5))[0]

    if split:
        print(f"── SPLIT VERDICTS ({len(split)}) — read these first " + "─" * 20)
        for k, rs, verdicts in sorted(split, key=lambda x: SEVERITY_ORDER.get(worst_sev(x[1]), 5)):
            print(f"\n  {k}   [worst severity: {worst_sev(rs) or '?'}]")
            for rv in sorted(verdicts):
                row = next(r for r in rs if r["_reviewer"] == rv)
                ev = (row.get("evidence") or "").strip().replace("\n", " ")
                print(f"    {rv:20} {verdicts[rv]:15} {(row.get('severity') or ''):9} "
                      f"{ev[:60]}")
        print()

    if unanimous:
        print(f"── AGREED ({len(unanimous)}) " + "─" * 45)
        for k, rs, v in sorted(unanimous, key=lambda x: SEVERITY_ORDER.get(worst_sev(x[1]), 5)):
            title = (rs[0].get("title") or "").strip()[:44]
            print(f"  {v:15} {worst_sev(rs) or '-':9} {k[:44]:46} {title}")
        print()

    if singleton:
        print(f"── SEEN BY ONE REVIEWER ({len(singleton)}) — incl. NEW-* finds " + "─" * 10)
        for k, r in sorted(singleton, key=lambda x: SEVERITY_ORDER.get(
                (x[1].get("severity") or "").strip().upper(), 5)):
            print(f"  {r['_reviewer']:20} {(r.get('verdict') or ''):15} "
                  f"{(r.get('severity') or ''):9} {r.get('task_id', ''):8} {k[:40]}")
        print("\n  A NEW-* row nobody else found is either the best result of the\n"
              "  run or a hallucination. There is no third option — check it.\n")

    if args.csv:
        write_merged_csv(args.csv, groups, reviewers, worst_sev)
        print(f"merged table -> {args.csv}")
    return 0


# ── the --csv output ─────────────────────────────────────────────────────────
# The on-screen report is grouped and scannable but you cannot sort or pivot it.
# --csv writes the same thing as a spreadsheet: ONE ROW PER FINDING, with every
# reviewer as its own column holding that reviewer's verdict. Read a row left to
# right to see who said what; sort by `severity`, filter `agreement == SPLIT`.
#
#   finding     the grouping key — "path::symbol", or the task_id when the row
#               named no file. One finding = one row here.
#   severity    worst severity anyone gave it (CRITICAL > HIGH > MEDIUM > LOW > NONE).
#   agreement   SPLIT     — reviewers gave 2+ distinct verdicts. Read these.
#               UNANIMOUS — everyone who looked gave the same verdict.
#               SOLO      — only one reviewer judged it (a NEW-* discovery, or a
#                           list entry nobody else reached).
#   reviewers   how many distinct reviewers judged this finding.
#   confirmed / dismissed / fixed
#               vote tally. dismissed = FALSE_POSITIVE + OUT_OF_SCOPE +
#               UNVERIFIABLE; fixed = ALREADY_FIXED. confirmed+dismissed+fixed
#               can exceed `reviewers` when the source list carried the same
#               symbol under two task-ids and a reviewer judged both.
#   task_ids    the IMPROVEMENTS.md id(s) that map onto this finding.
#   title       the longest title any reviewer gave it.
#   <one column per reviewer>
#               that reviewer's verdict, or blank if they never judged it.
#               "A|B" means they judged it twice (two task-ids) and disagreed
#               with themselves — that is also what makes the tallies exceed
#               `reviewers`.
#
# Rows are ordered: severity, then SPLIT before SOLO before UNANIMOUS, then
# finding — so the contested, high-severity findings are at the top.
MERGED_META = ["finding", "severity", "agreement", "reviewers",
               "confirmed", "dismissed", "fixed", "task_ids", "title"]
_DISMISS_VERDICTS = {"FALSE_POSITIVE", "OUT_OF_SCOPE", "UNVERIFIABLE"}
_AGREEMENT_ORDER = {"SPLIT": 0, "SOLO": 1, "UNANIMOUS": 2}


def write_merged_csv(path, groups, reviewers, worst_sev):
    def row_for(k, rs):
        by_rv = defaultdict(list)
        for r in rs:
            by_rv[r["_reviewer"]].append((r.get("verdict") or "").strip().upper())
        tally = Counter(v for vs in by_rv.values() for v in vs)
        if len(by_rv) == 1 and len(reviewers) > 1:
            agreement = "SOLO"
        else:
            agreement = "SPLIT" if len(set(tally) - {""}) > 1 else "UNANIMOUS"
        out = {
            "finding": k,
            "severity": worst_sev(rs) or "",
            "agreement": agreement,
            "reviewers": len(by_rv),
            "confirmed": tally.get("CONFIRMED", 0),
            "dismissed": sum(tally[v] for v in _DISMISS_VERDICTS),
            "fixed": tally.get("ALREADY_FIXED", 0),
            "task_ids": " ".join(sorted({(r.get("task_id") or "").strip()
                                         for r in rs if (r.get("task_id") or "").strip()})),
            "title": max(((r.get("title") or "").strip() for r in rs),
                         key=len, default=""),
        }
        for rv in reviewers:
            out[rv] = "|".join(sorted(set(by_rv.get(rv, [])))) if rv in by_rv else ""
        return out

    rows = [row_for(k, rs) for k, rs in groups.items()]
    rows.sort(key=lambda r: (SEVERITY_ORDER.get(r["severity"], 5),
                             _AGREEMENT_ORDER.get(r["agreement"], 3), r["finding"]))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MERGED_META + list(reviewers),
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    sys.exit(main())
