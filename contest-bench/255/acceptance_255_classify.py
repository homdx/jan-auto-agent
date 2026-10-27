#!/usr/bin/env python3
"""CC-1 (255) acceptance: does `classify` agree with the bench's key?

Written from the ticket alone (docs/claim-check/tickets/255-cc-1-anchors.md,
"Acceptance"). Against `contest-bench/cc/claims_fixture.json` and the fixture
repository at its base sha:

  * `classify` agrees with each claim's `kind` on >= 95 % of the 80;
  * it agrees on EVERY dangling claim (code, and the anchor does not resolve);
  * no `world` claim is classified `code` (or `mixed`);

then the same on the 30 real claims at `real_sha` of this repository (>= 90 %).

    python3 contest-bench/255/acceptance_255_classify.py [--code DIR]

`--code DIR` is the checkout holding the entry's `tools/claimcheck/` (default: the
checkout this file is in). The repositories the claims are judged against are built
here, as git checkouts, so a commit claim can resolve.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
BENCH = REPO / "contest-bench" / "cc"
WANT = {"code": "code", "world": "world", "mixed": "mixed",
        "dangling": "code", "ticket": "code", "commit": "code"}


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def load_fixture_builder():
    spec = importlib.util.spec_from_file_location("cc_make_fixture", BENCH / "make_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def checkout(source: Path, sha: str, dest: Path) -> Path:
    sh("git", "clone", "-q", "--shared", str(source), str(dest))
    sh("git", "checkout", "-q", sha, cwd=dest)
    return dest


def judge(name, claims, view, anchors, need, failures):
    right = 0
    for c in claims:
        resolved = anchors.resolve_anchors(anchors.extract_anchors(c["claim"]), view)
        got = anchors.classify(c["claim"], resolved)
        dangling = anchors.is_dangling(resolved)
        want = WANT[c["kind"]]
        ok = got == want
        right += ok
        if c["kind"] == "dangling" and not (got == "code" and dangling):
            failures.append(f"{name} {c['id']}: dangling claim classified {got}, dangling={dangling}")
        if c["kind"] == "world" and got != "world":
            failures.append(f"{name} {c['id']}: a world claim classified {got}")
        if not ok:
            print(f"  miss {c['id']} key={c['kind']} got={got}: {c['claim'][:90]}")
    share = right / len(claims)
    print(f"{name}: {right}/{len(claims)} = {share:.0%} (need >= {need:.0%})")
    if share < need:
        failures.append(f"{name}: {share:.0%} < {need:.0%}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--code", type=Path, default=REPO, help="checkout that holds tools/claimcheck/")
    args = ap.parse_args(argv)
    sys.path.insert(0, str(args.code.resolve()))
    from tools.claimcheck import anchors   # noqa: E402

    fixture = json.loads((BENCH / "claims_fixture.json").read_text(encoding="utf-8"))
    real = json.loads((BENCH / "claims_real.json").read_text(encoding="utf-8"))
    failures: list = []
    with tempfile.TemporaryDirectory(prefix="acc255-") as tmp:
        tmp = Path(tmp)
        built = load_fixture_builder().build_fixture(tmp / "fixture-repo")
        assert built.base_sha == fixture["base_sha"], "the fixture's base sha moved"
        base = checkout(tmp / "fixture-repo", fixture["base_sha"], tmp / "fixture-base")
        judge("fixture", fixture["claims"], anchors.PathRepoView(base), anchors, 0.95, failures)
        tree = checkout(REPO, real["real_sha"], tmp / "real-tree")
        judge("real", real["claims"], anchors.PathRepoView(tree), anchors, 0.90, failures)
    for line in failures:
        print("FAIL", line)
    print("PASS" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
