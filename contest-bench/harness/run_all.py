"""run_all.py — run every scenario against every entrant worktree, sequentially.

    python3 contest-bench/harness/run_all.py --wt <worktrees-dir> --scenarios <scenarios.py> \
        --out <results.json> [--base-ref <rev>] [entrant ...] [scenario ...]

Positional arguments are filters: entrant names (subdirectories of --wt) and/or
scenario names; omit both to run everything. `S0_static` is the static
ground-rule check (static_checks.py). Results are merged into --out, so a
re-run of a subset only refreshes those cells. One subprocess per
(entrant, scenario) — never in parallel; the machine is shared.
"""
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

H = Path(__file__).resolve().parent


def load_scenarios(path: Path):
    spec = importlib.util.spec_from_file_location("scenarios", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(H))
    sys.modules["scenarios"] = mod
    spec.loader.exec_module(mod)
    return mod


def _move_aside(out_path: Path, why: str) -> None:
    """Warn and move a damaged `results.json` to a free `.corrupt` name, so it can still be read."""
    corrupt = out_path.with_name(out_path.name + ".corrupt")
    n = 1
    while corrupt.exists():
        corrupt = out_path.with_name(f"{out_path.name}.corrupt.{n}")
        n += 1
    try:
        os.replace(out_path, corrupt)
    except OSError:
        pass
    print(f"WARNING: {out_path} {why}; moved to {corrupt} and starting from empty results",
          file=sys.stderr)


def _load_results(out_path: Path) -> dict:
    """The merged results, or `{}` when the file is absent or unreadable.

    A truncated or otherwise corrupt `results.json` used to raise out of
    `json.loads` and block every rerun. Now it is moved aside and the run starts
    from empty results — a broken artifact degrades to "no results yet", never an exit.
    """
    if not out_path.exists():
        return {}
    try:
        loaded = json.loads(out_path.read_text())
    except (OSError, ValueError) as exc:
        _move_aside(out_path, f"is not readable JSON ({exc})")
        return {}
    if not isinstance(loaded, dict):
        _move_aside(out_path, "is not a JSON object")
        return {}
    return loaded


def _write_results(out_path: Path, results: dict) -> None:
    """Write *results* atomically: a reader never sees a half-written file."""
    tmp = out_path.with_name(out_path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(results, indent=1, default=str))
        os.replace(tmp, out_path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wt", required=True, help="directory holding one worktree per entrant")
    ap.add_argument("--scenarios", required=True, help="path to the round's scenarios.py")
    ap.add_argument("--out", required=True, help="results.json (merged)")
    ap.add_argument("--base-ref", default="HEAD", help="ticket base commit for static_checks.py")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("filters", nargs="*")
    a = ap.parse_intermixed_args()
    WT = Path(a.wt).resolve()
    scen_path = Path(a.scenarios).resolve()
    scenarios = load_scenarios(scen_path)
    ALL = ["S0_static"] + list(scenarios.SCENARIOS)
    only = [f for f in a.filters if f in ALL]
    names = [f for f in a.filters if f not in ALL] or sorted(p.name for p in WT.iterdir() if p.is_dir())
    scen_names = only or ALL
    out_path = Path(a.out)
    results = _load_results(out_path)
    t0 = time.time()
    for n in names:
        results.setdefault(n, {})
        for sc in scen_names:
            if sc == "S0_static":
                cmd = [sys.executable, str(H / "static_checks.py"), str(WT / n), "--base-ref", a.base_ref]
            else:
                cmd = [sys.executable, str(H / "run_one.py"), str(WT / n), str(scen_path), sc]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=a.timeout,
                                   env=dict(os.environ, STATIC_SCENARIOS=str(scen_path)))
                line = [l for l in p.stdout.splitlines() if l.startswith("@@RESULT@@")]
                if line:
                    res = json.loads(line[-1][len("@@RESULT@@"):])
                else:
                    res = {"scenario": sc, "worktree": n, "checks": [],
                           "error": "no result\n" + (p.stderr[-1500:] if p.stderr else p.stdout[-1500:])}
            except subprocess.TimeoutExpired:
                res = {"scenario": sc, "worktree": n, "checks": [], "error": f"TIMEOUT {a.timeout}s"}
            results[n][sc] = res
            ok = sum(1 for c in res["checks"] if c["ok"]); tot = len(res["checks"])
            err = ("  ERROR: " + res["error"].strip().splitlines()[-1][:100]) if res.get("error") else ""
            print(f"{n:12s} {sc:44s} {ok:2d}/{tot:2d}{err}", flush=True)
        _write_results(out_path, results)
    print(f"done in {time.time() - t0:.0f}s → {out_path}")


if __name__ == "__main__":
    main()
