"""static_checks.py <worktree> [--base-ref <rev>] — ground-rule checks that need no execution.

Emits `@@RESULT@@{json}` like run_one.py. `--base-ref` is the ticket base; the
entrant's own commits are `merge-base(HEAD, base-ref)..HEAD`, so a tree that
was rebased onto a later commit is still measured on its own work only.
Round-specific keys (which ini key must be documented, its default) come from
CONFIG_KEYS in the round's scenarios.py when STATIC_SCENARIOS env points at it;
without it only the generic rules run.
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("worktree")
ap.add_argument("--base-ref", default="HEAD")
a = ap.parse_args()
wt = Path(a.worktree).resolve()


def git(*args):
    return subprocess.run(["git", *args], cwd=wt, capture_output=True, text=True).stdout


def ck(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:300]}


base = git("merge-base", "HEAD", a.base_ref).strip()
ncommits = int(git("rev-list", "--count", f"{base}..HEAD").strip() or 0)
changed = git("diff", "--name-only", f"{base}..HEAD").split()
added_files = git("diff", "--diff-filter=A", "--name-only", f"{base}..HEAD").split()
new_tests = [f for f in added_files if re.match(r"^(tests|tests_bugfix)/test_.*\.py$", f)]
n_new_test_fns = sum(1 for l in git("diff", f"{base}..HEAD", "--", "tests", "tests_bugfix").splitlines()
                     if l.startswith("+") and re.match(r"\+\s*(async )?def test_", l))
sync = subprocess.run([sys.executable, "scripts/sync_test_tiers.py", "--check"], cwd=wt, capture_output=True, text=True)
numstat = git("diff", "--numstat", f"{base}..HEAD", "--", "tools")
added = sum(int(l.split()[0]) for l in numstat.splitlines() if l.split()[0].isdigit())
removed = sum(int(l.split()[1]) for l in numstat.splitlines() if l.split()[1].isdigit())

checks = [
    ck("S.CollectBridge._shrink / collect_bridge.py untouched", "tools/auto/collect_bridge.py" not in changed, changed),
    ck("S.epic-tasks/ untouched", not any(f.startswith("epic-tasks/") for f in changed), [f for f in changed if f.startswith("epic-tasks/")]),
    ck("S.agents_128k.ini untouched", "agents_128k.ini" not in changed, ""),
    ck("S.one commit", ncommits == 1, ncommits),
    ck("S.ships tests (new file or new test functions)", bool(new_tests) or n_new_test_fns > 0,
       f"new_files={len(new_tests)} new_test_fns={n_new_test_fns}"),
    ck("S.smoke mirror in sync (sync_test_tiers --check)", sync.returncode == 0, (sync.stdout + sync.stderr)[-200:]),
]

# round-specific: config keys that must be documented in agents.ini
scen_path = os.environ.get("STATIC_SCENARIOS")
if scen_path:
    spec = importlib.util.spec_from_file_location("scenarios", scen_path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    spec.loader.exec_module(mod)
    ini = (wt / "agents.ini").read_text(encoding="utf-8", errors="replace")
    for section, key, default in getattr(mod, "CONFIG_KEYS", []):
        m = re.search(r"^\[" + re.escape(section) + r"\](.*?)(?=^\[|\Z)", ini, re.S | re.M)
        body = m.group(1) if m else ""
        checks.append(ck(f"S.{key} documented in agents.ini [{section}]", key in body, "present" if key in body else "missing"))
        checks.append(ck(f"S.{key} default {default} in agents.ini",
                         bool(re.search(re.escape(key) + r"\s*=\s*" + re.escape(str(default)) + r"\b", body)),
                         re.findall(re.escape(key) + r"\s*=\s*\S+", body)[:1]))

info = {"base": base[:7], "commits": ncommits, "diff_lines_tools": f"+{added}/-{removed}", "changed": changed,
        "new_tests": new_tests, "n_new_test_fns": n_new_test_fns,
        "touched_existing_tests": [f for f in changed if f.startswith(("tests/", "tests_bugfix/")) and f not in new_tests]}
print("@@RESULT@@" + json.dumps({"scenario": "S0_static", "worktree": wt.name, "checks": checks, "info": info, "error": None}))
