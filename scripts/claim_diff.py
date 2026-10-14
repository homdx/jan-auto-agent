#!/usr/bin/env python3
"""CC-7: judge the same claims at two commits and say what changed (FIXED/STILL/NEW/GONE/UNCLEAR).

Every claim is judged twice through CC-6 (``scripts/claim_vote.py``'s ``prepare_target``,
``ask_v2`` and ``tally``; nothing of the voting is rebuilt here): at ``--base`` with the
pack built at base, and at ``--head`` with the pack built at head plus the ``base..head``
diff chunks of the anchored paths, so the voter sees what changed. The two judgements of a
claim use the same voters, and the jobs of both sides go through one pool (the per-host
pacing of ``claim_vote.PACER`` is shared). A ``world`` claim has no before and after: it
is judged once, at head, and reported ``n/a``. The rule that turns two judgements into a
change is ``tools.claimcheck.compare.classify_delta``; ``docs/claim-check/BEFORE-AFTER.md``
says what the words mean and what a ``FIXED`` does not promise.

    python3 scripts/claim_diff.py claims.json --target REPO --base main --head origin/branch \\
        --profiles a b c [--runs 3] [--out DIR] [--check]

``claims.json`` is a list of strings, of ``{"claim", "id"?, "expect"?}``, or an object with
a ``claims`` list (``contest-bench/cc/claims_fixture.json`` as it is). ``expect`` is
``fixed|still|new|gone``; with ``--check`` the exit code is 1 when a claim with an
``expect`` got another change (each mismatch printed with its evidence ids), so the command
is a gate for "this branch fixes what its tickets say". Claims without ``expect`` never fail it.

Written to ``--out`` (default ``<repo-root>/claim-check-out/diff-<UTC>``): ``delta.json``
(``{"base": sha, "head": sha, "deltas": [...]}``, what ``contest-bench/cc/score_cc.py
delta.json claims_fixture.json`` reads), ``delta.txt`` (the table printed), and
``votes_base.json`` / ``votes_head.json`` (``claim_vote.py --out``'s shape, one per side:
``score_cc.py votes_head.json claims_fixture.json --side head`` scores a side on its own).

CC-11: ``--accept all|committed`` (``[claim_vote] accept``, default ``all``) is the acceptance
rule of both sides' `tally` — one rule for both, so a FIXED never compares a side accepted over
abstainers with one that was not; both ``votes_*.json`` record it, and ``delta.json`` does when
it is not ``all``.

CC-14: ``--base-votes FILE`` reuses the base side an earlier run wrote to
``votes_base.json``, so a round of entries that all start from the same commit pays for the base
side once instead of once per entry. The reuse is conditional: the record's identity (the claims
voted, the base sha, the voters, the runs, the acceptance rule) must be this run's own, and any
difference is a refusal (exit 2, naming the field) rather than a stale base judged as this round's.
``judge_both`` is the same seam, with a ``base_side`` recorded by ``base_side_record``.

Exit: 0, or 1 for a ``--check`` mismatch, or 2 for a refusal (a target or ref that does not
open, an unreadable claim file, an ``expect`` that is not one of the four words, a recorded base
side whose identity is not this run's).
"""
from __future__ import annotations

import argparse
import difflib
import concurrent.futures
import json
import sys
import threading
import time
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import claim_vote as cv  # noqa: E402  (CC-6: prepare_target, ask_v2, tally, the ini and the pacer)
from tools.claimcheck import compare as cc_compare  # noqa: E402  (CC-7: the rule, delta.json)
from tools.claimcheck import judge as cc_judge  # noqa: E402  (batches: the cost of a run)
from tools.claimcheck.pack import PackBudget, build_pack  # noqa: E402
from tools.claimcheck.target import Target, TargetError  # noqa: E402

COST = """\
cost: one voter, one run, one side is one request per 10 world claims ([claim_vote] batch)
plus one per 2 code or mixed claims ([claim_vote] code_batch); both sides is about twice
that (world claims are judged at head only). The 80 claims of contest-bench/cc's fixture
are 36 requests a side, 72 both; three voters with --runs 3 about 650. The planned count
is printed first ([plan] on stderr), and a [progress] line follows every request and every
finished voter run, so a run of minutes shows which voter is alive."""

#: vote(items, side) -> the ask_v2 rows (every voter x run) of that side
VoteFn = Callable[[list, str], list]


