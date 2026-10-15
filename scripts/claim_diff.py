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


#: What makes a recorded base side this run's own, in the order the refusal names it.
#:
#: CC-16 (Part B) added the settings that decide what the voters were shown and who they
#: were: the collect flag, the pack budget (chars, chunks, per chunk), the seed, the
#: fixed-prompt mode and the model behind each voter name. The verdicts of a side depend
#: on all of them — a base judged with the big pack and the collect facts compared against
#: a head judged with the small pack and none is the "stale base judged as this round's"
#: error the refusal exists to prevent — so a record judged under other settings is
#: another run's evidence.
#:
#: CC-16 (Part E) took the claims *out* of the identity: which claims are code claims is
#: answered per (base, head) pair, so the same claim anchors in one entry and not in
#: another, and a record of the first entry's classification refused the judging of the
#: whole round. The claims are now matched claim by claim instead.
BASE_FIELDS = ("collect", "budget", "seed", "fixed_prompt", "models",
               "base", "voters", "runs", "accept")


def _budget_identity(budget) -> "dict | None":
    """The three numbers of a pack budget, or ``None`` when this run does not state one."""
    if budget is None:
        return None
    return {"chars": budget.chars, "chunks": budget.chunks, "per_chunk": budget.per_chunk}


def _claim_key(claim: dict):
    """The identity of one claim: its id and its text. Two claims are the same claim when
    both agree — a claim whose id changed is a different claim, and one with no id is
    identified by its text alone."""
    return (claim.get("id"), claim.get("claim"))


def _rekey_votes(row: dict, mapping: dict) -> dict:
    """One vote row with its votes re-keyed through *mapping* (old position -> new).

    A vote's key is the position of its claim in the item list the voter was shown, so a
    row judged over one claim set does not mean the same claims in another: the same
    re-keying the tally's own `int(k)` normalisation does. A key with no mapping is
    dropped — it belongs to a claim this side is not judging. `accepted` (CC-11's
    ``committed`` rule reads it) is re-keyed the same way, and a row read back from JSON
    has string keys where a live one has ints, so both are normalised here."""
    out = dict(row)
    for field in ("votes", "accepted"):
        if field not in row:
            continue
        keyed = {}
        for k, v in (row.get(field) or {}).items():
            try:
                k = int(k)
            except (TypeError, ValueError):
                continue
            if k in mapping:
                keyed[mapping[k]] = v
        out[field] = keyed
    return out


def _record_slots(claims: list, recorded_keys: list, index: list) -> dict:
    """`{the record's position -> this record's position}` for the claims both hold.

    CC-16 (Part E): the record's rows are keyed by the claim set of the run that judged
    them; `index` is this side's own claim set, in order. A claim that is in one and not
    the other has no mapping and its votes are dropped by `_rekey_votes` — a world claim
    for this entry, or one the record never held."""
    keys = [_claim_key(c) for c in claims]
    out = {}
    for k, key in enumerate(recorded_keys):
        if key in keys:
            out[k] = index.index(keys.index(key))
    return out


def _merge_vote_rows(recorded: list, fresh: list, fresh_map: dict) -> list:
    """The recorded rows plus *fresh*'s, one row per (model, run).

    CC-16 (Part E): the two halves answer different claims of the same base, so they are
    merged rather than one replacing the other — a voter that appears in only one half
    still gets its row, and its votes for the other half's claims stand."""
    out = []
    for row in recorded:
        merged = dict(row)
        other = next((f for f in fresh
                      if f.get("model") == row.get("model") and f.get("run") == row.get("run")), None)
        if other is not None:
            rekeyed = _rekey_votes(other, fresh_map)
            for field in ("votes", "accepted"):
                if field in rekeyed or field in merged:
                    merged[field] = {**(merged.get(field) or {}), **(rekeyed.get(field) or {})}
        out.append(merged)
    for f in fresh:
        if not any(r.get("model") == f.get("model") and r.get("run") == f.get("run") for r in out):
            out.append(_rekey_votes(f, fresh_map))
    return out


