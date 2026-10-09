#!/usr/bin/env python3
"""CC-0: score a claim-check run against a claim file with a known answer.

    score_cc.py votes.json claims_fixture.json [--side base|head] [--thresholds thresholds.json] [--json]
    score_cc.py delta.json claims_fixture.json [--thresholds …]     # CC-7: FIXED/STILL/NEW/GONE

``votes.json`` is what ``scripts/claim_vote.py --out`` writes: ``claims`` rows
with ``verdict`` and ``unanimous`` (and, from CC-6 on, ``id``, ``evidence``,
``quotes`` and ``downgrades``). A row is matched to the key by ``id`` when it
has one, else by the claim's text.

Per kind and overall:

* ``decided`` — rows the tool accepts: ``unanimous`` with a TRUE/FALSE verdict;
* ``right`` / ``wrong`` — decided rows against the key; ``undecided`` the rest;
* ``coverage`` = decided / claims, ``precision`` = right / decided,
  ``wrong_rate`` = wrong / decided;
* ``raw_*`` — the same over what the models said before the tool's own vetoes:
  every voter's verdict (``by_model``) equal and committed. ``claim_vote.py``
  never accepts a claim it thinks is about code (``CODE-CHECK``), so without
  ``raw`` the no-evidence baseline would be zero by construction, not by
  measurement.

A claim whose truth on the chosen side is ``null`` (a ``gone`` claim at head)
has no answer to be right about: it is counted in ``no_truth`` only.

A ``delta.json`` (a list of ``{id|claim, change}``, or ``{"deltas": [...]}``)
is scored against each claim's ``expect``: the confusion of expected against
measured change, and ``fix_right`` — the share of ``fixed``/``still`` claims
whose change is ``FIXED``/``STILL`` respectively; ``still_as_fixed`` counts the
``still`` claims reported ``FIXED`` — the costly error, a bug called gone that is not.

With ``--thresholds``, each target of the claim file's set is checked; exit 1
when one is missed. A target whose metric is ``null`` or absent from this
run is reported ``n/a`` and does not fail.
"""

from __future__ import annotations

import argparse
import json
import operator
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

COMMITTED = ("TRUE", "FALSE")
CHANGES = ("FIXED", "STILL", "NEW", "GONE", "UNCLEAR", "n/a")
_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq}


# ── reading ───────────────────────────────────────────────────────────────

def key_rows(truth) -> list[dict]:
    """The claim rows of a claim file (the object, or a bare list)."""
    return truth["claims"] if isinstance(truth, dict) else list(truth)


def truth_of(row: dict, side: str) -> Optional[bool]:
    """The known answer on *side*; ``truth`` for a file with one side only."""
    field = f"truth_{side}"
    return row[field] if field in row else row.get("truth")


def _index(rows: list[dict]) -> tuple[dict, dict]:
    return ({r["id"]: r for r in rows if r.get("id")},
            {r["claim"]: r for r in rows})


def _match(vote: dict, by_id: dict, by_text: dict) -> Optional[dict]:
    if vote.get("id") in by_id:
        return by_id[vote["id"]]
    return by_text.get(vote.get("claim"))


def _voters(votes: dict) -> set:
    return {r["model"] for r in votes.get("results", []) if r.get("votes")}


def _raw_verdict(vote: dict, voters: set, quorum: int = 3) -> Optional[str]:
    """What every voter said, when they all committed to one verdict."""
    by_model = vote.get("by_model") or {}
    if not voters or set(by_model) != voters or len(voters) < quorum:
        return None
    said = set(by_model.values())
    return said.pop() if len(said) == 1 and next(iter(said)) in COMMITTED else None


def _downgrades(vote: dict) -> int:
    d = vote.get("downgrades", 0)
    return len(d) if isinstance(d, (list, tuple)) else int(d or 0)


# ── scoring ───────────────────────────────────────────────────────────────

def _empty() -> dict:
    return Counter(claims=0, no_truth=0, decided=0, right=0, wrong=0, undecided=0,
                   raw_decided=0, raw_right=0, raw_wrong=0, downgrades=0)


def _finish(c: Counter) -> dict:
    out = dict(c)
    judged = c["claims"] - c["no_truth"]
    out["coverage"] = round(c["decided"] / judged, 4) if judged else None
    out["precision"] = round(c["right"] / c["decided"], 4) if c["decided"] else None
    out["wrong_rate"] = round(c["wrong"] / c["decided"], 4) if c["decided"] else None
    out["raw_coverage"] = round(c["raw_decided"] / judged, 4) if judged else None
    out["raw_wrong_rate"] = round(c["raw_wrong"] / c["raw_decided"], 4) if c["raw_decided"] else None
    return out