# ------------------------------------------------------------------ input

def load_claims(path: Path) -> list:
    """The claims of *path* as dicts; ``expect`` normalised (a word outside the four raises ValueError)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("claims")
    if not isinstance(raw, list):
        raise ValueError("not a list of claims, nor an object with a `claims` list")
    claims = []
    for k, c in enumerate(raw):
        c = dict(c) if isinstance(c, dict) else {"claim": c}
        if not isinstance(c.get("claim"), str) or not c["claim"].strip():
            raise ValueError(f"claim {k + 1}: no `claim` text")
        # `"expected": "fixed"` was read as no expectation, and the gate passed it silently
        typo = [key for key in c if key != "expect" and difflib.get_close_matches(key, ["expect"], 1, 0.7)]
        if typo:
            raise ValueError(f"claim {c.get('id') or k + 1}: key `{typo[0]}` — did you mean `expect`?")
        try:
            c["expect"] = cc_compare.normalise_expect(c.get("expect"))
        except ValueError as exc:
            raise ValueError(f"claim {c.get('id') or k + 1}: {exc}") from None
        c.setdefault("truth", None)
        claims.append(c)
    return claims


# ------------------------------------------------------------------ the two sides

def prepare_sides(claims: list, base: Target, head: Target, budget: PackBudget,
                  collect: bool = True) -> dict:
    """The items, meta and resolved anchors of both sides, the world flags and the GONE flags.

    A claim that is ``world`` at one side only (a commit that exists at head alone) is not a
    world claim: its world side gets a pack too — usually the empty one — so a vote there
    cannot stand without evidence (CC-6's quote check downgrades it), and the rule gives
    ``NEW`` or ``UNCLEAR`` instead of a judgement nobody could see."""
    items_b, meta_b = cv.prepare_target(claims, base, budget, collect=collect)
    items_h, meta_h = cv.prepare_target(claims, head, budget, base=base.sha, collect=collect)
    model_b = base.collect() if collect else None      # cached by the target: no second build
    model_h = head.collect() if collect else None
    view_b, view_h = base.view(), head.view()
    res_b = [cc_compare.resolve(c["claim"], view_b) for c in claims]
    res_h = [cc_compare.resolve(c["claim"], view_h) for c in claims]
    world, gone = [], []
    for i, c in enumerate(claims):
        kb, kh = meta_b[i]["kind"], meta_h[i]["kind"]
        world.append(kb == "world" and kh == "world")
        if kb == "world" and kh != "world":
            items_b[i] = (c["claim"], build_pack(c["claim"], res_b[i], view_b, model=model_b, budget=budget))
            meta_b[i] = {**meta_b[i], "kind": kh}
        elif kh == "world" and kb != "world":
            items_h[i] = (c["claim"], build_pack(c["claim"], res_h[i], view_h, model=model_h, base=base.sha,
                                                 head=head.sha, budget=budget))
            meta_h[i] = {**meta_h[i], "kind": kb}
        gone.append(cc_compare.primary_vanished(res_b[i], res_h[i]))
    return {"items_b": items_b, "meta_b": meta_b, "items_h": items_h, "meta_h": meta_h,
            "world": world, "gone": gone}


def requests_of(items: list, voters: int, runs: int, batch: int, code_batch: int) -> int:
    return voters * runs * len(cc_judge.batches(items, batch, code_batch))


#: The five things that make a recorded base side this run's own, in the order the refusal names them.
BASE_FIELDS = ("claims", "base", "voters", "runs", "accept")


def base_side_record(claims: list, base: Target, voters: list, runs: int, got: dict) -> dict:
    """The ``base`` part of *got* with the identity of the run that produced it.

    A round judges the same claims at the same base once and reuses that side for every
    entry, so the record has to say who judged it: the claims voted, the base sha, the
    voters, the runs and the acceptance rule. ``base_side_differs`` compares all five
    before a second entry reuses it."""
    side = got["base"]
    index = side.get("index") or []
    return {**side, "identity": {
        "claims": [[claims[i].get("id"), claims[i].get("claim")] for i in index],
        "base": base.sha,
        "voters": sorted(str(v) for v in (voters or [])),
        "runs": int(runs or 0),
        "accept": got.get("accept", cv.DEFAULT_ACCEPT),
    }}


def base_side_from_votes(path) -> dict:
    """A ``votes_base.json`` as a recorded base side: the file's own identity, read from it.

    ``--base-votes`` is the same reuse from the command line, so the identity comes out of the
    file rather than being believed: the base sha is its ``target.sha``, the voters and runs
    its ``results``, the claims the tally rows it was tallied over. An unreadable or broken
    file is ``{}`` — the caller refuses that as an empty record, it never raises here."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    results = data.get("results") or []
    table = data.get("claims") or []
    runs = 0
    for r in results:
        if isinstance(r, dict):
            runs = max(runs, int(r.get("run") or 0) + 1)
    return {"rows": None, "results": results, "table": table,
            "index": list(range(len(table))),
            "identity": {
                "claims": [[row.get("id"), row.get("claim")] for row in table if isinstance(row, dict)],
                "base": str((data.get("target") or {}).get("sha") or ""),
                "voters": sorted({str(r.get("model")) for r in results if isinstance(r, dict)}),
                "runs": runs,
                "accept": str(data.get("accept") or cv.DEFAULT_ACCEPT),
            }}


def base_side_differs(record: dict, *, claims: list, base_sha: str, voters, runs,
                      accept: str, index: list) -> str:
    """The one field of *record*'s identity that is not this run's, or ``""`` when it is this run's base.

    The base side is reused only when all five agree (``BASE_FIELDS``); a record that differs in
    one of them is another round's base, so it is refused rather than judged as this round's
    evidence."""
    if not isinstance(record, dict) or not record.get("results"):
        return "the record carries no base votes: nothing to reuse"
    ident = record.get("identity") or {}
    want = [[claims[i].get("id"), claims[i].get("claim")] for i in index]
    if ident.get("claims") != want:
        got = ident.get("claims") or []
        return (f"the claims are not the record's ({len(got)} recorded, "
                f"{len(want)} in this run, first difference "
                f"{next((k for k, (a, b) in enumerate(zip(got, want)) if a != b), min(len(got), len(want))) + 1})")
    if ident.get("base") != base_sha:
        return f"base sha {ident.get('base') or '(not recorded)'}, not {base_sha}"
    if voters is not None and sorted(str(v) for v in voters) != ident.get("voters"):
        return f"voters {ident.get('voters') or []}, not {sorted(str(v) for v in voters)}"
    if runs is not None and int(ident.get("runs") or 0) != int(runs):
        return f"runs {ident.get('runs')}, not {runs}"
    if ident.get("accept") != accept:
        return f"accept rule {ident.get('accept')!r}, not {accept!r}"
    return ""


def judge_both(claims: list, base: Target, head: Target, vote: VoteFn,
               budget: PackBudget = PackBudget(), collect: bool = True,
               accept: str = cv.DEFAULT_ACCEPT, base_side: "dict | None" = None,
               voters=None, runs: "int | None" = None,
               sides: "dict | None" = None) -> dict:
    """The core of the command: both sides prepared, voted (the two sides at once, so their
    jobs share one pool) and tallied, and the deltas. *vote* is the seam: the CLI's
    `make_vote` (``ask_v2`` over voters x runs), or a test's fake voter.

    Returns ``{"deltas", "accept", "base": {"rows", "results", "table"}, "head": {...}}``; ``rows`` is
    one tally row per claim (``None`` at base for a world claim, which is judged at head only).
    *accept* (CC-11) is the one acceptance rule of both sides.

    *base_side* (CC-14) is the ``base_side_record`` of an earlier call of this function: the base
    side is not voted again, its recorded ``results`` stand in for the request. It is reused only
    when every field of its identity is this run's (``base_side_differs``); any difference raises
    ValueError naming the field, never a silent re-use. *voters* and *runs* are the run's own, so
    a record judged by other voters is seen to differ; *sides* spares the caller a second
    ``prepare_sides``."""
    sides = prepare_sides(claims, base, head, budget, collect) if sides is None else sides
    # A world claim is judged once, at head, so its change is always n/a; an expect on it can never be met, so it is refused before any request is made.
    odd = [str(c.get("id") or i + 1) for i, c in enumerate(claims) if c.get("expect") and sides["world"][i]]
    if odd:
        raise ValueError(f"expect on a world claim ({', '.join(odd)}): it is judged once, at head; "
                         "remove the expect or name the code it is about")
    idx_b = [i for i, w in enumerate(sides["world"]) if not w]
    items_b = [sides["items_b"][i] for i in idx_b]
    voter = vote
    if base_side is not None:
        diff = base_side_differs(base_side, claims=claims, base_sha=base.sha, voters=voters,
                                 runs=runs, accept=accept, index=idx_b)
        if diff:
            raise ValueError(f"recorded base side not re-used: {diff}")
        recorded = base_side["results"]

        def voter(items, side):
            return recorded if side == "base" else vote(items, side)
    with concurrent.futures.ThreadPoolExecutor(2) as two:
        got_b = two.submit(voter, items_b, "base")
        got_h = two.submit(voter, sides["items_h"], "head")
        results_b, results_h = got_b.result(), got_h.result()
    table_b = cv.tally([claims[i] for i in idx_b], results_b, meta=[sides["meta_b"][i] for i in idx_b],
                       accept=accept)
    table_h = cv.tally(claims, results_h, meta=sides["meta_h"], accept=accept)
    rows_b: list = [None] * len(claims)
    for i, row in zip(idx_b, table_b):
        rows_b[i] = row
    deltas = cc_compare.compare(claims, rows_b, table_h, gone=sides["gone"], world=sides["world"])
    return {"deltas": deltas, "accept": accept,
            "base": {"rows": rows_b, "results": results_b, "table": table_b, "index": idx_b},
            "head": {"rows": table_h, "results": results_h, "table": table_h}}


def make_vote(voters: list, runs: int, pool: concurrent.futures.Executor, ask: Callable,
              total: "dict | None" = None) -> VoteFn:
    """The real voting: ``ask(ref, items, run) -> ask_v2 row`` for every voter x run, on
    *pool* (shared by both sides). One ``[progress]`` line on stderr per finished job, with
    its side, so the log says which side of which voter is done."""
    total = total if total is not None else {}

    def vote(items: list, side: str) -> list:
        jobs = [(m, r) for m in voters for r in range(runs)]
        done = [0]
        lock = threading.Lock()   # the pool's threads finish together: an unlocked += lost counts

        def one(job):
            started = time.monotonic()
            row = ask(job[0], items, job[1])
            with lock:
                done[0] += 1
                n = done[0]
            print(f"[progress] {side} {job[0]} run{job[1]} done: votes={len(row.get('votes', {}))}"
                  f"/{len(items)} rejected={len(row.get('rejected', ()))} job {n}/{len(jobs)} "
                  f"{time.monotonic() - started:.0f}s"
                  + (f" ERROR {row['error']}" if row.get("error") else ""),
                  file=sys.stderr, flush=True)
            return row
        return list(pool.map(one, jobs))
    return vote


# ------------------------------------------------------------------ output

def side_report(target: Target, side: dict, base_sha: "str | None",
                accept: str = cv.DEFAULT_ACCEPT, *, budget: "PackBudget | None" = None,
                collect: "bool | None" = None, models: "dict | None" = None) -> dict:
    """``claim_vote.py --out``'s shape for one side (``score_cc.py --side`` reads it).

    The target fields are claim_vote's: ``base_sha`` next to ``base`` (without it claimcheck_truth
    warned that ``--base <sha>`` was "a name, not a sha"), ``collect`` and ``budget`` (the truth
    step's rebuilt packs lacked the collect chunks and used the ini's budget: not what the voters
    saw), and the voters' ``models`` (their families wherever the truth step runs)."""
    info = {"repo": target.repo, "ref": target.ref, "sha": target.sha,
            "base": base_sha, "base_sha": base_sha, "warnings": list(target.warnings)}
    if collect is not None:
        info["collect"] = bool(collect)
    if budget is not None:
        info["budget"] = {"chars": budget.chars, "chunks": budget.chunks, "per_chunk": budget.per_chunk}
    out = {"target": info, "accept": accept}
    if models is not None:
        out["models"] = models
    return {**out, "results": side["results"], "claims": side["table"]}


def write_outputs(out: Path, base: Target, head: Target, got: dict, *, budget: "PackBudget | None" = None,
                  collect: "bool | None" = None, models: "dict | None" = None) -> str:
    out.mkdir(parents=True, exist_ok=True)
    deltas = got["deltas"]
    accept = got.get("accept", cv.DEFAULT_ACCEPT)
    # The rule both sides were tallied under, so a later delta table says which one it is; `all` writes as before (no key: old delta.json reads as `all`).
    data = cc_compare.delta_json(base.sha, head.sha, deltas)
    if accept != cv.DEFAULT_ACCEPT:
        data["accept"] = accept
    (out / "delta.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, target, side, b in (("base", base, got["base"], None),
                                  ("head", head, got["head"], base.sha)):
        (out / f"votes_{name}.json").write_text(json.dumps(side_report(target, side, b, accept, budget=budget,
                                                                       collect=collect, models=models),
                                                           ensure_ascii=False, indent=1), encoding="utf-8")
    table = cc_compare.render(base.sha, head.sha, deltas)
    (out / "delta.txt").write_text(table + "\n", encoding="utf-8")
    return table


def _voter_lines(got: dict) -> list:
    lines = []
    for name in ("base", "head"):
        for r in got[name]["results"]:
            lines.append(f"{name} {r['model']:44} run{r['run']} votes={len(r['votes'])}"
                         f" rejected={len(r.get('rejected', ()))}"
                         + (f" ERROR {r['error']}" if r.get("error") else ""))
        dead = sorted({r["model"] for r in got[name]["results"] if not r["votes"]})
        lines.append(f"{name}: models with no votes at all: {', '.join(dead) or 'none'}")
    return lines


# ------------------------------------------------------------------ the command

def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], epilog=COST,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("claims", type=Path)
    ap.add_argument("--target", required=True, metavar="REPO", help="the repository (path or URL)")
    ap.add_argument("--base", required=True, metavar="REF", help="the commit before")
    ap.add_argument("--head", required=True, metavar="REF", help="the commit after")
    ap.add_argument("--fetch", action="store_true", help="git fetch --target before resolving the refs")
    ap.add_argument("--profiles", nargs="+", help="ini section names (contest.local.ini)")
    ap.add_argument("--models", nargs="+", help="provider/model from Kilo's files, no profile needed")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--fixed-prompt", action="store_true", help="the same prompt every run (a control)")
    ap.add_argument("--parallel", type=int, default=8, help="voter runs in flight, both sides together")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--batch", type=int, help="world claims per request ([claim_vote] batch)")
    ap.add_argument("--code-batch", type=int, help="code and mixed claims per request ([claim_vote] code_batch)")
    ap.add_argument("--pack-chars", type=int, help="rendered characters of one claim's pack ([claim_vote] pack_chars)")
    ap.add_argument("--per-provider", type=int, help="calls in flight per host ([claim_vote] per_provider)")
    ap.add_argument("--interval", type=float, help="seconds between call starts per host ([claim_vote] interval_sec)")
    ap.add_argument("--accept", metavar="all|committed",
                    help="the acceptance rule of both sides ([claim_vote] accept, default all; CC-11)")
    ap.add_argument("--out", type=Path, help="where delta.json and the rest go (default: <repo-root>/claim-check-out/diff-<UTC>)")
    ap.add_argument("--repo-root", type=Path, default=Path.cwd(), help="where contest.ini / contest.local.ini live")
    ap.add_argument("--scratch", type=Path, help="where the two worktrees go (default: <repo-root>/claim-check-out/scratch)")
    ap.add_argument("--check", action="store_true", help="exit 1 when a claim's `expect` is not its change")
    ap.add_argument("--no-collect", action="store_true", help="packs without the collect facts of the two trees")
    ap.add_argument("--base-votes", type=Path, metavar="FILE",
                    help="reuse the base side an earlier run recorded as votes_base.json "
                         "(the base is voted once, not per claim set): refused when its claims, "
                         "base sha, voters, runs or rule differ")
    args = ap.parse_args(argv)

    try:
        claims = load_claims(args.claims)
    except (OSError, ValueError) as exc:   # json.JSONDecodeError is a ValueError
        print(f"claim_diff: {args.claims}: {getattr(exc, 'strerror', None) or exc}", file=sys.stderr)
        return 2
    if args.check and not any(c["expect"] for c in claims):
        # "check: 0 of 0 claims with an expect match" and exit 0 was a gate that checked nothing
        print(f"claim_diff: --check: no claim in {args.claims} carries an `expect`: nothing to check",
              file=sys.stderr)
        return 2
    bad = cv.check_counts(args)
    if bad:
        ap.error(bad)
    parser = cv.read_ini(args.repo_root)
    accept, why = cv.accept_rule(args.accept, parser)
    if why:
        print(f"claim_diff: {why}", file=sys.stderr)
        return 2
    get = lambda key, cast, fallback: cast(parser.get(cv.SECTION, key, fallback=str(fallback)))  # noqa: E731
    batch = cv.given_or(args.batch, get("batch", int, 10))
    code_batch = cv.given_or(args.code_batch, get("code_batch", int, 2))
    budget = PackBudget.from_config(parser)
    if args.pack_chars:
        budget = PackBudget(chars=args.pack_chars, chunks=budget.chunks, per_chunk=budget.per_chunk)
    cv.PACER.__init__(cv.given_or(args.per_provider, get("per_provider", int, 2)),
                      cv.given_or(args.interval, get("interval_sec", float, 4.0)))
    voters = (args.profiles or []) + (args.models or []) or [
        n.strip() for n in parser.get(cv.SECTION, "llm_profiles", fallback="").split(",") if n.strip()]
    if not voters:
        ap.error("need voters: --profiles, --models, or [claim_vote] llm_profiles")
    if len(voters) < cv.MIN_COMMITTED:
        print(f"claim_diff: {len(voters)} voters, quorum is {cv.MIN_COMMITTED}: "
              "no claim can be decided, every change is UNCLEAR", file=sys.stderr)
    scratch = args.scratch or args.repo_root / "claim-check-out" / "scratch"
    out = args.out or args.repo_root / "claim-check-out" / f"diff-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    base_record = None
    if args.base_votes:
        base_record = base_side_from_votes(args.base_votes)
        if not base_record:
            print(f"claim_diff: --base-votes {args.base_votes}: no base votes to reuse "
                  "(an unreadable file, or not a votes_base.json)", file=sys.stderr)
            return 2

    with ExitStack() as stack:
        try:
            base = stack.enter_context(Target.open(args.target, args.base, scratch=scratch, fetch=args.fetch))
            head = stack.enter_context(Target.open(args.target, args.head, scratch=scratch))
        except TargetError as exc:
            print(f"claim_diff: --target refused: {exc}", file=sys.stderr)
            return 2
        for name, t in (("base", base), ("head", head)):
            for warning in t.warnings:
                print(f"claim_diff: warning ({name}): {warning}", file=sys.stderr)
        if base.sha == head.sha:
            print(f"claim_diff: warning: base and head are the same commit {base.sha[:7]}: "
                  "no FIXED is possible", file=sys.stderr)

        def ask(ref, items, run):
            return cv.ask_v2(ref, items, run, args.seed, parser, args.timeout, batch, code_batch,
                             args.fixed_prompt)

        planned = {}

        def plan_and_vote(items, side):
            if base_record is not None and side == "base":
                # The base side is this run's own record: its requests were paid by the run that wrote it,
                # so the plan names zero here rather than billing them twice.
                planned[side] = 0
                print(f"[plan] {side}: re-used from --base-votes, 0 requests "
                      f"({len(items)} claims not voted again)", file=sys.stderr, flush=True)
                return vote(items, side)
            planned[side] = requests_of(items, len(voters), args.runs, batch, code_batch)
            print(f"[plan] {side}: {len(items)} claims, {planned[side]} requests "
                  f"({len(voters)} voters x {args.runs} runs)", file=sys.stderr, flush=True)
            return vote(items, side)

        with concurrent.futures.ThreadPoolExecutor(max(1, args.parallel)) as pool:
            vote = make_vote(voters, args.runs, pool, ask)
            try:
                got = judge_both(claims, base, head, plan_and_vote, budget, collect=not args.no_collect,
                                 accept=accept, base_side=base_record, voters=voters, runs=args.runs)
            except ValueError as exc:
                print(f"claim_diff: {args.claims}: {exc}", file=sys.stderr)
                return 2
        table = write_outputs(out, base, head, got, budget=budget, collect=not args.no_collect,
                              models=cv.voter_models(voters, parser))

    for line in _voter_lines(got):
        print(line)
    print(table)
    deltas = got["deltas"]
    print(f"== {out}  changes: {dict(Counter(d.change for d in deltas))}"
          f" | requests planned: {sum(planned.values())} | rule: {accept}")
    if not args.check:
        return 0
    bad = cc_compare.mismatches(deltas)
    for d in bad:
        print(cc_compare.mismatch_line(d))
    checked = sum(1 for d in deltas if d.expect)
    print(f"check: {checked - len(bad)} of {checked} claims with an expect match"
          + ("" if not bad else f", {len(bad)} do not"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
