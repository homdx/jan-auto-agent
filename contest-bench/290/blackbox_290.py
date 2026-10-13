#!/usr/bin/env python3
"""CC-8 (290) black box: the candidate's command line over a REAL votes.json, nothing imported.

`votes_live.json` is the output of CC-6 on the fixture at its base commit (three kenary voters, live,
round 285): 70 claims, 61 settled (unanimous), 9 not. The fixture repo is rebuilt, then, as processes:

    claimcheck_truth.py votes.json --out truth.csv --pending pending.md --target FX [--append]
    run_claim_check.sh r.md --target . --ref HEAD --truth t.csv --dry-run

Checks (the ticket's acceptance + the parts of its test table a process can show):
  rows = the unanimous claims, none for any other; `truth` right on >= 97 % against the key;
  header exactly finding,truth,checked_by,duplicate_of,how; `pending.md` names every unsettled claim
  once, each with a chunk id; votes.json byte-for-byte unchanged; `harvest_report.load_truth` reads
  the CSV; --append twice changes nothing; a disagreeing existing row -> exit 3, old row kept;
  a dead voter is not in checked_by; an empty votes file writes the header only; the dry run writes nothing.

    python3 contest-bench/290/blackbox_290.py --code DIR [-v]   # DIR: a checkout with scripts/claimcheck_truth.py
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CC = HERE.parent / "cc"
HEADER = ["finding", "truth", "checked_by", "duplicate_of", "how"]
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
fails: list = []


def check(ok: bool, line: str) -> None:
    print(("ok   " if ok else "FAIL ") + line)
    if not ok:
        fails.append(line)


def run(code: Path, args: list, cwd: Path):
    return subprocess.run([sys.executable, str(code / "scripts" / "claimcheck_truth.py"), *map(str, args)],
                          cwd=cwd, capture_output=True, text=True, env=ENV, timeout=900)


def rows_of(path: Path) -> list:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", type=Path, required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    opts = ap.parse_args()
    code = opts.code.resolve()
    votes = json.loads((HERE / "votes_live.json").read_text(encoding="utf-8"))
    key = {c["claim"]: c for c in json.loads((CC / "claims_fixture.json").read_text(encoding="utf-8"))["claims"]}
    settled = [c for c in votes["claims"] if c["unanimous"]]
    unsettled = [c for c in votes["claims"] if not c["unanimous"]]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        subprocess.run([sys.executable, str(CC / "make_fixture.py"), "--build", str(tmp / "fx")],
                       check=True, capture_output=True, env=ENV)
        votes["target"]["repo"] = str(tmp / "fx")
        vf = tmp / "votes.json"
        vf.write_text(json.dumps(votes), encoding="utf-8")
        digest = hashlib.sha256(vf.read_bytes()).hexdigest()
        out, pend = tmp / "truth.csv", tmp / "pending.md"
        p = run(code, [vf, "--out", out, "--pending", pend, "--target", tmp / "fx"], tmp)
        check(p.returncode == 0, f"exit 0 (got {p.returncode}) {(p.stderr or '')[-300:] if p.returncode else ''}")
        if not out.is_file() or not pend.is_file():
            print("FAIL no output files")
            return 1
        header = out.read_text(encoding="utf-8").splitlines()[0].split(",")
        check(header == HEADER, f"header {header}")
        rows = rows_of(out)
        check(len(rows) == len(settled),
              f"rows {len(rows)} for {len(settled)} unanimous claims, {len(unsettled)} unsettled")
        check(all(r["truth"] in ("REAL", "FALSE") for r in rows), "truth values are REAL or FALSE")
        check(all(r["finding"] for r in rows), "every row has a finding")
        check(all(r["checked_by"].startswith("claimcheck:") and "@d36d59e" in r["checked_by"] for r in rows),
              f"checked_by names families and sha7, e.g. {rows[0]['checked_by'] if rows else None}")
        check(rows and all(len(r["how"]) <= 400 and "\n" not in r["how"] for r in rows), "how is one line")
        # right against the key, claim by claim, in file order of the settled claims
        right = sum(1 for c, r in zip(settled, rows)
                    if r["truth"] == ("REAL" if key[c["claim"]]["truth_base"] else "FALSE"))
        check(len(rows) == len(settled) and right / max(1, len(rows)) >= 0.97,
              f"truth right on {right}/{len(rows)} against the key (need >= 97 %)")
        text = pend.read_text(encoding="utf-8")
        spots = sorted((text.find(c["claim"]), text.count(c["claim"]), c) for c in unsettled)
        check(all(n == 1 for _, n, _ in spots), f"pending names each of {len(unsettled)} unsettled claims once: "
              f"{[n for _, n, _ in spots]}")
        check(not any(c["claim"] in text for c in settled), "pending holds no settled claim")
        ends = [s for s, _, _ in spots[1:]] + [len(text)]
        with_chunk = sum(1 for (s, _, c), e in zip(spots, ends)
                         if s >= 0 and re.search(r"\b(src|git|diff):\S+", text[s:e]))
        packed = sum(1 for c in unsettled if c.get("kind") != "world")
        check(with_chunk >= packed, f"{with_chunk} pending entries show a chunk id ({packed} have a pack)")
        check(hashlib.sha256(vf.read_bytes()).hexdigest() == digest, "votes.json unchanged")
        # the readers of the file
        sys.path.insert(0, str(code / "scripts"))
        try:
            import harvest_report
            truth = harvest_report.load_truth(str(out))
            # a finding with one truth is read; one that two claims about one place settled REAL and FALSE is
            # left unscored (review batch m: the last row was its truth, the order of the rows decided)
            said = {}
            for r in rows:
                said.setdefault(r["finding"], set()).add(r["truth"])
            one = {f for f, ts in said.items() if len(ts) == 1}
            check(set(truth) == one, f"harvest_report.load_truth reads {len(truth)} findings "
                                     f"({len(said) - len(one)} with two truths unscored)")
        except Exception as exc:  # noqa: BLE001
            check(False, f"harvest_report.load_truth raised {exc!r}")
        # --append: idempotent, conflict
        before = out.read_bytes()
        p = run(code, [vf, "--out", out, "--pending", pend, "--target", tmp / "fx", "--append"], tmp)
        check(p.returncode == 0 and out.read_bytes() == before, f"--append again: exit {p.returncode}, file unchanged")
        flipped = [dict(r, truth="FALSE" if r["truth"] == "REAL" else "REAL") if i == 0 else r for i, r in enumerate(rows)]
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=HEADER)
            w.writeheader()
            w.writerows(flipped)
        old = out.read_bytes()
        p = run(code, [vf, "--out", out, "--pending", pend, "--target", tmp / "fx", "--append"], tmp)
        check(p.returncode == 3, f"existing row disagrees -> exit {p.returncode} (need 3)")
        check(rows_of(out)[0]["truth"] == flipped[0]["truth"], "the old row is kept")
        # dead voter
        dead = json.loads(vf.read_text(encoding="utf-8"))
        dead["results"].append({"model": "ghost/dead-model-9", "run": 0, "votes": {}, "error": "404"})
        for c in dead["claims"]:
            c["by_model"]["ghost/dead-model-9"] = "UNSURE"
        df = tmp / "dead.json"
        df.write_text(json.dumps(dead), encoding="utf-8")
        d_out = tmp / "dead.csv"
        run(code, [df, "--out", d_out, "--pending", tmp / "dead.md", "--target", tmp / "fx"], tmp)
        dr = rows_of(d_out) if d_out.is_file() else []
        check(bool(dr) and all("dead" not in r["checked_by"] and "ghost" not in r["checked_by"] for r in dr),
              "a dead voter is not in checked_by")
        # empty votes
        ef = tmp / "empty.json"
        ef.write_text(json.dumps({"target": votes["target"], "results": [], "claims": []}), encoding="utf-8")
        e_out = tmp / "empty.csv"
        p = run(code, [ef, "--out", e_out, "--pending", tmp / "empty.md", "--target", tmp / "fx"], tmp)
        check(p.returncode == 0 and e_out.is_file() and e_out.read_text(encoding="utf-8").strip().split(",") == HEADER,
              "empty votes -> header only")
        # shell script dry run
        sandbox = tmp / "repo"
        (sandbox / "scripts").mkdir(parents=True)
        for f in (code / "scripts").glob("*"):
            if f.is_file():
                (sandbox / "scripts" / f.name).write_bytes(f.read_bytes())
        (sandbox / "r.md").write_text("report\n", encoding="utf-8")
        p = subprocess.run(["bash", "scripts/run_claim_check.sh", "r.md", "--target", ".", "--ref", "HEAD",
                            "--truth", "t.csv", "--dry-run"], cwd=sandbox, capture_output=True, text=True, env=ENV)
        names = sorted(x.name for x in sandbox.rglob("*") if x.is_file() and x.parent.name != "scripts")
        check(p.returncode == 0 and "claimcheck_truth" in p.stdout and "claim_vote" in p.stdout
              and "claim_extract" in p.stdout, f"dry run exit {p.returncode}, prints extract, vote and truth")
        check(names == ["r.md"] and not (sandbox / "claim-check-out").exists(), f"dry run wrote nothing {names}")
        vline = next((ln for ln in p.stdout.splitlines() if "claim_vote" in ln), "")
        check("--dry-run" not in vline and "--truth" not in vline, "vote command carries neither --dry-run nor --truth")
        if opts.verbose:
            print(p.stdout[-800:])
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
