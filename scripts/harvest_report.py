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
#: A verdict is only "right" against something. --truth supplies that: a CSV of
#: finding,truth where truth is REAL (the defect is genuinely there), FALSE (it
#: is not) or FIXED (was real, now guarded). Only findings a human actually
#: checked belong in it — an unscored finding is left out of the accuracy maths
#: rather than counted as a pass, so a reviewer cannot gain by guessing.
TRUTH_VALUES = {"REAL", "FALSE", "FIXED"}
CONFIRMING = {"CONFIRMED"}
DISMISSING = {"FALSE_POSITIVE", "OUT_OF_SCOPE", "UNVERIFIABLE"}


#: A reviewer CSV must have these. Anything else in the directory — the truth
#: file, an exported action list, a previous report — is skipped rather than
#: mistaken for a sixth reviewer, which is what a bare *.csv glob would do.
REQUIRED_COLS = {"task_id", "verdict", "severity", "file"}


def load(paths):
    rows, skipped = [], []
    for p in paths:
        base = os.path.basename(p).rsplit(".", 1)[0].lower()
        name = base.split("-", 2)[2] if base.count("-") >= 2 else base
        with open(p, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if not REQUIRED_COLS.issubset(set(reader.fieldnames or [])):
                skipped.append(os.path.basename(p))
                continue
            for r in reader:
                r["_rv"] = name
                r["verdict"] = (r.get("verdict") or "").strip().upper()
                r["severity"] = (r.get("severity") or "").strip().upper()
                rows.append(r)
    if skipped:
        print(f"skipped (not reviewer CSVs): {', '.join(skipped)}", file=sys.stderr)
    return rows


def load_truth(path):
    if not path:
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            t = (r.get("truth") or "").strip().upper()
            if t in TRUTH_VALUES:
                out[(r.get("finding") or "").strip()] = t
    return out


def verdict_matches(verdict, truth):
    """Did this reviewer get it right? None when the finding is unscored."""
    if truth is None:
        return None
    if truth == "REAL":
        return verdict in CONFIRMING
    if truth == "FALSE":
        return verdict in DISMISSING
    return verdict == "ALREADY_FIXED"          # FIXED


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


def scorecard(rows, reviewers, groups=None, truth=None):
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
        solo = right = wrong = 0
        if groups:
            for k, rs in groups.items():
                if len(rs) == 1 and rs[0]["_rv"] == rv:
                    solo += 1
        if truth:
            for k, rs in (groups or {}).items():
                t = truth.get(k)
                for r in rs:
                    if r["_rv"] != rv:
                        continue
                    ok = verdict_matches(r["verdict"], t)
                    if ok is True:
                        right += 1
                    elif ok is False:
                        wrong += 1
        out.append({
            "reviewer": rv, "rows": len(mine), "confirmed": conf,
            "fixed": v.get("ALREADY_FIXED", 0), "dismissed": dism, "new": new,
            "skepticism": dism / len(mine) if mine else 0.0,
            "no_evidence": noev, "no_disproof": nodis,
            "solo": solo, "right": right, "wrong": wrong,
            "accuracy": right / (right + wrong) if (right + wrong) else None,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csvs", nargs="+")
    ap.add_argument("--report", metavar="FILE.md", help="write the markdown report")
    ap.add_argument("--actions", metavar="FILE.csv", help="write just the ACT rows")
    ap.add_argument("--truth", metavar="FILE.csv",
                    help="ground truth (finding,truth) to score reviewers against")
    ap.add_argument("--solo", metavar="FILE.csv", help="write the solo-findings table")
    ap.add_argument("--pending", metavar="FILE.md",
                    help="write the self-contained verification queue: findings that "
                         "are not dismissed and not yet in --truth")
    a = ap.parse_args()

    paths = [p for pat in a.csvs for p in sorted(glob.glob(pat))] or a.csvs
    rows = load(paths)
    if not rows:
        print("no rows", file=sys.stderr)
        return 1
    reviewers = sorted({r["_rv"] for r in rows})
    groups = group(rows)
    truth = load_truth(a.truth)

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

    # ── solo findings ────────────────────────────────────────────────────────
    # A finding only one reviewer recorded. Two very different things live here
    # and they must not be averaged: a NEW-* nobody else was looking for (the
    # discovery this whole exercise is for) and a list entry nobody else got to
    # (a coverage gap, which says nothing about anyone's judgement). One
    # reviewer seeing something is weak evidence about the finding, but strong
    # evidence about the reviewer — so this table is read per model, not summed.
    solo = [(k, rs[0]) for k, rs in groups.items() if len(rs) == 1]
    solo_new = [(k, r) for k, r in solo if (r.get("task_id") or "").startswith("NEW-")]
    solo_list = [(k, r) for k, r in solo
                 if not (r.get("task_id") or "").startswith("NEW-")
                 and r["verdict"] in CONFIRMING | {"ALREADY_FIXED"}]

    L.append(f"\n## Solo findings ({len(solo_new)} discoveries, "
             f"{len(solo_list)} unshared judgements)\n")
    L.append("Found or judged by exactly one reviewer. A single vote is weak evidence "
             "about the finding and strong evidence about the reviewer: whoever brings "
             "a solo find that verifies is doing something the others are not.\n")

    def solo_table(items, heading, note):
        L.append(f"### {heading}\n")
        if not items:
            L.append("_none_\n")
            return
        L.append(f"{note}\n")
        L.append("| finding | reviewer | verdict | severity | truth | title |")
        L.append("|---|---|---|---|---|---|")
        for k, r in sorted(items, key=lambda x: SEV.get(x[1]["severity"], 5)):
            t = truth.get(k, "")
            mark = {"REAL": "REAL ✅", "FALSE": "FALSE ✗", "FIXED": "FIXED"}.get(t, "_unchecked_")
            L.append(f"| `{k}` | {r['_rv']} | {r['verdict']} | {r['severity']} | {mark} | "
                     f"{(r.get('title') or '')[:70]} |")
        L.append("")

    solo_table(solo_new, "Discoveries — not on the list at all",
               "These are the reason to run several reviewers. Check each one by hand: "
               "a solo NEW is either the best result of the run or a hallucination.")
    solo_table(solo_list, "Unshared judgements — nobody else reached this entry",
               "Usually a coverage gap rather than insight. Worth re-running another "
               "reviewer over these ids before trusting a lone verdict.")

    L.append("\n## Reviewer scorecard\n")
    L.append("| reviewer | rows | confirmed | fixed | dismissed | NEW | solo | "
             "skepticism | accuracy | no-evidence | no-disproof |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    cards = scorecard(rows, reviewers, groups, truth)
    for c in sorted(cards, key=lambda c: (-(c["accuracy"] if c["accuracy"] is not None else -1),
                                          -c["new"], -c["rows"])):
        acc = (f"{c['accuracy']:.0%} ({c['right']}/{c['right']+c['wrong']})"
               if c["accuracy"] is not None else "—")
        L.append(f"| {c['reviewer']} | {c['rows']} | {c['confirmed']} | {c['fixed']} | "
                 f"{c['dismissed']} | {c['new']} | {c['solo']} | {c['skepticism']:.0%} | "
                 f"{acc} | {c['no_evidence']} | {c['no_disproof']} |")
    L.append("\n**skepticism** = share of findings this reviewer rejected. Near 100% "
             "with no NEW-* rows means it dismissed the list without reading the code; "
             "near 0% means it agreed with everything. The reviewers worth keeping "
             "reject most of a noisy list *and* still bring findings of their own.\n")
    L.append("**accuracy** is scored only against findings listed in `--truth`, so it "
             "measures the checked subset and cannot be gained by guessing. Rank on "
             "accuracy first, then on NEW — a reviewer that is right about a noisy list "
             "and still discovers something is the one to keep.\n")
    L.append("**solo** counts findings only this reviewer recorded — discoveries and "
             "coverage gaps together; see the solo table for the split.\n")
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

    # ── the verification queue ───────────────────────────────────────────────
    # A finding one reviewer confirmed and nobody contradicted is not a result.
    # It is a question: real bug, or plausible-sounding false positive? Nothing
    # in the run can answer that — only reading the code can. This file is that
    # question list, written to stand alone so it can be carried into a separate
    # session with no CSVs, no reviewer names to weigh, and no report to re-read.
    if a.pending:
        pend = []
        for k, rs in groups.items():
            if truth.get(k):
                continue                       # already decided by a human
            live = [r for r in rs if r["verdict"] in CONFIRMING]
            if not live:
                continue                       # nobody thinks it is real
            pend.append((k, rs, live))
        pend.sort(key=lambda x: (SEV.get(worst(x[1], CONFIRMING), 5), -len(x[2])))

        Q = ["# Verification queue\n",
             f"{len(pend)} finding(s) that at least one reviewer confirmed and no human "
             "has checked yet.\n",
             "Each entry is a **question, not a result**: the reviewer may have found a "
             "real defect or may have pattern-matched a shape that is harmless here. "
             "Only reading the code settles it. Everything needed is quoted below — no "
             "CSVs required.\n",
             "For each: open the file, find the symbol, and decide `REAL`, `FALSE` or "
             "`FIXED`. Then append the answer to the ground-truth table so every future "
             "run is scored against it.\n",
             "---\n"]
        for i, (k, rs, live) in enumerate(pend, 1):
            confirmers = sorted(r["_rv"] for r in live)
            against = sorted(r["_rv"] for r in rs if r["verdict"] in DISMISSING)
            Q.append(f"## {i}. `{k}`\n")
            Q.append(f"**{best_line(rs, 'title')}** — severity {worst(rs, CONFIRMING)}, "
                     f"class `{best_line(rs, 'defect_class')}`\n")
            Q.append(f"- confirmed by: {', '.join(confirmers)}"
                     + (f" · disputed by: {', '.join(against)}" if against else
                        " · **nobody else looked here**"))
            Q.append(f"- caller actually mutates it: "
                     f"{best_line(rs, 'caller_mutates') or 'unstated'}")
            for f_, label in (("impact", "claimed impact"), ("evidence", "quoted code"),
                              ("repro", "reproduction"), ("disproof", "what they checked")):
                v = best_line(rs, f_)
                if v and v.lower() != "none":
                    Q.append(f"- **{label}:** {v[:600]}")
            Q.append(f"\n**Verdict:** `REAL` / `FALSE` / `FIXED` — _________\n")
            Q.append("---\n")
        Q.append("\n## Recording the answers\n")
        Q.append("Append each decided line to `validate<N>/truth.csv` "
                 "(create it if absent):\n")
        Q.append("```csv\nfinding,truth,checked_by,how\n"
                 + "\n".join(f"{k},<REAL|FALSE|FIXED>,<you>,\"<how you checked>\""
                              for k, _, _ in pend[:3])
                 + "\n```\n")
        open(a.pending, "w", encoding="utf-8").write("\n".join(Q))
        print(f"verification queue -> {a.pending}  ({len(pend)} to check)")

    if a.solo:
        with open(a.solo, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["finding", "kind", "reviewer", "verdict",
                                               "severity", "truth", "title", "evidence"])
            w.writeheader()
            for kind, items in (("discovery", solo_new), ("unshared", solo_list)):
                for k, r in items:
                    w.writerow({"finding": k, "kind": kind, "reviewer": r["_rv"],
                                "verdict": r["verdict"], "severity": r["severity"],
                                "truth": truth.get(k, ""), "title": r.get("title", ""),
                                "evidence": r.get("evidence", "")})
        print(f"solo table -> {a.solo}")

    print(f"\nACT {len(buckets['ACT'])}  DISPUTED {len(buckets['DISPUTED'])}  "
          f"FIXED {len(buckets['FIXED'])}  DISMISSED {len(buckets['DISMISSED'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
