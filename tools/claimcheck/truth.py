"""CC-8: into the pipeline — a `votes.json` becomes `truth.csv` rows and a stage-3 pending list.

The competition runbook already has a file for settled answers (`validate<N>/truth.csv`, read
by `harvest_report.load_truth` and `truth_consensus.load`) and a queue for unsettled ones
(stage 3). This module writes the claim check's result into those shapes, nothing new:

    settled  (``unanimous`` is true, ``verdict`` TRUE or FALSE)  ->  a ``truth.csv`` row
    anything else (SPLIT, UNSURE, CODE-CHECK, a plurality with a dissenter)  ->  pending.md

**`verdict` alone is never a settled answer.** A real row of round 280 says ``verdict: TRUE``
with ``by_model`` TRUE, TRUE, FALSE and ``unanimous: false``: that is a doubt, and every doubt
goes to the pending list, never to a row (`settled`). The same rule as CC-7's lesson: a
positive label needs positive evidence, a plurality is not that.

`votes.json` holds no pack and no resolved anchors (only the chunk ids accepted votes cited),
so `scripts/claimcheck_truth.py` rebuilds both at the recorded sha (`rebuild`): `Target.open`
on the exact sha, CC-6's `prepare_target` for the packs, CC-1's `resolve_anchors` for the keys.
No model, no network. Without a target (a run without ``--target``, or a target that cannot be
opened) the key falls back to the path inside the first cited chunk id, else the claim's hash.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

TRUTH_HEADER = ("finding", "truth", "checked_by", "duplicate_of", "how")
#: a settled verdict and the ``truth`` it writes (`harvest_report.TRUTH_VALUES` holds both)
TRUTH_OF = {"TRUE": "REAL", "FALSE": "FALSE"}
HOW_QUOTE = 160          # characters of the first accepted quote in ``how``
PREVIEW_LINES = 3        # first lines of each chunk in pending.md
PREVIEW_CHARS = 160      # characters of one preview line
#: the pending list's groups, in order; a row that is none of them is a plurality with a dissent
PENDING_ORDER = ("SPLIT", "DISSENT", "CODE-CHECK", "UNSURE")
PENDING_TITLE = {"SPLIT": "SPLIT — the committed voters tie",
                 "DISSENT": "TRUE / FALSE with a dissenter — a plurality, not a settled answer",
                 "CODE-CHECK": "CODE-CHECK — about the code, judged without it (no evidence found)",
                 "UNSURE": "UNSURE — fewer than the quorum committed"}


# ------------------------------------------------------------------ settled or not

def settled(row: dict) -> Optional[str]:
    """``REAL`` / ``FALSE`` for a settled row, else None. Settled is ``unanimous is True`` and a
    verdict of TRUE or FALSE — nothing else (not ``verdict`` alone, not ``unanimous`` with any
    other verdict)."""
    if row.get("unanimous") is not True:
        return None
    return TRUTH_OF.get(row.get("verdict"))


# ------------------------------------------------------------------ the voters: families

def family_of(ref: str, model: Optional[str] = None) -> str:
    """The voter's family: the model part after the last ``/``, ``:free`` (any ``:tag``)
    removed, cut at the first ``-``, lower case (``kenary/agnes-3-0-flash:free`` -> ``agnes``).
    *model*: the ``model`` an ini profile names, for a ref that is a section name."""
    name = (model or ref).rsplit("/", 1)[-1].split(":", 1)[0]
    return name.split("-", 1)[0].lower() or name.lower()


def live_voters(votes: dict) -> list:
    """The refs that cast at least one vote somewhere in the run, sorted; a voter with no vote
    at all (dead, out of its free plan) is not a voter (CC-6's `tally` rule)."""
    return sorted({r["model"] for r in votes.get("results") or () if r.get("votes")})


def dead_voters(votes: dict) -> list:
    live = set(live_voters(votes))
    return sorted({r["model"] for r in votes.get("results") or ()} - live)


def checked_by(votes: dict, *, models: Optional[dict] = None, sha: Optional[str] = None,
               base_sha: Optional[str] = None) -> str:
    """``claimcheck:<families, sorted, +>@<sha7>``, or ``@<base7>..<head7>`` for a before/after
    run. *sha* defaults to ``votes["target"]["sha"]``; with no sha at all there is no ``@``.
    *models* maps a profile name to the model it names (`family_of`)."""
    models = models or {}
    fams = sorted({family_of(ref, models.get(ref)) for ref in live_voters(votes)})
    sha = sha or (votes.get("target") or {}).get("sha") or ""
    text = "claimcheck:" + ("+".join(fams) or "none")
    if sha and base_sha:
        return f"{text}@{base_sha[:7]}..{sha[:7]}"
    return f"{text}@{sha[:7]}" if sha else text


# ------------------------------------------------------------------ the key: finding

def path_of_chunk_id(cid: str) -> str:
    """The repository path inside a chunk id, or ``""``: ``src:<p>:a-b``, ``git:<sha7>:<p>:a-b``,
    ``diff:<b7>..<h7>:<p>:a-b``, ``collect:<kind>:<p>``, ``gitlog:<p>``, ``log:<p>``. A commit
    header, a diff's ``stat``, a note or a ticket id names no file of the claim."""
    cid = (cid or "").strip()
    if cid.startswith("[[") and cid.endswith("]]"):
        cid = cid[2:-2]
    head, _, rest = cid.partition(":")
    if not rest:
        return ""
    if head in ("git", "diff", "collect"):
        _, _, rest = rest.partition(":")       # the sha, the range or the collect kind
    elif head not in ("src", "gitlog", "log"):
        return ""
    if head in ("src", "git", "diff"):
        rest = re.sub(r":\d+-\d+$", "", rest)
        if rest in ("header", "stat"):
            return ""
    return rest


def claim_hash(claim: str) -> str:
    """``claim:<sha1[:8]>`` of the claim's normalised text (case and runs of whitespace aside)."""
    norm = " ".join((claim or "").split()).lower()
    return "claim:" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


def finding_key(claim: str, resolved=None, evidence=()) -> str:
    """The grouping key `harvest_report.py` and `merge_validations.py` use: ``<path>::<qualname>``
    of the claim's first found symbol anchor, else ``<path>`` of its first found path anchor,
    else the path inside the first cited chunk id (*evidence*), else `claim_hash`."""
    resolved = list(resolved or ())
    for r in resolved:
        if r.anchor.kind == "symbol" and r.found and r.path and r.qualname:
            return f"{r.path}::{r.qualname}"
    for r in resolved:
        if r.anchor.kind == "path" and r.found and r.path:
            return r.path
    for cid in evidence or ():
        path = path_of_chunk_id(cid)
        if path:
            return path
    return claim_hash(claim)


# ------------------------------------------------------------------ the row: how

def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace(";", "\\;")


def how_of(row: dict) -> str:
    """One line: the accepted chunk ids, then the first accepted quote (whitespace and newlines
    collapsed, cut at `HOW_QUOTE`, ``\\``, ``"`` and ``;`` backslash-escaped). A world claim with
    no evidence says ``world fact``."""
    ids = [str(c) for c in row.get("evidence") or ()]
    quotes = [q for q in row.get("quotes") or () if q and str(q).strip()]
    if not ids and not quotes:
        return "world fact" if row.get("kind", "world") == "world" else "no evidence cited"
    parts = [" ".join(ids)] if ids else []
    if quotes:
        quote = " ".join(str(quotes[0]).split())
        if len(quote) > HOW_QUOTE:
            quote = quote[:HOW_QUOTE - 1] + "…"
        parts.append(f'"{_escape(quote)}"')
    return "; ".join(parts)


# ------------------------------------------------------------------ to_truth_rows

def to_truth_rows(votes: dict, *, checked_by: str, resolved: Optional[list] = None) -> list:
    """One `TRUTH_HEADER` dict per settled claim of *votes* (CC-6's ``votes.json``), in claim
    order. *resolved*: one list of resolved anchors per claim (`rebuild`), else the key comes
    from the cited chunk ids. Two claims with one ``finding`` and one ``truth``: the second's
    ``duplicate_of`` is that finding."""
    rows, seen = [], set()
    claims = votes.get("claims") or []
    for i, c in enumerate(claims):
        truth = settled(c)
        if truth is None:
            continue
        res = resolved[i] if resolved is not None and i < len(resolved) else None
        finding = finding_key(c.get("claim", ""), res, c.get("evidence") or ())
        dup = finding if (finding, truth) in seen else ""
        seen.add((finding, truth))
        rows.append({"finding": finding, "truth": truth, "checked_by": checked_by,
                     "duplicate_of": dup, "how": how_of(c)})
    return rows


def pending_rows(votes: dict) -> list:
    """(index, claim row) of every claim that is not settled, in claim order."""
    return [(i, c) for i, c in enumerate(votes.get("claims") or []) if settled(c) is None]


def disagreements(rows: list) -> list:
    """The findings that got both ``REAL`` and ``FALSE`` from two claims of one run."""
    by: dict = {}
    for r in rows:
        by.setdefault(r["finding"], set()).add(r["truth"])
    return sorted(f for f, ts in by.items() if len(ts) > 1)


# ------------------------------------------------------------------ pending.md

def _group(c: dict) -> str:
    v = c.get("verdict")
    if v in ("SPLIT", "CODE-CHECK", "UNSURE"):
        return v
    return "DISSENT" if v in ("TRUE", "FALSE") else "UNSURE"


def _preview(text: str) -> list:
    lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()][:PREVIEW_LINES]
    return [ln if len(ln) <= PREVIEW_CHARS else ln[:PREVIEW_CHARS - 1] + "…" for ln in lines]


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def pending_markdown(votes: dict, packs: Optional[list] = None, *,
                     models: Optional[dict] = None) -> str:
    """The stage-3 queue: a header (sha, voters, dead voters), then every unsettled claim once,
    grouped `PENDING_ORDER`, each with its kind, a verdict table by model, the rejected-quote
    count, the claim and the pack's chunk ids with their first lines. *packs*: one `Pack` (or
    None) per claim, rebuilt at the recorded sha; without it the cited chunk ids are listed."""
    target = votes.get("target") or {}
    sha = target.get("sha") or ""
    live, dead = live_voters(votes), dead_voters(votes)
    models = models or {}
    pend = pending_rows(votes)
    total = len(votes.get("claims") or [])
    lines = ["# Claim check — pending (stage 3)", "",
             f"sha: {sha or '(no --target: the voters did not see the code)'}"
             + (f" (base {target['base']})" if target.get("base") else ""),
             "voters: " + (", ".join(f"{v} ({family_of(v, models.get(v))})" for v in live) or "none"),
             "dead: " + (", ".join(dead) or "none"),
             f"settled: {total - len(pend)} of {total}; pending: {len(pend)}", ""]
    fams = {family_of(v, models.get(v)) for v in live}
    if len(live) > len(fams):
        lines += [f"Note: {len(live)} voters of {len(fams)} families — unanimity across one family "
                  "counts less than it says.", ""]
    for group in PENDING_ORDER:
        members = [(i, c) for i, c in pend if _group(c) == group]
        if not members:
            continue
        lines += [f"## {PENDING_TITLE[group]} ({len(members)})", ""]
        for i, c in members:
            who = f"{c['id']}, " if c.get("id") not in (None, "") else ""
            rejected = c.get("rejected") or {}
            lines += [f"### claim {i + 1} ({who}{c.get('kind', '?')}, verdict {c.get('verdict')})", ""]
            lines += ["> " + ln if ln else ">" for ln in str(c.get("claim", "")).splitlines() or [""]]
            lines += ["", "| voter | verdict |", "|---|---|"]
            by_model = c.get("by_model") or {}
            for v in live:
                lines.append(f"| {_cell(v)} | {by_model.get(v, '(no vote)')} |")
            lines += ["", f"rejected quotes: {sum(rejected.values()) if isinstance(rejected, dict) else 0}"
                      + (f" ({', '.join(f'{k} {n}' for k, n in sorted(rejected.items()))})"
                         if isinstance(rejected, dict) and rejected else ""), ""]
            pack = packs[i] if packs is not None and i < len(packs) else None
            if pack is not None and pack.chunks:
                lines.append("evidence (the pack at the recorded sha):")
                lines.append("")
                for ch in pack.chunks:
                    lines.append(f"- `{ch.id}`")
                    lines += ["      " + ln for ln in _preview(ch.text)]
                lines.append("")
            elif packs is not None and c.get("kind") != "world":
                lines += ["evidence: the pack at the recorded sha is empty — nothing found to read", ""]
            elif c.get("evidence"):
                lines += ["evidence cited: " + ", ".join(f"`{e}`" for e in c["evidence"]), ""]
            elif c.get("kind") == "world":
                lines += ["evidence: a world fact — no pack", ""]
            else:
                lines += ["evidence: not rebuilt (no target to open)", ""]
    if not pend:
        lines += ["Nothing pending: every claim is settled.", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ writing truth.csv

def read_truth(path: Path) -> list:
    """The rows of an existing truth CSV (any columns), or [] for a missing file."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def merge_rows(existing: list, rows: list) -> tuple:
    """(rows to add, conflicts) for ``--append``: a row whose ``finding`` is already in the
    file with the same ``truth`` is skipped; one whose ``finding`` is there with only another
    ``truth`` is a conflict (``(finding, old, new)``) and is not added."""
    have = {((r.get("finding") or "").strip(), (r.get("truth") or "").strip().upper()) for r in existing}
    known = {f for f, _ in have}
    add, conflicts = [], []
    for r in rows:
        # Normalize the incoming row the same way as ``existing`` above, so a padded
        # ``finding`` or a lowercase ``truth`` is not seen as new (duplicate) or conflicting.
        finding = (r["finding"] or "").strip()
        truth = (r["truth"] or "").strip().upper()
        key = (finding, truth)
        if key in have:
            continue
        if finding in known:
            old = sorted(t for f, t in have if f == finding)
            conflicts.append((finding, "/".join(old), truth))
            continue
        add.append({**r, "finding": finding, "truth": truth})
    return add, conflicts


def _csv_text(header, rows: list) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(header), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in header})
    return buf.getvalue()


def atomic_write(path: Path, text: str) -> None:
    """*text* into *path* through a temporary file in the same directory and a rename: a failure
    on the way leaves the old file as it was, and no temporary file behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_truth(path: Path, rows: list, *, append: bool = False) -> tuple:
    """Write *rows* to the truth CSV at *path*; (rows written, conflicts). Without *append* the
    file is replaced by `TRUTH_HEADER` and *rows*. With it the existing rows stay (their own
    columns kept, a missing one of ours added empty) and only `merge_rows`'s additions follow;
    an existing file is not touched when there is nothing to add, a missing one is created with
    the header (the operator then finds the file the run names, empty, not a missing path)."""
    path = Path(path)
    if not append:
        atomic_write(path, _csv_text(TRUTH_HEADER, rows))
        return rows, []
    existing = read_truth(path)
    add, conflicts = merge_rows(existing, rows)
    if not add and path.exists():
        return [], conflicts
    header = list(TRUTH_HEADER)
    if existing:
        with path.open(newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        header += [k for k in TRUTH_HEADER if k not in header]
    atomic_write(path, _csv_text(header, existing + add))
    return add, conflicts


# ------------------------------------------------------------------ rebuild at the recorded sha

def rebuild(votes: dict, repo, *, scratch, parser=None) -> tuple:
    """(resolved, packs, base_sha) for every claim of *votes*, rebuilt offline at the recorded
    ``target.sha`` of *repo*: ``resolve_anchors`` for the keys, CC-6's ``prepare_target`` for
    the packs (with the recorded ``base``). Raises `TargetError` when the target cannot be
    opened. No model, no network (the target is never fetched)."""
    import sys
    from tools.claimcheck import anchors as cc_anchors
    from tools.claimcheck.pack import PackBudget
    from tools.claimcheck.target import Target, TargetError

    scripts = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import claim_vote

    target_info = votes.get("target") or {}
    sha = target_info.get("sha")
    if not sha:
        raise TargetError("votes.json has no target.sha: nothing to rebuild at")
    claims = [{"claim": c.get("claim", "")} for c in votes.get("claims") or []]
    with Target.open(str(repo), sha, scratch=Path(scratch)) as target:
        base_sha = None
        base = target_info.get("base")
        if base:
            try:
                base_sha = target.view().git("rev-parse", "--verify", f"{base}^{{commit}}").strip() or None
            except Exception:   # noqa: BLE001 — a base that does not resolve leaves the packs without it
                base_sha = None
        items, _meta = claim_vote.prepare_target(claims, target, PackBudget.from_config(parser),
                                                 base_sha)
        view = target.view()
        resolved = [cc_anchors.resolve_anchors(cc_anchors.extract_anchors(c["claim"]), view)
                    for c in claims]
    return resolved, [pack for _text, pack in items], base_sha
