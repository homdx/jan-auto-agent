#!/usr/bin/env python3
"""Merge adjudicator verdicts into one ground truth, and report the splits.

Stage 3 runs several adjudicators over the same queue. Where they agree, the
answer is settled and becomes truth.csv — the scoring key for every reviewer and
every future run. Where they disagree, no amount of voting helps: the code
either does the thing or it does not, so a split is escalated to a human rather
than resolved by majority.

    python3 scripts/truth_consensus.py validate1/truth-*.csv \\
        --truth validate1/truth.csv --report validate1/adjudication.md

Exit code 4 when anything is left disputed — useful in a script.
"""
import argparse
import csv
import glob
import os
import sys
from collections import Counter, defaultdict

SEV = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4, "": 5}
NEEDED = {"finding", "truth"}


def load(paths):
    rows, skipped = [], []
    for p in paths:
        name = os.path.basename(p).rsplit(".", 1)[0].lower()
        name = name[6:] if name.startswith("truth-") else name
        with open(p, newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            if not NEEDED.issubset(set(rd.fieldnames or [])):
                skipped.append(os.path.basename(p)); continue
            for r in rd:
                r["_adj"] = name
                r["truth"] = (r.get("truth") or "").strip().upper()
                rows.append(r)
    if skipped:
        print(f"skipped (not verdict CSVs): {', '.join(skipped)}", file=sys.stderr)
    return rows


def longest(rs, field):
    return max(((r.get(field) or "").strip() for r in rs), key=len, default="")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvs", nargs="+")
    ap.add_argument("--truth", metavar="FILE.csv", help="write the settled ground truth")
    ap.add_argument("--report", metavar="FILE.md")
    a = ap.parse_args()

    paths = [p for pat in a.csvs for p in sorted(glob.glob(pat))] or a.csvs
    rows = load(paths)
    if not rows:
        print("no verdict rows", file=sys.stderr)
        return 1
    adjudicators = sorted({r["_adj"] for r in rows})

    g = defaultdict(list)
    for r in rows:
        g[(r.get("finding") or "").strip()].append(r)

    settled, disputed = {}, {}
    for k, rs in g.items():
        votes = {r["_adj"]: r["truth"] for r in rs}
        distinct = {v for v in votes.values() if v != "UNDECIDED"}
        if len(distinct) == 1:
            settled[k] = (distinct.pop(), rs)
        elif distinct:
            disputed[k] = (votes, rs)

    L = [f"# Adjudication\n",
         f"{len(adjudicators)} adjudicators · {len(g)} findings · "
         f"{len(settled)} settled · {len(disputed)} disputed\n",
         f"Adjudicators: {', '.join(adjudicators)}\n"]

    real = [(k, rs) for k, (t, rs) in settled.items() if t == "REAL"]
    real.sort(key=lambda x: SEV.get(longest(x[1], "severity").upper(), 5))
    L.append(f"\n## Confirmed real ({len(real)}) — these become work\n")
    if not real:
        L.append("_none_\n")
    for k, rs in real:
        L.append(f"### `{k}` — {longest(rs, 'severity') or 'unrated'}\n")
        L.append(f"- **consequence:** {longest(rs, 'consequence')}")
        L.append(f"- **evidence:** {longest(rs, 'evidence')}")
        L.append(f"- **how checked:** {longest(rs, 'how')}")
        if longest(rs, "fix"):
            L.append(f"- **suggested fix:** {longest(rs, 'fix')}")
        L.append(f"- agreed by: {', '.join(sorted(r['_adj'] for r in rs))}\n")

    for label, want in (("Confirmed false", "FALSE"), ("Already fixed", "FIXED")):
        items = [(k, rs) for k, (t, rs) in settled.items() if t == want]
        L.append(f"\n## {label} ({len(items)})\n")
        L.append("| finding | how it was checked |\n|---|---|" if items else "_none_\n")
        for k, rs in items:
            L.append(f"| `{k}` | {longest(rs, 'how')[:160]} |")
        if items:
            L.append("")

    L.append(f"\n## Disputed ({len(disputed)}) — a human decides\n")
    if not disputed:
        L.append("_none_\n")
    for k, (votes, rs) in disputed.items():
        L.append(f"### `{k}`\n")
        for adj in sorted(votes):
            r = next(x for x in rs if x["_adj"] == adj)
            L.append(f"- **{adj}: {votes[adj]}** — {(r.get('how') or '')[:220]}")
        L.append("\nThe code either does this or it does not; a split means one side "
                 "did not run the check it claims. Read it yourself.\n")

    L.append("\n## Adjudicator scorecard\n")
    L.append("| adjudicator | verdicts | REAL | FALSE | FIXED | undecided | "
             "agreed with consensus |")
    L.append("|---|---|---|---|---|---|---|")
    for adj in adjudicators:
        mine = [r for r in rows if r["_adj"] == adj]
        c = Counter(r["truth"] for r in mine)
        agree = sum(1 for r in mine
                    if r["finding"] in settled and settled[r["finding"]][0] == r["truth"])
        scored = sum(1 for r in mine if r["finding"] in settled)
        L.append(f"| {adj} | {len(mine)} | {c.get('REAL',0)} | {c.get('FALSE',0)} | "
                 f"{c.get('FIXED',0)} | {c.get('UNDECIDED',0)} | "
                 f"{agree}/{scored}" + (f" ({agree/scored:.0%})" if scored else "") + " |")
    L.append("\nAgreement with consensus is not accuracy — consensus can be wrong "
             "together. It only flags an adjudicator that is reliably out of step, "
             "which is worth reading before trusting it.\n")

    text = "\n".join(L)
    if a.report:
        open(a.report, "w", encoding="utf-8").write(text)
        print(f"report -> {a.report}")
    else:
        print(text)

    if a.truth:
        with open(a.truth, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["finding", "truth", "checked_by", "how"])
            w.writeheader()
            for k, (t, rs) in sorted(settled.items()):
                w.writerow({"finding": k, "truth": t,
                            "checked_by": "+".join(sorted(r["_adj"] for r in rs)),
                            "how": longest(rs, "how")})
        print(f"ground truth -> {a.truth}  ({len(settled)} settled)")

    print(f"\nREAL {len(real)}  settled {len(settled)}  disputed {len(disputed)}")
    return 4 if disputed else 0


if __name__ == "__main__":
    sys.exit(main())
