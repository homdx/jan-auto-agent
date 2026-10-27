#!/usr/bin/env python3
"""CC-6 (280) acceptance, offline part, written from the ticket's "Acceptance" section.

The whole chain over the contest-bench/cc fixture's 80 claims at base_sha — anchors
extracted and resolved in the pinned tree (CC-1 + CC-2), `classify`, `build_pack` (CC-5),
`build_prompt_v2`, `parse_votes_v2`, `verify_quotes` and `claim_vote.tally` (CC-6) — with
two deterministic fake voters in place of the models, three of each:

  * a **perfect pack-reader**: it knows the answer (the key's `truth_base`) and, for a
    claim with a pack, cites it from the pack it was shown — the probe's line when the
    pack holds it, else the first line of the top chunk. Target: >= 95 % of the 80 claims
    decided unanimously and right (it measures that a right, honestly-quoted answer
    survives the chain: the packs, the prompt, the parser and the quote check).
  * a **lazy** voter: `TRUE` on everything, naming the top chunk and quoting the claim.
    Target: the quote check downgrades >= 95 % of its verdicts on claims with a pack.

No exception anywhere in the chain is the third condition. No network: the fakes are the
`completion_fn` of `judge.ask_with_packs`.

    python3 contest-bench/280/acceptance_280.py [--code DIR] [-v]   # DIR: checkout holding tools/claimcheck/
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
PERFECT_TARGET = 0.95
LAZY_TARGET = 0.95

_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
_LABEL = re.compile(r"^\[\[(.+?)\]\]  \(\w+\)$", re.M)
_GUTTER = re.compile(r"^ *\d*\| ?")


def chunks_of(evidence: str) -> list:
    """(id, [content lines]) of each chunk in a rendered pack, gutters dropped."""
    labels = list(_LABEL.finditer(evidence))
    out = []
    for k, m in enumerate(labels):
        end = labels[k + 1].start() if k + 1 < len(labels) else len(evidence)
        lines = []
        for line in evidence[m.end():end].strip("\n").splitlines():
            if line.startswith("# …") or line.startswith("(evidence trimmed"):
                continue
            lines.append(_GUTTER.sub("", line, count=1).strip())
        out.append((m.group(1), [ln for ln in lines if len(ln) >= 8]))
    return out


def perfect(key: dict, judge):
    """The perfect pack-reader: the key's verdict, cited from the pack it was shown."""
    def complete(prompt: str) -> str:
        rows = []
        for m in _BLOCK.finditer(prompt):
            n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
            row = key[claim]
            verdict = "TRUE" if row["truth_base"] else "FALSE"
            if judge.WORLD_BLOCK in evidence:
                rows.append({"id": n, "verdict": verdict})
                continue
            chunks = [c for c in chunks_of(evidence) if c[1]]
            if not chunks:
                rows.append({"id": n, "verdict": "UNSURE"})
                continue
            probe = [ln.strip() for ln in (row.get("probe") or "").splitlines() if len(ln.strip()) >= 8]
            cited = next(((cid, ln) for ln in probe for cid, lines in chunks if ln in lines), None)
            cid, quote = cited or (chunks[0][0], chunks[0][1][0])
            rows.append({"id": n, "verdict": verdict, "chunk": f"[[{cid}]]", "quote": quote[:200]})
        return json.dumps(rows)
    return complete


def lazy(prompt: str) -> str:
    """TRUE on everything: the top chunk named, the claim's own words as the quote."""
    rows = []
    for m in _BLOCK.finditer(prompt):
        n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
        label = _LABEL.search(evidence)
        rows.append({"id": n, "verdict": "TRUE", "chunk": label.group(1) if label else "",
                     "quote": claim[:200]})
    return json.dumps(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, default=REPO)
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()
    code = opts.code.resolve()
    sys.path.insert(0, str(code))
    sys.path.insert(0, str(code / "scripts"))
    sys.path.insert(0, str(CC))
    import claim_vote as cv
    import make_fixture
    from tools.claimcheck import judge
    from tools.claimcheck.pack import PackBudget
    from tools.claimcheck.target import Target

    fixture = json.loads((CC / "claims_fixture.json").read_text(encoding="utf-8"))
    probes = {s["id"]: (s["probe"][1] if s.get("probe") else "") for s in make_fixture.SPEC}
    claims = fixture["claims"]
    key = {c["claim"]: {**c, "probe": probes.get(c["id"], "")} for c in claims}
    failures: list = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=env)
        with Target.open(tmp / "fx", fixture["base_sha"], scratch=tmp / "s") as target:
            items, meta = cv.prepare_target(claims, target, PackBudget())
        packed = [i for i, (_c, p) in enumerate(items) if p is not None]

        def run(fn) -> tuple:
            results = []
            for model in ("a/fake", "b/fake", "c/fake"):
                got = judge.ask_with_packs(items, 0, 1, fn, batch=10, code_batch=2)
                results.append({"model": model, "run": 0, **got})
            return cv.tally(claims, results, meta=meta), results

        table, _results = run(perfect(key, judge))
        right = 0
        for c, t in zip(claims, table):
            ok = t["unanimous"] and t["verdict"] == ("TRUE" if c["truth_base"] else "FALSE")
            right += ok
            if opts.verbose or not ok:
                print(f"  {'ok  ' if ok else 'MISS'} {c['id']} {t['kind']:5} {t['verdict']:7} "
                      f"rejected={t['rejected']} evidence={t['evidence'][:2]}")
        share = right / len(claims)
        line = f"perfect reader: {right}/{len(claims)} right and unanimous = {share:.0%} (target {PERFECT_TARGET:.0%})"
        print(("ok   " if share >= PERFECT_TARGET else "FAIL ") + line)
        if share < PERFECT_TARGET:
            failures.append(line)

        _table, results = run(lazy)
        caught = {(r["model"], row["claim"]) for r in results for row in r["rejected"]}
        verdicts = len(packed) * len(results)
        share = len(caught) / verdicts if verdicts else 0.0
        line = (f"lazy voter: {len(caught)}/{verdicts} code verdicts downgraded = {share:.0%} "
                f"(target {LAZY_TARGET:.0%}; {len(packed)} of {len(claims)} claims carry a pack)")
        print(("ok   " if share >= LAZY_TARGET else "FAIL ") + line)
        if share < LAZY_TARGET:
            failures.append(line)
    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
