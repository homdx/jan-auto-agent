"""tools/collect/model.py — COLLECT-1: data model with provenance isolation.

This module is the antihallucination foundation for `collect` mode (EPIC A).
Nothing here talks to an LLM; it only *shapes* the records that Pass A (AST,
EPIC B), Pass B (LLM summaries, EPIC E) and Pass C (verification, EPIC E) will
produce and exchange, and it enforces one invariant end to end:

    A fact derived from the AST can never be relabeled or overwritten as if
    it came from an LLM.

Why this matters
-----------------
Everything downstream (fail-open registry, contracts, bughunt-suppression in
COLLECT-22) trusts ``provenance == "static"`` fields as ground truth. If an
LLM summarizer could quietly attach ``provenance="llm"`` to, say, a
``GuardedAccess`` record, or silently overwrite a structural field on a
``FunctionRecord``, the whole verification chain (COLLECT-17 / COLLECT-22)
would be trusting fiction instead of facts. So the isolation below is
enforced at the type level, not by convention:

1. **Pure-fact record types** (``ConfigRead``, ``ExceptSite``,
   ``GuardedAccess``) are frozen dataclasses whose ``provenance`` field is
   validated in ``__post_init__`` to equal exactly the value the type
   allows. There is no method on these classes that accepts a different
   provenance — constructing one with ``provenance="llm"`` raises
   ``ProvenanceViolation`` before the object exists.

2. **Derived-fact record types** (``ContractRecord``, ``GateRecord``) may be
   ``static`` (seeded/observed directly) or ``derived`` (computed from other
   static facts), but never ``llm`` — same enforcement mechanism.

3. **Composite records that *do* get LLM prose** (``FunctionRecord``,
   ``ModuleRecord``) keep their structural fields flat, plain, and
   write-once (frozen dataclass — no setters at all). The *only* thing an
   LLM-facing summarizer can attach is an ``LLMSummary`` object, which is a
   completely separate type containing exactly ``purpose`` and ``notes``.
   There is no field on ``LLMSummary`` for anything structural, so there is
   no "setter" for Pass B to reach a static field even by accident — the
   type simply doesn't have one. This is the write-once-from-AST contract:
   the whitelist ``LLM_WRITABLE_FIELDS`` is not just documentation, it is
   the complete set of fields that exist on the one type LLM code is allowed
   to instantiate.

Roundtripping
-------------
``to_dict`` / ``ModuleRecord.from_dict`` give a lossless dataclass <-> JSON
mapping (provenance included) for the manifest/consumer side (EPIC F/G).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, fields
from typing import Any, Dict, Optional, Tuple


# ── Provenance ────────────────────────────────────────────────────────────────


class Provenance:
    """The three provenance tags a field/record can carry.

    static  — produced directly by Pass A's AST walk; ground truth.
    llm     — produced by Pass B's summarizer; prose, not authoritative.
    derived — computed from other static facts (e.g. contracts inferred
              from guarded_accesses + the call graph); not LLM, not raw AST,
              but still trustworthy because it's a pure function of static
              facts.
    """

    STATIC = "static"
    LLM = "llm"
    DERIVED = "derived"

    ALL = frozenset({STATIC, LLM, DERIVED})


class ProvenanceViolation(RuntimeError):
    """Raised when a record is constructed (or would be mutated) with a
    provenance tag its type does not allow.

    This is the enforcement point for the antihallucination guarantee: it
    fires at construction time, so an invalid record can never exist long
    enough to be serialized, queried, or trusted downstream.
    """


# The complete LLM-writable surface for the whole `collect` model. If a
# field name is not in this set, no code path in this package can ever
# attach provenance="llm" to it — see LLMSummary below, which is the only
# type that exposes these fields at all.
LLM_WRITABLE_FIELDS: frozenset = frozenset({"purpose", "notes"})


def llm_writable(field_name: str) -> bool:
    """Whether `field_name` is one an LLM summarizer may ever write."""
    return field_name in LLM_WRITABLE_FIELDS


def _require_provenance(instance: Any, allowed: frozenset) -> None:
    prov = instance.provenance
    if prov not in Provenance.ALL:
        raise ProvenanceViolation(
            f"{type(instance).__name__}: unknown provenance {prov!r}; "
            f"must be one of {sorted(Provenance.ALL)}"
        )
    if prov not in allowed:
        raise ProvenanceViolation(
            f"{type(instance).__name__} may only carry provenance in "
            f"{sorted(allowed)}, got {prov!r}. Structural/derived facts can "
            f"never be authored as provenance='llm'."
        )


# ── Pure AST facts: provenance is always exactly 'static' ─────────────────────
#
# These three record types are the direct output of Pass A (EPIC B). A
# constructor call is the only way they come into existence, and that
# constructor rejects anything but provenance="static". There is no update
# path at all — treat instances as write-once.


@dataclass(frozen=True, kw_only=True)
class ConfigRead:
    """One `config.get*` call site (COLLECT-5)."""

    section: str
    key: str
    fallback: Any = None
    reader_module: str = ""
    has_mode_override: bool = False
    provenance: str = Provenance.STATIC

    def __post_init__(self) -> None:
        _require_provenance(self, frozenset({Provenance.STATIC}))


@dataclass(frozen=True, kw_only=True)
class ExceptSite:
    """One `except` block classification (COLLECT-6)."""

    location: str
    exception_type: str
    body_kind: str  # "pass" | "log" | "re-raise" | "continue" | "return"
    is_fail_open: bool = False
    provenance: str = Provenance.STATIC

    def __post_init__(self) -> None:
        _require_provenance(self, frozenset({Provenance.STATIC}))


@dataclass(frozen=True, kw_only=True)
class GuardedAccess:
    """One indexed-access site + its dataflow guard status (COLLECT-7)."""

    location: str
    access: str  # e.g. "stack[-1]"
    guard: Optional[str] = None  # e.g. "early-return at L71", or None
    status: str = "UNGUARDED"  # "GUARDED" | "UNGUARDED"
    provenance: str = Provenance.STATIC

    def __post_init__(self) -> None:
        _require_provenance(self, frozenset({Provenance.STATIC}))
        if self.status not in ("GUARDED", "UNGUARDED"):
            raise ValueError(f"GuardedAccess.status must be GUARDED/UNGUARDED, got {self.status!r}")
        if self.status == "GUARDED" and not self.guard:
            raise ValueError("GuardedAccess.status=GUARDED requires a `guard` description")


# ── Derived facts: static or derived, never llm ────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class ContractRecord:
    """A cross-module invariant (COLLECT-10): seeded by hand (static) or
    computed from guarded_accesses/graph (derived). Never LLM prose."""

    name: str
    description: str
    kind: str = "seed"  # "seed" | "derived"
    known_edge: Optional[str] = None
    provenance: str = Provenance.STATIC

    def __post_init__(self) -> None:
        _require_provenance(self, frozenset({Provenance.STATIC, Provenance.DERIVED}))


@dataclass(frozen=True, kw_only=True)
class GateRecord:
    """One registered gate (EPIC D). Static/derived only."""

    name: str
    location: str
    kind: str = ""
    provenance: str = Provenance.STATIC

    def __post_init__(self) -> None:
        _require_provenance(self, frozenset({Provenance.STATIC, Provenance.DERIVED}))


# ── The only LLM-writable surface in the whole model ──────────────────────────


@dataclass(frozen=True, kw_only=True)
class LLMSummary:
    """Everything Pass B (the LLM summarizer) is allowed to produce.

    This type has exactly two data fields — `purpose` and `notes` — which is
    what `LLM_WRITABLE_FIELDS` documents. There is no field here for
    anything structural, so there is no way for an LLM-facing code path to
    even *attempt* to set e.g. a module path or a guarded-access status:
    the attribute does not exist on this type.
    """

    purpose: str = ""
    notes: str = ""
    provenance: str = Provenance.LLM

    def __post_init__(self) -> None:
        _require_provenance(self, frozenset({Provenance.LLM}))
        extra = {
            f.name
            for f in fields(self)
            if f.name != "provenance" and not llm_writable(f.name)
        }
        if extra:  # pragma: no cover - guards against future edits, not reachable today
            raise ProvenanceViolation(
                f"LLMSummary must only contain {sorted(LLM_WRITABLE_FIELDS)}; "
                f"found unexpected field(s) {sorted(extra)}"
            )


# ── Composite records (structural facts + optional LLM summary) ───────────────


@dataclass(frozen=True, kw_only=True)
class FunctionRecord:
    """One public symbol (function/class) found by Pass A (COLLECT-4;
    Java support COLLECT-25+ reuses this same type — see `access_modifier`).

    All fields below `summary` are structural/static and write-once: this is
    a frozen dataclass, so there is no setter for them at all, from Pass B
    or anywhere else. The only field an LLM summarizer can populate is
    `summary`, and only by attaching a whole new `LLMSummary` instance via
    `with_llm_summary` — it can never reach `qualname`, `signature`, etc.

    `access_modifier` is `None` for every Python symbol (the language has
    no such keyword; `is_private` alone already captures its one binary
    `_x` convention) and one of `"public"`/`"protected"`/`"package-private"`/
    `"private"` for a Java one (COLLECT-26) — the finer 4-way distinction
    Java actually has, alongside `is_private`, which stays the
    language-neutral "is this part of the public surface" signal both
    extractors populate: for Java, `is_private` is `True` for anything
    that isn't `public` (`protected`/package-private/`private` are all,
    like Python's leading underscore, "not a guaranteed external API"),
    with `access_modifier` preserving which one it actually was.
    """

    qualname: str
    module: str
    lineno: int
    signature: str
    docstring_first_line: str = ""
    is_private: bool = False
    access_modifier: Optional[str] = None
    summary: Optional[LLMSummary] = None

    def with_llm_summary(self, summary: LLMSummary) -> "FunctionRecord":
        """The one sanctioned way Pass B attaches prose to this record.

        Structural fields are copied unchanged (dataclasses.replace touches
        only `summary`); passing anything that isn't an `LLMSummary` is a
        programming error, not a provenance bypass, since `LLMSummary` is
        the only type that can be constructed with provenance='llm' at all.
        """
        if not isinstance(summary, LLMSummary):
            raise TypeError("with_llm_summary() requires an LLMSummary instance")
        return dataclasses.replace(self, summary=summary)

    def field_provenance(self) -> Dict[str, str]:
        """Per-field provenance map, for callers (e.g. the verifier) that
        need to know which parts of this record are authoritative."""
        prov = {f.name: Provenance.STATIC for f in fields(self) if f.name != "summary"}
        if self.summary is not None:
            prov["purpose"] = self.summary.provenance
            prov["notes"] = self.summary.provenance
        return prov

    def to_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FunctionRecord":
        d = dict(d)
        summary_d = d.pop("summary", None)
        summary = LLMSummary(**summary_d) if summary_d else None
        return cls(summary=summary, **d)


@dataclass(frozen=True, kw_only=True)
class ModuleRecord:
    """One source module's full structural record (EPIC B output) plus an
    optional LLM-written summary (EPIC E)."""

    path: str
    public_symbols: Tuple[FunctionRecord, ...] = ()
    imports: Tuple[str, ...] = ()
    config_reads: Tuple[ConfigRead, ...] = ()
    except_sites: Tuple[ExceptSite, ...] = ()
    guarded_accesses: Tuple[GuardedAccess, ...] = ()
    parse_error: Optional[str] = None
    summary: Optional[LLMSummary] = None
    language: str = "python"

    def with_llm_summary(self, summary: LLMSummary) -> "ModuleRecord":
        if not isinstance(summary, LLMSummary):
            raise TypeError("with_llm_summary() requires an LLMSummary instance")
        return dataclasses.replace(self, summary=summary)

    def field_provenance(self) -> Dict[str, str]:
        static_fields = (
            "path",
            "public_symbols",
            "imports",
            "config_reads",
            "except_sites",
            "guarded_accesses",
            "parse_error",
            "language",
        )
        prov = {name: Provenance.STATIC for name in static_fields}
        if self.summary is not None:
            prov["purpose"] = self.summary.provenance
            prov["notes"] = self.summary.provenance
        return prov

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "public_symbols": [s.to_dict() for s in self.public_symbols],
            "imports": list(self.imports),
            "config_reads": [dataclasses.asdict(c) for c in self.config_reads],
            "except_sites": [dataclasses.asdict(e) for e in self.except_sites],
            "guarded_accesses": [dataclasses.asdict(g) for g in self.guarded_accesses],
            "parse_error": self.parse_error,
            "summary": dataclasses.asdict(self.summary) if self.summary else None,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ModuleRecord":
        d = dict(d)
        symbols = tuple(FunctionRecord.from_dict(s) for s in d.get("public_symbols", ()))
        config_reads = tuple(ConfigRead(**c) for c in d.get("config_reads", ()))
        except_sites = tuple(ExceptSite(**e) for e in d.get("except_sites", ()))
        guarded_accesses = tuple(GuardedAccess(**g) for g in d.get("guarded_accesses", ()))
        summary_d = d.get("summary")
        summary = LLMSummary(**summary_d) if summary_d else None
        return cls(
            path=d["path"],
            public_symbols=symbols,
            imports=tuple(d.get("imports", ())),
            config_reads=config_reads,
            except_sites=except_sites,
            guarded_accesses=guarded_accesses,
            parse_error=d.get("parse_error"),
            summary=summary,
            language=d.get("language", "python"),
        )


def to_dict(record: Any) -> Dict[str, Any]:
    """Generic dict conversion for any record type in this module that
    exposes `to_dict`, falling back to `dataclasses.asdict`."""
    if hasattr(record, "to_dict"):
        return record.to_dict()
    return dataclasses.asdict(record)
