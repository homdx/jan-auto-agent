"""tools/auto/canon_validator.py — AUTO-CR-7: periodic canon / fact validator.

Creative mode (AUTO-CR-1..6) deliberately made the Gate-2 validator a *soft*,
fail-open gate so a small model (``llama3.1:8b``) cannot hard-block a chapter
over malformed output.  That trade left a hole: nothing checks whether a newly
written chapter CONTRADICTS facts established in earlier chapters.  This module
fills it.

Design
------
The canon is the story already told: the verified ``synopsis.md`` produced by
:mod:`tools.auto.summary_memory` (which is the compressed, fidelity-checked
representation of chapters ``1..N-1``), backed by the chapters themselves via
the prose-pull (:mod:`tools.auto.context_broker`, AUTO-CR-6).

Flow of :meth:`CanonValidator.check`:

1. Extract the new chapter's factual claims (one line-oriented LLM call).
2. Ground each claim against the canon and ask for a verdict line:

       DIRECT            — supported by canon
       INDIRECT          — plausible, no conflict
       NONE              — new / unsupported (allowed; fiction adds facts)
       CONFLICT: <what>  — contradicts canon  ← the only actionable verdict

3. For each ``CONFLICT`` the validator pulls the specific earlier passage (via
   the broker) so the feedback names both sides and the source chapter.

Bounding (the "cannot loop" guarantees, mirroring AUTO-CR-5):

* **Periodic.** :meth:`should_check` only runs the gate every
  ``canon_check_every`` chapters (so it does not burn budget every attempt).
* **Bounded revisions.** The caller (InnerLoop) grants at most
  ``max_canon_revisions`` canon-driven rejections per chapter, then
  accepts-with-warning.  This module exposes the cap; the loop enforces it.

Everything is **fail-open**: any LLM / parse error degrades a verdict to
``INDIRECT`` (non-blocking) and is logged — a rambling 8B reply never stalls or
hard-blocks the pipeline.

Public surface::

    from tools.auto.canon_validator import CanonValidator, CanonResult, make_canon_validator
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tools.auto.utils import chars_per_token

logger = logging.getLogger(__name__)

# llm_call(system, user) -> str   (same callable contract as summary_memory)
LlmCall = Callable[[str, str], str]

# Matches chapter_07 / chapter_7 / Chapter_07.md … — mirrors context_assembler
# and context_broker so "chapter index" means the same thing everywhere.
_CHAPTER_RE = re.compile(r"chapter[_\-\s]?(\d+)", re.IGNORECASE)

# Reserve room for the model's own verdict output + instructions when sizing
# the canon context we paste into a grounding prompt.
_GROUNDING_OVERHEAD_TOKENS = 400


# ── Prompts (line-oriented, fail-open) ───────────────────────────────────────

_SYSTEM_EXTRACT_CLAIMS = (
    "You are a continuity checker for long-form fiction. "
    "Read the chapter and list its concrete factual claims about characters, "
    "world, objects, relationships, and timeline — the things that must stay "
    "consistent with the rest of the book. "
    "Output one claim per line, plain text, no numbering, no commentary. "
    "Keep each claim short and specific (a name, an attribute, an event)."
)

_SYSTEM_GROUND_CLAIM = (
    "You are a strict continuity judge for long-form fiction. "
    "You are given established CANON (facts from earlier chapters) and ONE new "
    "CLAIM from the latest chapter. Decide how the claim relates to the canon "
    "and reply with EXACTLY ONE line whose first token is one of:\n"
    "  DIRECT             — the canon directly supports the claim\n"
    "  INDIRECT           — consistent / plausible, the canon neither states "
    "nor contradicts it\n"
    "  NONE               — a genuinely new fact the canon does not mention "
    "(this is allowed)\n"
    "  CONFLICT: <reason> — the claim CONTRADICTS the canon; state what the "
    "canon said and what the claim says\n"
    "Only answer CONFLICT when there is a real contradiction (e.g. a dead "
    "character acting, a changed eye colour, a broken timeline). "
    "Do NOT return JSON. One line only, first token from the list above."
)


# ── Result object ────────────────────────────────────────────────────────────

@dataclass
class CanonResult:
    """Outcome of one canon check.

    Attributes
    ----------
    checked:
        True if the grounding pass actually ran (False when skipped because the
        chapter index did not fall on the periodic cadence, or there were no
        prior chapters to check against).
    conflicts:
        Human-readable conflict descriptions — the only actionable output.
    none_facts:
        Claims the canon does not mention (allowed; surfaced for observability).
    claims_checked:
        How many claims were grounded this pass.
    """

    checked: bool = False
    conflicts: list[str] = field(default_factory=list)
    none_facts: list[str] = field(default_factory=list)
    claims_checked: int = 0

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicts)

    def feedback(self) -> str:
        """Render conflicts as Gate-2-style prescriptive feedback for the coder."""
        if not self.conflicts:
            return ""
        lines = [
            "CANON CONFLICT — your chapter contradicts facts established earlier. "
            "Revise so these are consistent:"
        ]
        for c in self.conflicts:
            lines.append(f"  - {c}")
        return "\n".join(lines)


# ── Validator ────────────────────────────────────────────────────────────────

class CanonValidator:
    """Periodic, bounded, fail-open canon/fact checker for creative mode.

    Parameters
    ----------
    llm_call:
        ``llm_call(system, user) -> str`` — same contract as SummaryMemory.
    broker:
        A :class:`tools.auto.context_broker.ContextBroker` used for prose-pull
        of the specific earlier passage behind a conflict.  Optional; when
        ``None`` the feedback still names the conflict, just without an inline
        source excerpt.
    base_dir:
        Project root holding the chapter files and ``synopsis.md``.
    canon_check_every:
        Run the gate only on chapters whose index is a multiple of this value.
        ``<= 1`` means "every chapter".
    max_canon_revisions:
        Advertised cap on canon-driven revisions per chapter.  Enforced by the
        InnerLoop; exposed here so the loop reads it from one place.
    synopsis_path:
        Filename of the running synopsis relative to ``base_dir``.
    max_claims:
        Hard cap on claims grounded per chapter (bounds LLM calls).
    num_ctx / max_tokens:
        Used to size the canon context pasted into grounding prompts.
    """

    def __init__(
        self,
        llm_call: LlmCall,
        *,
        broker=None,
        base_dir: "str | Path" = ".",
        canon_check_every: int = 3,
        max_canon_revisions: int = 1,
        synopsis_path: str = "synopsis.md",
        max_claims: int = 12,
        num_ctx: int = 8192,
        max_tokens: int = 2048,
    ) -> None:
        self._llm = llm_call
        self._broker = broker
        self._base_dir = Path(base_dir)
        self._every = max(1, int(canon_check_every))
        self.max_canon_revisions = max(0, int(max_canon_revisions))
        self._synopsis_path = synopsis_path
        self._max_claims = max(1, int(max_claims))
        # Canon context budget: leave room for output + the claim. Stored as
        # a token count — the char budget depends on the synopsis text's
        # script (Cyrillic tokenizes ~2x denser than Latin — see
        # chars_per_token()), so it's computed per-call in _load_canon().
        self._canon_budget_tokens = max(0, int(num_ctx) - int(max_tokens) - _GROUNDING_OVERHEAD_TOKENS)

    # ── Cadence ──────────────────────────────────────────────────────────────

    @staticmethod
    def chapter_index(chapter_file: str) -> "int | None":
        """Return the numeric chapter index from a filename, or ``None``."""
        m = _CHAPTER_RE.search(str(chapter_file))
        return int(m.group(1)) if m else None

    def should_check(self, chapter_file: str) -> bool:
        """True when this chapter falls on the periodic cadence AND has at least
        one predecessor to check against.
        """
        idx = self.chapter_index(chapter_file)
        if idx is None:
            # Non-chapter target (rare) — be safe, check it.
            return True
        if idx <= 1:
            return False  # nothing established before chapter 1
        return idx % self._every == 0

    # ── Main entry ───────────────────────────────────────────────────────────

    def check(
        self,
        new_chapter_text: str,
        chapter_file: str,
        base_dir: "str | Path | None" = None,
    ) -> CanonResult:
        """Ground *new_chapter_text*'s claims against canon. Never raises."""
        _base = Path(base_dir) if base_dir is not None else self._base_dir

        canon = self._load_canon(_base)
        if not canon.strip():
            logger.debug("CanonValidator: no canon yet — skipping check.")
            return CanonResult(checked=False)

        claims = self._extract_claims(new_chapter_text)
        if not claims:
            logger.debug("CanonValidator: no claims extracted — skipping.")
            return CanonResult(checked=False)

        result = CanonResult(checked=True)
        for claim in claims[: self._max_claims]:
            verdict, reason = self._ground_claim(claim, canon)
            result.claims_checked += 1
            if verdict == "CONFLICT":
                excerpt = self._pull_source(claim, chapter_file, _base)
                msg = reason or f"the chapter contradicts earlier canon: {claim!r}"
                if excerpt:
                    msg += f"  (canon: {excerpt})"
                result.conflicts.append(msg)
            elif verdict == "NONE":
                result.none_facts.append(claim)
            # DIRECT / INDIRECT → consistent, nothing to do.

        if result.conflicts:
            logger.info(
                "CanonValidator: %d conflict(s) in %s (checked %d claims).",
                len(result.conflicts), chapter_file, result.claims_checked,
            )
        return result

    # ── Internals ────────────────────────────────────────────────────────────

    def _load_canon(self, base_dir: Path) -> str:
        """Read synopsis.md (the verified canon store), capped to budget.

        Newest sections are most relevant, so when the synopsis exceeds the
        budget we keep the TAIL (latest chapters) and drop the oldest.
        """
        path = base_dir / self._synopsis_path
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        canon_budget_chars = max(800, int(self._canon_budget_tokens * chars_per_token(text)))
        if len(text) <= canon_budget_chars:
            return text
        # Keep the most recent canon (tail); mark the truncation.
        tail = text[-canon_budget_chars:]
        return "… [older canon omitted]\n" + tail

    def _extract_claims(self, chapter_text: str) -> list[str]:
        """One LLM call → list of claim lines. Fail-open: [] on any error."""
        from tools.auto.utils import detect_language, language_instruction
        _sys = _SYSTEM_EXTRACT_CLAIMS
        _instr = language_instruction(detect_language(chapter_text))
        if _instr:
            _sys = _SYSTEM_EXTRACT_CLAIMS + " " + _instr
        try:
            reply = self._llm(_sys, f"CHAPTER:\n{chapter_text}") or ""
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            logger.warning("CanonValidator: claim extraction failed: %s", exc)
            return []
        claims: list[str] = []
        # Bugfix: this used to be
        #   line = raw.strip().lstrip("-*0123456789. \t").strip()
        # str.lstrip(chars) removes ANY of the given characters from the
        # left, repeatedly — it has no concept of "a marker" versus "the
        # claim's own leading digits". A claim that legitimately starts
        # with a number (an age, a year, a population count — precisely
        # what a CANON validator exists to protect) had that number
        # silently eaten: "3.5 million people lived there" became "million
        # people lived there" before ever reaching the conflict check.
        _marker_re = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+")
        for raw in reply.splitlines():
            line = _marker_re.sub("", raw.strip(), count=1).strip()
            if line:
                claims.append(line)
        return claims

    def _ground_claim(self, claim: str, canon: str) -> tuple[str, str]:
        """Return ``(verdict, reason)``.

        verdict ∈ {DIRECT, INDIRECT, NONE, CONFLICT}.  fail-open: any LLM /
        parse error or unrecognised reply → ``("INDIRECT", "")`` (non-blocking).
        """
        user = f"CANON:\n{canon}\n\nCLAIM:\n{claim}"
        try:
            reply = self._llm(_SYSTEM_GROUND_CLAIM, user) or ""
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning("CanonValidator: grounding failed for %r: %s", claim, exc)
            return "INDIRECT", ""

        first = ""
        for ln in reply.splitlines():
            if ln.strip():
                first = ln.strip()
                break
        upper = first.upper()

        if upper.startswith("CONFLICT"):
            # Capture the reason after the first ':' if present.
            reason = first.split(":", 1)[1].strip() if ":" in first else ""
            full = reason or claim
            return "CONFLICT", f"{full}"
        if upper.startswith("DIRECT"):
            return "DIRECT", ""
        if upper.startswith("NONE"):
            return "NONE", ""
        if upper.startswith("INDIRECT"):
            return "INDIRECT", ""
        # Unparseable → fail-open, non-blocking.
        logger.debug("CanonValidator: unparseable verdict %r — treating as INDIRECT.", first)
        return "INDIRECT", ""

    def _pull_source(self, claim: str, chapter_file: str, base_dir: Path) -> str:
        """Best-effort prose-pull of the earlier passage behind a conflict.

        Uses the broker (AUTO-CR-6) to fetch the most relevant earlier-chapter
        fragment for the claim's salient tokens.  Returns a short excerpt or
        ``""`` (broker absent / nothing found).  Never raises.
        """
        if self._broker is None:
            return ""
        # Salient tokens: capitalised words (names/places) are the cheapest,
        # most useful query terms for an entity-based prose pull.
        # BUGFIX: [A-Z][a-zA-Z]{2,} is ASCII-only, so it matched nothing at
        # all for Cyrillic character names (Иван, Мария, Андрей, ...) —
        # every Russian-fiction claim hit the `if not tokens: return ""`
        # guard below, silently dropping the earlier-chapter excerpt from
        # every canon-conflict message. The coder was told WHAT conflicted
        # but never WHERE the conflicting canon was established. Python 3's
        # \b is already Unicode-aware, so adding the Cyrillic ranges to the
        # character class is enough — no extra flags needed.
        tokens = re.findall(r"\b[A-ZА-ЯЁ][a-zA-Zа-яёА-ЯЁ]{2,}\b", claim)
        if not tokens:
            return ""
        try:
            earlier = self._earlier_chapters(chapter_file, base_dir)
            resolved = self._broker.resolve(tokens[:3], earlier, base_dir)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("CanonValidator: source pull failed: %s", exc)
            return ""
        if not resolved:
            return ""
        # Compact the first hit to a single short excerpt.
        block = next(iter(resolved.values()))
        excerpt = " ".join(block.split())
        return excerpt[:200] + ("…" if len(excerpt) > 200 else "")

    @staticmethod
    def _earlier_chapters(chapter_file: str, base_dir: Path) -> list[str]:
        """Relative paths of chapters with a lower index than *chapter_file*,
        in ascending order (canon = what came before).
        """
        idx = CanonValidator.chapter_index(chapter_file)
        out: list[tuple[int, str]] = []
        for p in base_dir.glob("*"):
            if p.suffix.lower() not in (".md", ".txt"):
                continue
            cidx = CanonValidator.chapter_index(p.name)
            if cidx is None:
                continue
            if idx is None or cidx < idx:
                out.append((cidx, p.name))
        out.sort()
        return [name for _, name in out]


