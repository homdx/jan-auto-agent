#!/usr/bin/env python3
"""Every KC-6 scenario × every entry worktree → results.json (+ a line per run).

    python3 contest-bench/kc6/run_all_kc6.py --wt <dir> [--out results.json] [entry ...] [sNN_scenario ...]

``<dir>`` holds one git worktree per entry plus ``base`` (the round base with
no patch) — ``setup_kc6.sh`` builds it. Entries default to every directory
under ``<dir>`` but ``base``; naming scenarios (``s14_…``) restricts the run.
Each scenario runs as ``python3 run_one_kc6.py <scenario> <dir>/base`` with
``cwd`` = the entry worktree, in a fresh interpreter, 120 s cap. Results are
merged into ``--out`` (default: ``results.json`` next to this file), so a
subset re-run updates only what it ran.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "run_one_kc6.py"

ap = argparse.ArgumentParser()
ap.add_argument("--wt", required=True, help="folder with <entry>/ worktrees and base/")
ap.add_argument("--out", default=str(HERE / "results.json"))
ap.add_argument("names", nargs="*", help="entries and/or scenarios (sNN_…) to run")
args = ap.parse_args()

wt = Path(args.wt).resolve()
base = wt / "base"
only_s = [a for a in args.names if a[:1] == "s" and a[1:2].isdigit()]
entries = [a for a in args.names if a not in only_s] or sorted(
    d.name for d in wt.iterdir() if d.is_dir() and d.name != "base")
scen = subprocess.run([sys.executable, str(PROBE), "--list"], capture_output=True,
                      text=True, cwd=str(base)).stdout.split()
if only_s:
    scen = [s for s in scen if s in only_s]

out = Path(args.out)
res = json.load(open(out)) if out.exists() else {}
for e in entries:
    res.setdefault(e, {})
    for s in scen:
        t0 = time.monotonic()
        try:
            r = subprocess.run([sys.executable, str(PROBE), s, str(base)], cwd=str(wt / e),
                               capture_output=True, text=True, timeout=120,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            line = ((r.stdout.strip().splitlines() or [""])[-1] if r.stdout.strip()
                    else f"ERROR rc={r.returncode} {r.stderr.strip()[-300:]}")
        except subprocess.TimeoutExpired:
            line = "ERROR timeout 120s"
        res[e][s] = line.replace(str(wt), "<wt>")
        print(f"{e:9} {s:52} {time.monotonic() - t0:5.1f}s {line[:160]}", flush=True)
    json.dump(res, open(out, "w"), indent=1, ensure_ascii=False)

print()
print("%-58s" % "scenario" + " ".join("%9s" % e for e in entries))
for s in scen:
    print("%-58s" % s + " ".join("%9s" % res[e].get(s, "?").split()[0][:5] for e in entries))
print("%-58s" % "PASS total" + " ".join(
    "%9d" % sum(1 for s in scen if res[e].get(s, "").startswith("PASS")) for e in entries))
