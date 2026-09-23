"""FL-2 — prove the guards by breaking what they guard (POSTMORTEM-FL-1 §9, B.2).

For each injection: save the file, replace one anchored line, run the named
tests (must go RED), restore, run them again (must go GREEN). An anchor that
is not found aborts the run — a stale anchor would inject nothing and "prove"
a guard that was never tested.

Run from the ideal worktree's root: `python3 <this> [--acceptance PATH]`.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

LOCK_TESTS = "tests/test_git_run_index_lock.py"
GM_TESTS = "tests_bugfix/test_git_manager_has_staged_changes_oserror.py"

INJECTIONS = [
    ("the shared ladder is one attempt",
     "tools/git_run.py",
     "    for attempt in range(max(1, retries)):",
     "    for attempt in range(1):  # INJECTED FL-2 REGRESSION",
     [LOCK_TESTS, GM_TESTS, "ACC"]),
    ("workspace._git bypasses the ladder",
     "tools/contest/workspace.py",
     '    proc = run_git(["git", "-C", str(cwd), *args])',
     '    proc = run_git(["git", "-C", str(cwd), *args], retries=1)  # INJECTED',
     [LOCK_TESTS, "ACC"]),
    ("gates.git bypasses the ladder",
     "tools/contest/gates.py",
     '    r = run_git(["git", *args], cwd=cwd)',
     '    r = run_git(["git", *args], cwd=cwd, retries=1)  # INJECTED',
     [LOCK_TESTS, "ACC"]),
    ("manifest._run_git bypasses the ladder",
     "tools/collect/manifest.py",
     "        proc = run_git(\n",
     "        proc = run_git(retries=1, cmd=\n",
     [LOCK_TESTS, "ACC"]),
    ("_dirty_tree reads a failed status as clean again",
     "tools/contest/runner.py",
     "    if r.returncode != 0:\n        raise TreeReadError(",
     "    if False:  # INJECTED\n        raise TreeReadError(",
     [LOCK_TESTS, "tests/test_contest_runner.py", "ACC"]),
    ("GitManager stops waiting through its _backoff seam",
     "tools/auto/git_manager.py",
     "                sleep=self._backoff,\n",
     "",
     [GM_TESTS]),
]


def pytest(targets):
    r = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q", "-n0",
                        "-p", "no:cacheprovider", "--timeout=180"],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout.strip().splitlines() or [""])[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--acceptance", required=True)
    ap.add_argument("--own-only", action="store_true",
                    help="leave the judge suite out: the ideal's own tests must catch each one")
    args = ap.parse_args()
    acc = pathlib.Path("tests/acceptance_fl2.py")
    shutil.copy(args.acceptance, acc)
    ok = True
    try:
        for name, path, old, new, targets in INJECTIONS:
            targets = [str(acc) if t == "ACC" else t for t in targets
                       if not (args.own_only and t == "ACC")]
            p = pathlib.Path(path)
            good = p.read_text(encoding="utf-8")
            assert good.count(old) == 1, f"{name}: anchor not found once in {path}"
            p.write_text(good.replace(old, new, 1), encoding="utf-8")
            try:
                red, red_line = pytest(targets)
            finally:
                p.write_text(good, encoding="utf-8")
            green, green_line = pytest(targets)
            verdict = "OK " if red != 0 and green == 0 else "BAD"
            ok &= verdict == "OK "
            print(f"{verdict} {name}\n     injected: {red_line}\n     restored: {green_line}")
    finally:
        acc.unlink(missing_ok=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
