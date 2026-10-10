"""CC-7: before and after — the same claims judged at two commits, and what changed.

`scripts/claim_diff.py` judges every claim twice through CC-6 (`claim_vote.prepare_target`,
`ask_v2`, `tally`): at *base* with the pack built at base, at *head* with the pack built at
head plus the `base..head` diff chunks of the anchored paths. This module is the part that
needs no voter: the rule that turns two judgements into a change, the `GONE` test on the
claim's anchors, `delta.json`, the human table and `--check`.

The rule (`classify_delta`), "unanimous TRUE" meaning ``verdict == "TRUE" and unanimous``
(a plurality of three is ``TRUE`` with a dissenter; ``verdict`` alone is not enough):

    n/a      a ``world`` claim at both sides: judged once, it has no before and after
    GONE     the claim's primary anchor (first ``symbol``, else first ``path``) resolved at
             base and does not at head — the name the claim is about is gone; the votes at
             head are not read
    FIXED    unanimous TRUE at base and unanimous FALSE at head
    STILL    unanimous TRUE at both
    NEW      not unanimous TRUE at base (FALSE, split, unsure) and unanimous TRUE at head
    UNCLEAR  anything else: a side that is not unanimous, UNSURE on either, FALSE at both

``FALSE`` at both is ``UNCLEAR`` on purpose: the claim was never true here, and "fixed" would
be a lie. A ``FIXED`` says the code at head no longer shows what the claim says, not that the
behaviour is fixed. The costly error is a ``still`` claim reported ``FIXED``; an ``UNCLEAR``
is an honest answer, so every doubt falls to it.

Evidence ids are read **per side**: a ``src:`` id carries no sha, so ``src:config.py:18-20``
at base and at head are two pieces of evidence that happen to share a name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from tools.claimcheck import anchors as cc_anchors

FIXED, STILL, NEW, GONE, UNCLEAR, NA = "FIXED", "STILL", "NEW", "GONE", "UNCLEAR", "n/a"
CHANGES = (FIXED, STILL, NEW, GONE, UNCLEAR, NA)
#: what a claim's ``expect`` may say, and the change it asks for
EXPECTS = {"fixed": FIXED, "still": STILL, "new": NEW, "gone": GONE}
#: the side of a world claim that is not judged (it is judged once, at head)
NOT_JUDGED = {"verdict": NA, "unanimous": False, "evidence": []}


@dataclass(frozen=True)
class Delta:
    """One claim's change between base and head. *base* / *head* are the side's
    ``{"verdict", "unanimous", "evidence"}``; *evidence_base* / *evidence_head* the chunk ids
    the votes that stood cited on that side (never compared with each other)."""

    claim: str
    base: dict
    head: dict
    change: str
    evidence_base: list = field(default_factory=list)
    evidence_head: list = field(default_factory=list)
    id: str = ""
    kind: str = ""
    expect: str = ""

    def to_json(self) -> dict:
        """The ``delta.json`` row ``contest-bench/cc/score_cc.py`` reads (``id`` / ``claim``, ``change``)."""
        row = {"id": self.id} if self.id else {}
        row.update(claim=self.claim, change=self.change, kind=self.kind,
                   base=dict(self.base), head=dict(self.head))
        if self.expect:
            row["expect"] = self.expect
        return row


# ------------------------------------------------------------------ the rule

def unanimous_true(side: Optional[dict]) -> bool:
    return bool(side) and side.get("verdict") == "TRUE" and side.get("unanimous") is True


def unanimous_false(side: Optional[dict]) -> bool:
    return bool(side) and side.get("verdict") == "FALSE" and side.get("unanimous") is True


def classify_delta(base: Optional[dict], head: Optional[dict], *, gone: bool = False,
                   world: bool = False) -> str:
    """The change of one claim from its two sides (tally rows, or anything with ``verdict``
    and ``unanimous``). *world*: a world claim at both sides; *gone*: its primary anchor
    vanished between them (`primary_vanished`). The order of the tests is the rule's."""
    if world:
        return NA
    if gone:
        return GONE
    if unanimous_true(base):
        if unanimous_false(head):
            return FIXED
        if unanimous_true(head):
            return STILL
        return UNCLEAR
    if unanimous_true(head):
        return NEW
    return UNCLEAR


# ------------------------------------------------------------------ GONE: the anchors of both sides

def primary_anchor(resolved) -> Optional[object]:
    """The claim's first ``symbol`` anchor, else its first ``path`` one (a `ResolvedAnchor`), or None."""
    resolved = list(resolved or ())
    for kind in ("symbol", "path"):
        for r in resolved:
            if r.anchor.kind == kind:
                return r
    return None


