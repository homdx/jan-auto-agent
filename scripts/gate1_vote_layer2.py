#!/usr/bin/env python3
"""CC-10 layer 2, launch and forget: Gate 1 on a known-answer set, the vote off and on with 3/5/7/9(/11) voters.

    python3 scripts/gate1_vote_layer2.py --detach          # starts it in the background and returns
    tail -f contest-out/gate1-layer2/<UTC>/progress.log    # optional; nothing needs watching
    cat contest-out/gate1-layer2/<UTC>/SUMMARY.md          # the answer, when DONE appears in progress.log

What it does, per variant (``off``, ``on-3``, ``on-5``, ``on-7``, ``on-9``, ``on-11``), one after the other
(the free providers share a rate limit, so the variants never run side by side):

  1. a fresh clone of the target at the recorded sha, the recorded plan copied to ``.agent/plan.json``;
  2. an ini made from ``agents.ini`` with a real Gate 1 reader, the variant's voters and ``[gate1]
     claim_vote`` / ``vote_profiles`` / ``vote_accept`` (written 0600 into the output directory only:
     it carries keys from ``contest.local.ini`` and is never printed or committed);
  3. ``main.py --validate-plan`` on the clone, the same Gate 1 path ``--auto`` uses at plan time;
  4. the tasks it removed are read off its report and scored against the known answers.

The known-answer set is ``contest-bench/cc/gate1_known_answers/`` (``plan.json``, ``truth.csv``, ``TARGET``).
Pass rule of the ticket, per ``on`` variant against ``off``: false positives let through are not more, true bugs
rejected are not more, and the vote decided at least a third of the candidates. Anything else leaves
``[gate1] claim_vote = off``.

Exit code: 0 when every variant ran and every ``on`` variant passed, 1 when one failed the rule, 2 when a variant
could not run (the output directory says which; ``--resume DIR`` runs only what is missing).
"""

from __future__ import annotations

import argparse
import configparser
import csv
import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWN = ROOT / "contest-bench" / "cc" / "gate1_known_answers"
OUT_BASE = ROOT / "contest-out" / "gate1-layer2"

P = "claim_vote_llm."
#: voters by set size, by measured wrong rate (docs/claim-check/RUNBOOK.md, "Voters"); each set holds the smaller one
ROSTER = [
    "kenary_step_5_preview_free", "kenary_muse_spark_1_3_contributor_free", "kenary_nemotron_3_ultra_550b_a55b_free",   # 3: the default
    "kenary_agnes_3_0_flash_free", "kenary_hy3_free",                                                                  # 5
    "kenary_nemotron_3_super_120b_a12b_free", "vercel_8080_inclusionai_ling_3_1_flash_free",                           # 7
    "kenary_agnes_2_5_flash_free", "kenary_mimo_v2_6_flash_free",                                                      # 9
    "kenary_laguna_s_2_1_free", "kenary_north_mini_code_free",                                                         # 11: two weak voters
]
DEFAULT_SETS = "3:all,5:committed,7:committed,9:committed"
READER = "kenary_agnes_3_0_flash_free"
CHECKED = re.compile(r"Checked (\d+) task\(s\): (\d+) confirmed still needed, (\d+) removed")
REMOVED = re.compile(r"^\s+- (\S+) \(([^)]*)\): (.*)$")
VOTE = re.compile(r"Gate 1 vote: asked (\d+), decided (\d+), overridden by the reader (\d+), requests (\d+)")


def log(out: Path, msg: str) -> None:
    line = f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(out / "progress.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def parse_sets(text: str) -> list:
    """``3:all,5:committed,7`` -> [(3, 'all'), (5, 'committed'), (7, 'committed')]; ``0`` is not a set (off always runs)."""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        n, _, rule = part.partition(":")
        n = int(n)
        if not 3 <= n <= len(ROSTER):
            raise SystemExit(f"set size {n}: from 3 to {len(ROSTER)}")
        out.append((n, rule or ("all" if n == 3 else "committed")))
    return out


def load_truth(known: Path) -> dict:
    path = known / "truth.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["id"]: r["truth"].strip().upper() for r in csv.DictReader(fh)
                if r.get("truth", "").strip().upper() in ("REAL", "FALSE")}


def build_ini(dest: Path, local: configparser.ConfigParser, base_ini: Path, mode: str,
              voters: list, accept: str, reader: str) -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(base_ini)
    rd = local[P + reader]
    for key in ("base_url", "api_key", "model"):
        cfg["gate1_llm"][key] = rd[key]
    cfg["gate1"]["presence_llm_profile"] = "gate1_llm"
    cfg["gate1"]["claim_vote"] = mode
    cfg["gate1"]["vote_accept"] = accept
    if voters:
        for v in voters:
            if not cfg.has_section(P + v):
                cfg.add_section(P + v)
            for key, val in local[P + v].items():
                cfg.set(P + v, key, val)
        cfg["gate1"]["vote_profiles"] = ", ".join(P + v for v in voters)
    with open(dest, "w", encoding="utf-8") as fh:
        cfg.write(fh)
    os.chmod(dest, 0o600)


