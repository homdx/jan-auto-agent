#!/usr/bin/env python3
"""CC-8 (290) acceptance, offline part, written from the ticket's "Acceptance" section.

The contest-bench/cc fixture's 80 claims judged at head_sha through CC-6's chain
(`claim_vote.prepare_target`, `judge.ask_with_packs`, `claim_vote.tally`), written as
`claim_vote.main_target` writes votes.json, then `scripts/claimcheck_truth.py` over it.
The voters are **not** all perfect (CC-7's lesson: a perfect panel never dissents, so the
"plurality is not settled" path is never walked):

  * three perfect pack-readers (`acceptance_285.reader`, side ``head``);
  * one **noisy** voter: the perfect answer, flipped on every 7th claim;
  * one **dead** voter (no vote at all, an HTTP error) — not a voter, not in ``checked_by``.

Targets: `truth.csv` has a row for every unanimous claim and none for any other; against the
fixture's key (`truth_head`) the ``truth`` column is right on >= 97 % of its rows; `pending.md`
lists every non-unanimous claim exactly once, each with at least one chunk id when a pack
existed; `harvest_report.py <csv> --truth truth.csv --report` runs to completion; a second
`--append` run adds nothing.

The live part is the operator's: `run_cc.sh` over the fixture with three real voters, then

    python3 scripts/claimcheck_truth.py OUT/votes.json --out OUT/truth.csv --target DIR

    python3 contest-bench/290/acceptance_290.py [--code DIR] [-v]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = HERE.parent / "cc"
REPO = HERE.parent.parent
RIGHT_TARGET = 0.97
NOISY_EVERY = 7


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, default=REPO)
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()
    code = opts.code.resolve()
    for p in (code, code / "scripts", CC, HERE.parent / "285"):
        sys.path.insert(0, str(p))
    import acceptance_285 as a285
    import claim_vote as cv
    import claimcheck_truth as ct
    import make_fixture
    from tools.claimcheck import judge
    from tools.claimcheck import truth as tr
    from tools.claimcheck.pack import PackBudget
    from tools.claimcheck.target import Target

    fixture = json.loads((CC / "claims_fixture.json").read_text(encoding="utf-8"))
    probes = {s["id"]: (s["probe"][1] if s.get("probe") else "") for s in make_fixture.SPEC}
    key = {c["claim"]: {**c, "probe": probes.get(c["id"], "")} for c in fixture["claims"]}
    claims = [{"id": c["id"], "claim": c["claim"], "truth": c.get("truth_head")} for c in fixture["claims"]]
    failures: list = []

    def check(ok: bool, line: str) -> None:
        print(("ok   " if ok else "FAIL ") + line)
        if not ok:
            failures.append(line)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=env)
        perfect = a285.reader(key, "head", judge)

        def noisy(prompt: str) -> str:
            rows = json.loads(perfect(prompt))
            for r in rows:
                n = int(r["id"])
                block = prompt.split(f"=== CLAIM {n} ===\n", 1)[1].split("\n", 1)[0]
                idx = next(i for i, c in enumerate(claims) if c["claim"] == block)
                if idx % NOISY_EVERY == 0 and r["verdict"] in ("TRUE", "FALSE"):
                    r["verdict"] = "FALSE" if r["verdict"] == "TRUE" else "TRUE"
            return json.dumps(rows)

        with Target.open(tmp / "fx", fixture["head_sha"], scratch=tmp / "s") as target:
            items, meta = cv.prepare_target(claims, target, PackBudget())
            results = []
            # three families: a truth row needs them (EPIC-CC principle 6); "perfect-1..3" were one
            for model, fn in (("a/alpha-1", perfect), ("b/beta-2", perfect), ("c/gamma-3", perfect),
                              ("d/noisy-1", noisy)):
                got = judge.ask_with_packs(items, 0, 1, fn, batch=10, code_batch=2)
                results.append({"model": model, "run": 0, "votes": got["votes"],
                                "accepted": got["accepted"], "rejected": got["rejected"]})
            results.append({"model": "e/dead-1", "run": 0, "votes": {}, "accepted": {}, "rejected": [],
                            "error": "HTTP 402"})
            table = cv.tally(claims, results, meta=meta)
            votes = {"target": {"repo": str(tmp / "fx"), "ref": fixture["head_sha"], "sha": target.sha,
                                "base": None, "warnings": []}, "results": results, "claims": table}
        vpath = tmp / "votes.json"
        vpath.write_text(json.dumps(votes, ensure_ascii=False, indent=1), encoding="utf-8")
        votes = json.loads(vpath.read_text(encoding="utf-8"))        # the file's shape: string keys
        before = vpath.read_bytes()

        out = tmp / "out" / "truth.csv"
        rc = ct.main([str(vpath), "--out", str(out), "--scratch", str(tmp / "s2"), "--repo-root", str(tmp)])
        check(rc == 0, f"claimcheck_truth.py exit {rc} (want 0)")
        check(vpath.read_bytes() == before, "votes.json unchanged by the command")
        rows = list(csv.DictReader(out.open(encoding="utf-8")))
        settled = [c for c in votes["claims"] if c["unanimous"] is True and c["verdict"] in ("TRUE", "FALSE")]
        unsettled = [c for c in votes["claims"] if c not in settled]
        print(f"== {len(votes['claims'])} claims: {len(settled)} unanimous, {len(unsettled)} not; "
              f"dissents {sum(c['verdict'] in ('TRUE', 'FALSE') for c in unsettled)}")
        check(len(rows) == len(settled), f"a row for every unanimous claim and none other: {len(rows)} rows, "
                                         f"{len(settled)} unanimous")
        right = sum((r["truth"] == "REAL") == bool(c["truth"]) for r, c in zip(rows, settled))
        rate = right / len(rows) if rows else 0.0
        for r, c in zip(rows, settled):
            if opts.verbose or (r["truth"] == "REAL") != bool(c["truth"]):
                print(f"  {'ok  ' if (r['truth'] == 'REAL') == bool(c['truth']) else 'WRONG'} "
                      f"{c.get('id')} {r['finding']:40} {r['truth']:5} key={c['truth']} {r['how'][:60]}")
        check(rate >= RIGHT_TARGET, f"truth right on {right}/{len(rows)} = {rate:.1%} (target >= {RIGHT_TARGET:.0%})")
        check("dead" not in rows[0]["checked_by"] if rows else False,
              f"checked_by {rows[0]['checked_by'] if rows else '-'} leaves the dead voter out")

        md = (out.parent / "pending.md").read_text(encoding="utf-8")
        entries = md.split("\n### claim ")[1:]
        once = all(md.count(f"> {c['claim'].splitlines()[0]}\n") == 1 for c in unsettled)
        check(len(entries) == len(unsettled) and once,
              f"pending.md lists every non-unanimous claim exactly once ({len(entries)} entries, "
              f"{len(unsettled)} unsettled)")
        with_pack = {i for i, (_t, p) in enumerate(items) if p is not None and p.chunks}
        missing = [e.split("\n", 1)[0] for e in entries
                   if int(e.split(" ", 1)[0]) - 1 in with_pack and "\n- `" not in e]
        check(not missing, f"every pending claim with a pack lists a chunk id (missing: {missing[:3]})")
        for c in settled:
            if f"> {c['claim'].splitlines()[0]}\n" in md:
                failures.append(f"settled claim {c.get('id')} in pending.md")

        rc = ct.main([str(vpath), "--out", str(out), "--append", "--scratch", str(tmp / "s2"),
                      "--repo-root", str(tmp)])
        check(rc == 0 and len(list(csv.DictReader(out.open(encoding="utf-8")))) == len(rows),
              f"a second --append adds nothing (exit {rc})")

        reviewer = tmp / "validation-v1-fake.csv"
        with reviewer.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["task_id", "verdict", "severity", "file", "symbol"])
            for r in rows[:5]:
                f, _, s = r["finding"].partition("::")
                w.writerow([r["finding"], "CONFIRMED", "LOW", f, s])
        proc = subprocess.run([sys.executable, str(code / "scripts" / "harvest_report.py"), str(reviewer),
                               "--truth", str(out), "--report", str(tmp / "harvest.md")],
                              capture_output=True, text=True, timeout=120)
        check(proc.returncode == 0 and (tmp / "harvest.md").exists(),
              f"harvest_report.py --truth truth.csv --report runs to completion (exit {proc.returncode}"
              f"{': ' + proc.stderr.strip().splitlines()[-1] if proc.returncode and proc.stderr.strip() else ''})")
    print("PASS" if not failures else f"FAIL ({len(failures)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
