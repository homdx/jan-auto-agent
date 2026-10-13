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
from collections import Counter
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
#: CC-13: the line Gate 1 prints at the start of a run — whether the collect model was there and
#: whether the reader and the vote pack used it, so a variant says what its judges had
COLLECT_LINE = re.compile(r"collect: (fresh|stale(?: \(\d+ modules? changed\))?|present|absent)"
                          r" — reader: (used|not used), vote pack: (used|not used)")


def log(out: Path, msg: str) -> None:
    line = f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(out / "progress.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def parse_sets(text: str) -> list:
    """``3,5:all,7`` -> [(3, 'all', ()), (5, 'all', ()), (7, 'committed', ())].

    ``veto`` is ``vote_confirm = off`` (a unanimous FALSE rejects, a unanimous TRUE is the reader's); ``notests`` is
    ``vote_skip_tests = on`` (a candidate citing a test file is not put to the voters); ``collect`` is
    ``[gate1] vote_collect = auto`` and ``[collect] use_in_auto = true`` (CC-13: the judges read the collect
    model of the target, the artifact of ``--collect-from``) — it is a suffix on the variant name
    (``on-3+collect``), so the table puts the same variant with and without side by side."""
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
        bad = set(opts) - {"veto", "notests", "collect"}
        if bad or (rule and rule not in ("all", "committed")):
            raise SystemExit(f"set {part!r}: options are +veto, +notests and +collect, rules all|committed")
        item = (n, rule or ("all" if n == 3 else "committed"), tuple(sorted(opts)))
        # two sets of one name shared one directory and one result.json, and --resume took the second as done
        if any((o[0], o[2], o[1]) == (item[0], item[2], item[1]) for o in out):
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
    ``on-3-committed``), so ``--sets 5:all,5:committed`` — what committed buys at one size — is two variants.
    ``collect`` is a ``+collect`` suffix (``on-3+collect``, ``off+collect``), not a dash segment: the other
    options are switches of the same vote, this one is the artifact the judges were handed."""
    default = "all" if n == 3 else "committed"
    head = "-".join([f"on-{n}"] + ([rule] if rule and rule != default else [])
                    + [o for o in opts if o != "collect"])
    return head + ("+collect" if "collect" in opts else "")


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
    # CC-13: +collect hands the judges the artifact of --collect-from. vote_collect = auto puts it in the
    # voters' packs; use_in_auto = true gives the single reader the same model it gets in --auto, so the
    # variant measures the pipeline as --auto runs it, not a leaner one.
    cfg["gate1"]["vote_collect"] = "auto" if "collect" in opts else "off"
    cfg["collect"]["use_in_auto"] = "true" if "collect" in opts else "false"
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


def check_collect(artifact_dir: Path, work: Path) -> str:
    """``""`` when the artifact at *artifact_dir* was built for the tree at *work*, else the first
    path that differs — a path the manifest has that the clone does not, whose bytes moved, or a
    path the clone has that the manifest does not.

    CC-13: ``--collect-from`` is the whole point of the ``+collect`` variants, so a wrong artifact
    is a wrong table, not a slow one. The check is the manifest's own hashes against the clone's
    tree, walked with the same ``skip_dirs`` the artifact recorded (an artifact with no key was
    built with the defaults), so a manifest of another tree, another ``collector_version``, or a
    tree that drifted since never slips through as ``fresh``. Read-only: it copies nothing."""
    from tools.collect import manifest as mf

    if not artifact_dir.is_dir():
        return f"{artifact_dir} is not a directory"
    if not (artifact_dir / "artifact.json").is_file():
        return f"{artifact_dir} has no artifact.json"
    manifest_path = artifact_dir / "collect_manifest.json"
    if not manifest_path.is_file():
        return f"{artifact_dir} has no collect_manifest.json"
    try:
        man = mf.read_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        return f"{manifest_path} is unreadable ({exc})"
    recorded = frozenset(man.skip_dirs) if man.skip_dirs else mf.DEFAULT_SKIP_DIRS
    try:
        # the manifest's own paths plus whatever the walk finds now: `discover_files` walks `.py`
        # by default, so a manifest built with `[collect] languages = python,java` would show every
        # .java file as "the manifest has it and the clone does not" without the union. `hash_tree`
        # drops the paths that are gone, which is how a deletion reaches the diff as `removed`.
        paths = sorted(man.file_hashes.keys() | set(mf.discover_files(work, skip_dirs=recorded)))
        current = mf.hash_tree(work, paths)
    except OSError as exc:
        return f"the clone at {work} could not be walked ({exc})"
    if current == man.file_hashes:
        return ""
    diff = mf.diff_files(man.file_hashes, current)
    changed = sorted(diff.added | diff.modified | diff.removed)
    if not changed:
        return "the manifest records nothing to compare"
    first = changed[0]
    if first in diff.modified:
        why = "its bytes differ"
    elif first in diff.added:
        why = "the clone has it and the manifest does not"
    else:
        why = "the manifest has it and the clone does not"
    return f"{first} ({why}; {len(changed)} file(s) differ in all)"


def install_collect(artifact_dir: Path, work: Path, config_path: "Path | None" = None) -> Path:
    """Copy the artifact into the clone's own ``[collect] dir`` and return that directory. Read-only
    data, copied not linked: the clones are separate trees so that ``--validate-plan`` cannot touch
    anything shared, and the artifact goes with them.

    The directory is resolved with the variant's own ini (*config_path*), the very call the vote makes
    when it reads: with a custom ``[collect] dir`` the default ``.collect`` would be written where
    nobody reads and the ``+collect`` variant would run without the model."""
    import configparser
    from tools.collect import cli as cli_mod

    cfg = None
    if config_path is not None:
        cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
        cfg.read(config_path, encoding="utf-8-sig")
    collect_dir = cli_mod.resolve_collect_dir(work, cfg)
    collect_dir.mkdir(parents=True, exist_ok=True)
    for name in ("artifact.json", "collect_manifest.json"):
        shutil.copy(artifact_dir / name, collect_dir / name)
    return collect_dir


def check_and_install(artifact_dir: Path, repo: Path, sha: str, scratch: Path) -> str:
    """The one preflight of ``--collect-from``: prepare a throwaway clone at *sha*, check the
    artifact against its tree, and refuse with the first differing path before any variant ran.
    Returns ``""`` when the artifact is for this tree."""
    scratch.mkdir(parents=True, exist_ok=True)
    work = scratch / "target"
    if work.exists():
        shutil.rmtree(work)
    try:
        subprocess.run(["git", "clone", "-q", str(repo), str(work)], check=True)
        subprocess.run(["git", "-C", str(work), "checkout", "-q", sha], check=True)
        return check_collect(artifact_dir, work)
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)


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
    c = COLLECT_LINE.search(text)
    if c:
        res["collect"], res["collect_reader"], res["collect_pack"] = c.groups()
    return res


def run_variant(out: Path, name: str, known: Path, repo: Path, sha: str, base_ini: Path,
                local, voters: list, accept: str, reader: str, timeout: int, opts: tuple = (),
                titles: "dict | None" = None, collect_from: "Path | None" = None) -> dict:
    vdir = out / name
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    ini = vdir / "agents.ini"
    build_ini(ini, local, base_ini, "off" if name.startswith("off") else "on", voters, accept, reader, opts=opts)
    prepare_target(vdir / "target", repo, sha, known / "plan.json")
    if collect_from is not None and "collect" in opts:
        # CC-13: the artifact was checked against this sha before any variant ran (--collect-from),
        # and the clone is the same tree, so the copy is what the judges read.
        install_collect(collect_from, vdir / "target", ini)
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
    lines = ["# CC-10 layer 2 — Gate 1 on a known-answer set\n",
             f"{len(ids)} candidates, {labelled} with a recorded answer "
             f"({sum(v == 'REAL' for v in truth.values())} real, {sum(v == 'FALSE' for v in truth.values())} false positives). "
             f"Run {out.name}.\n",
             "| variant | voters | accept | ok | removed | unresolved | false positives through | true bugs rejected | asked | decided | overridden | requests | skipped | seconds | collect (reader / vote pack) | verdict |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    verdicts = {}
    for r in results:
        if not r["ok"]:
            why = r.get("error") or f"rc={r['rc']}"
            lines.append(f"| {r['variant']} | {len(r['voters'])} | {r['accept']} | **FAILED {why}** | | | | | | | | | | {r['seconds']} | | cannot run |")
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
        # CC-13: a `+collect` variant is a measurement of the judges WITH the collect model; one whose run did not
        # have it (the line says absent, or the vote pack did not use it) is a different measurement and must
        # not be credited with a PASS or blamed with a FAIL
        has_collect = "collect" in r.get("opts", [])
        col = f"{r.get('collect', 'no line')} ({r.get('collect_reader', '?')} / {r.get('collect_pack', '?')})" if "collect" in r else \
            ("no line" if has_collect else "-")
        if has_collect and not (r.get("collect") in ("fresh", "stale") and r.get("collect_pack") == "used"):
            verdict = (f"NO COLLECT (the run says collect: {r.get('collect') or 'no line'}, vote pack: "
                       f"{r.get('collect_pack') or '?'} — not the measurement the name says)")
        verdicts[r["variant"]] = verdict
        lines.append(f"| {r['variant']} | {len(r['voters'])} | {r['accept']} | yes | {r.get('removed_n', len(r['removed']))} | "
                     f"{len(r.get('inconclusive') or ())} | "
                     f"{len(sc['false_positives_through'])} | {len(sc['true_bugs_rejected'])} | {r.get('votes_asked', '-')} | "
                     f"{r.get('votes_decided', '-')} | {r.get('votes_overridden', '-')} | {r.get('votes_requests', '-')} | "
                     f"{r.get('votes_skipped', '-')} | {r['seconds']} | {col} | {verdict} |")
    offs = [r for r in results if r["variant"].startswith("off") and r["ok"]]
    if len(offs) >= 2:
        a, b = offs[0], offs[1]
        # on the candidates both judged, as the verdicts count them: one the reader could not judge in either
        # run was a false positive of the other run here (kept and FALSE), and a flip
        both_out = set(a.get("inconclusive") or ()) | set(b.get("inconclusive") or ())
        flips = sorted((set(a["removed"]) ^ set(b["removed"])) - both_out)
        lines += ["", f"Noise of the baseline: `{a['variant']}` and `{b['variant']}` (same code, same candidates) disagree on "
                      f"{len(flips)} candidate(s); false positives through "
                      f"{len(score(a, ids, truth, skip=both_out)['false_positives_through'])} "
                      f"and {len(score(b, ids, truth, skip=both_out)['false_positives_through'])}"
                      + (f" (on the {len(ids) - len(both_out)} both judged)" if both_out else "")
                      + ". A difference between a vote variant and `off` smaller than that is not a result."]
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
    ap.add_argument("--sets", default=DEFAULT_SETS, help=f"voter sets, size[:all|committed][+veto][+notests][+collect],…  (default {DEFAULT_SETS}; 11 adds two weak voters; +veto: the vote only vetoes; +notests: test-file candidates are not voted; +collect: the judges read the collect model of --collect-from)")
    ap.add_argument("--off-runs", type=int, default=None, help="how many times the baseline (vote off) runs; the reader is a model and does not repeat itself, so 2 sizes its noise (default: [gate1_layer2] off_runs of contest.ini, else 2)")
    ap.add_argument("--collect-from", metavar="DIR", help="a .collect/ built for TARGET's sha: copied into every +collect clone, and the baselines get the +collect suffix too so the table puts each variant with and without side by side. Refused, naming the first differing path, when the manifest does not match the clone's tree")
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
    # CC-13: the baselines get the +collect suffix whenever any variant opts in, so the table has
    # each variant with and without the artifact — the comparison is the point of the option.
    any_collect = any("collect" in opts for _, _, opts in sets)
    suffix = "+collect" if any_collect else ""
    variants = [(("off" if i == 0 else f"off-{i + 1}") + suffix, [], "all", ()) for i in range(max(1, off_runs))]
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
    # Gate 1's "kept, presence unknown" line names a candidate by its title: two of one title could not be told
    # apart, and a run marked both unresolved when one was
    twins = sorted(t for t, n in Counter(titles.values()).items() if n > 1)
    if twins:
        print(f"{known / 'plan.json'}: candidates share a title ({twins[0]!r}): the run's log names a candidate "
              "by its title, so the set needs one title a candidate", file=sys.stderr)
        return 2
    sha = (known / "TARGET").read_text(encoding="utf-8").split()[0]
    truth = load_truth(known)

    if args.dry_run:
        print(f"known answers: {known} ({len(ids)} candidates, {len(truth)} with an answer), target sha {sha}")
        print(f"output: {out}")
        for name, voters, rule, opts in variants:
            print(f"  {name:16} accept={rule:9} voters={len(voters)} {', '.join(voters)}")
        print(f"reader: {P}{args.reader}")
        if any_collect:
            print(f"collect: {args.collect_from or 'NOT GIVEN — +collect variants cannot run'}")
        if missing:
            print("MISSING sections in the local ini:", ", ".join(missing))
        return 2 if missing else 0
    if missing:
        print("cannot run, sections missing in", args.local_ini, ":", ", ".join(missing), file=sys.stderr)
        return 2
    if any_collect and not args.collect_from:
        print("cannot run, a set names +collect but --collect-from was not given", file=sys.stderr)
        return 2
    if args.collect_from:
        # the artifact is what every +collect variant reads; a wrong one is a wrong table, so it is
        # checked against a throwaway clone of the target before a single variant starts.
        problem = check_and_install(Path(args.collect_from), Path(args.repo), sha, out / ".collect-check")
        if problem:
            print(f"cannot run, --collect-from does not match the target at {sha[:8]}: {problem}",
                  file=sys.stderr)
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
                # the option is a keyword, not a positional: without --collect-from the call is the old one
                extra = {"collect_from": Path(args.collect_from)} if args.collect_from else {}
                res = run_variant(out, name, known, Path(args.repo), sha, Path(args.base_ini), local,
                                  voters, rule, args.reader, args.timeout, opts, titles, **extra)
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
