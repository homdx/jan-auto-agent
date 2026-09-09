#!/usr/bin/env python3
"""
Colored console validation report for merged.csv
Usage:
  python3 report.py [merged.csv]
  python3 report.py merged.csv --no-color
  python3 report.py merged.csv --only-ground
  python3 report.py merged.csv --min-reviewers N
"""

import csv, sys, os, argparse
from collections import defaultdict

# ── ANSI ──────────────────────────────────────────────────────────────────────
RST  = "\033[0m"
BOLD = "\033[1m"
DIM  = "\033[2m"

BLK="\033[30m"; RED="\033[31m"; GRN="\033[32m"; YLW="\033[33m"
BLU="\033[34m"; MAG="\033[35m"; CYN="\033[36m"; WHT="\033[37m"
GRY="\033[90m"; LRED="\033[91m"; LGRN="\033[92m"; LYLW="\033[93m"
LBLU="\033[94m"; LMAG="\033[95m"; LCYN="\033[96m"

BG_BLU="\033[44m"; BG_RED="\033[41m"; BG_GRN="\033[42m"
BG_YLW="\033[43m"; BG_DRK="\033[48;5;235m"; BG_HDR="\033[48;5;17m"

COLOR = True

def c(*codes): return "".join(codes) if COLOR else ""
def r(): return RST if COLOR else ""

def tw():
    try: return os.get_terminal_size().columns
    except: return 120

# ── Ground truth ──────────────────────────────────────────────────────────────
GROUND = {
    "tools/search_agent.py::_DEFAULT_SKIP_DIRS":                    "REAL",
    "tools/search_agent.py::SearchAgent.__init__":                   "REAL",
    "tools/search_agent.py::SearchAgent.skip_dirs":                  "REAL",
    "tools/auto/arch_probe.py::ArchProbe.last_by_op":               "REAL",
    "tools/metrics_collector.py::MetricsCollector._load_all_cached":"FALSE",
    "tools/auto/state.py::StateStore.get_progress":                  "FALSE",
    "tools/auto/controller.py::AutoController.config":               "REAL_LOW",
    "tools/auto/state.py::StateStore.get_task":                      "FIXED",
    "tools/auto/state.py::StateStore.all_tasks":                     "FIXED",
    "tools/auto/state.py::StateStore.resume_info":                   "FIXED",
}

CONFIRMED_P = ["CONFIRMED","ACT","REAL","NEW"]
DISMISSED_P = ["FALSE_POSITIVE","DISMISSED","FALSE","OUT_OF_SCOPE","UNVERIFIABLE","ALREADY_FIXED"]

def norm(v):
    v = (v or "").strip()
    if not v: return None
    for p in CONFIRMED_P:
        if v.startswith(p): return "C"
    for p in DISMISSED_P:
        if v.startswith(p): return "D"
    return "?"

SHORT_LABEL = {
    "FALSE_POSITIVE":"FP","ALREADY_FIXED":"FIXED","OUT_OF_SCOPE":"OOS",
    "UNVERIFIABLE":"UNVER","CONFIRMED":"CONF","DISMISSED":"DISM",
    "ACT":"ACT","REAL":"REAL","NEW":"NEW",
}
def short(v):
    v = (v or "").strip()
    if not v: return ""
    # take only the first token when pipe-separated (e.g. FIXED|OUT_OF_SCOPE)
    v = v.split("|")[0].strip()
    for long, s in SHORT_LABEL.items():
        if v.startswith(long):
            return s
    return v[:6]

# ── Model short names (padded to fixed width) ─────────────────────────────────
def make_short(name):
    n = name
    n = n.replace("sensenova","sns")
    n = n.replace("-flash-lite","").replace("-flash","")
    n = n.replace("-2-5","").replace("-2-0","").replace("-medium","")
    n = n.replace("-free","").replace("-pass1","+p2").replace("-3-5","")
    n = n.replace("-lite","")
    return n

# ── Drawing primitives ────────────────────────────────────────────────────────
def hline(ch="─", col=None, width=None):
    w = width or tw()
    line = ch * w
    if col: print(c(col) + line + r())
    else:   print(line)

