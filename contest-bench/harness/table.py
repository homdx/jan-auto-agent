"""table.py <results.json> [--scenarios scenarios.py] [--md] — score table + per-entrant misses.

Groups come from `GROUPS` in the round's scenarios.py (ordered list of
(label, prefix-tuple)); without it every check is grouped by the text before
its first dot. Checks whose name starts with a `BONUS_PREFIXES` prefix are
shown but not counted in the total. An errored scenario counts as all-failed
(padded with the check names other trees produced for it).
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("results")
ap.add_argument("--scenarios")
ap.add_argument("--md", action="store_true", help="markdown output")
ap.add_argument("--base", default="base", help="name of the unpatched reference tree")
a = ap.parse_args()
R = json.load(open(a.results))

GROUPS, BONUS = None, ("BONUS",)
if a.scenarios:
    spec = importlib.util.spec_from_file_location("scenarios", a.scenarios)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    spec.loader.exec_module(mod)
    GROUPS = getattr(mod, "GROUPS", None)
    BONUS = tuple(getattr(mod, "BONUS_PREFIXES", BONUS))

names_by_sc = {}
for wt, scs in R.items():
    for scn, sc in scs.items():
        if len(sc["checks"]) > len(names_by_sc.get(scn, [])):
            names_by_sc[scn] = [c["name"] for c in sc["checks"]]
rows = []
for wt, scs in R.items():
    for scn, sc in scs.items():
        if sc.get("error") and not sc["checks"]:
            sc["checks"] = [{"name": n, "ok": False, "detail": "ERROR"} for n in names_by_sc.get(scn, [])]
    checks = [c for sc in scs.values() for c in sc["checks"]]
    errs = [sc["scenario"] for sc in scs.values() if sc.get("error")]
    if GROUPS is None:
        labels = sorted({c["name"].split(".")[0] for c in checks})
        groups = [(l, (l + ".",)) for l in labels]
    else:
        groups = GROUPS
    g = {}
    for label, prefixes in groups:
        cs = [c for c in checks if c["name"].startswith(tuple(prefixes))]
        g[label] = (sum(c["ok"] for c in cs), len(cs))
    scored = [c for c in checks if not c["name"].startswith(BONUS)]
    tot = sum(c["ok"] for c in scored)
    rows.append((wt, tot, len(scored), g, errs))
rows.sort(key=lambda r: (r[0] == a.base, -r[1]))
labels = list(rows[0][3]) if rows else []

if a.md:
    print("| entrant | total | " + " | ".join(labels) + " | errors |")
    print("|---|---|" + "---|" * len(labels) + "---|")
    for wt, tot, n, g, errs in rows:
        print(f"| {wt} | {tot}/{n} | " + " | ".join(f"{g[k][0]}/{g[k][1]}" for k in labels) + f" | {', '.join(errs)} |")
else:
    print(f"{'entrant':12s} {'total':>8s} " + " ".join(f"{k[:7]:>7s}" for k in labels))
    for wt, tot, n, g, errs in rows:
        print(f"{wt:12s} {tot:3d}/{n:<3d}  " + " ".join(f"{g[k][0]:3d}/{g[k][1]:<3d}" for k in labels)
              + ("  ERR:" + ",".join(errs) if errs else ""))
print()
for wt, tot, n, g, errs in rows:
    if wt == a.base:
        continue
    fails = [c for sc in R[wt].values() for c in sc["checks"] if not c["ok"]]
    head = f"{wt} ({tot}/{n}) — {len(fails)} missed"
    print(("### " if a.md else "--- ") + head)
    for c in fails:
        bonus = " (bonus, not counted)" if c["name"].startswith(BONUS) else ""
        if a.md:
            print(f"- `{c['name']}`{bonus} — got: `{c['detail'][:160]}`")
        else:
            print(f"      {c['name']}{bonus}  | got: {c['detail'][:120]}")
    print()
