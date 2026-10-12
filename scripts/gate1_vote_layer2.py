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

A candidate Gate 1 could not judge (the reader never answered: plan_validator's "could not be checked" list, or a
``presence_unknown = keep`` line in the run's log) is "unresolved": it is left out of the comparison on both sides,
shown as ``?`` and counted in its own column — a provider's failures are not the vote's false positives.

Exit code: 0 when every variant ran and every ``on`` variant passed; 1 when one failed the rule; 2 when a variant
could not run (the output directory says which; ``--resume DIR`` runs it again, with what is missing); 3 when an
``on`` variant has no verdict (no recorded answers, no baseline). ``[gate1_layer2] off_runs`` in ``contest.ini`` is
how many times the baseline runs (``--off-runs`` overrides it).
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
#: the same line's tail when Gate 1 could not check some: ", 2 left unresolved (technical failure)"
UNRESOLVED_N = re.compile(r"Checked \d+ task\(s\):.*?, (\d+) left unresolved")
REMOVED = re.compile(r"^\s+- (\S+) \(([^)]*)\): (.*)$")
#: plan_validator's list of the candidates Gate 1 could not check: a heading, then "  - <id>: <reason>"
UNRESOLVED_HEAD = re.compile(r"task\(s\) could not be checked")
UNRESOLVED = re.compile(r"^\s+- (\S+): (.*)$")
#: a candidate the reader never answered and `presence_unknown = keep` passed through (kept, not judged)
KEPT_UNKNOWN = re.compile(r"Gate1\[presence\] KEPT \(presence_unknown = keep\) (.+?) — ")
VOTE = re.compile(r"Gate 1 vote: asked (\d+), decided (\d+), overridden by the reader (\d+), requests (\d+)")
#: the vote line's tail since the review of 2026-10-12: candidates that reached the vote and were not voted on
VOTE_SKIPPED = re.compile(r"Gate 1 vote: .*?, skipped (\d+)")
#: contest.ini's section of this script
INI_SECTION = "gate1_layer2"
DEFAULT_OFF_RUNS = 2


def log(out: Path, msg: str) -> None:
    line = f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(out / "progress.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def parse_sets(text: str) -> list:
    """``3:all,5:committed+veto,7+notests`` -> [(3, 'all', ()), (5, 'committed', ('veto',)), (7, 'committed', ('notests',))].

    ``veto`` is ``vote_confirm = off`` (a unanimous FALSE rejects, a unanimous TRUE is the reader's); ``notests`` is
    ``vote_skip_tests = on`` (a candidate citing a test file is not put to the voters)."""
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        head, *opts = part.split("+")
        n, _, rule = head.partition(":")
        try:
            n = int(n)
        except ValueError:
            raise SystemExit(f"set {part!r}: the size is a number, from 3 to {len(ROSTER)}") from None
        if not 3 <= n <= len(ROSTER):
            raise SystemExit(f"set size {n}: from 3 to {len(ROSTER)}")
        bad = set(opts) - {"veto", "notests"}
        if bad or (rule and rule not in ("all", "committed")):
            raise SystemExit(f"set {part!r}: options are +veto and +notests, rules all|committed")
        item = (n, rule or ("all" if n == 3 else "committed"), tuple(sorted(opts)))
        # two sets of one name shared one directory and one result.json, and --resume took the second as done
        if any(variant_name(o[0], o[2], o[1]) == variant_name(item[0], item[2], item[1]) for o in out):
            raise SystemExit(f"set {part!r}: {variant_name(item[0], item[2], item[1])} is named twice")
        out.append(item)
    return out


def ini_off_runs(path: Path) -> int:
    """``[gate1_layer2] off_runs`` of *path* (contest.ini), else `DEFAULT_OFF_RUNS`; a bad value is the default."""
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    try:
        cfg.read(path, encoding="utf-8-sig")
        value = cfg.getint(INI_SECTION, "off_runs", fallback=DEFAULT_OFF_RUNS)
    except (configparser.Error, ValueError, OSError):
        return DEFAULT_OFF_RUNS
    return value if value >= 1 else DEFAULT_OFF_RUNS


def variant_name(n: int, opts: tuple, rule: "str | None" = None) -> str:
    """``on-5``, ``on-5-veto``; the rule is in the name when it is not the size's default (``on-5-all``,
    ``on-3-committed``), so ``--sets 5:all,5:committed`` — what committed buys at one size — is two variants."""
    default = "all" if n == 3 else "committed"
    return "-".join([f"on-{n}"] + ([rule] if rule and rule != default else []) + list(opts))


def load_truth(known: Path) -> dict:
    path = known / "truth.csv"
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return {r["id"]: r["truth"].strip().upper() for r in csv.DictReader(fh)
                if r.get("truth", "").strip().upper() in ("REAL", "FALSE")}


def build_ini(dest: Path, local: configparser.ConfigParser, base_ini: Path, mode: str,
              voters: list, accept: str, reader: str, reader_timeout: int = 240,
              opts: tuple = ()) -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(base_ini, encoding="utf-8-sig")
    rd = local[P + reader]
    for key in ("base_url", "api_key", "model"):
        cfg["gate1_llm"][key] = rd[key]
    cfg["gate1"]["presence_llm_profile"] = "gate1_llm"
    # one hung request must not hold a variant for the 40 minutes agents.ini allows a Coder (the first live
    # run sat 35 minutes on a connection that never answered): the reader gives up and retries after this
    # this bounds one attempt; [gate1] presence_deadline_sec of the base ini bounds one whole check (its retries
    # included: 61 attempts of 240 s held one candidate for about 4 h)
    cfg["loop"]["timeout_seconds"] = str(reader_timeout)
    cfg["gate1"]["claim_vote"] = mode
    cfg["gate1"]["vote_accept"] = accept
    cfg["gate1"]["vote_confirm"] = "off" if "veto" in opts else "on"
    cfg["gate1"]["vote_skip_tests"] = "on" if "notests" in opts else "off"
    if voters:
        for v in voters:
            if not cfg.has_section(P + v):
                cfg.add_section(P + v)
            for key, val in local[P + v].items():
                cfg.set(P + v, key, val)
        cfg["gate1"]["vote_profiles"] = ", ".join(P + v for v in voters)
    # created 0600: written first and chmod-ed after, the keys of contest.local.ini were readable for a moment
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(dest, 0o600)          # an existing file keeps its mode through O_TRUNC
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        cfg.write(fh)


def prepare_target(work: Path, repo: Path, sha: str, plan: Path) -> None:
    subprocess.run(["git", "clone", "-q", str(repo), str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", sha], check=True)
    (work / ".agent").mkdir(exist_ok=True)
    shutil.copy(plan, work / ".agent" / "plan.json")


def read_report(text: str, titles: dict) -> dict:
    """What one variant's run.log says: ``checked``/``kept_n``/``removed_n``/``unresolved_n``, ``removed``
    ({id: reason}), ``inconclusive`` (the ids Gate 1 could not judge: plan_validator's list, and a candidate
    ``presence_unknown = keep`` passed through, found by its title) and the vote line's counters."""
    res: dict = {"removed": {}, "inconclusive": []}
    m = CHECKED.search(text)
    if m:
        res["checked"], res["kept_n"], res["removed_n"] = (int(g) for g in m.groups())
        n = UNRESOLVED_N.search(text)
        res["unresolved_n"] = int(n.group(1)) if n else 0
    unresolved, listing = [], False
    for line in text.splitlines():
        r = REMOVED.match(line)
        if r and r.group(1).startswith("AUTO-"):
            res["removed"][r.group(1)] = f"{r.group(2)}: {r.group(3)[:160]}"
            continue
        if UNRESOLVED_HEAD.search(line):
            listing = True
            continue
        u = UNRESOLVED.match(line) if listing else None
        if u and u.group(1).startswith("AUTO-"):
            unresolved.append(u.group(1))
        elif listing and line.strip():
            listing = False
    by_title: dict = {}
    for tid, title in titles.items():
        by_title.setdefault(repr(title), []).append(tid)
    for k in KEPT_UNKNOWN.finditer(text):
        unresolved += by_title.get(k.group(1), [])
    res["inconclusive"] = sorted(set(unresolved) - set(res["removed"]))
    v = VOTE.search(text)
    if v:
        res["votes_asked"], res["votes_decided"], res["votes_overridden"], res["votes_requests"] = \
            (int(g) for g in v.groups())
        k = VOTE_SKIPPED.search(text)
        if k:
            res["votes_skipped"] = int(k.group(1))
    return res


def run_variant(out: Path, name: str, known: Path, repo: Path, sha: str, base_ini: Path,
                local, voters: list, accept: str, reader: str, timeout: int, opts: tuple = (),
                titles: "dict | None" = None) -> dict:
    vdir = out / name
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    ini = vdir / "agents.ini"
    build_ini(ini, local, base_ini, "off" if name.startswith("off") else "on", voters, accept, reader, opts=opts)
    prepare_target(vdir / "target", repo, sha, known / "plan.json")
    started = time.time()
    timed_out = False
    with open(vdir / "run.log", "w", encoding="utf-8") as fh:
        try:
            rc = subprocess.run([sys.executable, str(ROOT / "main.py"), "--validate-plan",
                                 "--base", str(vdir / "target"), "--config", str(ini)],
                                cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc, timed_out = -1, True      # recorded with its real time: a 3 h timeout read as 0 s
    text = (vdir / "run.log").read_text(encoding="utf-8", errors="replace")
    res = {"variant": name, "rc": rc, "opts": list(opts), "seconds": round(time.time() - started), "voters": voters,
           "accept": accept, "removed": {}, "inconclusive": []}
    if timed_out:
        res["error"] = f"timed out after {timeout} s"
    res.update(read_report(text, titles or {}))
    res["ok"] = rc == 0 and "checked" in res
    (vdir / "result.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def score(res: dict, ids: list, truth: dict, skip=()) -> dict:
    """The variant's mistakes on the candidates it judged: one Gate 1 could not judge (``inconclusive``) is
    neither a false positive let through nor a rejection; *skip* leaves out the baseline's unresolved too, so
    the two sides are counted on the same candidates."""
    out_ = set(res.get("inconclusive") or ()) | set(skip)
    kept = [i for i in ids if i not in res["removed"] and i not in out_]
    return {"false_positives_through": sorted(i for i in kept if truth.get(i) == "FALSE"),
            "true_bugs_rejected": sorted(i for i in res["removed"] if truth.get(i) == "REAL" and i not in out_)}


def summary(out: Path, results: list, ids: list, truth: dict, titles: dict) -> tuple:
    # every baseline run that ran: a variant is compared with each, so the verdict does not depend on
    # which of the two noisy `off` runs came first (PASS against 14, FAIL against 10)
    bases = [r for r in results if r["variant"].startswith("off") and r["ok"]]
    base_unresolved = set().union(*[set(b.get("inconclusive") or ()) for b in bases]) if bases else set()
    labelled = len(truth)
    lines = [f"# CC-10 layer 2 — Gate 1 on a known-answer set\n",
             f"{len(ids)} candidates, {labelled} with a recorded answer "
             f"({sum(v == 'REAL' for v in truth.values())} real, {sum(v == 'FALSE' for v in truth.values())} false positives). "
             f"Run {out.name}.\n",
             "| variant | voters | accept | ok | removed | unresolved | false positives through | true bugs rejected | asked | decided | overridden | requests | skipped | seconds | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    verdicts = {}
    for r in results:
        if not r["ok"]:
            why = r.get("error") or f"rc={r['rc']}"
            lines.append(f"| {r['variant']} | {len(r['voters'])} | {r['accept']} | **FAILED {why}** | | | | | | | | | | {r['seconds']} | cannot run |")
            verdicts[r["variant"]] = "ERROR"
            continue
        # both sides on the same candidates: what either could not judge is out of both counts
        own = set(r.get("inconclusive") or ())
        is_base = r["variant"].startswith("off")
        sc = score(r, ids, truth, skip=() if is_base else base_unresolved)
        unresolved = sorted(own | (set() if is_base else base_unresolved))
        verdict = "baseline"
        if r["variant"].startswith("off"):
            verdict = "baseline" if r["variant"] == "off" else "baseline repeat (the reader's own noise)"
        else:
            if not labelled:
                verdict = "no answers recorded"
            elif not bases:
                verdict = "no baseline"
            else:
                skip = own | base_unresolved
                scs = [score(b, ids, truth, skip=skip) for b in bases]
                best_fp = min(len(x["false_positives_through"]) for x in scs)
                best_tr = min(len(x["true_bugs_rejected"]) for x in scs)
                third = math.ceil(r.get("checked", len(ids)) / 3)
                veto = "veto" in r.get("opts", [])
                # a veto-only vote decides little by design, so no third is asked of it — but one that never
                # vetoed did nothing, and "not worse than off" was a PASS for an inert switch
                decided = r.get("votes_decided", 0)
                enough = decided >= 1 if veto else decided >= third
                ok = (len(sc["false_positives_through"]) <= best_fp and len(sc["true_bugs_rejected"]) <= best_tr
                      and enough)
                n_off = f"each of {len(bases)} off runs" if len(bases) > 1 else "off"
                verdict = ("PASS" + (" (veto only: at least one veto, no third required)" if veto else "")) if ok else \
                    "FAIL (needs fp<=%s, true rejected<=%s, decided>=%d)" % (n_off, n_off, 1 if veto else third)
                if unresolved:
                    verdict += f" — on {len(ids) - len(unresolved)} of {len(ids)} candidates, {len(unresolved)} unresolved"
        verdicts[r["variant"]] = verdict
        lines.append(f"| {r['variant']} | {len(r['voters'])} | {r['accept']} | yes | {r.get('removed_n', len(r['removed']))} | "
                     f"{len(r.get('inconclusive') or ())} | "
                     f"{len(sc['false_positives_through'])} | {len(sc['true_bugs_rejected'])} | {r.get('votes_asked', '-')} | "
                     f"{r.get('votes_decided', '-')} | {r.get('votes_overridden', '-')} | {r.get('votes_requests', '-')} | "
                     f"{r.get('votes_skipped', '-')} | {r['seconds']} | {verdict} |")
    offs = [r for r in results if r["variant"].startswith("off") and r["ok"]]
    if len(offs) >= 2:
        a, b = offs[0], offs[1]
        flips = sorted(set(a["removed"]) ^ set(b["removed"]))
        lines += ["", f"Noise of the baseline: `{a['variant']}` and `{b['variant']}` (same code, same candidates) disagree on "
                      f"{len(flips)} candidate(s); false positives through {len(score(a, ids, truth)['false_positives_through'])} "
                      f"and {len(score(b, ids, truth)['false_positives_through'])}. A difference between a vote variant and `off` "
                      f"smaller than that is not a result."]
    lines += ["", "## Per candidate (R = removed by Gate 1, k = kept, ? = not judged: the run failed or the reader "
              "never answered)", "",
              "| id | answer | " + " | ".join(r["variant"] for r in results) + " | title |",
              "|---|---|" + "---|" * len(results) + "---|"]
    for i in ids:
        cells = ["R" if i in r.get("removed", {}) else "?" if not r["ok"] or i in (r.get("inconclusive") or ())
                 else "k" for r in results]
        lines.append(f"| {i} | {truth.get(i, '?')} | " + " | ".join(cells) + f" | {titles.get(i, '')[:70]} |")
    lines += ["", "A variant that differs from `off` on a candidate with an answer is where to look: "
              "`<variant>/run.log` has the vote line and the reader's reason for it.", ""]
    (out / "SUMMARY.md").write_text("\n".join(lines), encoding="utf-8")
    return verdicts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", default=DEFAULT_SETS, help=f"voter sets, size[:all|committed][+veto][+notests],…  (default {DEFAULT_SETS}; 11 adds two weak voters; +veto: the vote only vetoes; +notests: test-file candidates are not voted)")
    ap.add_argument("--off-runs", type=int, default=None, help="how many times the baseline (vote off) runs; the reader is a model and does not repeat itself, so 2 sizes its noise (default: [gate1_layer2] off_runs of contest.ini, else 2)")
    ap.add_argument("--known", default=str(KNOWN), help="directory with plan.json, truth.csv, TARGET (default: the recorded set)")
    ap.add_argument("--repo", default=str(ROOT), help="repository to clone the target from (default: this checkout; TARGET's sha must be in it)")
    ap.add_argument("--base-ini", default=str(ROOT / "agents.ini"))
    ap.add_argument("--local-ini", default=str(ROOT / "contest.local.ini"), help="where the claim_vote_llm.* sections with the keys live")
    ap.add_argument("--reader", default=READER, help="claim_vote_llm.* section used as Gate 1's single reader")
    ap.add_argument("--out", help="output directory (default contest-out/gate1-layer2/<UTC>)")
    ap.add_argument("--resume", metavar="DIR", help="continue DIR: variants with a successful result.json are not run again (a failed one is)")
    ap.add_argument("--timeout", type=int, default=10800, help="seconds one variant may take (default 10800; 9 voters took 6200 s, 11 timed out at 7200)")
    ap.add_argument("--detach", action="store_true", help="start in the background, print where the progress is, return")
    ap.add_argument("--dry-run", action="store_true", help="print the variants and what they would use, run nothing")
    args = ap.parse_args(argv)

    known = Path(args.known)
    sets = parse_sets(args.sets)
    off_runs = args.off_runs if args.off_runs is not None else ini_off_runs(ROOT / "contest.ini")
    variants = [("off" if i == 0 else f"off-{i + 1}", [], "all", ()) for i in range(max(1, off_runs))]
    variants += [(variant_name(n, opts, rule), ROSTER[:n], rule, opts) for n, rule, opts in sets]
    out = Path(args.resume) if args.resume else Path(args.out) if args.out else OUT_BASE / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    local = configparser.ConfigParser(interpolation=None)
    local.read(args.local_ini, encoding="utf-8-sig")       # a BOM (Notepad) or a locale's codec was a traceback
    # the reader is checked with or without a vote set: `--sets ""` skipped it, and only a FAILED `off` said so
    wanted = ROSTER[:max((n for n, _, _ in sets), default=0)] + [args.reader]
    missing = sorted({P + s for s in wanted} - set(local.sections()))
    plan = json.loads((known / "plan.json").read_text(encoding="utf-8"))
    ids = [t["id"] for t in plan["tasks"]]
    titles = {t["id"]: t["title"] for t in plan["tasks"]}
    sha = (known / "TARGET").read_text(encoding="utf-8").split()[0]
    truth = load_truth(known)

    if args.dry_run:
        print(f"known answers: {known} ({len(ids)} candidates, {len(truth)} with an answer), target sha {sha}")
        print(f"output: {out}")
        for name, voters, rule, opts in variants:
            print(f"  {name:16} accept={rule:9} voters={len(voters)} {', '.join(voters)}")
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
            # the child runs where this one was started: its relative --local-ini / --known / --out were
            # checked and printed against this directory, and cwd=ROOT read them from another one
            subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                             cwd=os.getcwd())
        print(f"started in the background.\n  progress: {out}/progress.log\n  answer:   {out}/SUMMARY.md (when progress.log says DONE)")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    log(out, f"layer 2: {len(ids)} candidates, {len(truth)} answers, target {sha[:8]}, variants {[v[0] for v in variants]}")
    if not truth:
        log(out, "WARNING: no truth.csv rows — the run counts what Gate 1 removes but cannot say PASS/FAIL")
    results, failed_run = [], False
    for name, voters, rule, opts in variants:
        done = out / name / "result.json"
        kept = json.loads(done.read_text(encoding="utf-8")) if args.resume and done.exists() else None
        if kept is not None and kept.get("ok"):
            res = kept
            log(out, f"{name}: already done (kept)")
        else:
            if kept is not None:
                # a failed variant is what --resume is for: it was "done" and never ran again
                log(out, f"{name}: failed before ({kept.get('error') or 'rc=%s' % kept.get('rc')}) — running it again")
            log(out, f"{name}: start ({len(voters)} voters, accept={rule})")
            try:
                res = run_variant(out, name, known, Path(args.repo), sha, Path(args.base_ini), local,
                                  voters, rule, args.reader, args.timeout, opts, titles)
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
    # an `on` variant with no verdict (no answers recorded, no baseline) is not a pass: 0 said "every on variant
    # passed" when none had been judged
    undecided = [v for v, x in verdicts.items() if not v.startswith("off") and x not in ("ERROR",)
                 and not x.startswith(("PASS", "FAIL"))]
    log(out, "verdicts: " + "; ".join(f"{k} = {v}" for k, v in verdicts.items()))
    log(out, "DONE — " + str(out / "SUMMARY.md"))
    return 2 if failed_run else 1 if bad else 3 if undecided else 0


if __name__ == "__main__":
    sys.exit(main())