def base_side_record(claims: list, base: Target, voters: list, runs: int, got: dict, *,
                     collect: "bool | None" = None, budget: "PackBudget | None" = None,
                     seed: "int | None" = None, fixed_prompt: "bool | None" = None,
                     models: "dict | None" = None) -> dict:
    """The ``base`` part of *got* with the identity of the run that produced it.

    A round judges the same claims at the same base once and reuses that side for every
    entry, so the record has to say who judged it and under what settings: the base sha,
    the voters, the runs, the acceptance rule, the collect flag, the pack budget, the
    seed, the fixed-prompt mode and the model behind each voter name (CC-16, Part B).
    ``base_side_differs`` compares them all before a second entry reuses it.

    ``claims`` is indexed by *got*'s own ``base.index``, so a record built from an entry
    that judged only some of the claims still describes exactly the claims it holds."""
    side = got["base"]
    index = side.get("index") or []
    return {**side, "identity": {
        "claims": [[claims[i].get("id"), claims[i].get("claim")] for i in index],
        "base": base.sha,
        "voters": sorted(str(v) for v in (voters or [])),
        "runs": int(runs or 0),
        "accept": got.get("accept", cv.DEFAULT_ACCEPT),
        "collect": None if collect is None else bool(collect),
        "budget": _budget_identity(budget),
        "seed": None if seed is None else int(seed),
        "fixed_prompt": None if fixed_prompt is None else bool(fixed_prompt),
        "models": {str(k): str(v) for k, v in (models or {}).items()},
    }}


def base_side_from_votes(path, *, seed: "int | None" = None,
                         fixed_prompt: "bool | None" = None) -> dict:
    """A ``votes_base.json`` as a recorded base side: the file's own identity, read from it.

    ``--base-votes`` is the same reuse from the command line, so the identity comes out of the
    file rather than being believed: the base sha is its ``target.sha``, the voters and runs
    its ``results``, the claims the tally rows it was tallied over, and — CC-16 (Part B) —
    the collect flag, the budget and the voters' models from ``side_report``'s ``target``
    and ``models``. The seed and the prompt mode are options of the run, not of a votes
    file, so they are what the command line states (a file that carries them wins).

    An unreadable or broken file is ``{}`` — the caller refuses that as an empty record, it
    never raises here."""
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
    target = data.get("target") or {}
    budget = target.get("budget") if isinstance(target.get("budget"), dict) else None
    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    seed_value = seed
    if isinstance(data.get("seed"), int) and not isinstance(data.get("seed"), bool):
        seed_value = data["seed"]
    prompt_value = fixed_prompt
    if isinstance(data.get("fixed_prompt"), bool):
        prompt_value = data["fixed_prompt"]
    return {"rows": None, "results": results, "table": table,
            "index": list(range(len(table))),
            "identity": {
                "claims": [[row.get("id"), row.get("claim")] for row in table if isinstance(row, dict)],
                "base": str(target.get("sha") or ""),
                "voters": sorted({str(r.get("model")) for r in results if isinstance(r, dict)}),
                "runs": runs,
                "accept": str(data.get("accept") or cv.DEFAULT_ACCEPT),
                "collect": target.get("collect") if isinstance(target.get("collect"), bool) else None,
                "budget": ({"chars": int(budget.get("chars")), "chunks": int(budget.get("chunks")),
                            "per_chunk": int(budget.get("per_chunk"))}
                           if budget is not None and all(k in budget for k in ("chars", "chunks", "per_chunk")) else None),
                "seed": None if seed_value is None else int(seed_value),
                "fixed_prompt": None if prompt_value is None else bool(prompt_value),
                "models": {str(k): str(v) for k, v in models.items()},
            }}


