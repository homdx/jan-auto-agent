#!/usr/bin/env python3
"""Merge adjudicator verdicts into one ground truth, and report the splits.

Stage 3 runs several adjudicators over the same queue. Where they agree, the
answer is settled and becomes truth.csv — the scoring key for every reviewer and
every future run. Where they disagree, no amount of voting helps: the code
either does the thing or it does not, so a split is escalated to a human rather
than resolved by majority.

    python3 scripts/truth_consensus.py validate1/truth-*.csv \\
        --truth validate1/truth.csv --report validate1/adjudication.md

Exit code 4 when anything is left disputed — useful in a script. Exit code 2 when a row's truth is
no verdict (REAL, FALSE, FIXED; UNDECIDED or a blank cell decides nothing), or when --truth would
lose rows of an existing file that this run does not settle (--replace writes it anyway).
"""
import argparse
import csv
import glob
import os
import sys
from collections import Counter, defaultdict

SEV = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4, "": 5}
NEEDED = {"finding", "truth"}
#: what settles a finding; UNDECIDED (and a blank cell) is a row that decides nothing
VERDICTS = ("REAL", "FALSE", "FIXED")
TRUTH_COLUMNS = ["finding", "truth", "checked_by", "how"]


def load(paths):
    """Every verdict row, its adjudicator in ``_adj``, its file and line in ``_at``. A blank truth is
    UNDECIDED: it settled as the verdict "" (two blank rows "agreed"); see `no_verdicts`."""
    rows, skipped, nameless = [], [], []
    for p in paths:
        name = os.path.basename(p).rsplit(".", 1)[0].lower()
        name = name[6:] if name.startswith("truth-") else name
        with open(p, newline="", encoding="utf-8-sig") as fh:   # a spreadsheet's BOM: "\ufefffinding"
            rd = csv.DictReader(fh)
            if not NEEDED.issubset(set(rd.fieldnames or [])):
                skipped.append(os.path.basename(p)); continue
            for r in rd:
                if not (r.get("finding") or "").strip():
                    # a verdict without a finding settled as the finding "" (truth.csv got ",REAL,a+b,x")
                    if (r.get("truth") or "").strip():
                        nameless.append(f"{p}:{rd.line_num}")
                    continue
                r["_adj"] = name
                r["_at"] = f"{p}:{rd.line_num}"
                r["truth"] = (r.get("truth") or "").strip().upper() or "UNDECIDED"
                rows.append(r)
    if skipped:
        print(f"skipped (not verdict CSVs): {', '.join(skipped)}", file=sys.stderr)
    if nameless:
        print(f"skipped (a verdict with no finding): {', '.join(nameless)}", file=sys.stderr)
    return rows


def no_verdicts(rows) -> list:
    """``file:line: 'WORD'`` of each row whose truth is no verdict: MAYBE settled as one, and so did a second
    header line of a file (``TRUTH``, append_verdict's doubled header)."""
    return [f"{r['_at']}: {r['truth']!r}" for r in rows if r["truth"] not in VERDICTS and r["truth"] != "UNDECIDED"]


def longest(rs, field):
    return max(((r.get(field) or "").strip() for r in rs), key=len, default="")


