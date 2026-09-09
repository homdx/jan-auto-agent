#!/usr/bin/env python3
"""Turn adjudicated REAL findings into ready-to-file task tickets.

The pipeline's output is only worth something if it ends in work someone can
pick up. This takes the settled ground truth plus the reviewer detail behind it
and writes one ticket per confirmed defect, in the shape this repo's own fix
workflow expects: verify first, one commit per bug, a regression test in
tests_bugfix/, all four pytest roots run separately.

    python3 scripts/make_jira_tasks.py \\
        --truth validate1/truth.csv \\
        --findings validate1/validation-*.csv \\
        --verdicts validate1/truth-*.csv \\
        --out tasks/

Writes tasks/<n>-<slug>.md plus tasks/INDEX.md.
"""
import argparse
import csv
import glob
import os
import re
import sys
from collections import defaultdict

SEV = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4, "": 5}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48]


def read(paths, key="finding"):
    out = defaultdict(list)
    for pat in paths or []:
        for p in glob.glob(pat):
            with open(p, newline="", encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                for r in rd:
                    k = (r.get(key) or "").strip()
                    if not k and r.get("file"):
                        k = f"{r['file'].strip()}::{(r.get('symbol') or '').strip()}"
                    if k:
                        r["_src"] = os.path.basename(p)
                        out[k].append(r)
    return out


def longest(rs, f):
    return max(((r.get(f) or "").strip() for r in rs), key=len, default="")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", required=True)
    ap.add_argument("--findings", nargs="*", help="stage-2 reviewer CSVs (for detail)")
    ap.add_argument("--verdicts", nargs="*", help="stage-3 adjudicator CSVs (for detail)")
    ap.add_argument("--out", default="tasks")
    a = ap.parse_args()

    with open(a.truth, newline="", encoding="utf-8") as fh:
        truth_rows = list(csv.DictReader(fh))
    # The same defect often gets reported at several symbols — the constant, the
    # constructor that binds it, the attribute it lands on. Those are one bug and
    # one ticket. A `duplicate_of` column naming the canonical finding folds the
    # aliases in rather than filing the same work three times.
    aliases = defaultdict(list)
    real = []
    for r in truth_rows:
        if (r.get("truth") or "").strip().upper() != "REAL":
            continue
        canon = (r.get("duplicate_of") or "").strip()
        if canon:
            aliases[canon].append(r["finding"])
        else:
            real.append(r)
    if not real:
        print("no REAL findings in the ground truth — nothing to file")
        return 0

    detail = read(a.findings)
    verdict = read(a.verdicts)
    os.makedirs(a.out, exist_ok=True)

    real.sort(key=lambda r: SEV.get(
        longest(verdict.get(r["finding"], []) + detail.get(r["finding"], []), "severity").upper(), 5))

    index = ["# Task index\n",
             f"{len(real)} confirmed defect(s), highest severity first.\n",
             "| # | severity | finding | ticket |", "|---|---|---|---|"]

    for i, row in enumerate(real, 1):
        k = row["finding"]
        d, v = detail.get(k, []), verdict.get(k, [])
        both = v + d
        file_, _, symbol = k.partition("::")
        sev = longest(both, "severity").upper() or "MEDIUM"
        title = longest(d, "title") or f"Fix {symbol or file_}"
        name = f"{i:02d}-{slug(symbol or os.path.basename(file_))}.md"

        alias_note = aliases.get(k, [])
        T = [f"# {title}\n",
             f"**Severity:** {sev}  ",
             f"**File:** `{file_}`  ",
             f"**Symbol:** `{symbol or '—'}`  ",
             f"**Status:** confirmed by adjudication ({row.get('checked_by', '?')})  ",
             (f"**Also reported as:** " + ", ".join(f"`{x}`" for x in alias_note) + "\n"
              if alias_note else "\n"),
             "## The defect\n",
             (longest(both, "consequence") or longest(both, "impact")
              or (row.get("how") or "").strip()
              or "_not stated — read the evidence below_") + "\n",
             "## Evidence\n", "```python", longest(both, "evidence") or "—", "```\n"]

        if longest(both, "repro"):
            T += ["## Reproduction\n", "```", longest(both, "repro"), "```\n"]
        # Prefer a stage-3 adjudicator's write-up; fall back to the one-line
        # `how` recorded straight in truth.csv when no truth-*.csv was supplied.
        how = longest(v, "how") or (row.get("how") or "").strip()
        if how:
            T += ["## How it was verified\n", how + "\n"]
        if longest(both, "disproof"):
            T += ["## What was checked to try to disprove it\n",
                  longest(both, "disproof") + "\n"]
        if longest(both, "fix"):
            T += ["## Suggested fix\n", longest(both, "fix") + "\n"]

        T += ["## Acceptance\n",
              "- [ ] Verify the defect against the live code before changing anything — "
              "these reports go stale faster than anyone updates them.",
              "- [ ] Check every caller of the symbol before altering what it returns.",
              "- [ ] Fix, with a regression test in `tests_bugfix/` that fails without it.",
              "- [ ] All four pytest roots, run separately:",
              "  ```bash",
              "  for d in tests tests_bugfix .smoke_tests .regression_tests; do "
              "python3 -m pytest \"$d\" -q --timeout=180; done",
              "  ```",
              "- [ ] One local commit for this bug alone.\n",
              "## Provenance\n",
              f"- reported by: {', '.join(sorted({r['_src'] for r in d})) or '—'}",
              f"- adjudicated by: {', '.join(sorted({r['_src'] for r in v})) or row.get('checked_by', '') or '—'}",
              f"- ground truth: `{a.truth}`\n"]

        open(os.path.join(a.out, name), "w", encoding="utf-8").write("\n".join(T))
        index.append(f"| {i} | {sev} | `{k}` | [{name}]({name}) |")
        print(f"  {name}")

    index += ["", "## Working these", "",
              "One ticket per commit. Verify before fixing — the pipeline that produced "
              "these measured an 88% noise floor on its own proposals, and adjudication "
              "narrowed that but does not replace reading the code."]
    open(os.path.join(a.out, "INDEX.md"), "w", encoding="utf-8").write("\n".join(index))
    print(f"\n{len(real)} ticket(s) -> {a.out}/  (INDEX.md written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
