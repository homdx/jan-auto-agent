#!/usr/bin/env python3
"""CC-3 (265) acceptance, written from the ticket's "Acceptance" section.

  * contest-bench/cc fixture at base_sha, the 50 code claims: the lines the claim's `how`
    names for base ("base: read store.py:22-23") are printed in the claim's chunks
    (a line behind an omission marker does not count). Target: >= 90 % of the claims,
    and the chunk text per claim <= 6000 characters for >= 95 % of them.
  * this repository at real_sha, every claim whose `how` reads a file at lines
    ("read tools/contest/gates.py:468-472"): the same check, target >= 80 %. A claim
    decided by running code ("run Policy.decide ...") names no lines and is not counted.

The anchors are the pipeline's own: `extract_anchors(claim)` resolved against the pinned
tree (CC-1 + CC-2), not the hand-written `anchors` list, so a miss of the extractor
shows here too.

    python3 contest-bench/265/acceptance_265.py [--code DIR] [-v]   # DIR: checkout holding tools/claimcheck/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = HERE.parent / "cc"
REPO = HERE.parent.parent
CHAR_CAP = 6000
_READ = re.compile(r"read\s+([\w./-]+\.\w+):(\d+)(?:-(\d+))?")


def deciding_lines(how: str, side: str = "") -> list:
    """[(path, first, last)] the claim's `how` reads; for a fixture claim, the *side* part."""
    if side:
        part = [p for p in how.split(";") if p.strip().startswith(side + ":")]
        how = part[0] if part else ""
    return [(m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))) for m in _READ.finditer(how)]


def printed(chunks, path: str, line: int) -> bool:
    gutter = re.compile(rf"^ *{line}\| ", re.M)
    return any(c.path == path and c.start <= line <= c.end and gutter.search(c.text) for c in chunks)


def score(name: str, claims: list, view, side: str, target: float, verbose: bool, failures: list) -> None:
    from tools.claimcheck.anchors import extract_anchors, resolve_anchors
    from tools.claimcheck.evidence_source import source_chunks

    counted = hit = within = 0
    for c in claims:
        need = deciding_lines(c.get("how", ""), side)
        if not need:
            continue
        counted += 1
        resolved = resolve_anchors(extract_anchors(c["claim"]), view)
        chunks = source_chunks(resolved, view, claim=c["claim"])
        chars = sum(len(ch.text) for ch in chunks)
        ok = all(printed(chunks, p, n) for p, a, b in need for n in range(a, b + 1))
        hit += ok
        within += chars <= CHAR_CAP
        if verbose or not ok:
            print(f"  {'ok  ' if ok else 'MISS'} {c['id']} {chars:5d} ch  need {need}  "
                  f"chunks {[ch.id for ch in chunks]}")
    recall = hit / counted if counted else 0.0
    share = within / counted if counted else 0.0
    line = f"{name}: recall {hit}/{counted} = {recall:.0%} (target {target:.0%}); " \
           f"<= {CHAR_CAP} ch {within}/{counted} = {share:.0%} (target 95%)"
    print(("ok   " if recall >= target and share >= 0.95 else "FAIL ") + line)
    if recall < target or share < 0.95:
        failures.append(line)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, default=REPO)
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()
    sys.path.insert(0, str(opts.code.resolve()))
    from tools.claimcheck.target import Target

    fixture = json.loads((CC / "claims_fixture.json").read_text(encoding="utf-8"))
    real = json.loads((CC / "claims_real.json").read_text(encoding="utf-8"))
    failures: list = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=env)
        code = [c for c in fixture["claims"] if c["kind"] == "code"]
        with Target.open(tmp / "fx", fixture["base_sha"], scratch=tmp / "s") as t:
            score(f"fixture base, {len(code)} code claims", code, t.view(), "base", 0.90, opts.verbose, failures)
        with Target.open(REPO, real["real_sha"], scratch=tmp / "s") as t:
            score(f"real {real['real_sha'][:7]}, claims that read lines", real["claims"], t.view(), "",
                  0.80, opts.verbose, failures)
    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
