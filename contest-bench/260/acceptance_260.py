#!/usr/bin/env python3
"""CC-2 (260) acceptance, written from the ticket's "Acceptance" section.

  * contest-bench/cc fixture: open at base_sha and at head_sha -> two trees whose files
    differ in exactly the files the head commit touched; the operator's checkout is
    unchanged (status, HEAD, index bytes and mtime); collect() on both returns a model
    whose module("pipeline.py") is not None.
  * this repository at real_sha: open finishes in under 60 s (no collect) and the
    tree's HEAD is real_sha.

    python3 contest-bench/260/acceptance_260.py [--code DIR]   # DIR: checkout holding tools/claimcheck/target.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = HERE.parent / "cc"
REPO = HERE.parent.parent


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True).stdout.strip()


def operator_state(repo: Path) -> tuple:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    status = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo), capture_output=True,
                            text=True, env=env).stdout
    index = repo / ".git" / "index"
    return (status, git(repo, "rev-parse", "HEAD"), index.read_bytes(), index.stat().st_mtime_ns)


def tree_files(root: Path) -> dict:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not rel.startswith((".git/", ".collect/")) and rel != ".git":
            out[rel] = path.read_bytes()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, default=REPO)
    code = ap.parse_args().code.resolve()
    sys.path.insert(0, str(code))
    from tools.claimcheck.target import Target

    facts = json.loads((CC / "fixture_facts.json").read_text(encoding="utf-8"))
    real_sha = json.loads((CC / "claims_real.json").read_text(encoding="utf-8"))["real_sha"]
    failures = []

    def check(ok: bool, what: str) -> None:
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=env)
        fx = tmp / "fx"
        (fx / "dirty.txt").write_text("operator's untracked file\n")
        (fx / "pipeline.py").write_text("# the operator is editing this\n")
        git(fx, "status", "--porcelain")
        before = operator_state(fx)

        base_sha, head_sha = facts["base_sha"], facts["head_sha"]
        with Target.open(fx, base_sha, scratch=tmp / "s") as tb, Target.open(fx, head_sha, scratch=tmp / "s") as th:
            check(tb.sha == base_sha and th.sha == head_sha, "both trees are at the sha asked for")
            fb, fh = tree_files(tb.tree), tree_files(th.tree)
            differ = sorted(p for p in set(fb) | set(fh) if fb.get(p) != fh.get(p))
            touched = sorted(git(fx, "diff", "--name-only", base_sha, head_sha).splitlines())
            check(differ == touched, f"trees differ in exactly the head commit's files ({len(touched)})")
            for name, t in (("base", tb), ("head", th)):
                model = t.collect()
                check(model.module("pipeline.py") is not None, f"collect() at {name}: module('pipeline.py') is known")
        check(operator_state(fx) == before, "the operator's checkout is unchanged (status, HEAD, index bytes and mtime)")
        check((fx / "pipeline.py").read_text() == "# the operator is editing this\n", "the operator's dirty file is intact")

        t0 = time.monotonic()
        try:
            with Target.open(REPO, real_sha, scratch=tmp / "s") as tr:
                took = time.monotonic() - t0
                check(took < 60, f"open at real_sha {real_sha[:7]} took {took:.1f} s (< 60)")
                check(git(tr.tree, "rev-parse", "HEAD") == real_sha, "the real tree's HEAD is real_sha")
        except Exception as err:   # noqa: BLE001
            check(False, f"open at real_sha: {err}")

    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
