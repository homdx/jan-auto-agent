#!/usr/bin/env python3
"""Turn several reviewers' CSVs into one ranked, actionable report.

merge_validations.py answers "who said what". This answers "what do I fix, and
which reviewers can I trust", which is the question you actually have once the
run is finished.

Grouping is by file+symbol, because reviewers assign different task_ids to the
same code. A finding's standing comes from how its reviewers split, weighted by
one rule: a reviewer who never disagrees with anything has not verified, so
unanimity among reviewers who *do* disagree elsewhere is worth more than
unanimity among rubber stamps.

    python3 scripts/harvest_report.py validate1/*.csv --report harvest.md

Sections, in the order you want to read them:
  ACT       — confirmed by 2+ reviewers, or a NEW-* find with evidence
  DISPUTED  — reviewers split; a human decides
  FIXED     — real once, guarded now
  DISMISSED — agreed false positives; the list's noise floor
"""
import argparse
import csv
import glob
import os
import sys
from collections import Counter, defaultdict

SEV = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4, "": 5}
CONFIRMING = {"CONFIRMED"}
DISMISSING = {"FALSE_POSITIVE", "OUT_OF_SCOPE", "UNVERIFIABLE"}


def load(paths):
    rows = []
    for p in paths:
        base = os.path.basename(p).rsplit(".", 1)[0].lower()
        name = base.split("-", 2)[2] if base.count("-") >= 2 else base
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                r["_rv"] = name
                r["verdict"] = (r.get("verdict") or "").strip().upper()
                r["severity"] = (r.get("severity") or "").strip().upper()
                rows.append(r)
    return rows


def group(rows):
    g = defaultdict(list)
    for r in rows:
        f, s = (r.get("file") or "").strip(), (r.get("symbol") or "").strip()
        g[f"{f}::{s}" if f else (r.get("task_id") or "?")].append(r)
    return g


def classify(rs):
    v = Counter(r["verdict"] for r in rs)
    conf = sum(v[x] for x in CONFIRMING)
    fixed = v.get("ALREADY_FIXED", 0)
    dism = sum(v[x] for x in DISMISSING)
    is_new = any((r.get("task_id") or "").startswith("NEW-") for r in rs)
    if conf and dism:
        return "DISPUTED"
    if conf >= 2 or (conf and is_new):
        return "ACT"
    if conf == 1:
        return "DISPUTED" if len(rs) > 1 else "ACT"
    if fixed:
        return "FIXED"
    return "DISMISSED"


def worst(rs, only=None):
    sevs = [r["severity"] for r in rs if only is None or r["verdict"] in only]
    return sorted(sevs, key=lambda s: SEV.get(s, 5))[0] if sevs else ""


def best_line(rs, field, only=CONFIRMING):
    cand = [r for r in rs if r["verdict"] in only] or rs
    vals = [(r.get(field) or "").strip() for r in cand]
    return max(vals, key=len) if vals else ""