def _identity_refusal(field: str, got, want) -> str:
    """The refusal for one identity field that is not this run's, naming it as the
    first five always did."""
    if field == "collect":
        if got and not want:
            return "collect: the record was judged with the collect facts, this run is not"
        return "collect: the record was judged without the collect facts, this run has them"
    if field == "budget":
        return f"budget: the record was judged with a pack of {got}, this run's is {want}"
    if field == "seed":
        return f"seed: the record was judged with seed {got}, this run's is {want}"
    if field == "fixed_prompt":
        return (f"fixed prompt: the record was judged with fixed-prompt={got}, "
                f"this run's is {want}")
    if field == "models":
        return f"voter models: the record was judged by {got}, this run's are {want}"
    return f"{field}: {got!r}, not {want!r}"


def base_side_differs(record: dict, *, claims: list, base_sha: str, voters, runs,
                      accept: str, index: list, collect: "bool | None" = None,
                      budget: "PackBudget | None" = None, seed: "int | None" = None,
                      fixed_prompt: "bool | None" = None,
                      models: "dict | None" = None) -> str:
    """The one field of *record*'s identity that is not this run's, or ``""`` when it is this run's base.

    The base side is reused only when the run's own settings agree (``BASE_FIELDS``); a record
    judged under other settings is another run's base, so it is refused rather than judged
    as this round's evidence. A field this run does not state is not compared: only
    ``judge_epic_round --claims`` and ``claim_diff --base-votes`` state all of them, and a
    caller that says nothing about the budget cannot be told the budget differs.

    CC-16 (Part B): a record written before these fields existed has none of them, and it
    cannot be *shown* to be this run's, so it is refused with a message that says so —
    never accepted by the absence of a comparison.

    CC-16 (Part E): the claims are not compared here. Which of them are code claims is a
    per-entry answer, so a claim set that differs is answered claim by claim by `judge_both`
    (the record's verdicts for the claims it holds, a fresh vote for the ones it does not),
    never by a refusal that would take down the judging of every entry after the first."""
    if not isinstance(record, dict) or not record.get("results"):
        return "the record carries no base votes: nothing to reuse"
    ident = record.get("identity") or {}
    for field, want in (("collect", None if collect is None else bool(collect)),
                        ("budget", _budget_identity(budget)),
                        ("seed", None if seed is None else int(seed)),
                        ("fixed_prompt", None if fixed_prompt is None else bool(fixed_prompt)),
                        ("models", {str(k): str(v) for k, v in (models or {}).items()})):
        if want is None:
            continue
        if field not in ident:
            return (f"{field}: the record does not say what it was judged with — it was "
                    f"written before {field} was part of a base side's identity, so it "
                    f"cannot be shown to be this run's")
        if ident[field] != want:
            return _identity_refusal(field, ident[field], want)
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
               sides: "dict | None" = None, seed: "int | None" = None,
               fixed_prompt: "bool | None" = None,
               models: "dict | None" = None) -> dict:
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
    a record judged by other voters is seen to differ; *seed*, *fixed_prompt* and *models* are the
    rest of what the voters were shown (CC-16, Part B). *sides* spares the caller a second
    ``prepare_sides``.

    CC-16 (Part E): the reuse is claim by claim, not all-or-nothing. Whether a claim is a
    world claim (judged at head alone) or a code claim is decided per (base, head) pair, so
    the same claim anchors in one entry and not in another; the record holds what the first
    entry judged. A claim of *this* side that the record holds is taken from it; a claim it
    does not hold is voted here, once, and kept in the side's ``index`` so the next entry
    that needs it finds it in the record. A claim set that differs is therefore never a
    refusal — only the identity fields above are.
    """
    sides = prepare_sides(claims, base, head, budget, collect) if sides is None else sides
    # A world claim is judged once, at head, so its change is always n/a; an expect on it can never be met, so it is refused before any request is made.
    odd = [str(c.get("id") or i + 1) for i, c in enumerate(claims) if c.get("expect") and sides["world"][i]]
    if odd:
        raise ValueError(f"expect on a world claim ({', '.join(odd)}): it is judged once, at head; "
                         "remove the expect or name the code it is about")
    idx_b = [i for i, w in enumerate(sides["world"]) if not w]
    items_b = [sides["items_b"][i] for i in idx_b]
    base_index = list(idx_b)
    # CC-16 (Part E): what the record holds, what it does not, and the re-keying between
    # its claim set and this side's. `recorded`/`slot`/`fresh` stay undefined when there is
    # no record, which is what the `voter` below branches on.
    if base_side is not None:
        diff = base_side_differs(base_side, claims=claims, base_sha=base.sha, voters=voters,
                                 runs=runs, accept=accept, index=idx_b, collect=collect,
                                 budget=budget, seed=seed, fixed_prompt=fixed_prompt, models=models)
        if diff:
            raise ValueError(f"recorded base side not re-used: {diff}")
        recorded = base_side["results"]
        recorded_keys = [tuple(k) for k in ((base_side.get("identity") or {}).get("claims") or [])]
        # The record's position -> this side's position, for the claims the record holds
        # (HARDEN-8 A). `_rekey_votes` maps old position -> new, and a recorded row's key is the
        # position of its claim in the item list the record's voter was shown, so the mapping has
        # to run from the record's side, not this side's. The two directions agree only while the
        # claims sit in the same order as in the record, which is the one case the earlier tests
        # put under test — reordered, a reused side handed each claim another claim's verdict.
        slot = {recorded_keys.index(_claim_key(claims[i])): j
                for j, i in enumerate(idx_b) if _claim_key(claims[i]) in recorded_keys}
        # A claim the record does not hold is voted here: its base verdict did not exist
        # yet, and refusing the whole side over it took down every entry after the first.
        fresh = [j for j in range(len(idx_b)) if j not in slot.values()]
        fresh_items = [items_b[j] for j in fresh]
        # Every claim of this run the record holds stays in the record: a claim that is a
        # world claim for *this* entry was voted at the base by an entry it anchored in,
        # and the next entry that needs it must not pay for it again.
        held = [i for i, c in enumerate(claims) if _claim_key(c) in recorded_keys]
        base_index = sorted(set(idx_b) | set(held))
        # A vote's key is the position of its claim in the item list the voter was shown,
        # so both halves are re-keyed — into this side's own positions for the tally, into
        # the record's for the side that is handed back — before they are merged.
        record_slots = _record_slots(claims, recorded_keys, base_index)
        fresh_out: dict = {"rows": []}

        def voter(items, side):
            if side != "base":
                return vote(items, side)
            if not items:
                return [_rekey_votes(r, slot) for r in recorded]
            fresh_rows = vote(items, "base")
            fresh_out["rows"] = fresh_rows
            return _merge_vote_rows([_rekey_votes(r, slot) for r in recorded], fresh_rows,
                                    {p: fresh[p] for p in range(len(fresh))})
    else:
        def voter(items, side):
            return vote(items, side)

    with concurrent.futures.ThreadPoolExecutor(2) as two:
        # The base side is asked for the claims the record does not hold only, so its job
        # is the fresh one — empty when the record covers the side, which is what makes a
        # re-use cost nothing.
        got_b = two.submit(voter, fresh_items if base_side is not None else items_b, "base")
        got_h = two.submit(voter, sides["items_h"], "head")
        results_b, results_h = got_b.result(), got_h.result()
    table_b = cv.tally([claims[i] for i in idx_b], results_b, meta=[sides["meta_b"][i] for i in idx_b],
                       accept=accept)
    table_h = cv.tally(claims, results_h, meta=sides["meta_h"], accept=accept)
    rows_b: list = [None] * len(claims)
    for i, row in zip(idx_b, table_b):
        rows_b[i] = row
    deltas = cc_compare.compare(claims, rows_b, table_h, gone=sides["gone"], world=sides["world"])
    # `results` is what was tallied here; `voted` (Part E) is every claim already judged
    # at the base, keyed by `index`, so the record the caller builds from this side keeps
    # them for the next entry. They are the same list whenever nothing was carried over.
    if base_side is None:
        voted_b = results_b
    else:
        # The fresh half is keyed by the positions of the item list it was shown, so a position
        # p there is the claim `idx_b[fresh[p]]`; `base_index` holds claim indices, so the two
        # spaces are joined through `idx_b` and never by a position used as an index (HARDEN-8 B).
        # Used raw, a side whose code claims were a subset of the round's raised
        # `ValueError: <position> is not in list` after the head and the fresh claim had been
        # voted and paid for, and `claims_phase` reported it as a refusal of that entry.
        voted_b = _merge_vote_rows(
            [_rekey_votes(r, record_slots) for r in recorded], fresh_out["rows"],
            {p: base_index.index(idx_b[fresh[p]]) for p in range(len(fresh))})
    return {"deltas": deltas, "accept": accept,
            "base": {"rows": rows_b, "results": results_b, "voted": voted_b,
                     "table": table_b, "index": base_index},
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
                collect: "bool | None" = None, models: "dict | None" = None,
                seed: "int | None" = None, fixed_prompt: "bool | None" = None) -> dict:
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
    # The seed and the prompt mode decide what the voters are asked, so a side that is reused later has to say which it was
    # judged with (a record without them is refused: it cannot be shown to be this run's).
    if seed is not None:
        out["seed"] = int(seed)
    if fixed_prompt is not None:
        out["fixed_prompt"] = bool(fixed_prompt)
    return {**out, "results": side["results"], "claims": side["table"]}


def write_outputs(out: Path, base: Target, head: Target, got: dict, *, budget: "PackBudget | None" = None,
                  collect: "bool | None" = None, models: "dict | None" = None,
                  seed: "int | None" = None, fixed_prompt: "bool | None" = None) -> str:
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
                                                                       collect=collect, models=models, seed=seed,
                                                                       fixed_prompt=fixed_prompt),
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
                         "(the base is voted once, not per claim set): refused when its "
                         "collect flag, pack budget, seed, prompt mode, voter models, base "
                         "sha, voters, runs or rule differ; the claims it does not hold "
                         "are voted here (CC-16)")
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
        base_record = base_side_from_votes(args.base_votes, seed=args.seed,
                                           fixed_prompt=args.fixed_prompt)
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
                # The base side is this run's own record: the claims it holds were paid by
                # the run that wrote it, so the plan names only the claims it does not
                # (CC-16, Part E) rather than billing the whole side twice.
                planned[side] = requests_of(items, len(voters), args.runs, batch, code_batch)
                if not items:
                    print(f"[plan] {side}: re-used from --base-votes, 0 requests "
                          f"(every claim of this side is in the record)", file=sys.stderr, flush=True)
                    return []
                print(f"[plan] {side}: {len(items)} claim(s) not in --base-votes, "
                      f"{planned[side]} requests ({len(voters)} voters x {args.runs} runs)",
                      file=sys.stderr, flush=True)
                return vote(items, side)
            planned[side] = requests_of(items, len(voters), args.runs, batch, code_batch)
            print(f"[plan] {side}: {len(items)} claims, {planned[side]} requests "
                  f"({len(voters)} voters x {args.runs} runs)", file=sys.stderr, flush=True)
            return vote(items, side)

        with concurrent.futures.ThreadPoolExecutor(max(1, args.parallel)) as pool:
            vote = make_vote(voters, args.runs, pool, ask)
            try:
                got = judge_both(claims, base, head, plan_and_vote, budget, collect=not args.no_collect,
                                 accept=accept, base_side=base_record, voters=voters, runs=args.runs,
                                 seed=args.seed, fixed_prompt=args.fixed_prompt,
                                 models=cv.voter_models(voters, parser))
            except ValueError as exc:
                print(f"claim_diff: {args.claims}: {exc}", file=sys.stderr)
                return 2
        table = write_outputs(out, base, head, got, budget=budget, collect=not args.no_collect,
                              models=cv.voter_models(voters, parser), seed=args.seed, fixed_prompt=args.fixed_prompt)

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
