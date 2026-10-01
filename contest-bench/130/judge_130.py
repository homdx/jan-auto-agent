"""contest-bench/130 — score round 130 (find and fix real bugs in tools/contest/harvest.py).

Per entry, on a worktree of the round's base with the entry's patch applied:
  * real   — contest-bench/130 cases an agent-written PROGRESS.csv reaches (of 6);
  * guard  — the git-cannot-run case (of 1);
  * own    — the entry's tests/test_contest_harvest.py, green runs of 3 (-n 8);
  * basegreen — the base's tests/test_contest_harvest.py still green on the
    patched module: no behaviour change the old tests pin;
  * tiers  — scripts/sync_test_tiers.py --check.
Usage: python3 contest-bench/130/judge_130.py [entry ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASE = "70861e291658d52247735e41450c006fdbabd410"
OUT = REPO / "contest-out" / "130"
TEST = "tests/test_contest_harvest.py"
BENCH = REPO / "contest-bench" / "130" / "test_bench_130.py"


def sh(cmd, cwd, timeout=900):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def pytest(wt, *args, n="8"):
    r = sh([sys.executable, "-m", "pytest", *args, "-q", "-n", n, "-p", "no:cacheprovider"], wt)
    return r.returncode == 0, r.stdout


def judge(entry: str, tmp: Path) -> dict:
    wt = tmp / entry
    sh(["git", "worktree", "add", "--detach", str(wt), BASE], REPO)
    try:
        ap = sh(["git", "apply", str(OUT / f"{entry}.patch")], wt)
        if ap.returncode:
            return {"entry": entry, "error": "apply: " + ap.stderr.strip()[:200]}
        (wt / "contest-bench" / "130").mkdir(parents=True, exist_ok=True)
        bench = wt / "contest-bench" / "130" / BENCH.name
        bench.write_text(BENCH.read_text())
        r = sh([sys.executable, "-m", "pytest", str(bench.relative_to(wt)), "-q", "-n", "0",
                "-p", "no:cacheprovider", "-W", "ignore", "-rA"], wt)
        passed = [l.split("::")[1].split()[0] for l in r.stdout.splitlines()
                  if l.startswith("PASSED") and "::" in l]
        real = sum(p.startswith("test_real") for p in passed)
        guard = sum(p.startswith("test_guard") for p in passed)
        tiers = sh([sys.executable, "scripts/sync_test_tiers.py", "--check"], wt).returncode == 0
        own = sum(pytest(wt, TEST)[0] for _ in range(3))
        sh(["git", "show", f"{BASE}:{TEST}"], wt)
        (wt / "tests" / "test_base_harvest_130.py").write_text(
            sh(["git", "show", f"{BASE}:{TEST}"], wt).stdout)
        basegreen = pytest(wt, "tests/test_base_harvest_130.py")[0]
        added = sh(["git", "diff", "--numstat", BASE, "--", TEST], wt).stdout.split()
        return {"entry": entry, "real": real, "guard": guard, "own": own,
                "basegreen": basegreen, "tiers": tiers,
                "tests_added_lines": int(added[0]) if added else 0}
    finally:
        sh(["git", "worktree", "remove", "--force", str(wt)], REPO)


def main() -> None:
    entries = sys.argv[1:] or sorted(p.stem for p in OUT.glob("*.patch"))
    with tempfile.TemporaryDirectory(prefix="j130-") as tmp:
        rows = [judge(e, Path(tmp)) for e in entries]
    rows.sort(key=lambda r: (-r.get("real", 0), -r.get("basegreen", 0), -r.get("own", 0),
                             -r.get("guard", 0)))
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