def prepare_target(work: Path, repo: Path, sha: str, plan: Path) -> None:
    subprocess.run(["git", "clone", "-q", str(repo), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", sha], check=True)
    (work / ".agent").mkdir(exist_ok=True)
    shutil.copy(plan, work / ".agent" / "plan.json")


def run_variant(out: Path, name: str, known: Path, repo: Path, sha: str, base_ini: Path,
                local, voters: list, accept: str, reader: str, timeout: int) -> dict:
    vdir = out / name
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    ini = vdir / "agents.ini"
    build_ini(ini, local, base_ini, "off" if name == "off" else "on", voters, accept, reader)
    prepare_target(vdir / "target", repo, sha, known / "plan.json")
    started = time.time()
    with open(vdir / "run.log", "w", encoding="utf-8") as fh:
        rc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--validate-plan",
                             "--base", str(vdir / "target"), "--config", str(ini)],
                            cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout).returncode
    text = (vdir / "run.log").read_text(encoding="utf-8", errors="replace")
    res = {"variant": name, "rc": rc, "seconds": round(time.time() - started), "voters": voters,
           "accept": accept, "removed": {}, "inconclusive": []}
    m = CHECKED.search(text)
    if m:
        res["checked"], res["kept_n"], res["removed_n"] = (int(g) for g in m.groups())
    for line in text.splitlines():
        r = REMOVED.match(line)
        if r and r.group(1).startswith("AUTO-"):
            res["removed"][r.group(1)] = f"{r.group(2)}: {r.group(3)[:160]}"
    v = VOTE.search(text)
    if v:
        res["votes_asked"], res["votes_decided"], res["votes_overridden"], res["votes_requests"] = (int(g) for g in v.groups())
    res["ok"] = rc == 0 and "checked" in res
    (vdir / "result.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def score(res: dict, ids: list, truth: dict) -> dict:
    kept = [i for i in ids if i not in res["removed"]]
    return {"false_positives_through": sorted(i for i in kept if truth.get(i) == "FALSE"),
            "true_bugs_rejected": sorted(i for i in res["removed"] if truth.get(i) == "REAL")}


def summary(out: Path, results: list, ids: list, truth: dict, titles: dict) -> tuple:
    off = next((r for r in results if r["variant"] == "off" and r["ok"]), None)
    labelled = len(truth)
    lines = [f"# CC-10 layer 2 — Gate 1 on a known-answer set\n",
             f"{len(ids)} candidates, {labelled} with a recorded answer "
             f"({sum(v == 'REAL' for v in truth.values())} real, {sum(v == 'FALSE' for v in truth.values())} false positives). "
             f"Run {out.name}.\n",
             "| variant | voters | accept | ok | removed | false positives through | true bugs rejected | asked | decided | overridden | requests | seconds | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    verdicts = {}
    sc_off = score(off, ids, truth) if off else None
    for r in results:
        if not r["ok"]:
            lines.append(f"| {r['variant']} | {len(r['voters'])} | {r['accept']} | **FAILED rc={r['rc']}** | | | | | | | | {r['seconds']} | cannot run |")
            verdicts[r["variant"]] = "ERROR"
            continue
        sc = score(r, ids, truth)
        verdict = "baseline"
        if r["variant"] != "off":
            if not labelled:
                verdict = "no answers recorded"
            elif sc_off is None:
                verdict = "no baseline"
            else:
                third = math.ceil(r.get("checked", len(ids)) / 3)
                ok = (len(sc["false_positives_through"]) <= len(sc_off["false_positives_through"])
                      and len(sc["true_bugs_rejected"]) <= len(sc_off["true_bugs_rejected"])
                      and r.get("votes_decided", 0) >= third)
                verdict = "PASS" if ok else "FAIL (needs fp<=off, true rejected<=off, decided>=%d)" % third
        verdicts[r["variant"]] = verdict
        lines.append(f"| {r['variant']} | {len(r['voters'])} | {r['accept']} | yes | {r.get('removed_n', len(r['removed']))} | "
                     f"{len(sc['false_positives_through'])} | {len(sc['true_bugs_rejected'])} | {r.get('votes_asked', '-')} | "
                     f"{r.get('votes_decided', '-')} | {r.get('votes_overridden', '-')} | {r.get('votes_requests', '-')} | "
                     f"{r['seconds']} | {verdict} |")
    lines += ["", "## Per candidate (R = removed by Gate 1, k = kept)", "",
              "| id | answer | " + " | ".join(r["variant"] for r in results) + " | title |",
              "|---|---|" + "---|" * len(results) + "---|"]
    for i in ids:
        cells = ["R" if i in r.get("removed", {}) else "k" if r["ok"] else "?" for r in results]
        lines.append(f"| {i} | {truth.get(i, '?')} | " + " | ".join(cells) + f" | {titles.get(i, '')[:70]} |")
    lines += ["", "A variant that differs from `off` on a candidate with an answer is where to look: "
              "`<variant>/run.log` has the vote line and the reader's reason for it.", ""]
    (out / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    return verdicts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", default=DEFAULT_SETS, help=f"voter sets, size[:all|committed],…  (default {DEFAULT_SETS}; add 11 for two weak voters)")
    ap.add_argument("--known", default=str(KNOWN), help="directory with plan.json, truth.csv, TARGET (default: the recorded set)")
    ap.add_argument("--repo", default=str(ROOT), help="repository to clone the target from (default: this checkout; TARGET's sha must be in it)")
    ap.add_argument("--base-ini", default=str(ROOT / "agents.ini"))
    ap.add_argument("--local-ini", default=str(ROOT / "contest.local.ini"), help="where the claim_vote_llm.* sections with the keys live")
    ap.add_argument("--reader", default=READER, help="claim_vote_llm.* section used as Gate 1's single reader")
    ap.add_argument("--out", help="output directory (default contest-out/gate1-layer2/<UTC>)")
    ap.add_argument("--resume", metavar="DIR", help="continue DIR: variants with a result.json are not run again")
    ap.add_argument("--timeout", type=int, default=7200, help="seconds one variant may take (default 7200)")
    ap.add_argument("--detach", action="store_true", help="start in the background, print where the progress is, return")
    ap.add_argument("--dry-run", action="store_true", help="print the variants and what they would use, run nothing")
    args = ap.parse_args(argv)

    known = Path(args.known)
    sets = parse_sets(args.sets)
    variants = [("off", [], "all")] + [(f"on-{n}", ROSTER[:n], rule) for n, rule in sets]
    out = Path(args.resume) if args.resume else Path(args.out) if args.out else OUT_BASE / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    local = configparser.ConfigParser(interpolation=None)
    local.read(args.local_ini)
    missing = sorted({P + s for s in ROSTER[:max(n for n, _ in sets)] + [args.reader]} - set(local.sections())) if sets else []
    plan = json.loads((known / "plan.json").read_text(encoding="utf-8"))
    ids = [t["id"] for t in plan["tasks"]]
    titles = {t["id"]: t["title"] for t in plan["tasks"]}
    sha = (known / "TARGET").read_text(encoding="utf-8").split()[0]
    truth = load_truth(known)

    if args.dry_run:
        print(f"known answers: {known} ({len(ids)} candidates, {len(truth)} with an answer), target sha {sha}")
        print(f"output: {out}")
        for name, voters, rule in variants:
            print(f"  {name:7} accept={rule:9} voters={len(voters)} {', '.join(voters)}")
        print(f"reader: {P}{args.reader}")
        if missing:
            print("MISSING sections in the local ini:", ", ".join(missing))
        return 2 if missing else 0
    if missing:
        print("cannot run, sections missing in", args.local_ini, ":", ", ".join(missing), file=sys.stderr)
        return 2
    if args.detach:
        out.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(Path(__file__).resolve())] + [a for a in (argv or sys.argv[1:]) if a != "--detach"]
        if "--out" not in cmd and "--resume" not in cmd:
            cmd += ["--out", str(out)]
        with open(out / "nohup.out", "a") as fh:
            subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True, cwd=ROOT)
        print(f"started in the background.\n  progress: {out}/progress.log\n  answer:   {out}/SUMMARY.md (when progress.log says DONE)")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    log(out, f"layer 2: {len(ids)} candidates, {len(truth)} answers, target {sha[:8]}, variants {[v[0] for v in variants]}")
    if not truth:
        log(out, "WARNING: no truth.csv rows — the run counts what Gate 1 removes but cannot say PASS/FAIL")
    results, failed_run = [], False
    for name, voters, rule in variants:
        done = out / name / "result.json"
        if args.resume and done.exists():
            res = json.loads(done.read_text(encoding="utf-8"))
            log(out, f"{name}: already done (kept)")
        else:
            log(out, f"{name}: start ({len(voters)} voters, accept={rule})")
            try:
                res = run_variant(out, name, known, Path(args.repo), sha, Path(args.base_ini), local,
                                  voters, rule, args.reader, args.timeout)
            except Exception as exc:   # noqa: BLE001 — one variant failing must not stop the others
                res = {"variant": name, "rc": -1, "seconds": 0, "voters": voters, "accept": rule, "removed": {},
                       "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
            log(out, f"{name}: {'ok' if res['ok'] else 'FAILED'} in {res['seconds']} s, "
                     f"removed {res.get('removed_n', '?')}, decided {res.get('votes_decided', '-')}/{res.get('votes_asked', '-')}")
        failed_run |= not res["ok"]
        results.append(res)
        summary(out, results, ids, truth, titles)      # a partial SUMMARY.md after every variant
    verdicts = summary(out, results, ids, truth, titles)
    bad = [v for v, x in verdicts.items() if x.startswith("FAIL")]
    log(out, "verdicts: " + ", ".join(f"{k}={v.split(' ')[0]}" for k, v in verdicts.items()))
    log(out, "DONE — " + str(out / "SUMMARY.md"))
    return 2 if failed_run else 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
