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

import ast
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from tools.collect.ast_facts import extract_all_defined_names
from tools.collect.graph import Graph, import_edges as _import_edges
from tools.collect.model import LLMSummary, ModuleRecord, Provenance, ProvenanceViolation
from tools.collect.registries import build_fail_open_registry, fail_open_locations
from tools.collect.test_paths import is_test_path

# ── reason codes ────────────────────────────────────────────────────────────────

REASON_NO_CITATION = "dropped:no-citation"
REASON_CONTRADICTS_GUARD = "dropped:contradicts-guard"
REASON_CONTRADICTS_FAIL_OPEN = "dropped:contradicts-fail-open"
REASON_SIBLING_CITATION_FAILED = "dropped:sibling-citation-failed"
# COLLECT-17 follow-up (self-play round 2): the mirror image of
# REASON_CONTRADICTS_GUARD. A claim that an access is *safe* ("is fully
# guarded", "cannot raise") is checked against a real UNGUARDED status the
# same way a claim that an access *crashes* is checked against GUARDED.
REASON_CONTRADICTS_CRASH = "dropped:contradicts-crash-risk"


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
    kind: str = "generic"  # "access_crash" | "access_safe" | "silent_except" | "generic"
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
# A `path.py:identifier` citation — the same "file:thing" shape as
# `_LOCATION_RE`, but naming a symbol instead of a line number (identifier
# starts with a letter/underscore, so it can never collide with the
# all-digit line-number form above). This is what lets `extract_claims`
# recognize an *invented* symbol name as an attempted citation at all — see
# the `known_names` guard where this is used below, which is what keeps
# this pattern from also flagging a *real* name Pass A's narrower
# `public_symbols` index just doesn't happen to track (a module-level
# constant, a class method).
_SYMBOL_CITATION_RE = re.compile(r"([\w./\\-]+\.py):([A-Za-z_]\w*)\b")
# Matches only the *start* of a candidate access citation — `name[` — so we
# can then hand-walk the brackets with `_iter_access_citations` below
# rather than trying to express "balanced, possibly-nested brackets" as a
# single regex. (BUGFIX, see `_iter_access_citations`'s docstring for why
# this replaced a plain single-pattern `_ACCESS_RE`.)
_ACCESS_START_RE = re.compile(r"[A-Za-z_][\w.]*(?=\[)")
# Base names that are typing/builtin generic containers, not variables —
# BUGFIX (found via self-play): a sentence like "parse_config returns
# dict[str, int] and raises ValueError on bad input" was previously read
# by `_ACCESS_RE` as citing an access `dict[str, int]`; combined with
# "raises...Error" tripping `_CRASH_WORDS_RE`, that turned an ordinary,
# entirely correct type-annotation sentence into an `access_crash` claim
# whose cited "access" can never appear in `known_accesses` (COLLECT-7's
# catalog only ever holds real indexed-access sites, never type
# expressions) — so `citation_check` dropped the whole sentence as a
# fabrication, even though nothing in it was fabricated. Any candidate
# citation whose base name is one of these is never treated as an access
# citation at all, the same "nothing to check, so it survives" treatment
# any other non-citation prose gets.
_TYPING_BASE_NAMES = frozenset({
    "dict", "list", "set", "tuple", "frozenset", "type",
    "Dict", "List", "Set", "Tuple", "FrozenSet", "Type",
    "Optional", "Union", "Callable", "Iterable", "Iterator",
    "Sequence", "MutableSequence", "Mapping", "MutableMapping",
    "Any", "ClassVar", "Literal", "Final", "Annotated",
})
_CRASH_WORDS_RE = re.compile(
    r"\b(crash(?:es|ed|ing)?|will fail|throws?|raises?\s+\w*Error|unguarded)\b",
    re.IGNORECASE,
)
_SILENT_WORDS_RE = re.compile(r"\b(silent(?:ly)?|swallow(?:s|ed|ing)?)\b", re.IGNORECASE)
# COLLECT-17 follow-up (self-play round 2, found via
# stub-test/run_hallucination_selfplay_v2.py's NEGATION-FLIP pattern): the
# mirror image of _CRASH_WORDS_RE. Before this, a sentence citing an access
# and asserting it is SAFE ("is fully guarded", "cannot raise", "will not
# crash") fell through to kind="generic" — it has an access citation, but
# _CRASH_WORDS_RE doesn't match affirmative-safety language, so
# citation_check never checked the access against known_accesses and
# contradiction_check's access_crash branch never ran at all. A false safety
# claim about a genuinely UNGUARDED (crashing) access therefore survived
# into the artifact completely unchecked — arguably worse than a false
# crash claim, since it actively tells a reader a real risk is handled.
# BUGFIX (verified-bugs audit #6): a safety sentence phrased with a
# NEGATED crash word ("stack[-1] will not crash", "it won't throw", "there is
# no risk of crash") was previously read as actual crash language:
# _CRASH_WORDS_RE matches the literal word "crash"/"throw"/"fail" even when
# it follows "will not"/"cannot"/"won't", so the crash branch above fired
# first and the sentence was classified access_crash. That collapsed a true
# "X is safe" claim into a crash claim — a true safety sentence about a
# GUARDED access was then dropped as "contradicts-guard", and a false "X will
# not crash" affirmation about an UNGUARDED access survived entirely
# unchecked (as an access_crash, which has no UNGUARDED contradiction check).
# The negation forms below (all three persons/tenses of "not", plus "never")
# are exactly what _CRASH_WORDS_RE otherwise cannot see past. Note the same
# problem never existed for "cannot raise"/"will not raise": the crash
# pattern for "raise" requires a following `\bError` suffix, so plain
# "cannot raise" never matched it — "crash"/"throw"/"fail" have no such
# suffix and are the ones that needed this.
_SAFE_WORDS_RE = re.compile(
    r"\bsafe(?:ly)?\b|\bguarded\b|"
    r"(?:\bcannot\b|\bcan'?t\b|\bwill\s+not\b|\bwon'?t\b|\bwould\s+not\b|\bwouldn'?t\b|"
    r"\bdoes\s+not\b|\bdoesn'?t\b|\bdo\s+not\b|\bdon'?t\b|\bdid\s+not\b|\bdidn'?t\b|"
    r"\bhave\s+not\b|\bhaven'?t\b|\bhas\s+not\b|\bhasn'?t\b|\bhad\s+not\b|\bhadn'?t\b|"
    r"\bnever\b)\s+"
    r"(?:raise|crash(?:es|ed|ing)?|fail(?:s|ure)?|throw(?:s|n|ing)?)\b|"
    r"\bno\s+risk\s+of\s+(?:raise|crash(?:es|ed|ing)?|fail(?:s|ure)?|throw(?:s|n|ing)?)\b",
    re.IGNORECASE,
)


