#!/usr/bin/env python3
"""CC-7 (285) acceptance, offline part, written from the ticket's "Acceptance" section.

The contest-bench/cc fixture's 80 claims judged at base_sha and at head_sha by
`scripts/claim_diff.judge_both` — both targets opened, both sides prepared (CC-1/2/5),
voted through `judge.ask_with_packs` (prompt v2, the parser, the quote check), tallied by
`claim_vote.tally` and compared by `compare.classify_delta` — with a fake **perfect
pack-reader**, three voters of it: on each side it answers the key's `truth_<side>`
(`null`: UNSURE), citing a line of the pack it was shown (the probe's line when the pack
holds it, else the first line of the top chunk).

Targets: `fixed` -> FIXED >= 19 of 20, `still` -> STILL >= 9 of 10, `gone` -> GONE 2 of 2,
`new` -> NEW 2 of 2, and no `still` claim FIXED (score_cc's `still_as_fixed` = 0), read
back through `score_cc.score_deltas` from the delta.json `write_outputs` writes. A second
pass with one of the four voters dead (no vote at all) must give the same table.

The live part (three real voters of three families, `claim_diff.py --check` over the
same file) is the operator's:

    python3 scripts/claim_diff.py contest-bench/cc/claims_fixture.json --target DIR \\
        --base <base_sha> --head <head_sha> --profiles a b c --runs 1 --out OUT
    python3 contest-bench/cc/score_cc.py OUT/delta.json contest-bench/cc/claims_fixture.json

with DIR built by `make_fixture.py --build DIR`. No network here: the fakes are the
`completion_fn` of `judge.ask_with_packs`.

    python3 contest-bench/285/acceptance_285.py [--code DIR] [-v]   # DIR: checkout holding tools/claimcheck/
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = HERE.parent / "cc"
REPO = HERE.parent.parent
TARGETS = {"fixed": ("FIXED", 19), "still": ("STILL", 9), "gone": ("GONE", 2), "new": ("NEW", 2)}

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


def reader(key: dict, side: str, judge):
    """The perfect pack-reader of one side: the key's verdict there, cited from the pack shown."""
    def complete(prompt: str) -> str:
        rows = []
        for m in _BLOCK.finditer(prompt):
            n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
            row = key[claim]
            truth = row.get(f"truth_{side}")
            if truth is None:
                rows.append({"id": n, "verdict": "UNSURE"})
                continue
            verdict = "TRUE" if truth else "FALSE"
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, default=REPO)
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()
    code = opts.code.resolve()
    sys.path.insert(0, str(code))
    sys.path.insert(0, str(code / "scripts"))
    sys.path.insert(0, str(CC))
    import claim_diff as cd
    import make_fixture
    import score_cc
    from tools.claimcheck import judge
    from tools.claimcheck.target import Target

    fixture = json.loads((CC / "claims_fixture.json").read_text(encoding="utf-8"))
    probes = {s["id"]: (s["probe"][1] if s.get("probe") else "") for s in make_fixture.SPEC}
    claims = cd.load_claims(CC / "claims_fixture.json")
    key = {c["claim"]: {**c, "probe": probes.get(c["id"], "")} for c in fixture["claims"]}
    failures: list = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=env)

        def vote_with(models):
            def vote(items, side):
                fn = reader(key, side, judge)
                return [{"model": m, "run": 0,
                         **(judge.ask_with_packs(items, 0, 1, fn, batch=10, code_batch=2)
                            if not m.startswith("dead/") else
                            {"votes": {}, "accepted": {}, "rejected": [], "error": "HTTP 404"})}
                        for m in models]
            return vote

        tables = []
        for label, models in (("three voters", ("a/fake", "b/fake", "c/fake")),
                              ("three + one dead", ("a/fake", "b/fake", "c/fake", "dead/x"))):
            with ExitStack() as stack:
                base = stack.enter_context(Target.open(tmp / "fx", fixture["base_sha"], scratch=tmp / "s"))
                head = stack.enter_context(Target.open(tmp / "fx", fixture["head_sha"], scratch=tmp / "s"))
                got = cd.judge_both(claims, base, head, vote_with(models))
                cd.write_outputs(tmp / label.replace(" ", "_"), base, head, got)
            delta = json.loads((tmp / label.replace(" ", "_") / "delta.json").read_text(encoding="utf-8"))
            scored = score_cc.score_deltas(delta, fixture)
            tables.append([d.change for d in got["deltas"]])
            print(f"== {label}: {dict(Counter(d.change for d in got['deltas']))}")
            for d in got["deltas"]:
                want = TARGETS.get(d.expect, (None,))[0]
                if opts.verbose or (want and d.change != want):
                    print(f"  {'ok  ' if d.change == want or not want else 'MISS'} {d.id} "
                          f"expect={d.expect or '-':6} {d.change:8} base={d.base['verdict']}"
                          f"{'*' if d.base['unanimous'] else ''} head={d.head['verdict']}"
                          f"{'*' if d.head['unanimous'] else ''} {d.evidence_head[:2]}")
            for expect, (change, need) in TARGETS.items():
                hit = scored["confusion"].get(expect, {}).get(change, 0)
                line = f"{label}: {expect} -> {change} {hit}/{scored['expected'].get(expect, 0)} (target >= {need})"
                print(("ok   " if hit >= need else "FAIL ") + line)
                if hit < need:
                    failures.append(line)
            saf = scored["delta"]["still_as_fixed"]
            line = f"{label}: still_as_fixed = {saf} (target 0); fix_right = {scored['delta']['fix_right']}"
            print(("ok   " if saf == 0 else "FAIL ") + line)
            if saf:
                failures.append(line)
        if tables[0] != tables[1]:
            failures.append("a dead fourth voter changed the table")
            print("FAIL a dead fourth voter changed the table")
        else:
            print("ok   a dead fourth voter changes nothing")
    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
