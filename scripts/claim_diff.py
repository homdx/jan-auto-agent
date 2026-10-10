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

Exit: 0, or 1 for a ``--check`` mismatch, or 2 for a refusal (a target or ref that does not
open, an unreadable claim file, an ``expect`` that is not one of the four words).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
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
        try:
            c["expect"] = cc_compare.normalise_expect(c.get("expect"))
        except ValueError as exc:
            raise ValueError(f"claim {c.get('id') or k + 1}: {exc}") from None
        c.setdefault("truth", None)
        claims.append(c)
    return claims


# ------------------------------------------------------------------ the two sides

def prepare_sides(claims: list, base: Target, head: Target, budget: PackBudget) -> dict:
    """The items, meta and resolved anchors of both sides, the world flags and the GONE flags.

    A claim that is ``world`` at one side only (a commit that exists at head alone) is not a
    world claim: its world side gets a pack too — usually the empty one — so a vote there
    cannot stand without evidence (CC-6's quote check downgrades it), and the rule gives
    ``NEW`` or ``UNCLEAR`` instead of a judgement nobody could see."""
    items_b, meta_b = cv.prepare_target(claims, base, budget)
    items_h, meta_h = cv.prepare_target(claims, head, budget, base=base.sha)
    view_b, view_h = base.view(), head.view()
    res_b = [cc_compare.resolve(c["claim"], view_b) for c in claims]
    res_h = [cc_compare.resolve(c["claim"], view_h) for c in claims]
    world, gone = [], []
    for i, c in enumerate(claims):
        kb, kh = meta_b[i]["kind"], meta_h[i]["kind"]
        world.append(kb == "world" and kh == "world")
        if kb == "world" and kh != "world":
            items_b[i] = (c["claim"], build_pack(c["claim"], res_b[i], view_b, budget=budget))
            meta_b[i] = {**meta_b[i], "kind": kh}
        elif kh == "world" and kb != "world":
            items_h[i] = (c["claim"], build_pack(c["claim"], res_h[i], view_h, base=base.sha,
                                                 head=head.sha, budget=budget))
            meta_h[i] = {**meta_h[i], "kind": kb}
        gone.append(cc_compare.primary_vanished(res_b[i], res_h[i]))
    return {"items_b": items_b, "meta_b": meta_b, "items_h": items_h, "meta_h": meta_h,
            "world": world, "gone": gone}


def requests_of(items: list, voters: int, runs: int, batch: int, code_batch: int) -> int:
    return voters * runs * len(cc_judge.batches(items, batch, code_batch))