# BUGFIX: `dataflow.py` catalogs a string-key subscript's `access` field
# via Python `repr()` on the key literal — which always renders with
# single quotes (`entry['stack']`) — while Pass B prose naturally quotes
# the same access the way it appears in the actual source, which is
# whatever quote style that source file used (`entry["stack"]`). Compared
# literally, those two strings never match, so *every* claim about a
# double-quoted string-key access — crash or safety, doesn't matter — was
# dropped by `citation_check` as "does not correspond to any indexed
# access site", even though the exact same site is sitting right there in
# `known_accesses` under the single-quoted spelling. `_normalize_access`
# collapses both quote characters to one canonical form before any
# access-citation comparison, on both sides, so quote style stops being
# load-bearing for whether a real, correctly-cited claim survives.
def _normalize_access(access: str) -> str:
    """Canonicalize an access-citation string for comparison purposes:
    fold both `'` and `"` to a single quote character. Only used for
    equality checks (`known_accesses` membership, `GuardedAccess.access`
    matching) — never surfaces to the user, so which quote character
    "wins" is arbitrary as long as it's applied identically everywhere.
    """
    return access.replace('"', "'")


def _iter_access_citations(sentence: str) -> List[str]:
    """Every `name[...]`-shaped access citation in `sentence`, with full,
    correctly-balanced bracket content — including one level (or more) of
    nesting like `cache[keys[0]]` — and with known typing/builtin generic
    bases (`dict[str, int]`, `Optional[int]`, ...) excluded entirely.

    BUGFIX (two independent defects the old `_ACCESS_RE =
    r"([A-Za-z_][\\w.]*\\[[^\\]]+\\])"` had):

    1. `[^\\]]+` is non-greedy in effect for nested brackets — it stops at
       the *first* `]`, so `"cache[keys[0]] is unguarded"` was captured as
       the truncated, syntactically-unbalanced `"cache[keys[0]"`. That
       string can never equal the real catalog entry `"cache[keys[0]]"`
       (see `dataflow._subscript_index_repr`'s own nested-subscript fix),
       so a true, correctly-cited claim about the *outer* access was
       dropped by `citation_check` as an uncatalogued fabrication — even
       though it was neither uncatalogued nor fabricated, just mismatched
       on a truncated string. Walking the brackets with an explicit depth
       counter instead of a regex character class captures the whole
       balanced span regardless of nesting depth.
    2. The old pattern had no way to tell a real access apart from a type
       annotation using the same subscript syntax (`dict[str, int]`) —
       see `_TYPING_BASE_NAMES`'s docstring above.
    """
    citations: List[str] = []
    for m in _ACCESS_START_RE.finditer(sentence):
        base = m.group(0)
        if base.rsplit(".", 1)[-1] in _TYPING_BASE_NAMES:
            continue
        start = m.end()  # index of the opening '['
        depth = 0
        end = None
        i = start
        while i < len(sentence):
            ch = sentence[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        if end is None:
            continue  # unbalanced — not a real subscript citation
        citations.append(sentence[m.start():end + 1])
    return citations


@lru_cache(maxsize=8)
def _symbol_patterns(known_symbols: FrozenSet[str]) -> Tuple[Tuple[str, "re.Pattern[str]"], ...]:
    """Precompile one `\\bshort\\b` regex per known symbol, once per distinct
    `known_symbols` set.

    `extract_claims` used to build a fresh `re.search(rf"\\b{...}\\b", ...)`
    pattern for *every symbol* on *every sentence* — with `known_symbols` in
    the low thousands and hundreds of claim sentences, that's up to
    millions of regex compilations for a single `verify_repo()` run (Pass C
    was observed stuck inside `re/_compiler.py` because of this). Caching
    the compiled patterns here, keyed by the (hashable, frozen) symbol set,
    means each distinct symbol set is only ever compiled once.
    """
    patterns = []
    for sym in known_symbols:
        short = sym.split(":")[-1]
        if short:
            patterns.append((sym, re.compile(rf"\b{re.escape(short)}\b")))
    return tuple(patterns)


def _prefer_citable_homonyms(
    symbols: List[str], citable_modules: Optional[FrozenSet[str]]
) -> List[str]:
    """V11: resolve a bare short-name mention to the citable homonym.

    `_symbol_patterns` is keyed per *qualname*, so one bare mention of
    `load_events` in a sentence yields one candidate per module that
    defines a `load_events` — repo-wide, 514 such names sit behind the
    imports of 215 of this repo's 383 test files. Without this step every
    one of those candidates became its own `Claim`; the ones outside the
    citable set failed `citation_check`, and `verify_claims`'s sink-or-swim
    rule then took the sentence down *with* the correctly-resolved one —
    so widening the citable set for a test would have kept nothing.

    When `citable_modules` is given and at least one candidate for a short
    name belongs to it, only those candidates stay; a short name with no
    citable candidate keeps all of them, and they fail exactly as before.
    `None` — the public default, for callers that never build a set — is a
    no-op and keeps every homonym. `verify_repo` builds a set for *every*
    module since V16: a test file's is its own path plus its imports (V11),
    a source module's is just `{own path}`, so a bare name in a source-file
    summary resolves to the symbol defined *here* instead of to every
    module that defines one by that name. Explicit `path.py:name` citations
    never pass through here — the caller appends those after this step,
    unfiltered.
    """
    if citable_modules is None or not symbols:
        return symbols
    by_short: Dict[str, List[str]] = {}
    for sym in symbols:
        by_short.setdefault(sym.split(":")[-1], []).append(sym)
    kept: List[str] = []
    for sym in symbols:
        siblings = by_short[sym.split(":")[-1]]
        if any(s.partition(":")[0] in citable_modules for s in siblings):
            if sym.partition(":")[0] in citable_modules:
                kept.append(sym)
        else:
            kept.append(sym)
    return kept


def extract_claims(
    text: str,
    module_path: str,
    known_symbols: FrozenSet[str] = frozenset(),
    known_names: FrozenSet[str] = frozenset(),
    citable_modules: Optional[FrozenSet[str]] = None,
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

    One sentence can produce *more than one* `Claim` — every distinct
    location, symbol, and access citation found in the sentence gets its
    own `Claim` (all sharing that sentence's `.text`/`.kind`), not just
    the first of each. BUGFIX (found via self-play hallucination review):
    earlier versions used a single `.search()` per field, so a sentence
    citing two things of the same kind ("make_summarizer_call and
    tools/collect/summarizer.py:_totally_invented_helper_fn both live in
    this module") only ever extracted the *first* — a real citation
    sitting next to a fabricated one in the same sentence vouched for the
    fabrication too, since the fabrication was never even turned into
    something `citation_check` could see. `verify_claims` (see its own
    docstring) enforces that a sentence's citations sink or swim
    together — one fabricated citation drops the whole sentence from the
    artifact, not just the `Claim` that carried it.

    `known_names` — BUGFIX (found via self-play hallucination review of an
    earlier version of this exact fix): a sentence can cite a symbol via
    `path.py:identifier` syntax naming something that appears *nowhere* in
    `known_symbols` for two very different reasons — (1) it's an outright
    invented name (the fabrication this parameter's check exists to still
    catch), or (2) it's a perfectly real name Pass A's `public_symbols`
    index just doesn't track, because COLLECT-4 deliberately scopes that
    index to top-level functions/classes only — a module-level constant or
    a class method is real but was never going to be in `known_symbols`.
    Treating both cases identically (flag it, let `citation_check` drop it
    as "not found") makes the *fix* itself a false-positive generator:
    `tools/collect/summarizer.py:DEFAULT_NUM_CTX` (a real constant) and
    `tools/formatter.py:render` (a real method) would both get dropped as
    fabrications alongside genuinely invented names, and the
    `verification_report.json` row would flatly assert "not found in Pass
    A index" about something that, in fact, is right there in the source.
    `known_names` (`ast_facts.extract_all_defined_names` — every name
    `def`/`class`/assigned anywhere in the module, any nesting depth) is
    the broader, permissive check that tells those two cases apart: a
    `path.py:identifier` citation is only treated as a checkable citation
    at all — i.e. only gets a `Claim.symbol` set from this fallback — when
    the identifier does *not* appear in `known_names` for that same
    `path`, or when `path` isn't this module at all (a cross-module
    citation always gets flagged, real-but-unindexed-elsewhere or not,
    because Pass B was never shown another module's facts to have
    legitimately cited it from — see the cross-module citation-check fix
    this session already landed). A same-module citation of a real,
    merely-unindexed name contributes no `Claim` for that citation at all
    (`kind="generic"` if it's the sentence's only citation) — the same
    "nothing to check, so it survives" treatment any other
    unverifiable-but-plausible prose already gets; it isn't promoted to a
    *verified* citation either, since `known_names` was never meant to be
    an authoritative index the way `known_symbols` is.
    """
    if not text or not text.strip():
        return []

    claims: List[Claim] = []
    for raw_sentence in _SENTENCE_SPLIT_RE.split(text.strip()):
        sentence = raw_sentence.strip()
        if not sentence:
            continue

        # BUGFIX (found via self-play hallucination review): every field
        # below used to be captured with a single `.search()` — the first
        # match in the sentence, full stop. A sentence citing *two* things
        # of the same kind ("make_summarizer_call and
        # tools/collect/summarizer.py:_totally_invented_helper_fn both
        # live in this module") only ever produced ONE `Claim`, carrying
        # only the first citation found (`make_summarizer_call`, real) —
        # the second, fabricated one was never extracted into anything
        # `citation_check` could see at all, so it survived verbatim
        # alongside the real citation that vouched for the sentence as a
        # whole. Same failure mode for two `path.py:line` citations in one
        # sentence (a real, in-range one plus a fabricated, out-of-range
        # one). Fixed by finding *every* location/symbol/access citation
        # in the sentence (not just the first of each) and emitting one
        # `Claim` per citation — all sharing this sentence's `.text`/
        # `.kind`, so `verify_claims` can (and does, below) enforce that
        # the whole sentence only survives into the artifact if *every*
        # citation it makes does.
        locations = [f"{m.group(1)}:{m.group(2)}" for m in _LOCATION_RE.finditer(sentence)]
        accesses = _iter_access_citations(sentence)

        # BUGFIX (bare short-name over-matching, found via self-play
        # hallucination review against this repo's own collect run):
        # `known_symbols` is the whole repo's flat table — in the low
        # thousands per `_symbol_patterns`' own docstring — so a bare
        # `\bshort\b` match with no `path.py:` prefix at all routinely hit
        # short names that are *also* everyday English words ("task",
        # "main", "check", "run", "repo"), coincidentally defined
        # *somewhere* in a few-hundred-file repo's test suite. A sentence
        # merely discussing "task metrics" or "the repo" got treated as an
        # attempted citation of some unrelated test fixture's `task`/`repo`
        # symbol and dropped as an unverifiable cross-module reference —
        # even though the sentence made no code reference at all; "Entry
        # point is main() via argparse" fared no better, flagged once per
        # *other* file that also happens to define its own `main`.
        #
        # The distinguishing signal isn't call syntax (real citations this
        # mechanism is *for* — "make_summarizer_call and summarize_repo
        # both live in this module" — are bare, parenthesis-free prose
        # too) — it's that a plain, single, undecorated lowercase word is
        # both (a) indistinguishable from ordinary English and (b) exactly
        # the shape of a generic, likely-non-unique local name (a stray
        # test fixture, a parameter), so a bare match on one is low-signal
        # either way. A real function/class name worth treating as a
        # citation candidate is virtually always multi-word — snake_case
        # (`make_summarizer_call`) or CamelCase (`AutoController`) — which
        # ordinary prose doesn't spontaneously produce. Restricting bare
        # (unprefixed) matching to that shape, while leaving the explicit
        # `path.py:identifier` branch below completely untouched, keeps the
        # mechanism's actual intended catch (a real multi-word helper name
        # dropped into prose without its module prefix) while dropping the
        # false-positive-prone single-word case entirely — it isn't lost,
        # it just falls through as ordinary unverifiable prose, the same
        # "nothing to check, so it survives" treatment any other
        # unfalsifiable sentence already gets. An outright *invented*
        # short name never matched `known_symbols` to begin with and is
        # unaffected either way.
        symbols: List[str] = []
        for sym, pattern in _symbol_patterns(known_symbols):
            # AUDIT NOTE: matches `_symbol_patterns`' own `sym.split(":")[-1]`
            # exactly (rather than `split(":", 1)[1]`, which would silently
            # disagree with it if a qualname ever contained a second colon)
            # — currently equivalent in practice, since `ast_facts.py`/
            # `java_facts.py` both build every qualname as exactly one
            # colon (`f"{module_path}:{name}"`), but there's no reason for
            # this loop to compute `short` any way other than the one the
            # regex it's about to reuse was itself keyed by.
            short = sym.split(":")[-1]
            if "_" not in short and short == short.lower():
                continue  # bare single lowercase word — too generic to trust
            if pattern.search(sentence):
                symbols.append(sym)
        symbols = _prefer_citable_homonyms(symbols, citable_modules)

        for sym_citation_match in _SYMBOL_CITATION_RE.finditer(sentence):
            cited_path, cited_ident = sym_citation_match.group(1), sym_citation_match.group(2)
            exact_qualname = f"{cited_path}:{cited_ident}"
            if exact_qualname in symbols:
                continue  # already captured via the known-symbol branch above
            if cited_path == module_path and cited_ident in known_names:
                continue  # real-but-unindexed name in this module — not a citation to check
            symbols.append(exact_qualname)

        # BUGFIX (see _SAFE_WORDS_RE): a safety sentence like "stack[-1] will
        # not crash" contains the literal crash word "crash", so probing the
        # raw sentence first would classify it access_crash. Probe the crash
        # keywords on a copy with every safe phrasing removed — only
        # genuinely affirmative crash language then trips the crash branch.
        # A sentence that contains BOTH a real crash assertion and safety
        # language still keeps the existing crash-wins behavior, because the
        # residual crash assertion survives the strip.
        _crash_probe = _SAFE_WORDS_RE.sub("", sentence)
        if accesses and _CRASH_WORDS_RE.search(_crash_probe):
            kind = "access_crash"
        elif accesses and _SAFE_WORDS_RE.search(sentence):
            # access_safe: the mirror image of access_crash (see
            # _SAFE_WORDS_RE). Checked below the crash branch so a sentence
            # that (unusually) contains both crash and safety language
            # keeps the existing access_crash behavior unchanged.
            kind = "access_safe"
        elif _SILENT_WORDS_RE.search(sentence):
            kind = "silent_except"
        else:
            kind = "generic"

        citation_fields: List[Dict[str, Optional[str]]] = (
            [{"location": loc} for loc in locations]
            + [{"symbol": sym} for sym in symbols]
            + [{"access": acc} for acc in accesses]
        )
        if not citation_fields:
            citation_fields = [{}]  # nothing checkable — one plain generic claim, as before

        for fields in citation_fields:
            claims.append(
                Claim(
                    text=sentence,
                    module=module_path,
                    kind=kind,
                    symbol=fields.get("symbol"),
                    location=fields.get("location"),
                    access=fields.get("access"),
                )
            )
    return claims


# ── check 1: citation ───────────────────────────────────────────────────────────


def citation_check(
    claim: Claim,
    known_symbols: FrozenSet[str],
    line_counts: Dict[str, int],
    known_accesses: FrozenSet[str] = frozenset(),
    citable_modules: Optional[FrozenSet[str]] = None,
) -> Optional[str]:
    """Return `None` if `claim` cites something real, else a detail string
    explaining what didn't resolve.

    * A cited `symbol` must be in `known_symbols` (Pass A's own index —
      COLLECT-4's `public_symbols`, nothing derived or LLM-sourced) *and*
      must belong to one of `citable_modules` — by default just the module
      this claim is about (`claim.module`), see the BUGFIX note below; for
      a test file `verify_repo` widens that to the modules the test
      imports (V11, see `verify_repo`).
    * A cited `location` (`path:line`) must name a scanned module and a
      line number within that file's actual line count, and that module
      must be `claim.module` itself — same BUGFIX, same reasoning. V11
      does not widen this one: Pass B never saw another module's source,
      so a line number in it is not something a test could have read off.
    * An `access_crash` claim's cited `access` (e.g. `"cache[-1]"`) must be
      one COLLECT-7's dataflow pass actually recorded for this module — in
      `known_accesses`, regardless of GUARDED/UNGUARDED status. Without
      this, a claim about an access that doesn't correspond to any real
      subscript site in the code at all (not merely a guarded one) has
      nothing in `contradiction_check` to match against, finds no
      contradiction, and survives — which is a fabrication reaching the
      artifact exactly as much as a fabricated symbol or line would be.
      Scoped to `kind="access_crash"` (and, symmetrically, `kind=
      "access_safe"` — see contradiction_check's mirror-image check below)
      specifically: a claim that merely *mentions* bracket syntax in
      passing, without asserting a crash or safety verdict, was never
      claiming that access is a cataloged site in the first place.
    A claim citing none of the above is not a citation-check failure — it
    simply has nothing to check (handled by the caller: only claims with a
    citation at all go through this gate meaningfully; see `verify_claims`).

    BUGFIX (found via self-play hallucination session on this repo):
    `known_symbols`/`line_counts` (built by `verify_repo`) are *repo-wide*
    — every module's symbols and every file's line count, all in one flat
    table — because that's the cheapest way to build them once per run.
    But `summarizer.py`'s own prompt only ever shows Pass B *one module's*
    facts and source (`_facts_block` — COLLECT-16's whole antihallucination
    premise is that Pass B never sees another module at all), so any claim
    about module A that cites a symbol or `path:line` actually belonging to
    module B was never grounded in anything Pass B was shown — it can only
    be a fabrication (or, worst case, a prompt-injection payload lifted
    from a docstring). Before this fix, `citation_check` verified only
    "does this symbol/location exist *somewhere* in the repo," not "does it
    belong to *this* module" — so `citation_check` (and thus
    `contradiction_check`, which trusts a claim's citation once it survives
    this gate) happily laundered a claim like "this module implements
    `backoff_seconds`, defined at tools/backoff.py:1" while summarizing
    `tools/formatter.py`, purely because `backoff_seconds` and that line
    are real *somewhere*. That is exactly the class of fabrication
    COLLECT-17 exists to catch — a citation that resolves globally but not
    locally is not a citation of this module at all.

    V11: for a *test* file that reasoning inverts — a test exists to name
    the symbols of the module it exercises, and its own source (which Pass
    B was shown) is where those imports sit. So `citable_modules` is the
    set of modules a symbol citation may resolve to: `None` means the
    original `{claim.module}` rule, and `verify_repo` passes the test's own
    path plus every module `graph.import_edges` says it imports. A symbol
    from a module the test does *not* import is still outside the set and
    still drops.
    """
    citable = citable_modules if citable_modules is not None else frozenset({claim.module})

    if claim.symbol is not None:
        if claim.symbol not in known_symbols:
            return f"cited symbol {claim.symbol!r} not found in Pass A index"
        symbol_module, _, _ = claim.symbol.partition(":")
        if symbol_module not in citable:
            return (
                f"cited symbol {claim.symbol!r} belongs to module "
                f"{symbol_module!r}, not {claim.module!r}"
                # V16: the suffix describes a set actually widened past the
                # module itself. A source module's set is `{claim.module}`
                # alone (what `None` folds into above), so its detail string
                # stays byte-identical to the pre-V11 wording — `.collect/`
                # diffs and the M-tickets' reason counters read it.
                + (" or any module it imports" if len(citable) > 1 else "")
            )

    if claim.location is not None:
        path, _, line_str = claim.location.rpartition(":")
        if not line_str.isdigit():
            return f"malformed location {claim.location!r}"
        max_line = line_counts.get(path)
        if max_line is None:
            return f"cited module {path!r} not found in Pass A index"
        if path != claim.module:
            return (
                f"cited location {claim.location!r} belongs to module "
                f"{path!r}, not {claim.module!r}"
            )
        line = int(line_str)
        if line < 1 or line > max_line:
            return f"cited line {line} out of range for {path!r} (1..{max_line})"

    if claim.kind in ("access_crash", "access_safe") and claim.access is not None:
        if _normalize_access(claim.access) not in known_accesses:
            return (
                f"cited access {claim.access!r} does not correspond to any "
                f"indexed access site in Pass A's guarded_accesses for this module"
            )

    return None


# ── check 2: contradiction ──────────────────────────────────────────────────────


REASON_CONTRADICTS_NOT_SILENT = "dropped:contradicts-not-silent"


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
    * `kind="silent_except"` claims get two checks against `module`'s own
      `except_sites` (COLLECT-6)/`fail_open_locs` (COLLECT-9's registry),
      genuinely mirroring the `access_crash` check above rather than only
      covering half of it:

      1. **Redundancy** — a cited location that's already a documented
         fail-open site is not a new discovery, so the claim is
         suppressed.
      2. **Falsehood** (BUGFIX) — a cited location that Pass A's own
         `except_sites` classified with `is_fail_open=False` (it logs,
         re-raises, or `continue`s — COLLECT-6 already proved it is *not*
         silent) directly contradicts a "silently swallows" claim about
         that exact site, the same way a `GUARDED` access contradicts an
         `access_crash` claim. Before this fix, the only check here was
         redundancy-against-`fail_open_locs`, which by construction can
         only ever agree with a *true* "this is silent" claim (a location
         in that set already *is* fail-open) — there was no check at all
         for the more dangerous case of a claim asserting silence about a
         location Pass A proved logs/re-raises/`continue`s, so a
         fabricated "site X silently swallows the exception" survived to
         the artifact untouched whenever X was a real `except` handler
         Pass A had already classified as *not* silent. This is exactly
         the class of unchecked assertion COLLECT-17 exists to catch —
         the module's own docstring already claimed this check was
         "symmetric" with the access_crash one; now it actually is.
    """
    if claim.kind == "access_crash" and claim.access:
        cited = _normalize_access(claim.access)
        for ga in module.guarded_accesses:
            if _normalize_access(ga.access) == cited and ga.status == "GUARDED":
                guard_desc = ga.guard or "guarded"
                return (
                    REASON_CONTRADICTS_GUARD,
                    f"{claim.access} at {ga.location} is GUARDED ({guard_desc})",
                )

    if claim.kind == "access_safe" and claim.access:
        # Mirror image of the access_crash check above: a claim that an
        # access is safe/guarded, checked against a real UNGUARDED status.
        # Found via stub-test/run_hallucination_selfplay_v2.py's
        # NEGATION-FLIP pattern — before this branch existed, this claim
        # kind was never even constructed (see _SAFE_WORDS_RE), so a false
        # "X is safely guarded" claim about a genuinely crashing access
        # reached the artifact unchecked in every module tested.
        cited = _normalize_access(claim.access)
        for ga in module.guarded_accesses:
            if _normalize_access(ga.access) == cited and ga.status == "UNGUARDED":
                return (
                    REASON_CONTRADICTS_CRASH,
                    f"{claim.access} at {ga.location} is UNGUARDED "
                    "(Pass A's dataflow found no guard - this is a real crash risk)",
                )

    if claim.kind == "silent_except" and claim.location:
        if claim.location in fail_open_locs:
            return (
                REASON_CONTRADICTS_FAIL_OPEN,
                f"{claim.location} is already a documented fail-open site",
            )
        for site in module.except_sites:
            if site.location == claim.location and not site.is_fail_open:
                return (
                    REASON_CONTRADICTS_NOT_SILENT,
                    f"{claim.location} is classified {site.body_kind!r} "
                    "(not silent) by Pass A's except-site analysis",
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
    citable_modules: Optional[FrozenSet[str]] = None,
) -> Tuple[List[Claim], List[DroppedClaim]]:
    """Run every check in order on each of `claims`; return `(kept, dropped)`.

    Citation runs before contradiction: a claim citing a location/symbol/
    access that doesn't even exist is a fabrication regardless of what it
    says about it, so there's no reason to also ask whether its
    (nonexistent) citation contradicts anything.

    Two passes. Pass 1 checks every claim independently, exactly as
    before. Pass 2 — BUGFIX (found via self-play hallucination review) —
    enforces that claims sharing the same sentence (`claim.text`, which
    `extract_claims` now sets identically for every citation pulled out of
    one sentence — see its docstring) sink or swim *together*: if any
    citation from a sentence failed Pass 1, every *other* claim from that
    same sentence is reclassified as dropped too, even though it
    individually checked out. Without this, "make_summarizer_call and
    tools/collect/summarizer.py:_totally_invented_helper_fn both live in
    this module" would keep the real citation's `Claim` in `kept` and only
    drop the fabricated one's `Claim` — but both `Claim`s carry the exact
    same sentence as `.text`, so the *sentence itself* (fabrication
    included) would still end up back in the reconstructed artifact text
    once `_verify_text` rejoins whatever's in `kept`. A sentence is only
    as trustworthy as its least trustworthy citation.

    Every claim ends up in exactly one of `kept`/`dropped` either way —
    the same "nothing vanishes silently" invariant `verify_repo`'s
    `kept_count` bookkeeping already depends on (see its own BUGFIX note).

    `citable_modules` (V11) goes straight to `citation_check`; `None` is
    the same-module-only rule every caller had before.
    """
    known_accesses = frozenset(_normalize_access(ga.access) for ga in module.guarded_accesses)
    provisionally_kept: List[Claim] = []
    dropped: List[DroppedClaim] = []
    for claim in claims:
        no_citation = citation_check(
            claim, known_symbols, line_counts, known_accesses, citable_modules,
        )
        if no_citation is not None:
            dropped.append(DroppedClaim(claim=claim, reason=REASON_NO_CITATION, detail=no_citation))
            continue

        contradiction = contradiction_check(claim, module, fail_open_locs)
        if contradiction is not None:
            reason, detail = contradiction
            dropped.append(DroppedClaim(claim=claim, reason=reason, detail=detail))
            continue

        provisionally_kept.append(claim)

    failed_sentences = {d.claim.text for d in dropped}
    kept: List[Claim] = []
    for claim in provisionally_kept:
        if claim.text in failed_sentences:
            dropped.append(
                DroppedClaim(
                    claim=claim,
                    reason=REASON_SIBLING_CITATION_FAILED,
                    detail="a different citation in the same sentence did not verify",
                )
            )
        else:
            kept.append(claim)
    return kept, dropped


def _verify_text(
    text: str,
    *,
    module: ModuleRecord,
    known_symbols: FrozenSet[str],
    line_counts: Dict[str, int],
    fail_open_locs: FrozenSet[str],
    known_names: FrozenSet[str] = frozenset(),
    citable_modules: Optional[FrozenSet[str]] = None,
) -> Tuple[str, List[DroppedClaim]]:
    """Extract + verify claims from one prose field (`purpose` or `notes`),
    return the reconstructed (filtered) text and whatever got dropped."""
    claims = extract_claims(text, module.path, known_symbols, known_names, citable_modules)
    kept, dropped = verify_claims(
        claims,
        module=module,
        known_symbols=known_symbols,
        line_counts=line_counts,
        fail_open_locs=fail_open_locs,
        citable_modules=citable_modules,
    )
    # BUGFIX (companion to `verify_claims`'s "sink or swim together" fix
    # above): a sentence with *multiple* citations that all individually
    # survived now has multiple `Claim`s in `kept` sharing the exact same
    # `.text` — naively joining every kept claim's text would duplicate
    # that sentence once per surviving citation. `dict.fromkeys` dedupes
    # while preserving first-seen order (plain `set` would not).
    filtered_text = " ".join(dict.fromkeys(c.text for c in kept))
    return filtered_text, dropped


def verify_module(
    module: ModuleRecord,
    *,
    known_symbols: FrozenSet[str],
    line_counts: Dict[str, int],
    fail_open_locs: FrozenSet[str],
    known_names: FrozenSet[str] = frozenset(),
    citable_modules: Optional[FrozenSet[str]] = None,
) -> Tuple[ModuleRecord, List[DroppedClaim]]:
    """Run Pass C on one already-summarized module.

    A module with no summary (Pass B skipped, or `--no-llm`) passes through
    unchanged — there is no prose to verify. Otherwise `purpose` and
    `notes` are each independently filtered down to their surviving claims
    and reattached as a fresh `LLMSummary` (COLLECT-1's whitelist — still,
    and always, `provenance="llm"`).

    `known_names` — this module's own broader "every name defined
    anywhere" set (`ast_facts.extract_all_defined_names`), used only to
    keep `extract_claims`'s `path.py:identifier` fabrication check from
    flagging a real-but-unindexed name (see `extract_claims`'s docstring).
    Defaults to empty, same fail-safe posture as an absent artifact: no
    broader set to check against just means this guard can't help, not
    that anything crashes.

    `citable_modules` (V11) — the modules a symbol citation in this
    module's prose may resolve to; `None` is the own-module rule. It
    reaches both `extract_claims` (bare-name resolution) and
    `citation_check` (the gate) through `_verify_text`.
    """
    if module.summary is None:
        return module, []

    verified_purpose, dropped_purpose = _verify_text(
        module.summary.purpose,
        module=module, known_symbols=known_symbols,
        line_counts=line_counts, fail_open_locs=fail_open_locs,
        known_names=known_names, citable_modules=citable_modules,
    )
    verified_notes, dropped_notes = _verify_text(
        module.summary.notes,
        module=module, known_symbols=known_symbols,
        line_counts=line_counts, fail_open_locs=fail_open_locs,
        known_names=known_names, citable_modules=citable_modules,
    )

    verified_summary = LLMSummary(purpose=verified_purpose, notes=verified_notes)
    return module.with_llm_summary(verified_summary), dropped_purpose + dropped_notes


def verify_repo(
    modules: Iterable[ModuleRecord],
    sources: Dict[str, str],
    *,
    root: Optional[Path] = None,
    import_edges: Optional[Graph] = None,
) -> Tuple[List[ModuleRecord], Dict[str, Any]]:
    """Run Pass C over every summarized module in `modules`.

    V11 — test files may cite what they import. A module
    `test_paths.is_test_path` calls test code (under a test root, or a
    `conftest.py`; the L5 rule — a bare `test_*.py` name is not a signal,
    `tools/collect/test_map.py` ships) gets `citable_modules` = its own
    path plus every module `import_edges` says it imports. Every other
    module gets `{own path}` (V16; it was `None` before, which let a bare
    name in a source summary expand to every homonym repo-wide and sink the
    sentence) — the own-module-only rule, unchanged in effect. `import_edges`
    is the same `graph.import_edges(modules)` the artifact is built from —
    `cli.py` passes the one it already has; a caller without one gets it
    computed here, so the rule is never silently off. A test citing a
    module it does not import still drops.

    `sources` maps module path -> its source text, used to compute each
    file's line count for the citation-check's range test, and (COLLECT-17
    fabricated-symbol-citation fix) to build each module's broader
    "every name defined anywhere" set that keeps that same check from
    flagging a real-but-unindexed name — the same text Pass A/B already
    had; nothing new is read here beyond that.

    Returns `(verified_modules, report)`, where `report` is the
    JSON-serializable ``verification_report.json`` payload: every dropped
    claim plus a kept/dropped count, for transparency (COLLECT-17's AC:
    "все выбросы залогированы").
    """
    modules = list(modules)
    known_symbols = frozenset(sym.qualname for m in modules for sym in m.public_symbols)
    line_counts = {m.path: len(sources.get(m.path, "").splitlines()) for m in modules}

    if import_edges is None:
        import_edges = _import_edges(modules)
    citable_by_module: Dict[str, FrozenSet[str]] = {
        m.path: frozenset({m.path}) | import_edges.get(m.path, frozenset())
        if is_test_path(m.path) else frozenset({m.path})
        for m in modules
    }

    # Broader "every name this module defines anywhere" index (COLLECT-17
    # fabricated-symbol-citation fix), one per module, built directly from
    # `sources` here rather than stored on `ModuleRecord` — it is a Pass-C
    # -only heuristic guard against false-positive drops, never a
    # structural fact COLLECT-1's provenance contract would need to track.
    # A module whose source fails to re-parse (shouldn't happen — Pass A
    # already parsed it — but `sources` is caller-supplied, so fail open to
    # an empty set exactly like `line_counts`/`fail_open_registry` already
    # do elsewhere in this function, rather than let one bad source string
    # abort the whole verification run).
    known_names_by_module: Dict[str, FrozenSet[str]] = {}
    for m in modules:
        try:
            known_names_by_module[m.path] = extract_all_defined_names(
                ast.parse(sources.get(m.path, ""), filename=m.path)
            )
        except (SyntaxError, ValueError):
            known_names_by_module[m.path] = frozenset()

    fail_open_registry = build_fail_open_registry(modules, root=root)
    fail_open_locs = fail_open_locations(fail_open_registry)

    verified_modules: List[ModuleRecord] = []
    all_dropped: List[DroppedClaim] = []
    kept_count = 0

    for m in modules:
        # BUGFIX: kept_count used to be recomputed by re-running
        # extract_claims on the *rejoined* (already-filtered) purpose/notes
        # text — but sentence-splitting doesn't round-trip through that
        # rejoin. `_verify_text` reassembles surviving claims by joining
        # their `.text` with a single space, so newline-separated,
        # unpunctuated claims (a real Pass B idiom for bullet-style notes,
        # e.g. "Handles retries\nLogs failures") collapse back into one
        # sentence on re-split, since the split regex's `\n+` alternative
        # no longer has a newline to match and the joined text has no
        # terminal punctuation to trigger the other alternative either.
        # Three surviving claims could silently get reported as one kept
        # claim — undermining the "kept/dropped count for transparency"
        # guarantee this report exists for (COLLECT-17 AC). Fixed by
        # counting kept-by-subtraction against the *original* claim count
        # instead: every claim `verify_claims` sees is either kept or
        # recorded in `dropped`, nothing vanishes silently, so
        # `total - len(dropped)` is exact by construction and needs no
        # round-trip through reconstructed text at all.
        # BUGFIX: `known_names` (the real-but-unindexed-name exemption
        # guard) must be identical here to what `verify_module` uses
        # internally below, or this count and `len(dropped)` stop being
        # counts of the *same* claim set. Before multi-citation sentences
        # existed (one `Claim` per sentence, always), a mismatched
        # `known_names` could only change *which* field a sentence's lone
        # `Claim` carried, never how many `Claim`s a sentence produced —
        # so this call getting the default (empty) `known_names` instead
        # of the module's real one was silently harmless. Now that one
        # sentence can produce several `Claim`s (one per citation), a
        # sentence combining an exempt (real-but-unindexed) citation with
        # a genuine one produces a *different claim count* depending on
        # whether the exemption fires — so an inconsistent `known_names`
        # here would desync this count from what `verify_module` actually
        # processes, breaking the `total - len(dropped) == len(kept)`
        # invariant this whole block exists to guarantee.
        # V11: the same holds for `citable_modules` — it changes how many
        # `Claim`s a bare homonym mention produces (`extract_claims`), so
        # this count and `verify_module` must see the same set.
        total_claims = 0
        m_citable = citable_by_module.get(m.path)
        if m.summary is not None:
            m_known_names = known_names_by_module.get(m.path, frozenset())
            total_claims = len(
                extract_claims(m.summary.purpose, m.path, known_symbols, m_known_names, m_citable)
            ) + len(
                extract_claims(m.summary.notes, m.path, known_symbols, m_known_names, m_citable)
            )

        verified, dropped = verify_module(
            m, known_symbols=known_symbols, line_counts=line_counts, fail_open_locs=fail_open_locs,
            known_names=known_names_by_module.get(m.path, frozenset()),
            citable_modules=m_citable,
        )
        verified_modules.append(verified)
        all_dropped.extend(dropped)
        kept_count += total_claims - len(dropped)

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