def primary_vanished(base_resolved, head_resolved) -> bool:
    """True when the claim's primary anchor is found at base and not at head — whether the
    file is still there (CC-1's dangling: ``path != ""``) or not. The two sides extract the
    same anchors from the same text, so the head's is matched by kind, text and place."""
    first = primary_anchor(base_resolved)
    if first is None or not first.found:
        return False
    key = (first.anchor.kind, first.anchor.text, first.anchor.start)
    for r in head_resolved or ():
        if (r.anchor.kind, r.anchor.text, r.anchor.start) == key:
            return not r.found
    return False


def resolve(claim: str, view) -> list:
    """CC-1's resolved anchors of *claim* in *view* (the definition index is built once per view)."""
    return cc_anchors.resolve_anchors(cc_anchors.extract_anchors(claim), view)


# ------------------------------------------------------------------ compare

def _side(row: Optional[dict]) -> dict:
    if not row:
        return {"verdict": "UNSURE", "unanimous": False, "evidence": []}
    return {"verdict": row.get("verdict", "UNSURE"), "unanimous": row.get("unanimous") is True,
            "evidence": list(row.get("evidence") or ())}


def compare(claims: list, base_rows: list, head_rows: list, *, gone: Optional[list] = None,
            world: Optional[list] = None) -> list:
    """One `Delta` per claim. *base_rows* / *head_rows* are `claim_vote.tally` rows, one per
    claim in the same order (``None`` for a side that was not judged); *gone* and *world* one
    bool per claim (default all False). A claim is a dict (``claim``, ``id``?, ``expect``?) or
    its text."""
    n = len(claims)
    gone = list(gone) if gone is not None else [False] * n
    world = list(world) if world is not None else [False] * n
    if not (len(base_rows) == len(head_rows) == len(gone) == len(world) == n):
        raise ValueError("compare: one base row, head row, gone and world flag per claim")
    out = []
    for c, b, h, g, w in zip(claims, base_rows, head_rows, gone, world):
        c = c if isinstance(c, dict) else {"claim": c}
        base = dict(NOT_JUDGED) if w else _side(b)
        head = _side(h)
        kind = (h or {}).get("kind") or (b or {}).get("kind") or ""
        out.append(Delta(claim=c["claim"], base=base, head=head,
                         change=classify_delta(base, head, gone=g, world=w),
                         evidence_base=list(base["evidence"]), evidence_head=list(head["evidence"]),
                         id=str(c["id"]) if c.get("id") not in (None, "") else "",
                         kind=kind, expect=normalise_expect(c.get("expect")) or ""))
    return out


# ------------------------------------------------------------------ expect, --check, output

def normalise_expect(value) -> Optional[str]:
    """``"Fixed "`` -> ``"fixed"``; None / empty -> None. An unknown word raises ValueError
    (a typo in a claim file would otherwise turn a gate into a claim that never fails)."""
    if value in (None, ""):
        return None
    word = str(value).strip().lower()
    if word not in EXPECTS:
        raise ValueError(f"expect {value!r}: not one of {', '.join(EXPECTS)}")
    return word


def mismatches(deltas: list) -> list:
    """The deltas whose claim carries an ``expect`` and whose change is another one."""
    return [d for d in deltas if d.expect and EXPECTS[d.expect] != d.change]


def mismatch_line(d: Delta) -> str:
    who = d.id or d.claim[:60]
    return (f"MISMATCH {who}: expect {d.expect}, got {d.change} "
            f"(base {d.base['verdict']}{'*' if d.base['unanimous'] else ''} "
            f"[{', '.join(d.evidence_base) or '-'}]; "
            f"head {d.head['verdict']}{'*' if d.head['unanimous'] else ''} "
            f"[{', '.join(d.evidence_head) or '-'}])")


def delta_json(base_sha: str, head_sha: str, deltas: list) -> dict:
    """``{"base": sha, "head": sha, "deltas": [...]}``, as `score_cc.score_deltas` reads it."""
    return {"base": base_sha, "head": head_sha, "deltas": [d.to_json() for d in deltas]}


def render(base_sha: str, head_sha: str, deltas: list) -> str:
    """The human table: a count per change, then one line a claim (``*`` = unanimous)."""
    counts = {c: sum(d.change == c for d in deltas) for c in CHANGES}
    lines = [f"before/after {base_sha[:7]}..{head_sha[:7]}: "
             + "  ".join(f"{c}={n}" for c, n in counts.items()),
             f"{'change':8} {'base':9} {'head':9} {'expect':7} claim"]
    for d in deltas:
        b = d.base["verdict"] + ("*" if d.base["unanimous"] else "")
        h = d.head["verdict"] + ("*" if d.head["unanimous"] else "")
        who = f"{d.id} " if d.id else ""
        lines.append(f"{d.change:8} {b:9} {h:9} {d.expect or '-':7} {who}{d.claim[:80]}")
    return "\n".join(lines)