def judge_both(claims: list, base: Target, head: Target, vote: VoteFn,
               budget: PackBudget = PackBudget()) -> dict:
    """The core of the command: both sides prepared, voted (the two sides at once, so their
    jobs share one pool) and tallied, and the deltas. *vote* is the seam: the CLI's
    `make_vote` (``ask_v2`` over voters x runs), or a test's fake voter.

    Returns ``{"deltas", "base": {"rows", "results", "table"}, "head": {...}}``; ``rows`` is one
    tally row per claim (``None`` at base for a world claim, which is judged at head only)."""
    sides = prepare_sides(claims, base, head, budget)
    idx_b = [i for i, w in enumerate(sides["world"]) if not w]
    items_b = [sides["items_b"][i] for i in idx_b]
    with concurrent.futures.ThreadPoolExecutor(2) as two:
        got_b = two.submit(vote, items_b, "base")
        got_h = two.submit(vote, sides["items_h"], "head")
        results_b, results_h = got_b.result(), got_h.result()
    table_b = cv.tally([claims[i] for i in idx_b], results_b, meta=[sides["meta_b"][i] for i in idx_b])
    table_h = cv.tally(claims, results_h, meta=sides["meta_h"])
    rows_b: list = [None] * len(claims)
    for i, row in zip(idx_b, table_b):
        rows_b[i] = row
    deltas = cc_compare.compare(claims, rows_b, table_h, gone=sides["gone"], world=sides["world"])
    return {"deltas": deltas,
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

        def one(job):
            started = time.monotonic()
            row = ask(job[0], items, job[1])
            done[0] += 1
            print(f"[progress] {side} {job[0]} run{job[1]} done: votes={len(row.get('votes', {}))}"
                  f"/{len(items)} rejected={len(row.get('rejected', ()))} job {done[0]}/{len(jobs)} "
                  f"{time.monotonic() - started:.0f}s"
                  + (f" ERROR {row['error']}" if row.get("error") else ""),
                  file=sys.stderr, flush=True)
            return row
        return list(pool.map(one, jobs))
    return vote


# ------------------------------------------------------------------ output

def side_report(target: Target, side: dict, base_sha: "str | None") -> dict:
    """``claim_vote.py --out``'s shape for one side (``score_cc.py --side`` reads it)."""
    return {"target": {"repo": target.repo, "ref": target.ref, "sha": target.sha,
                       "base": base_sha, "warnings": list(target.warnings)},
            "results": side["results"], "claims": side["table"]}


def write_outputs(out: Path, base: Target, head: Target, got: dict) -> str:
    out.mkdir(parents=True, exist_ok=True)
    deltas = got["deltas"]
    (out / "delta.json").write_text(json.dumps(cc_compare.delta_json(base.sha, head.sha, deltas),
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    for name, target, side, b in (("base", base, got["base"], None),
                                  ("head", head, got["head"], base.sha)):
        (out / f"votes_{name}.json").write_text(json.dumps(side_report(target, side, b),
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
    ap.add_argument("--out", type=Path, help="where delta.json and the rest go (default: <repo-root>/claim-check-out/diff-<UTC>)")
    ap.add_argument("--repo-root", type=Path, default=Path.cwd(), help="where contest.ini / contest.local.ini live")
    ap.add_argument("--scratch", type=Path, help="where the two worktrees go (default: <repo-root>/claim-check-out/scratch)")
    ap.add_argument("--check", action="store_true", help="exit 1 when a claim's `expect` is not its change")
    args = ap.parse_args(argv)

    try:
        claims = load_claims(args.claims)
    except (OSError, ValueError) as exc:   # json.JSONDecodeError is a ValueError
        print(f"claim_diff: {args.claims}: {getattr(exc, 'strerror', None) or exc}", file=sys.stderr)
        return 2
    parser = cv.read_ini(args.repo_root)
    get = lambda key, cast, fallback: cast(parser.get(cv.SECTION, key, fallback=str(fallback)))  # noqa: E731
    batch = args.batch or get("batch", int, 10)
    code_batch = args.code_batch or get("code_batch", int, 2)
    budget = PackBudget.from_config(parser)
    if args.pack_chars and args.pack_chars > 0:
        budget = PackBudget(chars=args.pack_chars, chunks=budget.chunks, per_chunk=budget.per_chunk)
    cv.PACER.__init__(args.per_provider or get("per_provider", int, 2),
                      args.interval or get("interval_sec", float, 4.0))
    voters = (args.profiles or []) + (args.models or []) or [
        n.strip() for n in parser.get(cv.SECTION, "llm_profiles", fallback="").split(",") if n.strip()]
    if not voters:
        ap.error("need voters: --profiles, --models, or [claim_vote] llm_profiles")
    if len(voters) < cv.MIN_COMMITTED:
        print(f"claim_diff: {len(voters)} voters, quorum is {cv.MIN_COMMITTED}: "
              "no claim can be decided, every change is UNCLEAR", file=sys.stderr)
    scratch = args.scratch or args.repo_root / "claim-check-out" / "scratch"
    out = args.out or args.repo_root / "claim-check-out" / f"diff-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

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
            planned[side] = requests_of(items, len(voters), args.runs, batch, code_batch)
            print(f"[plan] {side}: {len(items)} claims, {planned[side]} requests "
                  f"({len(voters)} voters x {args.runs} runs)", file=sys.stderr, flush=True)
            return vote(items, side)

        with concurrent.futures.ThreadPoolExecutor(max(1, args.parallel)) as pool:
            vote = make_vote(voters, args.runs, pool, ask)
            got = judge_both(claims, base, head, plan_and_vote, budget)
        table = write_outputs(out, base, head, got)

    for line in _voter_lines(got):
        print(line)
    print(table)
    deltas = got["deltas"]
    print(f"== {out}  changes: {dict(Counter(d.change for d in deltas))}"
          f" | requests planned: {sum(planned.values())}")
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