# ── Factory ──────────────────────────────────────────────────────────────────

def make_canon_validator(
    config,
    base_dir: "str | Path" = ".",
    *,
    task_mode: str = "creative",
    broker=None,
    synopsis_path: str = "synopsis.md",
) -> "CanonValidator | None":
    """Build a :class:`CanonValidator` from *config*, or ``None`` when disabled.

    Reads ``[auto] canon_check_every`` (default 3) and ``max_canon_revisions``
    (default 1); reads the creative token budget from ``[coder]`` via
    ``_cfg_mode``.  Returns ``None`` when ``canon_check_every <= 0`` so callers
    can treat the whole feature as off via one config key.
    """
    every = config.getint("auto", "canon_check_every", fallback=3)
    if every <= 0:
        logger.info("CanonValidator: disabled (canon_check_every <= 0).")
        return None

    max_rev = config.getint("auto", "max_canon_revisions", fallback=1)
    max_claims = config.getint("auto", "canon_max_claims", fallback=12)

    from tools.auto.utils import _cfg_mode
    from tools.auto.summary_memory import _make_llm_call

    num_ctx_str = _cfg_mode(config, "coder", "num_ctx", task_mode, fallback=None)
    if num_ctx_str is None:
        active = config.get("api", "active", fallback="local")
        num_ctx_str = config.get(f"api_{active}", "num_ctx", fallback="0")
    num_ctx = int(num_ctx_str)
    max_tokens = int(_cfg_mode(config, "coder", "max_tokens", task_mode, fallback="800"))

    if broker is None:
        try:
            from tools.auto.context_broker import ContextBroker
            broker = ContextBroker(
                max_symbols=config.getint("context_broker", "max_symbols", fallback=20),
            )
        except Exception:  # noqa: BLE001
            broker = None

    llm_call = _make_llm_call(config, task_mode=task_mode)

    return CanonValidator(
        llm_call,
        broker=broker,
        base_dir=base_dir,
        canon_check_every=every,
        max_canon_revisions=max_rev,
        synopsis_path=synopsis_path,
        max_claims=max_claims,
        num_ctx=num_ctx,
        max_tokens=max_tokens,
    )
