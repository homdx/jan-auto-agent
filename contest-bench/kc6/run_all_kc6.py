#!/usr/bin/env python3
"""Every KC-6 scenario × every entry worktree → results.json (+ stdout table)."""
import json, os, subprocess, sys, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
W = HERE.parent / "kc6"
BASE = W / "base"
PROBE = HERE / "run_one_kc6.py"
args = sys.argv[1:]
only_s = [a for a in args if a[:1] == "s" and a[1:2].isdigit()]
entries = [a for a in args if a not in only_s] or sorted(d.name for d in W.iterdir() if d.is_dir() and d.name != "base")
scen = subprocess.run([sys.executable, str(PROBE), "--list"], capture_output=True, text=True, cwd=str(BASE)).stdout.split()
if only_s: scen = [s for s in scen if s in only_s]
out = HERE / "results.json"
res = json.load(open(out)) if out.exists() else {}
for e in entries:
    res.setdefault(e, {})
    for s in scen:
        t0 = time.monotonic()
        try:
            r = subprocess.run([sys.executable, str(PROBE), s, str(BASE)], cwd=str(W / e), capture_output=True, text=True,
                               timeout=120, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            line = (r.stdout.strip().splitlines() or [""])[-1] if r.stdout.strip() else f"ERROR rc={r.returncode} {r.stderr.strip()[-300:]}"
        except subprocess.TimeoutExpired:
            line = "ERROR timeout 120s"
        res[e][s] = line
        print(f"{e:9} {s:52} {time.monotonic()-t0:5.1f}s {line[:160]}", flush=True)
json.dump(res, open(out, "w"), indent=1, ensure_ascii=False)