def scorecard(rows, reviewers):
    """Per-reviewer signal. Skepticism is the headline: a reviewer that
    confirms everything told you nothing, and one that dismisses everything
    is not reading either."""
    out = []
    for rv in reviewers:
        mine = [r for r in rows if r["_rv"] == rv]
        v = Counter(r["verdict"] for r in mine)
        conf = sum(v[x] for x in CONFIRMING)
        dism = sum(v[x] for x in DISMISSING)
        new = sum(1 for r in mine if (r.get("task_id") or "").startswith("NEW-"))
        noev = sum(1 for r in mine if r["verdict"] in CONFIRMING
                   and not (r.get("evidence") or "").strip())
        nodis = sum(1 for r in mine if r["verdict"] in CONFIRMING | {"ALREADY_FIXED"}
                    and not (r.get("disproof") or "").strip())
        out.append({
            "reviewer": rv, "rows": len(mine), "confirmed": conf,
            "fixed": v.get("ALREADY_FIXED", 0), "dismissed": dism, "new": new,
            "skepticism": dism / len(mine) if mine else 0.0,
            "no_evidence": noev, "no_disproof": nodis,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvs", nargs="+")
    ap.add_argument("--report", metavar="FILE.md", help="write the markdown report")
    ap.add_argument("--actions", metavar="FILE.csv", help="write just the ACT rows")
    a = ap.parse_args()

    paths = [p for pat in a.csvs for p in sorted(glob.glob(pat))] or a.csvs
    rows = load(paths)
    if not rows:
        print("no rows", file=sys.stderr)
        return 1
    reviewers = sorted({r["_rv"] for r in rows})
    groups = group(rows)

    buckets = defaultdict(list)
    for k, rs in groups.items():
        buckets[classify(rs)].append((k, rs))
    for b in buckets:
        buckets[b].sort(key=lambda x: (SEV.get(worst(x[1], CONFIRMING) or worst(x[1]), 5),
                                       -len(x[1])))

    L = []
    L.append("# Validation harvest\n")
    L.append(f"{len(reviewers)} reviewers · {len(groups)} distinct findings · "
             f"{len(rows)} judgements\n")
    L.append(f"Reviewers: {', '.join(reviewers)}\n")

    noise = len(buckets["DISMISSED"]) / len(groups) if groups else 0
    L.append(f"**Noise floor: {noise:.0%}** of findings were dismissed by every "
             f"reviewer who looked at them.\n")

    titles = {"ACT": "Act on these",
              "DISPUTED": "Disputed — a human decides",
              "FIXED": "Already fixed",
              "DISMISSED": "Dismissed"}
    for b in ("ACT", "DISPUTED", "FIXED", "DISMISSED"):
        items = buckets.get(b, [])
        L.append(f"\n## {titles[b]} ({len(items)})\n")
        if not items:
            L.append("_none_\n")
            continue
        if b == "DISMISSED":
            L.append("| finding | reviewers |\n|---|---|")
            for k, rs in items:
                L.append(f"| `{k}` | {len(rs)} |")
            L.append("")
            continue
        for k, rs in items:
            sev = worst(rs, CONFIRMING) or worst(rs)
            votes = ", ".join(f"{r['_rv']}={r['verdict'].split('_')[0].lower()}"
                              for r in sorted(rs, key=lambda r: r["_rv"]))
            new = " · **NEW**" if any((r.get("task_id") or "").startswith("NEW-")
                                      for r in rs) else ""
            L.append(f"### `{k}` — {sev}{new}\n")
            L.append(f"*{best_line(rs, 'title')}*\n")
            L.append(f"- **votes:** {votes}")
            for field, label in (("impact", "impact"), ("evidence", "evidence"),
                                 ("disproof", "disproof"), ("repro", "repro")):
                val = best_line(rs, field)
                if val and val.lower() != "none":
                    L.append(f"- **{label}:** {val[:400]}")
            L.append("")

    L.append("\n## Reviewer scorecard\n")
    L.append("| reviewer | rows | confirmed | fixed | dismissed | NEW | skepticism | "
             "no-evidence | no-disproof |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for s in sorted(scorecard(rows, reviewers), key=lambda s: -s["new"]):
        L.append(f"| {s['reviewer']} | {s['rows']} | {s['confirmed']} | {s['fixed']} | "
                 f"{s['dismissed']} | {s['new']} | {s['skepticism']:.0%} | "
                 f"{s['no_evidence']} | {s['no_disproof']} |")
    L.append("\n**skepticism** = share of findings this reviewer rejected. Near 100% "
             "with no NEW-* rows means it dismissed the list without reading the code; "
             "near 0% means it agreed with everything. The reviewers worth keeping "
             "reject most of a noisy list *and* still bring findings of their own.\n")
    L.append("**no-evidence / no-disproof** should both be 0 — the helper refuses "
             "rows without them, so anything above 0 is a file written by hand.\n")

    text = "\n".join(L)
    if a.report:
        open(a.report, "w", encoding="utf-8").write(text)
        print(f"report -> {a.report}")
    else:
        print(text)

    if a.actions:
        cols = ["finding", "severity", "title", "votes", "impact", "evidence", "repro"]
        with open(a.actions, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for k, rs in buckets.get("ACT", []):
                w.writerow({
                    "finding": k, "severity": worst(rs, CONFIRMING) or worst(rs),
                    "title": best_line(rs, "title"),
                    "votes": " ".join(sorted(r["_rv"] for r in rs if r["verdict"] in CONFIRMING)),
                    "impact": best_line(rs, "impact"),
                    "evidence": best_line(rs, "evidence"),
                    "repro": best_line(rs, "repro"),
                })
        print(f"action list -> {a.actions}")

    print(f"\nACT {len(buckets['ACT'])}  DISPUTED {len(buckets['DISPUTED'])}  "
          f"FIXED {len(buckets['FIXED'])}  DISMISSED {len(buckets['DISMISSED'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
