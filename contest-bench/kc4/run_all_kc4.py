"""run_all_kc4.py --wt <dir> --out results.json [names ...] [-- scenarios ...]

Runs every (entrant, scenario) pair in a fresh `python3` subprocess (one at a
time — matches contest-bench's rule of never running entries in parallel),
merges into results.json.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SCENARIOS = [
    "fresh_round",
    "rerun_after_dirty_crash",
    "foreign_folder_refused",
    "untracked_epic_tasks_refused",
    "unresolvable_base",
    "idempotent_reset_worktree",
    "attach_clone_dirty_then_force",
    "attach_clone_missing_base",
    "remove_round_cleans_up",
    "stale_branch_recreated",
    "worktree_of_other_repo",
    "repo_checkout_never_touched",
]


def _save(path: Path, results: dict) -> None:
    """The results file replaced whole: a failed write leaves the last good file, not half a JSON."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(results, indent=2, sort_keys=True))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wt", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    wt = Path(a.wt)
    names = a.names or sorted(p.name for p in wt.iterdir() if p.is_dir())

    out_path = Path(a.out)
    results = json.loads(out_path.read_text()) if out_path.exists() else {}

    for name in names:
        entry = results.setdefault(name, {})
        for scen in SCENARIOS:
            try:
                p = subprocess.run(
                    [sys.executable, str(HERE / "run_one_kc4.py"), str(wt / name), scen],
                    capture_output=True, text=True, timeout=120,
                )
                line = next((l for l in p.stdout.splitlines() if l.startswith("@@RESULT@@")), None)
                detail = (p.stdout + p.stderr)[-500:]
            except subprocess.TimeoutExpired:
                line, detail = None, "timed out after 120 s"
            try:
                entry[scen] = json.loads(line[len("@@RESULT@@"):]) if line is not None else None
            except ValueError:
                entry[scen] = None
                detail = f"unreadable result line: {line[:200]!r}"
            if entry[scen] is None:
                # one lost cell is a failed check, not the end of the suite
                entry[scen] = {"checks": [{"name": "subprocess", "ok": False, "detail": detail}]}
            print(f"[{name}] {scen}: "
                  f"{sum(c['ok'] for c in entry[scen]['checks'])}/{len(entry[scen]['checks'])}")
            _save(out_path, results)   # kept if a later cell dies
    _save(out_path, results)


if __name__ == "__main__":
    main()