def section(title):
    print()
    print(c(BOLD, LCYN) + f"▸ {title}" + r())
    hline("─", GRY)

def badge_truth(t):
    if t in ("REAL","REAL_LOW"): return c(BOLD,LRED)   +f"{'  '+t+'  ':^10}"+ r()
    if t == "FALSE":             return c(BOLD,LGRN)   +f"{'  '+t+'  ':^10}"+ r()
    if t == "FIXED":             return c(BOLD,LBLU)   +f"{'  '+t+'  ':^10}"+ r()
    return c(GRY) + f"{t:^10}" + r()

def badge_agree(a):
    if a=="UNANIMOUS": return c(LGRN) + f"{'UNANI':^7}" + r()
    if a=="SPLIT":     return c(LYLW) + f"{'SPLIT':^7}" + r()
    if a=="SOLO":      return c(LMAG) + f"{'SOLO':^7}"+  r()
    return f"{a:^7}"

def badge_sev(s):
    if s=="HIGH":   return c(BG_RED,WHT)   + f" HIGH " + r()
    if s=="MEDIUM": return c(BG_YLW,BLK)   + f" MED  " + r()
    if s=="LOW":    return c(LBLU)          + f" LOW  " + r()
    return c(GRY)                           + f" ·    " + r()

# ── Per-cell verdict rendering ────────────────────────────────────────────────
def vcell(raw, truth, width=7):
    n = norm(raw)
    lbl = short(raw)
    pad = width

    # no data
    if not (raw or "").strip():
        return c(GRY, DIM) + "·".center(pad) + r()

    # no ground truth → just color by verdict
    if truth is None:
        if n == "C": return c(LRED)  + lbl.center(pad) + r()
        if n == "D": return c(LGRN)  + lbl.center(pad) + r()
        return c(LYLW) + lbl.center(pad) + r()

    real = truth in ("REAL","REAL_LOW")
    ok   = (real and n=="C") or (not real and n=="D")
    fp   = (not real) and n=="C"
    fn   = real and n=="D"

    if ok: return c(LGRN)           + lbl.center(pad) + r()
    if fp: return c(BOLD,BG_RED,WHT)+ lbl.center(pad) + r()
    if fn: return c(BOLD,LYLW)      + lbl.center(pad) + r()
    return c(LYLW) + lbl.center(pad) + r()

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global COLOR
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?", default="merged.csv")
    ap.add_argument("--no-color",      action="store_true")
    ap.add_argument("--only-ground",   action="store_true")
    ap.add_argument("--min-reviewers", type=int, default=0)
    args = ap.parse_args()
    if args.no_color: COLOR = False

    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    META = {"finding","severity","agreement","reviewers","confirmed","dismissed","fixed","task_ids","title"}
    models   = [k for k in rows[0] if k not in META]
    shorts   = [make_short(m) for m in models]
    NM       = len(models)

    # ── HEADER ────────────────────────────────────────────────────────────────
    W = tw()
    print()
    print(c(BOLD,BG_HDR,LCYN) + " VALIDATION REPORT ".center(W) + r())
    print(c(DIM)    + f" {args.csv}  ·  {len(rows)} findings  ·  {NM} models ".center(W) + r())
    hline("═", BLU)

    # ── OVERVIEW ──────────────────────────────────────────────────────────────
    section("OVERVIEW")
    total   = len(rows)
    conf_ct = sum(1 for r2 in rows if int(r2.get("confirmed","0") or 0) > 0)
    dis_ct  = sum(1 for r2 in rows if int(r2.get("confirmed","0") or 0)==0
                                   and int(r2.get("dismissed","0") or 0)>0)
    fix_ct  = sum(1 for r2 in rows if int(r2.get("fixed","0") or 0) > 0)
    una_ct  = sum(1 for r2 in rows if r2["agreement"]=="UNANIMOUS")
    spl_ct  = sum(1 for r2 in rows if r2["agreement"]=="SPLIT")
    sol_ct  = sum(1 for r2 in rows if r2["agreement"]=="SOLO")
    noise   = dis_ct/total*100 if total else 0

    left  = [("Total findings", str(total),   WHT),
             ("Confirmed (≥1)", str(conf_ct),  LRED),
             ("All-dismissed",  str(dis_ct),   LGRN),
             ("Fixed",          str(fix_ct),   LBLU)]
    right = [("Unanimous",      str(una_ct),   LGRN),
             ("Split",          str(spl_ct),   LYLW),
             ("Solo",           str(sol_ct),   LMAG),
             ("Noise floor",    f"{noise:.0f}%", LYLW)]

    lw = 18
    for (ll,lv,lc),(rl,rv,rc) in zip(left,right):
        print(f"  {c(GRY)}{ll:<{lw}}{r()}{c(BOLD,lc)}{lv:>5}{r()}     "
              f"{c(GRY)}{rl:<{lw}}{r()}{c(BOLD,rc)}{rv:>5}{r()}")

    # ── MODEL COVERAGE bar chart ───────────────────────────────────────────────
    section("MODEL COVERAGE")

    # fixed col width so all models fit
    COL = max(len(s) for s in shorts) + 2
    COL = max(COL, 8)

    # header row
    print("  " + "".join(s.center(COL) for s in shorts))
    hline("·", GRY)

    for key, label, color in [("C","confirmed",LRED),("D","dismissed",LGRN)]:
        counts = [sum(1 for row in rows if norm(row.get(m,""))==key) for m in models]
        mx = max(counts) if counts else 1
        BAR = COL - 4
        line = "  "
        for v in counts:
            b = "█" * max(1, int(v/mx * BAR)) if v else ""
            cell = (b+" "+str(v)) if v else "0"
            line += c(color) + cell.center(COL) + r()
        print(f"  {c(GRY)}{label:<11}{r()}" + line[2:])

    # ── GROUND TRUTH ACCURACY ─────────────────────────────────────────────────
    section("GROUND TRUTH ACCURACY   (GROUND-competition.md)")

    csv_set     = {row["finding"].strip() for row in rows}
    missing_gt  = [f for f in GROUND if f not in csv_set]
    if missing_gt:
        print(c(BOLD,LYLW) + "  ⚠  Missing from merged.csv:" + r())
        for mf in missing_gt:
            print(c(RED) + f"     ✗  {mf}" + r())
        print()

    gt_rows = [(f, t, next((row for row in rows if row["finding"].strip()==f), None))
               for f, t in GROUND.items()]

    # accumulate per-model stats
    ms = defaultdict(lambda:{"rev":0,"ok":0,"fp":0,"fn":0})
    for finding, truth, row in gt_rows:
        if row is None: continue
        real = truth in ("REAL","REAL_LOW")
        for m in models:
            n = norm(row.get(m,""))
            if n is None: continue
            ms[m]["rev"] += 1
            if real:
                if n=="C": ms[m]["ok"] += 1
                else:      ms[m]["fn"] += 1
            else:
                if n=="D": ms[m]["ok"] += 1
                else:      ms[m]["fp"] += 1

    # table: model cols
    NC = COL
    print("  " + c(BOLD) + "".join(s.center(NC) for s in shorts) + r())
    hline("·", GRY)

    for key, label, col in [("ok","correct",LGRN),("fp","FP  🔴",LRED),("fn","FN  🟡",LYLW)]:
        vals = [ms[m][key] for m in models]
        line = "  " + c(GRY) + f"{label:<12}" + r()
        for v in vals:
            cc = col if v else GRY
            line += c(cc) + str(v).center(NC) + r()
        print(line)

    print()
    line = "  " + c(GRY) + f"{'accuracy':<12}" + r()
    for m in models:
        rev = ms[m]["rev"]; ok = ms[m]["ok"]
        pct = f"{ok/rev*100:.0f}%" if rev else "—"
        cc  = LGRN if rev and ok/rev>=.8 else (LYLW if rev else GRY)
        line += c(BOLD,cc) + pct.center(NC) + r()
    print(line)

    # ── GROUND TRUTH ROW DETAIL ───────────────────────────────────────────────
    section("GROUND TRUTH ROW DETAIL")

    FW = 38   # finding col
    TW = 11   # truth col
    VC = 8    # verdict col per model

    hdr = (c(BOLD) + f"  {'finding':<{FW}} {'truth':<{TW}} " +
           "".join(s[:VC-1].center(VC) for s in shorts) + r())
    print(hdr)
    hline("─", GRY)

    for finding, truth, row in gt_rows:
        sym  = finding.split("::")[-1][:FW]
        tb   = badge_truth(truth)
        gt_h = c(BOLD, LCYN) if truth else ""

        if row is None:
            print(f"  {c(GRY)}{sym:<{FW}}{r()} {tb} "
                  + c(GRY) + " ✗ NOT IN MERGED CSV".center(VC*NM) + r())
            continue

        cells = "".join(vcell(row.get(m,""), truth, VC) for m in models)
        print(f"  {gt_h}{sym:<{FW}}{r()} {tb} {cells}")

    # ── ALL FINDINGS ──────────────────────────────────────────────────────────
    section("ALL FINDINGS")

    filter_rows = rows
    if args.only_ground:
        filter_rows = [r2 for r2 in rows if r2["finding"].strip() in GROUND]
    if args.min_reviewers:
        filter_rows = [r2 for r2 in filter_rows
                       if int(r2.get("reviewers","0") or 0) >= args.min_reviewers]

    FF = 36   # finding
    SF = 6    # sev
    AF = 7    # agree
    NF = 4    # conf/dis
    VC2 = 7   # verdict

    hdr = (c(BOLD)
        + f"  {'finding':<{FF}} {'sev':<{SF}} {'agree':<{AF}} {'c':>{NF}} {'d':>{NF}}  "
        + "".join(s[:VC2-1].center(VC2) for s in shorts)
        + r())
    print(hdr)
    hline("─", GRY)

    for row in filter_rows:
        finding = row["finding"].strip()
        truth   = GROUND.get(finding)
        sym     = finding.split("::")[-1][:FF]
        sv      = badge_sev(row.get("severity",""))
        ag      = badge_agree(row.get("agreement",""))
        conf    = row.get("confirmed","0") or "0"
        dis     = row.get("dismissed","0") or "0"
        gt_h    = c(BOLD,LCYN) if truth else ""

        cells = "".join(vcell(row.get(m,""), truth, VC2) for m in models)
        print(f"  {gt_h}{sym:<{FF}}{r()} {sv} {ag} "
              f"{c(LRED)}{conf:>{NF}}{r()} {c(LGRN)}{dis:>{NF}}{r()}  {cells}")

    # ── SOLO DISCOVERIES ──────────────────────────────────────────────────────
    section("SOLO DISCOVERIES")
    solo = [r2 for r2 in rows if r2.get("agreement","")=="SOLO"]
    if not solo:
        print(c(GRY) + "  none" + r())
    else:
        for row in solo:
            finding = row["finding"].strip()
            truth   = GROUND.get(finding)
            sym     = finding.split("::")[-1]
            tb      = ("  " + badge_truth(truth)) if truth else ""
            who     = ", ".join(make_short(m) for m in models
                                if norm(row.get(m,""))=="C")
            title   = (row.get("title","") or "")[:72]
            conf    = int(row.get("confirmed","0") or 0)
            marker  = c(BOLD,LRED) if conf else c(GRY)
            print(f"  {marker}{sym}{r()}{tb}")
            if title: print(f"    {c(DIM)}{title}{r()}")
            if who:   print(f"    {c(GRY)}finder:{r()} {c(LMAG)}{who}{r()}")
            print()

    # ── FOOTER ────────────────────────────────────────────────────────────────
    hline("═", BLU)
    print(f"  {c(LGRN)}✅ correct{r()}   "
          f"{c(BG_RED,WHT)} FP {r()} false positive — confirmed non-bug   "
          f"{c(BOLD,LYLW)}FN{r()} false negative — missed real bug   "
          f"{c(GRY)}·  not reviewed{r()}")
    print()

if __name__ == "__main__":
    main()
