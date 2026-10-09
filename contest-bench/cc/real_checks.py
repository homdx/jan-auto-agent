#!/usr/bin/env python3
"""CC-0: re-run the checks behind the recorded truth of ``claims_real.json``.

Each real claim carries a ``check`` that settles it at ``real_sha``:

* ``{"grep": path, "text": s}`` — true iff *s* is in ``git show real_sha:path``;
* ``{"python": snippet}`` — the snippet runs in an export of ``real_sha``
  (``git archive``, so no worktree and no checkout is touched) with the helpers
  of ``PRELUDE`` in scope, and prints ``True`` or ``False``.

A check whose answer differs from the recorded ``truth`` is a failure: either the
claim file is wrong or the tree under ``real_sha`` is not what it was.

    python3 contest-bench/cc/real_checks.py [claims_real.json] [--repo .] [--only r006 r007]

Exit 0 when every check agrees, 1 when one disagrees, 2 when ``real_sha`` is not
in the repository (nothing to check against).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# In scope for every ``python`` check. ``src`` and ``git`` read the repository at
# the pinned sha, never the export's neighbours; ``decide`` asks the contest
# policy with contest.ini's own deny_commands, the way kc-bug-report.md did.
PRELUDE = r'''
import importlib, inspect, os, subprocess, sys, tempfile
from pathlib import Path
REPO, SHA = os.environ["CC_REPO"], os.environ["CC_SHA"]

def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                          check=True).stdout

def src(path):
    proc = subprocess.run(["git", "show", f"{SHA}:{path}"], cwd=REPO,
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None

def fn_src(module, name):
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    return inspect.getsource(obj)

def decide(command):
    import re
    from tools.contest.policy import Policy, PolicyContext
    from tools.contest.roster import ContestConfig
    line = re.search(r"^deny_commands\s*=(.*)$", src("contest.ini"), re.M).group(1)
    deny = tuple(p.strip() for p in line.split(",") if p.strip())
    def no_gate(*a, **k):
        raise RuntimeError("the gate must not be asked")
    policy = Policy(ContestConfig(deny_commands=deny, gate_settings=None), completion_fn=no_gate)
    event = {"type": "permission.asked", "properties": {
        "id": "per_cc0", "sessionID": "ses_cc0", "permission": "bash",
        "patterns": [command], "metadata": {"command": command}, "always": []}}
    return policy.decide(event, PolicyContext(worktree=Path(tempfile.mkdtemp())))
'''


def _git(repo: Path, *args: str, text: bool = True):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=text)


def has_sha(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def export(repo: Path, sha: str, dest: Path) -> Path:
    """The tree of *sha* unpacked under *dest* (``git archive``: read-only on *repo*)."""
    blob = _git(repo, "archive", "--format=tar", sha, text=False)
    if blob.returncode != 0:
        raise RuntimeError(blob.stderr.decode(errors="replace").strip())
    with tarfile.open(fileobj=io.BytesIO(blob.stdout)) as tar:
        tar.extractall(dest, filter="data") if sys.version_info >= (3, 12) else tar.extractall(dest)
    return dest


def run_check(check: dict, repo: Path, sha: str, tree: Path) -> "bool | None":
    """The truth the check observes; None when it could not run."""
    if "grep" in check:
        proc = _git(repo, "show", f"{sha}:{check['grep']}")
        return proc.returncode == 0 and check["text"] in proc.stdout
    env = {**os.environ, "CC_REPO": str(repo), "CC_SHA": sha, "PYTHONPATH": str(tree),
           "GIT_CONFIG_NOSYSTEM": "1"}
    proc = subprocess.run([sys.executable, "-c", PRELUDE + "\n" + check["python"]],
                          cwd=tree, env=env, capture_output=True, text=True, timeout=120)
    last = (proc.stdout.strip().splitlines() or [""])[-1]
    if proc.returncode != 0 or last not in ("True", "False"):
        sys.stderr.write(proc.stderr[-2000:])
        return None
    return last == "True"


def run_all(claims_file: Path, repo: Path, only=()) -> list[dict]:
    doc = json.loads(claims_file.read_text(encoding="utf-8"))
    sha = doc["real_sha"]
    rows = []
    with tempfile.TemporaryDirectory(prefix="cc-real-") as tmp:
        tree = export(repo, sha, Path(tmp))
        for claim in doc["claims"]:
            if only and claim["id"] not in only:
                continue
            seen = run_check(claim["check"], repo, sha, tree)
            rows.append({"id": claim["id"], "truth": claim["truth"], "observed": seen,
                         "ok": seen is not None and seen == claim["truth"]})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("claims", type=Path, nargs="?", default=HERE / "claims_real.json")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--only", nargs="+", default=())
    args = ap.parse_args(argv)
    sha = json.loads(args.claims.read_text(encoding="utf-8"))["real_sha"]
    if not has_sha(args.repo, sha):
        print(f"real_checks: {sha[:12]} is not in {args.repo}", file=sys.stderr)
        return 2
    rows = run_all(args.claims, args.repo.resolve(), args.only)
    for r in rows:
        print(f"{r['id']}  recorded={r['truth']!s:5}  observed={r['observed']!s:5}  "
              + ("ok" if r["ok"] else "MISMATCH"))
    bad = [r["id"] for r in rows if not r["ok"]]
    print(f"{len(rows) - len(bad)}/{len(rows)} agree" + (f"; mismatch: {' '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
