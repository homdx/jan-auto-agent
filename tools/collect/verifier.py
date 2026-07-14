"""tools/collect/verifier.py — COLLECT-17: Verification gate (Pass C).

⭐ This module is the core of the antihallucination guarantee. Pass B
(``summarizer.py``) is allowed to write prose — that's the whole point of
having an LLM in the loop — but nothing it writes reaches the artifact
without first passing two independent checks against Pass A's static
facts:

1. **Citation-check** — a claim that names a symbol must name one that
   actually exists in Pass A's symbol index; a claim that cites a
   ``path:line`` location must cite a line that is actually within that
   file. A claim that fails either check never happened, as far as the
   artifact is concerned — it's dropped and logged as ``dropped:no-citation``.

2. **Contradiction-suppression** — a claim that says something will
   *crash*/is *unguarded* is checked against COLLECT-7's
   ``guarded_accesses``: if Pass A already proved that exact access is
   guarded, the claim is dropped as ``dropped:contradicts-guard`` — no
   matter how confident the prose sounds. Symmetrically, a claim that
   something is a *silent* bug is checked against COLLECT-9's
   ``FAIL_OPEN_REGISTRY``: a location Pass A already knows is a documented
   fail-open site is not a fresh discovery, so the claim is dropped as
   ``dropped:contradicts-fail-open``.

3. **Provenance-stamp** — nothing that survives migrates off
   ``provenance="llm"``. ``Claim`` can only ever be constructed with
   ``provenance="llm"`` (COLLECT-1's enforcement mechanism, reused here),
   and the summary rebuilt from surviving claims is a plain ``LLMSummary``
   — never a ``ContractRecord``/``GateRecord``/other static-or-derived
   type. Pass B prose staying "llm" forever, even after surviving
   verification, is what lets COLLECT-22's bughunt-suppression later trust
   *only* ``static`` facts and never auto-suppress on unverified prose.

Every drop is recorded, not just silently discarded — ``verify_repo``
returns a JSON-shaped report (the ``verification_report.json`` artifact)
so a human can see exactly what Pass B claimed and why it didn't survive.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from tools.collect.model import LLMSummary, ModuleRecord, Provenance, ProvenanceViolation
from tools.collect.registries import build_fail_open_registry, fail_open_locations

# ── reason codes ────────────────────────────────────────────────────────────────

REASON_NO_CITATION = "dropped:no-citation"
REASON_CONTRADICTS_GUARD = "dropped:contradicts-guard"
REASON_CONTRADICTS_FAIL_OPEN = "dropped:contradicts-fail-open"


# ── Claim: the one type Pass C ever hands prose into ───────────────────────────


@dataclass(frozen=True, kw_only=True)
class Claim:
    """One atomic assertion pulled out of Pass B prose, about to be checked
    against Pass A's static facts.

    Always ``provenance="llm"`` — enforced the same way COLLECT-1 enforces
    it on ``LLMSummary``: the constructor rejects anything else before the
    object exists, so there is no path (bug or otherwise) by which a claim
    could be mistaken for a static fact downstream.
    """

    text: str
    module: str
    kind: str = "generic"  # "access_crash" | "silent_except" | "generic"
    symbol: Optional[str] = None  # e.g. "pkg/foo.py:some_function"
    location: Optional[str] = None  # e.g. "pkg/foo.py:79"
    access: Optional[str] = None  # e.g. "stack[-1]", for kind="access_crash"
    provenance: str = Provenance.LLM

    def __post_init__(self) -> None:
        if self.provenance != Provenance.LLM:
            raise ProvenanceViolation(
                f"Claim may only carry provenance={Provenance.LLM!r}, "
                f"got {self.provenance!r}. Pass C claims are never static."
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "module": self.module,
            "kind": self.kind,
            "symbol": self.symbol,
            "location": self.location,
            "access": self.access,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class DroppedClaim:
    """One row of the verification report: a claim that did not survive,
    and exactly why."""

    claim: Claim
    reason: str  # one of the REASON_* constants
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module": self.claim.module,
            "claim": self.claim.text,
            "kind": self.claim.kind,
            "reason": self.reason,
            "detail": self.detail,
        }


# ── claim extraction from Pass B prose ─────────────────────────────────────────

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_LOCATION_RE = re.compile(r"([\w./\\-]+\.py):(\d+)")
_ACCESS_RE = re.compile(r"([A-Za-z_][\w.]*\[[^\]]+\])")
_CRASH_WORDS_RE = re.compile(
    r"\b(crash(?:es|ed|ing)?|will fail|throws?|raises?\s+\w*Error|unguarded)\b",
    re.IGNORECASE,
)
_SILENT_WORDS_RE = re.compile(r"\b(silent(?:ly)?|swallow(?:s|ed|ing)?)\b", re.IGNORECASE)


def extract_claims(
    text: str,
    module_path: str,
    known_symbols: FrozenSet[str] = frozenset(),
) -> List[Claim]:
    """Split `text` (Pass B's raw ``purpose``/``notes`` prose) into
    sentence-level `Claim`s, tagging each with whatever a claim needs to be
    checked against Pass A: a cited location, a cited access expression, a
    cited known symbol, and a `kind` guessed from crash/silent language.

    This is intentionally a light heuristic, not an NLP claim-extractor:
    Pass C's job is to keep the model honest about the facts it *does*
    cite, not to guess what it meant when it cited nothing at all — a
    sentence with no location/symbol/access naturally ends up `kind=
    "generic"` and, having nothing checkable in it, always survives.
    """
    if not text or not text.strip():
        return []

    claims: List[Claim] = []
    for raw_sentence in _SENTENCE_SPLIT_RE.split(text.strip()):
        sentence = raw_sentence.strip()
        if not sentence:
            continue

        loc_match = _LOCATION_RE.search(sentence)
        location = f"{loc_match.group(1)}:{loc_match.group(2)}" if loc_match else None

        access_match = _ACCESS_RE.search(sentence)
        access = access_match.group(1) if access_match else None

        symbol = None
        for sym in known_symbols:
            short = sym.split(":")[-1]
            if short and re.search(rf"\b{re.escape(short)}\b", sentence):
                symbol = sym
                break

        if access and _CRASH_WORDS_RE.search(sentence):
            kind = "access_crash"
        elif _SILENT_WORDS_RE.search(sentence):
            kind = "silent_except"
        else:
            kind = "generic"

        claims.append(
            Claim(
                text=sentence,
                module=module_path,
                kind=kind,
                symbol=symbol,
                location=location,
                access=access,
            )
        )
    return claims


# ── check 1: citation ───────────────────────────────────────────────────────────


def citation_check(
    claim: Claim,
    known_symbols: FrozenSet[str],
    line_counts: Dict[str, int],
) -> Optional[str]:
    """Return `None` if `claim` cites something real, else a detail string
    explaining what didn't resolve.

    * A cited `symbol` must be in `known_symbols` (Pass A's own index —
      COLLECT-4's `public_symbols`, nothing derived or LLM-sourced).
    * A cited `location` (`path:line`) must name a scanned module and a
      line number within that file's actual line count.
    A claim citing neither is not a citation-check failure — it simply has
    nothing to check (handled by the caller: only claims with a citation
    at all go through this gate meaningfully; see `verify_claims`).
    """
    if claim.symbol is not None and claim.symbol not in known_symbols:
        return f"cited symbol {claim.symbol!r} not found in Pass A index"

    if claim.location is not None:
        path, _, line_str = claim.location.rpartition(":")
        if not line_str.isdigit():
            return f"malformed location {claim.location!r}"
        line = int(line_str)
        max_line = line_counts.get(path)
        if max_line is None:
            return f"cited module {path!r} not found in Pass A index"
        if line < 1 or line > max_line:
            return f"cited line {line} out of range for {path!r} (1..{max_line})"

    return None


# ── check 2: contradiction ──────────────────────────────────────────────────────


def contradiction_check(
    claim: Claim,
    module: ModuleRecord,
    fail_open_locs: FrozenSet[str],
) -> Optional[Tuple[str, str]]:
    """Return `None` if `claim` doesn't contradict a static fact, else
    `(reason, detail)`.

    * `kind="access_crash"` claims are checked against `module`'s own
      `guarded_accesses`: an exact match on the cited `access` expression
      that Pass A recorded as `GUARDED` contradicts the claim outright —
      this is the `stack[-1]` false-positive killer COLLECT-7 built the
      data for.
    * `kind="silent_except"` claims are checked against `fail_open_locs`
      (COLLECT-9's registry): a cited location that's already a documented
      fail-open site is not a new discovery, so the claim is suppressed
      the same way.
    """
    if claim.kind == "access_crash" and claim.access:
        for ga in module.guarded_accesses:
            if ga.access == claim.access and ga.status == "GUARDED":
                guard_desc = ga.guard or "guarded"
                return (
                    REASON_CONTRADICTS_GUARD,
                    f"{claim.access} at {ga.location} is GUARDED ({guard_desc})",
                )

    if claim.kind == "silent_except" and claim.location:
        if claim.location in fail_open_locs:
            return (
                REASON_CONTRADICTS_FAIL_OPEN,
                f"{claim.location} is already a documented fail-open site",
            )

    return None


# ── per-module / per-repo orchestration ─────────────────────────────────────────


def verify_claims(
    claims: Iterable[Claim],
    *,
    module: ModuleRecord,
    known_symbols: FrozenSet[str],
    line_counts: Dict[str, int],
    fail_open_locs: FrozenSet[str],
) -> Tuple[List[Claim], List[DroppedClaim]]:
    """Run every check in order on each of `claims`; return `(kept, dropped)`.

    Citation runs before contradiction: a claim citing a location/symbol
    that doesn't even exist is a fabrication regardless of what it says
    about it, so there's no reason to also ask whether its (nonexistent)
    citation contradicts anything.
    """
    kept: List[Claim] = []
    dropped: List[DroppedClaim] = []
    for claim in claims:
        no_citation = citation_check(claim, known_symbols, line_counts)
        if no_citation is not None:
            dropped.append(DroppedClaim(claim=claim, reason=REASON_NO_CITATION, detail=no_citation))
            continue

        contradiction = contradiction_check(claim, module, fail_open_locs)
        if contradiction is not None:
            reason, detail = contradiction
            dropped.append(DroppedClaim(claim=claim, reason=reason, detail=detail))
            continue

        kept.append(claim)
    return kept, dropped


def _verify_text(
    text: str,
    *,
    module: ModuleRecord,
    known_symbols: FrozenSet[str],
    line_counts: Dict[str, int],
    fail_open_locs: FrozenSet[str],
) -> Tuple[str, List[DroppedClaim]]:
    """Extract + verify claims from one prose field (`purpose` or `notes`),
    return the reconstructed (filtered) text and whatever got dropped."""
    claims = extract_claims(text, module.path, known_symbols)
    kept, dropped = verify_claims(
        claims,
        module=module,
        known_symbols=known_symbols,
        line_counts=line_counts,
        fail_open_locs=fail_open_locs,
    )
    filtered_text = " ".join(c.text for c in kept)
    return filtered_text, dropped


def verify_module(
    module: ModuleRecord,
    *,
    known_symbols: FrozenSet[str],
    line_counts: Dict[str, int],
    fail_open_locs: FrozenSet[str],
) -> Tuple[ModuleRecord, List[DroppedClaim]]:
    """Run Pass C on one already-summarized module.

    A module with no summary (Pass B skipped, or `--no-llm`) passes through
    unchanged — there is no prose to verify. Otherwise `purpose` and
    `notes` are each independently filtered down to their surviving claims
    and reattached as a fresh `LLMSummary` (COLLECT-1's whitelist — still,
    and always, `provenance="llm"`).
    """
    if module.summary is None:
        return module, []

    verified_purpose, dropped_purpose = _verify_text(
        module.summary.purpose,
        module=module, known_symbols=known_symbols,
        line_counts=line_counts, fail_open_locs=fail_open_locs,
    )
    verified_notes, dropped_notes = _verify_text(
        module.summary.notes,
        module=module, known_symbols=known_symbols,
        line_counts=line_counts, fail_open_locs=fail_open_locs,
    )

    verified_summary = LLMSummary(purpose=verified_purpose, notes=verified_notes)
    return module.with_llm_summary(verified_summary), dropped_purpose + dropped_notes


def verify_repo(
    modules: Iterable[ModuleRecord],
    sources: Dict[str, str],
    *,
    root: Optional[Path] = None,
) -> Tuple[List[ModuleRecord], Dict[str, Any]]:
    """Run Pass C over every summarized module in `modules`.

    `sources` maps module path -> its source text, used only to compute
    each file's line count for the citation-check's range test (the same
    text Pass A/B already had; nothing new is read here beyond that).

    Returns `(verified_modules, report)`, where `report` is the
    JSON-serializable ``verification_report.json`` payload: every dropped
    claim plus a kept/dropped count, for transparency (COLLECT-17's AC:
    "все выбросы залогированы").
    """
    modules = list(modules)
    known_symbols = frozenset(sym.qualname for m in modules for sym in m.public_symbols)
    line_counts = {m.path: sources.get(m.path, "").count("\n") + 1 for m in modules}

    fail_open_registry = build_fail_open_registry(modules, root=root)
    fail_open_locs = fail_open_locations(fail_open_registry)

    verified_modules: List[ModuleRecord] = []
    all_dropped: List[DroppedClaim] = []
    kept_count = 0

    for m in modules:
        verified, dropped = verify_module(
            m, known_symbols=known_symbols, line_counts=line_counts, fail_open_locs=fail_open_locs,
        )
        verified_modules.append(verified)
        all_dropped.extend(dropped)
        if verified.summary is not None:
            kept_count += len(
                extract_claims(verified.summary.purpose, m.path, known_symbols)
            ) + len(
                extract_claims(verified.summary.notes, m.path, known_symbols)
            )

    report = build_verification_report(all_dropped, kept_count=kept_count)
    return verified_modules, report


# ── report ──────────────────────────────────────────────────────────────────────


def build_verification_report(dropped: Iterable[DroppedClaim], *, kept_count: int = 0) -> Dict[str, Any]:
    """The `verification_report.json` payload: every dropped claim (with
    its reason), plus counts. Sorted by `(module, claim text)` for
    determinism (COLLECT-3) — two runs over an unchanged tree produce a
    byte-identical report.
    """
    rows = sorted(
        (d.to_dict() for d in dropped),
        key=lambda d: (d["module"], d["claim"], d["reason"]),
    )
    return {
        "dropped": rows,
        "dropped_count": len(rows),
        "kept_count": kept_count,
    }


def write_verification_report(report: Dict[str, Any], path: Path) -> None:
    """Write `report` as canonical (sorted-key, indented) JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