def score(votes: dict, truth, side: str = "base") -> dict:
    """The score table of *votes* (claim_vote.py's report) against *truth*."""
    rows = key_rows(truth)
    by_id, by_text = _index(rows)
    voters = _voters(votes)
    seen: dict[str, dict] = {}
    for vote in votes.get("claims", []):
        row = _match(vote, by_id, by_text)
        if row is not None:
            seen[row.get("id") or row["claim"]] = vote
    kinds: dict[str, Counter] = {}
    overall = _empty()
    for row in rows:
        kind = row.get("kind", "?")
        bucket = kinds.setdefault(kind, _empty())
        known = truth_of(row, side)
        vote = seen.get(row.get("id") or row["claim"], {})
        tick = Counter(claims=1, downgrades=_downgrades(vote))
        if known is None:
            tick["no_truth"] += 1
        else:
            want = "TRUE" if known else "FALSE"
            verdict = vote.get("verdict")
            if vote.get("unanimous") and verdict in COMMITTED:
                tick["decided"] += 1
                tick["right" if verdict == want else "wrong"] += 1
            else:
                tick["undecided"] += 1
            raw = _raw_verdict(vote, voters)
            if raw is not None:
                tick["raw_decided"] += 1
                tick["raw_right" if raw == want else "raw_wrong"] += 1
        bucket.update(tick)
        overall.update(tick)
    return {"set": truth.get("set") if isinstance(truth, dict) else None, "side": side,
            "voters": sorted(voters), "matched": len(seen), "claims": len(rows),
            "kinds": {k: _finish(v) for k, v in sorted(kinds.items())},
            "overall": _finish(overall)}


def score_deltas(deltas, truth) -> dict:
    """CC-7: measured change against each claim's ``expect``."""
    items = deltas.get("deltas", []) if isinstance(deltas, dict) else list(deltas)
    rows = key_rows(truth)
    by_id, by_text = _index(rows)
    confusion: dict[str, Counter] = {}
    for d in items:
        row = _match(d, by_id, by_text)
        if row is None or not row.get("expect"):
            continue
        confusion.setdefault(row["expect"], Counter())[d.get("change", "UNCLEAR")] += 1
    expected = Counter(r["expect"] for r in rows if r.get("expect"))
    fix_total = expected["fixed"] + expected["still"]
    fix_hits = confusion.get("fixed", Counter())["FIXED"] + confusion.get("still", Counter())["STILL"]
    return {"set": truth.get("set") if isinstance(truth, dict) else None,
            "expected": dict(expected),
            "confusion": {k: dict(v) for k, v in sorted(confusion.items())},
            "delta": {"fix_right": round(fix_hits / fix_total, 4) if fix_total else None,
                      "still_as_fixed": confusion.get("still", Counter())["FIXED"]}}


# ── thresholds ────────────────────────────────────────────────────────────

def _metric(table: dict, path: Optional[str]):
    if not path:
        return None
    node = table
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def check_thresholds(table: dict, thresholds: list[dict]) -> list[dict]:
    """One row per target of this set: ``ok`` True/False, or None (n/a)."""
    out = []
    for t in thresholds:
        if t.get("set") not in (None, table.get("set")):
            continue
        value = _metric(table, t.get("metric"))
        ok = None if value is None else _OPS[t["op"]](value, t["value"])
        out.append({**t, "observed": value, "ok": ok})
    return out


# ── printing ──────────────────────────────────────────────────────────────

def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.1f}%"


def render(table: dict) -> str:
    if "confusion" in table:
        lines = [f"set={table['set']}  fix_right={_pct(table['delta']['fix_right'])}  "
                 f"still_as_fixed={table['delta']['still_as_fixed']}",
                 f"{'expect':8} " + " ".join(f"{c:>7}" for c in CHANGES)]
        for exp, row in table["confusion"].items():
            lines.append(f"{exp:8} " + " ".join(f"{row.get(c, 0):>7}" for c in CHANGES))
        return "\n".join(lines)
    head = (f"set={table['set']} side={table['side']} claims={table['claims']} "
            f"matched={table['matched']} voters={len(table['voters'])}")
    cols = ("claims", "decided", "right", "wrong", "undecided", "coverage", "wrong_rate",
            "raw_decided", "raw_wrong", "raw_coverage", "downgrades")
    lines = [head, f"{'kind':9}" + "".join(f"{c:>13}" for c in cols)]
    for name, row in [*table["kinds"].items(), ("overall", table["overall"])]:
        cells = [_pct(row[c]) if c.endswith(("coverage", "rate")) else str(row[c]) for c in cols]
        lines.append(f"{name:9}" + "".join(f"{c:>13}" for c in cells))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("votes", type=Path, help="claim_vote.py's votes.json, or a CC-7 delta.json")
    ap.add_argument("claims", type=Path, help="claims_fixture.json or claims_real.json")
    ap.add_argument("--side", choices=("base", "head"), default="base")
    ap.add_argument("--thresholds", type=Path)
    ap.add_argument("--json", action="store_true", help="print the table as JSON")
    args = ap.parse_args(argv)
    votes = json.loads(args.votes.read_text(encoding="utf-8"))
    truth = json.loads(args.claims.read_text(encoding="utf-8"))
    is_delta = isinstance(votes, list) or "deltas" in votes
    table = score_deltas(votes, truth) if is_delta else score(votes, truth, args.side)
    print(json.dumps(table, indent=1) if args.json else render(table))
    if not args.thresholds:
        return 0
    checks = check_thresholds(table, json.loads(args.thresholds.read_text(encoding="utf-8"))["targets"])
    for c in checks:
        mark = {True: "ok  ", False: "MISS", None: "n/a "}[c["ok"]]
        print(f"{mark} {c['id']}: {c['metric']} {c['op']} {c['value']} (observed {c['observed']})")
    return 1 if any(c["ok"] is False for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
