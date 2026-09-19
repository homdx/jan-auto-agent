#!/usr/bin/env python3
"""Run every KC-5 scenario against every entry worktree; write results.json.

Layout it expects (kept OUTSIDE the repo tree, as contest-bench/README.md says):

    <scratch>/wt/base            # git worktree at the round base (4ff7d14)
    <scratch>/wt/<entry>         # one worktree per entry, its patch applied
    <scratch>/bench/run_one_kc5.py  (this folder's run_one_kc5.py)

    python3 run_all_kc5.py [entry ...]      # default: every dir under wt/ but base

Each scenario runs as `python3 run_one_kc5.py <scenario> <base>` with cwd set
to the entry worktree, so `import tools.contest.gates` resolves to that entry.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
W = HERE.parent / "wt"
BASE = W / "base"
PROBE = HERE / "run_one_kc5.py"

entries = sys.argv[1:] or sorted(d.name for d in W.iterdir() if d.is_dir() and d.name != "base")
scen = subprocess.run([sys.executable, str(PROBE), "--list"], capture_output=True, text=True).stdout.split()
out = HERE / "results.json"
res = json.load(open(out)) if out.exists() else {}
for e in entries:
    res[e] = {}
    for s in scen:
        r = subprocess.run([sys.executable, str(PROBE), s, str(BASE)], cwd=str(W / e),
                           capture_output=True, text=True,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        line = (r.stdout.strip().splitlines() or [""])[-1] if r.stdout.strip() else \
            f"ERROR rc={r.returncode} {r.stderr.strip()[-200:]}"
        res[e][s] = line
        print(f"{e:18} {s:40} {line[:150]}", flush=True)
json.dump(res, open(out, "w"), indent=1, ensure_ascii=False)