def severity(rs):
    """The most severe rating any adjudicator gave, upper-cased; "" when none rated it.

    Not `longest`: that picks the longest *word*, so MEDIUM beat HIGH and the order of the
    files decided a tie.
    """
    return min(((r.get("severity") or "").strip().upper() for r in rs),
               key=lambda rating: SEV.get(rating, SEV[""]), default="")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvs", nargs="+")
    ap.add_argument("--truth", metavar="FILE.csv", help="write the settled ground truth")
    ap.add_argument("--replace", action="store_true",
                    help="write --truth even when that drops rows of it this run does not settle (it is "
                         "written from the adjudicators' files alone; without this the run refuses)")
    ap.add_argument("--report", metavar="FILE.md")
    ap.add_argument("--arbiter", metavar="NAME",
                    help="whose verdict settles a split (e.g. the human who read the "
                         "code). Without one, splits stay open — which is correct, "
                         "because a majority of models can be confidently wrong together")
    a = ap.parse_args()
    # adjudicators are named from the file stem, lower-cased (truth-Bob.csv -> bob)
    if a.arbiter:
        a.arbiter = a.arbiter.strip().lower()

    paths, unmatched = [], []
    for pat in a.csvs:
        # A file with that name is that file; glob patterns apply only when no file matches.
        hits = [pat] if os.path.isfile(pat) else sorted(glob.glob(pat))
        paths += hits
        if not hits:
            unmatched.append(pat)
    if unmatched:
        # the pattern itself was opened: a FileNotFoundError traceback
        print(f"no file matches: {', '.join(unmatched)}", file=sys.stderr)
        return 2
    rows = load(paths)
    bad = no_verdicts(rows)
    if bad:
        print("not a verdict (REAL, FALSE, FIXED, or UNDECIDED for none):\n  " + "\n  ".join(bad[:20])
              + (f"\n  … and {len(bad) - 20} more" if len(bad) > 20 else ""), file=sys.stderr)
        return 2
    if not rows:
        print("no verdict rows", file=sys.stderr)
        return 1
    adjudicators = sorted({r["_adj"] for r in rows})

    g = defaultdict(list)
    for r in rows:
        g[(r.get("finding") or "").strip()].append(r)

    settled, disputed, arbitrated, torn = {}, {}, {}, {}
    for k, rs in g.items():
        # Two verdicts from one adjudicator (REAL and FALSE on one place) are UNDECIDED and reported; row order must not decide.
        said: dict = defaultdict(set)
        for r in rs:
            if r["truth"] != "UNDECIDED":
                said[r["_adj"]].add(r["truth"])
        votes = {r["_adj"]: "UNDECIDED" for r in rs}
        for adj, vs in said.items():
            if len(vs) == 1:
                votes[adj] = next(iter(vs))
            else:
                torn.setdefault(k, {})[adj] = sorted(vs)
        distinct = {v for v in votes.values() if v != "UNDECIDED"}
        if len(distinct) == 1:
            verdict = distinct.pop()
            # Only the adjudicators who said it are listed; an UNDECIDED one is not shown as agreeing.
            settled[k] = (verdict, [r for r in rs if r["truth"] == verdict and votes[r["_adj"]] == verdict])
        elif distinct:
            # An arbiter breaks ties: two models agreeing is not evidence when both applied the same wrong rule.
            if a.arbiter and a.arbiter in votes and votes[a.arbiter] != "UNDECIDED":
                settled[k] = (votes[a.arbiter], [r for r in rs if r["truth"] == votes[a.arbiter]
                                                 and votes[r["_adj"]] == votes[a.arbiter]])
                arbitrated[k] = (votes[a.arbiter], votes)
            else:
                disputed[k] = (votes, rs)
        else:
            # Every vote UNDECIDED: left open for a human, neither settled nor dropped from the report.
            disputed[k] = (votes, rs)

    L = ["# Adjudication\n",
         f"{len(adjudicators)} adjudicators · {len(g)} findings · "
         f"{len(settled)} settled · {len(disputed)} disputed\n",
         f"Adjudicators: {', '.join(adjudicators)}\n"]

    real = [(k, rs) for k, (t, rs) in settled.items() if t == "REAL"]
    real.sort(key=lambda x: SEV.get(severity(x[1]), SEV[""]))
    L.append(f"\n## Confirmed real ({len(real)}) — these become work\n")
    if not real:
        L.append("_none_\n")
    for k, rs in real:
        L.append(f"### `{k}` — {severity(rs) or 'unrated'}\n")
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

    if arbitrated:
        L.append(f"\n## Settled by the arbiter ({len(arbitrated)})\n")
        L.append(f"`{a.arbiter}` read the code and broke these ties. The other votes are "
                 "kept because a reviewer that is reliably wrong on split questions is "
                 "worth knowing about.\n")
        L.append("| finding | arbiter | the other votes |\n|---|---|---|")
        for k, (chosen, votes) in arbitrated.items():
            others = ", ".join(f"{n}={v}" for n, v in sorted(votes.items()) if n != a.arbiter)
            L.append(f"| `{k}` | **{chosen}** | {others} |")
        L.append("")

    if torn:
        L.append(f"\n## Contradicting themselves ({len(torn)})\n")
        L.append("An adjudicator's file says more than one verdict for these: that adjudicator counts as "
                 "UNDECIDED on them. Settle the file by hand (claimcheck_truth writes both rows when two "
                 "claims about one place disagree).\n")
        L.append("| finding | adjudicator: verdicts |\n|---|---|")
        for k, who in sorted(torn.items()):
            L.append(f"| `{k}` | " + "; ".join(f"{n}: {'/'.join(v)}" for n, v in sorted(who.items())) + " |")
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
    if arbitrated:
        L.append("\n### On the arbitrated questions\n")
        L.append("| adjudicator | agreed with arbiter |\n|---|---|")
        for adj in adjudicators:
            if adj == a.arbiter:
                continue
            hit = sum(1 for k, (chosen, votes) in arbitrated.items()
                      if votes.get(adj) == chosen)
            seen = sum(1 for k, (_, votes) in arbitrated.items() if adj in votes)
            if seen:
                L.append(f"| {adj} | {hit}/{seen} |")
        L.append("\nThese are the questions the models split on, so this column "
                 "separates them far better than overall agreement does.\n")

    L.append("\nAgreement with consensus is not accuracy — consensus can be wrong "
             "together. It only flags an adjudicator that is reliably out of step, "
             "which is worth reading before trusting it.\n")

    # The truth file is rebuilt from the adjudicators' files only; rows no adjudicator settles this run, and columns this run does not write, are kept.
    old_rows, old_cols = {}, []
    if a.truth and os.path.isfile(a.truth):
        with open(a.truth, newline="", encoding="utf-8-sig") as fh:
            rd = csv.DictReader(fh)
            old_cols = [c for c in (rd.fieldnames or []) if c and c not in TRUTH_COLUMNS]
            old_rows = {(r.get("finding") or "").strip(): r for r in rd if (r.get("finding") or "").strip()}
        lost = sorted(f for f in old_rows if f not in settled)
        if lost and not a.replace:
            print(f"{a.truth}: {len(lost)} of its rows are not settled by this run (first: {lost[0]}); writing it "
                  "would delete them. Put them in an adjudicator's file, or pass --replace.", file=sys.stderr)
            return 2

    text = "\n".join(L)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"report -> {a.report}")
    else:
        print(text)

    if a.truth:
        with open(a.truth, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=TRUTH_COLUMNS + old_cols)
            w.writeheader()
            for k, (t, rs) in sorted(settled.items()):
                w.writerow({**{c: old_rows.get(k, {}).get(c) or "" for c in old_cols},
                            "finding": k, "truth": t,
                            "checked_by": "+".join(sorted(r["_adj"] for r in rs)),
                            "how": longest(rs, "how")})
        print(f"ground truth -> {a.truth}  ({len(settled)} settled)")

    print(f"\nREAL {len(real)}  settled {len(settled)}  disputed {len(disputed)}")
    return 4 if disputed else 0


if __name__ == "__main__":
    sys.exit(main())
