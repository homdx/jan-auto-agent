"""tools/auto/summary_memory.py — AUTO-CR-5: Summary Memory.

Implements three things mandated by the epic:

1. **Staged, bounded compression** (`SummaryMemory.summarize_chapter`).
   - Short chapters → single LLM pass.
   - Long chapters → paragraph-aligned chunks, each summarised, then the
     chunk-summaries are themselves summarised.  This "recursion" is
     **hard-capped** at ``max_compression_passes`` total passes (default 2).
     When the cap is hit the current best text is returned as-is and a
     WARNING is logged.  No unbounded "compress until small enough" loop is
     permitted anywhere in this module.

2. **Fidelity verifier** (`SummaryFidelityVerifier.verify_and_fix`).
   - Re-reads the source chapter (or chunks) alongside the produced summary
     and asks the model to emit ``FIX: <correction>`` lines for anything
     omitted, contradicted, or distorted.
   - ``FIX:`` lines are appended/applied; the loop re-verifies.
   - **Hard-capped** at ``max_fidelity_rounds`` (default 2).  On the final
     round whatever fixes came back are applied and we stop regardless of
     whether the model still returns fixes.
   - fail-open: an unparseable verifier reply is treated as ``OK`` (logged).

3. **Persistence + idempotent hook** (`SummaryMemory.update`).
   - Reads the chapter file, runs compression → fidelity, then writes /
     replaces exactly one section in ``synopsis.md`` keyed by the chapter
     filename using ``<!-- BEGIN / END -->`` markers.  Re-running on the
     same chapter replaces in place (never duplicates).

Public surface
--------------
    from tools.auto.context_assembler import ContextAssembler  # consumer
    from tools.auto.summary_memory import (
        SummaryMemory,
        SummaryFidelityVerifier,
        make_summary_memory,
    )

    mem = make_summary_memory(config, base_dir=\"/path/to/project\")
    mem.update(\"chapter_07.md\", base_dir=\"/path/to/project\")

The ``llm_call`` parameter accepted by both classes is a simple callable::

    def llm_call(system: str, user: str) -> str: ...

This interface makes the classes trivially testable with stub functions.

Synopsis section format (matches ``ContextAssembler``'s reader)::

    <!-- BEGIN chapter_07.md -->
    ## chapter_07.md
    - <verified fact>
    - <verified fact>
    <!-- END chapter_07.md -->

Spec reference: AUTO-CR-5
"""
from __future__ import annotations

import configparser
import difflib
import logging
import re
import ssl
from pathlib import Path
from typing import Callable

from tools.auto.utils import atomic_write_text, chars_per_token

# Module-level import of LlmSettings, so type hints resolve under postponed evaluation (bugfix).
from tools.auto.llm_profile import LlmSettings
from tools.config_safe import safe_getboolean, safe_getfloat, safe_getint

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

_INSTRUCTION_OVERHEAD_TOKENS = 300

# Headroom in the summary budget for the system prompt and the pass-2 wrapper.
_SUMMARISE_OVERHEAD_TOKENS = 200

# Synopsis section marker format — must match ContextAssembler._SECTION_RE.
_SECTION_BEGIN = "<!-- BEGIN {name} -->"
_SECTION_END   = "<!-- END {name} -->"
_SECTION_RE = re.compile(
    # REL-6 #53: a name may hold spaces ("My Novel/chapter_01.md") — `\S+` wrote such a
    # section and never read it back, so there was no STORY SO FAR.
    r"<!--[^\S\n]*BEGIN[^\S\n]+(?P<name>[^\r\n]+?)[^\S\n]*-->(?P<body>.*?)<!--[^\S\n]*END[^\S\n]+(?P=name)[^\S\n]*-->",
    re.DOTALL,
)

# Prompts — line-oriented, fail-open.
_SYSTEM_SUMMARISE = (
    "You are a story archivist. "
    "List the durable facts of this chapter as short bullet lines: "
    "events, who/where, state changes, promises and setups. "
    "No prose, no commentary, no preamble, no parentheses, no self-evaluation "
    "— each line is one plain fact only."
)

_SYSTEM_FIDELITY = (
    "You are a careful fact-checker for a story synopsis. "
    "Compare the SUMMARY with the SOURCE text. "
    "If every bullet in the SUMMARY is faithful to the SOURCE and nothing "
    "important is missing, reply with exactly: OK\n"
    "Otherwise, output the CORRECTED summary as a clean bullet list — one fact "
    "per line starting with '• ' — fixing or removing any inaccurate bullet and "
    "adding any missing key fact. Output ONLY the corrected bullet list: no "
    "commentary, no explanations, no 'FIX:' prefixes, no parentheses, no "
    "self-evaluation about the bullets, no preamble. Keep it concise."
)

# ── LlmCall type alias ────────────────────────────────────────────────────────

LlmCall = Callable[[str, str], str]   # (system, user) -> response_text


# ── helpers ───────────────────────────────────────────────────────────────────

def _chunk_paragraphs(text: str, max_chars: int) -> list[str]:
    """Split *text* on paragraph breaks (blank lines) so each chunk stays ≤
    *max_chars*.  A single paragraph that exceeds *max_chars* is kept as its
    own chunk (we never split mid-paragraph).
    """
    # REP-CHUNK-BLANK: a blank line holding spaces is a paragraph break too; "\n   \n" kept
    # a whole chapter in one paragraph and sent it to the model in one oversized call.
    paragraphs = re.split(r"\n[^\S\n]*\n\s*", text.strip())
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        addition = len(para) + (2 if current else 0)   # "\n\n" joiner cost
        if current and current_len + addition > max_chars:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = len(para)
        else:
            current.append(para)
            current_len += addition

    if current:
        chunks.append("\n\n".join(current))
    return chunks or [text]


_META_PAREN_MARKERS = (
    "bullet", "remove", "correct", "original", "accurate", "partially",
    "should", "relevant", "mention", "grammat", "implies", "does not",
    "doesn't", "not explicitly", "пункт", "удалить", "ориги", "ошибоч",
    "следует", "граммат", "подразум", "уточня", "добавляет",
)


# HARDEN-27: every marker above is a prefix *family* on purpose ("grammat"
# covers grammatical and grammatically, "ориги" covers оригинал and оригинальный),
# and the old test was a plain substring search —
# ``any(k in inner for k in _META_PAREN_MARKERS)``. A family root therefore
# fired on any ordinary word that merely begins with the same letters:
# "should" in *shoulder*, "correct" in *correctional*, "bullet" in *bulletin*,
# "remove" in *removal*, "original" in *originality*, "пункт" in *пунктуация*.
# The bullet that owned that fact was then deleted from synopsis.md — the
# story's only long-term memory — with nothing logged.
#
# A marker now matches a whole word of the parenthetical, not a piece of one:
# at the start of a word, and only through the word forms the commentary
# actually uses — should / shouldn't, correct / corrected / correctly /
# correction / incorrect, remove / removed, relevant / irrelevant. The dict
# below is that rule, keyed by the single-word families; the two-word markers
# ("does not", "not explicitly") match as phrases on word boundaries.
#
# CC review fixes 48: every form below holds its family's marker, so a word is
# commentary only where the substring test also called it so — the rule moved
# the boundary and must not reach further. HARDEN-27 had listed forms the old
# test never matched, and the story fact went from synopsis.md again:
# "(он добавил соль в суп)", "(деревня удалена от города)", "(ему следовало
# бежать)", "(removing his helmet)", "(he went to grammar school)", "(as his
# tone implied)". The one exception is "удалён" / "удалены", which the
# HARDEN-27 follow-up pins as commentary, and "удален", the same word with е.
_META_MARKER_WORD_FORMS: dict[str, tuple[str, ...]] = {
    "bullet":    ("bullet", "bullets"),
    "remove":    ("remove", "removes", "removed"),
    # "corrections" is not here (AUDIT-23): "(her brother, a corrections
    # officer)", "(the department of corrections)" are story facts. As
    # commentary it is "(corrections: …)", matched as a phrase below. The
    # singular stays (CC review fixes 50): "(correction needed)", "(needs
    # correction)", "(minor correction)" are the model's notes to itself, and
    # with the word gone they went into synopsis.md.
    "correct":   ("correct", "corrects", "corrected", "correcting", "correctly",
                  "correction", "incorrect", "incorrectly"),
    "original":  ("original", "originals", "originally", "unoriginal"),
    "accurate":  ("accurate", "accurately", "inaccurate", "inaccurately"),
    "partially": ("partially",),
    "should":    ("should", "shouldn't"),
    "relevant":  ("relevant", "irrelevant"),
    "mention":   ("mention", "mentions", "mentioned", "mentioning"),
    "grammat":   ("grammatical", "grammatically"),
    "implies":   ("implies",),
    "doesn't":   ("doesn't",),
    "пункт":      ("пункт", "пункта", "пункту", "пункте", "пункты", "пунктам",
                  "пунктами", "пунктах", "пунктов", "пунктом"),
    "удалить":    ("удалить", "удалён", "удален", "удалены"),
    "ориги":      ("оригинал", "оригинала", "оригиналу", "оригинале", "оригиналом",
                  "оригиналы", "оригинальных", "оригинальный", "оригинальная",
                  "оригинальное", "оригинальные"),
    "ошибоч":     ("ошибочный", "ошибочная", "ошибочное", "ошибочные", "ошибочно",
                  "ошибочность", "ошибочности"),
    "следует":    ("следует",),
    "граммат":    ("грамматика", "грамматики", "грамматику", "грамматический",
                  "грамматическая", "грамматическое", "грамматические",
                  "грамматично", "грамматичность"),
    "подразум":   ("подразумевается", "подразумеваются", "подразумевает",
                  "подразумевать"),
    "уточня":     ("уточнять", "уточняю", "уточняет", "уточняется", "уточняются"),
    "добавляет":  ("добавляет",),
}

# Every word form the commentary uses, flat, so the test below is one set lookup.
_META_MARKER_WORDS = frozenset(w for forms in _META_MARKER_WORD_FORMS.values()
                               for w in forms)

# The two-word markers, as phrases. Bounded on both sides so "does not" cannot
# fire inside a longer word.
_META_PAREN_PHRASES: tuple[str, ...] = (
    r"(?<!\w)does\s+not(?!\w)",
    r"(?<!\w)not\s+explicitly(?!\w)",
    # "(correction: …)" / "(corrections — …)": the noun as the commentary's label.
    r"^\s*corrections?\s*(?::|-|—|–)",
)
_META_PAREN_PHRASE_RE = re.compile("|".join(_META_PAREN_PHRASES))

# One word of the parenthetical, letter for letter (a-z and Cyrillic), carrying
# the single apostrophe the commentary actually writes: shouldn't, doesn't.
_META_PAREN_WORD_RE = re.compile(r"[a-z\u0430-\u044f\u0451]+(?:'[a-z\u0430-\u044f\u0451]+)*")


def _is_meta_commentary(inner: str) -> bool:
    """True when a parenthetical reads as the model's commentary on its own
    bullet rather than a story fact (HARDEN-27). Never raises."""
    if not inner:
        return False
    lowered = inner.lower().replace("\u2019", "'")   # “shouldn’t” → “shouldn't”
    if _META_PAREN_PHRASE_RE.search(lowered):
        return True
    for word in _META_PAREN_WORD_RE.findall(lowered):
        # "this bullet's numbers are wrong": the possessive is the marker word's own form.
        if word in _META_MARKER_WORDS or (word.endswith("'s") and word[:-2] in _META_MARKER_WORDS):
            return True
    return False


def _strip_meta_parentheticals(fact: str) -> str:
    """Remove trailing/embedded ``(...)`` segments that are self-evaluation
    commentary rather than story facts (e.g. '(this bullet should be removed
    because...)'). Keeps parentheticals that look like genuine content.

    HARDEN-27: a marker matches a whole word, never a piece of one, so
    "(his left shoulder, ...)" and "(a correctional facility upstate)" are
    story facts and stay put. Only the commentary parenthetical of a bullet is
    removed; the others are left byte-for-byte. Runs in one pass over the text,
    so a 100 000-character parenthetical costs linear time. Fail-open: any
    surprise returns the bullet unchanged rather than raising into the run.
    """
    import re as _re

    try:
        def _repl(m: "_re.Match[str]") -> str:
            return "" if _is_meta_commentary(m.group(1)) else m.group(0)

        return _re.sub(r"\s*\(([^()]*)\)", _repl, fact).strip()
    except Exception:
        return fact


def _is_ok_sentinel(reply: str) -> bool:
    """True only if *reply* IS the one-word ``OK`` sentinel.

    FIX-2 #9: this used to be ``first_line.startswith("OK") and len(reply) <= 4``,
    which accepted ``"OKAY"`` — it starts with ``OK`` and is 4 characters long.
    ``OKAY`` is a real English word a model can easily emit instead of the bare
    ``OK`` the prompt asks for, and it is an interjection, not an assertion
    that the summary is faithful. Treating it as a pass stopped the loop
    before the correction the model was about to send was ever read.

    A shorter length bound alone is not enough either: ``len(reply) <= 4``
    with a first-line check still lets a two-line reply like ``"OK\\nX"``
    (4 characters total) through, silently discarding whatever the model put
    on the second line. The sentinel is one word, so match the *entire*
    reply against it exactly (case- and whitespace-insensitively, which is
    all the tolerance the old length bound ever provided for) rather than
    bounding the length of a multi-line reply.
    """
    return reply.strip().upper() == "OK"


def _clean_bullet_list(reply: str) -> str:
    """Normalise a verifier reply into a clean bullet list, or "" if unusable.

    Lines that START with a bullet/number marker (•, -, *, or 'N.') are
    accepted as facts. A stray 'FIX:' prefix is tolerated, and AUTO-CR-16
    meta-commentary parentheticals are stripped.

    AUTO-BUG-5 fix: if NO line has a marker at all, fall back to treating
    each short, non-empty line as one fact — a small local model very
    plausibly ignores the "one bullet per line" formatting instruction and
    just writes plain sentences, one fact per line. Without this fallback
    that entire (otherwise perfectly usable) extraction was silently
    discarded and the caller kept believing there were "no new facts". The
    fallback still fails open (returns "") when the reply doesn't look like
    a short fact list — e.g. full narrative prose / a refusal — to avoid
    accidentally ingesting chapter text as "facts".

    Returns bullets joined with newlines, each prefixed '• '.
    """
    import re as _re
    _marker = _re.compile(r"^\s*(?:[\u2022\-\*]|\d+[.)])\s+")
    out: list[str] = []
    any_marker = False
    for raw in reply.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not _marker.match(line):
            if line[:4].upper() == "FIX:":
                fact = _strip_meta_parentheticals(line[4:].strip())
                if fact:
                    out.append(f"• {fact}")
            continue
        any_marker = True
        fact = _marker.sub("", line, count=1).strip()
        if fact[:4].upper() == "FIX:":
            fact = fact[4:].strip()
        fact = _strip_meta_parentheticals(fact)
        if fact:
            out.append(f"• {fact}")

    if out or any_marker:
        return "\n".join(out)

    # ── Fallback: no bullet markers anywhere in the reply ───────────────────
    candidate_lines = [ln.strip() for ln in reply.splitlines() if ln.strip()]
    if not (2 <= len(candidate_lines) <= 20):
        return ""  # too few/many lines to plausibly be "one fact per line"
    if any(len(ln) > 220 for ln in candidate_lines):
        return ""  # looks like narrative prose, not short facts — fail open
    fallback: list[str] = []
    for ln in candidate_lines:
        fact = _strip_meta_parentheticals(ln)
        if fact:
            fallback.append(f"• {fact}")
    return "\n".join(fallback)


# ── SummaryFidelityVerifier ───────────────────────────────────────────────────

class SummaryFidelityVerifier:
    """Verifies and corrects a chapter summary against the source text.

    Parameters
    ----------
    llm_call:
        ``(system, user) -> str`` — any callable that sends a prompt to the
        model and returns the raw text reply.
    max_fidelity_rounds:
        Hard cap on correction rounds.  When the cap is reached whatever fixes
        the model last returned are applied and we stop — no unbounded loop.
    """

    def __init__(
        self,
        llm_call: LlmCall,
        *,
        max_fidelity_rounds: int = 2,
    ) -> None:
        self._llm = llm_call
        self._max_rounds = max(1, int(max_fidelity_rounds))

    def verify_and_fix(self, chapter_text: str, summary: str) -> str:
        """Return a corrected *summary*, running at most ``max_fidelity_rounds``
        rounds.  Never raises — fail-open on any unparseable reply.

        AUTO-CR-15: each round the verifier returns either ``OK`` or the FULL
        corrected bullet list, which REPLACES the working summary. (The old
        design appended ``[corrected] …`` annotations, which never fixed the
        original wrong bullet, never converged — so it always burned every
        round — and polluted synopsis.md with verbose meta-commentary.)
        """
        from tools.auto.utils import detect_language, language_instruction
        lang_instr = language_instruction(detect_language(chapter_text))
        system = _SYSTEM_FIDELITY + (("\n" + lang_instr) if lang_instr else "")
        if lang_instr:
            # The language instruction swallows the literal OK sentinel, so the early exit never fires; the check is kept in the language-neutral form (same bug class as the continuity validator).
            system += (
                "\nEXCEPTION TO THE LANGUAGE RULE ABOVE: if no correction is "
                "needed, reply with exactly the English word OK — do not "
                "translate or transliterate it into another language."
            )

        current = summary
        for rnd in range(1, self._max_rounds + 1):
            user_msg = f"SOURCE:\n{chapter_text}\n\nSUMMARY:\n{current}"
            print(
                f"\n🔎 [SummaryFidelityVerifier — round {rnd}] full text sent to validation:\n"
                f"{'-' * 80}\n{user_msg}\n{'-' * 80}"
            )
            try:
                reply = self._llm(system, user_msg) or ""
            except Exception as exc:
                logger.warning(
                    "SummaryFidelityVerifier: LLM error on round %d: %s — keeping current.",
                    rnd, exc,
                )
                break

            reply = reply.strip()
            if not reply:
                logger.warning(
                    "SummaryFidelityVerifier: empty reply on round %d — keeping current.", rnd,
                )
                break

            if _is_ok_sentinel(reply):
                logger.debug("SummaryFidelityVerifier: OK on round %d.", rnd)
                break

            corrected = _clean_bullet_list(reply)
            if not corrected:
                # Reply was neither a clean OK nor a usable bullet list.
                logger.warning(
                    "SummaryFidelityVerifier: round %d reply unusable — keeping current. "
                    "Reply preview: %.120s", rnd, reply,
                )
                break

            if corrected.strip() == current.strip():
                logger.debug(
                    "SummaryFidelityVerifier: round %d produced no change — done.", rnd,
                )
                break

            logger.info(
                "SummaryFidelityVerifier: round %d — replaced summary with corrected list.", rnd,
            )
            diff = "\n".join(
                difflib.unified_diff(
                    current.splitlines(),
                    corrected.splitlines(),
                    fromfile=f"summary (before round {rnd})",
                    tofile=f"summary (after round {rnd})",
                    lineterm="",
                )
            )
            print(
                f"\n✏️  [SummaryFidelityVerifier — round {rnd}] summary changed:\n"
                f"{'-' * 80}\n{diff}\n{'-' * 80}"
            )
            current = corrected

            if rnd == self._max_rounds:
                logger.warning(
                    "SummaryFidelityVerifier: reached max_fidelity_rounds=%d — "
                    "accepting current summary.", self._max_rounds,
                )

        return current


# ── SummaryMemory ─────────────────────────────────────────────────────────────

class SummaryMemory:
    """Compresses a chapter to a bullet-fact summary, verifies it, and persists
    it as a section in ``synopsis.md``.

    Parameters
    ----------
    llm_call:
        ``(system, user) -> str``.
    max_compression_passes:
        Hard cap on total summarisation passes.  Default 2 (one chunked pass +
        one merge pass).  The loop can NEVER run more than this many passes.
    max_fidelity_rounds:
        Forwarded to :class:`SummaryFidelityVerifier`.
    num_ctx:
        Model context window in tokens — used to size chunks and determine
        whether a chapter needs chunking.
    max_tokens:
        Output budget in tokens — reserved out of the context window.
    base_dir:
        Repository root.  ``synopsis.md`` is resolved relative to this.
    synopsis_path:
        Relative path (from *base_dir*) to the synopsis file.
    """

    def __init__(
        self,
        llm_call: LlmCall,
        *,
        max_compression_passes: int = 2,
        max_fidelity_rounds: int = 2,
        num_ctx: int = 8192,
        max_tokens: int = 2048,
        base_dir: str | Path = ".",
        synopsis_path: str = "synopsis.md",
    ) -> None:
        self._llm = llm_call
        self._max_passes = max(1, int(max_compression_passes))
        self._base_dir = Path(base_dir)
        self._synopsis_path = self._base_dir / synopsis_path
        self._verifier = SummaryFidelityVerifier(
            llm_call, max_fidelity_rounds=max_fidelity_rounds,
        )
        # Budget for the input of one summary call: output and overhead are reserved first.
        self._input_tokens = max(
            0,
            (int(num_ctx) if num_ctx else 4096)
            - (int(max_tokens) if max_tokens else 800)
            - _INSTRUCTION_OVERHEAD_TOKENS
            - _SUMMARISE_OVERHEAD_TOKENS,
        )

    def _chunk_budget_chars(self, sample_text: str = "") -> int:
        """Char budget for a single summarisation call, sized to *sample_text*'s
        script (see ``chars_per_token``)."""
        return max(200, int(self._input_tokens * chars_per_token(sample_text)))

    # ── Public API ────────────────────────────────────────────────────────────

    def summarize_chapter(self, chapter_text: str) -> str:
        """Compress *chapter_text* to bullet facts.

        Runs at most ``max_compression_passes`` *compression rounds* (recursion
        levels). A chunked round issues one LLM call per chunk, so the total
        number of LLM calls may exceed ``max_compression_passes``; the bound is
        on recursion depth, which is what guarantees the process always
        terminates and never loops. Returns the best summary available when the
        cap is hit, even if it is still long. Never raises.
        """
        return self._compress(chapter_text, passes_used=0)

    def update(self, chapter_file: str, base_dir: "str | Path | None" = None) -> None:
        """Read *chapter_file*, compress it, verify fidelity, and write / replace
        its section in ``synopsis.md``.

        Parameters
        ----------
        chapter_file:
            Relative path from *base_dir* to the chapter file (e.g.
            ``\"chapter_07.md\"``).
        base_dir:
            Override for the base directory set at construction time.
            If ``None``, uses the directory passed to ``__init__``.
        """
        _base = Path(base_dir) if base_dir is not None else self._base_dir
        chapter_path = _base / chapter_file
        # A chapter path outside the base directory is refused before it is read (BUGFIX).
        try:
            chapter_path.resolve().relative_to(_base.resolve())
        except ValueError:
            logger.error(
                "SummaryMemory.update: chapter_file %r escapes base_dir — "
                "skipping synopsis update.", chapter_file,
            )
            return
        try:
            # AUDIT3-9: in the chapter's own codec, so a cp1251 chapter is not
            # summarized from a run of U+FFFD.
            chapter_text, _enc, _eol = _read_in_own_codec(chapter_path, lossy_ok=True)
        except OSError as exc:
            logger.error(
                "SummaryMemory.update: cannot read %s: %s — skipping synopsis update.",
                chapter_file, exc,
            )
            return

        if not chapter_text.strip():
            logger.warning(
                "SummaryMemory.update: %s is empty — skipping synopsis update.", chapter_file,
            )
            return

        summary = self.summarize_chapter(chapter_text)
        verified = self._verifier.verify_and_fix(chapter_text, summary)
        # Residual meta-commentary is stripped, and the verified text is kept if the cleanup would empty it (AUTO-CR-16).
        cleaned = _clean_bullet_list(verified)
        body = cleaned or verified

        # A bare verdict word or an implausibly short summary is never written as a chapter summary (AUTO-BUG-6).
        _stripped = body.strip()
        _is_bare_verdict = _stripped.upper().rstrip(".:!") in {"APPROVED", "OK", "REVISE"}
        # A reply the cleaner rejects is not written, even when it has several lines (BUGFIX).
        _unclean = not cleaned
        if not _stripped or _is_bare_verdict or _unclean:
            logger.warning(
                "SummaryMemory.update: summary for %s looks invalid (%r) — "
                "not writing it to synopsis.md (keeping previous content, if any).",
                chapter_file, _stripped[:60],
            )
            return

        self._write_section(chapter_file, body)

    # ── Internal compression logic ────────────────────────────────────────────

    def _compress(self, text: str, passes_used: int) -> str:
        """Recursively compress *text* up to ``max_compression_passes`` total."""
        if passes_used >= self._max_passes:
            logger.warning(
                "SummaryMemory: reached max_compression_passes=%d — "
                "returning current best text (length %d chars).",
                self._max_passes, len(text),
            )
            return text

        chunk_budget_chars = self._chunk_budget_chars(text)
        fits_in_budget = len(text) <= chunk_budget_chars

        if fits_in_budget:
            # Single pass.
            result = self._summarise_once(text)
            if result:
                return result
            # LLM returned nothing — return the text as-is (fail-open).
            logger.warning("SummaryMemory: summarise returned empty — using input.")
            return text

        # Chunked pass: split, summarise each chunk, then merge.
        chunks = _chunk_paragraphs(text, chunk_budget_chars)
        logger.info(
            "SummaryMemory: chapter too large (%d chars > %d budget) — "
            "splitting into %d chunks (pass %d/%d).",
            len(text), chunk_budget_chars,
            len(chunks), passes_used + 1, self._max_passes,
        )
        chunk_summaries: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            cs = self._summarise_once(chunk)
            if cs:
                chunk_summaries.append(cs)
            else:
                logger.warning(
                    "SummaryMemory: chunk %d/%d returned empty — skipping.", i, len(chunks),
                )

        if not chunk_summaries:
            logger.warning("SummaryMemory: all chunks returned empty — using raw text.")
            return text[:chunk_budget_chars]

        merged = "\n".join(chunk_summaries)
        # Recurse: try to compress the merged chunk-summaries into one summary.
        return self._compress(merged, passes_used + 1)

    def _summarise_once(self, text: str) -> str:
        """Make a single summarisation LLM call.  Returns ``\"\"`` on failure."""
        from tools.auto.utils import detect_language, language_instruction
        _sys = _SYSTEM_SUMMARISE
        _instr = language_instruction(detect_language(text))
        if _instr:
            _sys = _SYSTEM_SUMMARISE + " " + _instr
        user_msg = f"CHAPTER TEXT:\n{text}"
        try:
            reply = self._llm(_sys, user_msg) or ""
            return reply.strip()
        except Exception as exc:
            logger.warning("SummaryMemory: LLM error during summarisation: %s", exc)
            return ""

    # ── Synopsis persistence ──────────────────────────────────────────────────

    def _write_section(self, chapter_file: str, summary_body: str) -> None:
        """Write / replace the ``<!-- BEGIN/END chapter_N.md -->`` section in
        ``synopsis.md``.  Idempotent: running again on the same chapter
        replaces the existing section — never duplicates it.
        """
        section_text = (
            f"{_SECTION_BEGIN.format(name=chapter_file)}\n"
            f"## {chapter_file}\n"
            f"{summary_body}\n"
            f"{_SECTION_END.format(name=chapter_file)}"
        )

        # Read existing synopsis — in its own codec and with its own line ends
        # (AUDIT3-9): a cp1251 synopsis read with errors="replace" and written
        # back as UTF-8 lost every earlier chapter's text to U+FFFD, and a CRLF
        # file came back LF.
        encodings: "tuple[str, ...]" = ("utf-8",)
        eol = "\n"
        try:
            existing, encodings, eol = _read_in_own_codec(self._synopsis_path)
        except FileNotFoundError:
            existing = ""
        except (OSError, UnicodeError) as exc:
            logger.error(
                "SummaryMemory: %s cannot be read without loss (%s) — left "
                "untouched; the section for %s is not written",
                self._synopsis_path, exc, chapter_file,
            )
            return

        # Replace existing section if present, otherwise append.
        pattern = re.compile(
            rf"<!--\s*BEGIN\s+{re.escape(chapter_file)}\s*-->.*?<!--\s*END\s+{re.escape(chapter_file)}\s*-->",
            re.DOTALL,
        )
        # Section text is inserted as a plain string, so backslashes in a summary are not read as group references (bugfix).
        if pattern.search(existing):
            new_content = pattern.sub(lambda _m: section_text, existing)
        else:
            separator = "\n\n" if existing.strip() else ""
            new_content = existing.rstrip() + separator + section_text + "\n"

        if eol != "\n":
            new_content = new_content.replace("\n", eol)
        encoding = _first_codec_holding(encodings, new_content)
        if encoding is None:
            # REL-2: an arrow, a minus sign or an emoji in a summary is common, and no legacy
            # codec of the list holds it; the section was dropped and the chapter left a hole
            # in the continuity memory for good. The text is already decoded, so the whole
            # file is written as UTF-8 instead, with its own line ends.
            logger.warning(
                "SummaryMemory: the section for %s holds characters %s's own codec "
                "(%s) cannot store — the synopsis is rewritten as UTF-8", chapter_file,
                self._synopsis_path, encodings[0],
            )
            encoding = "utf-8"
        try:
            self._synopsis_path.parent.mkdir(parents=True, exist_ok=True)
            # The synopsis is written atomically, so a kill mid-write cannot leave an incomplete continuity context (bugfix).
            atomic_write_text(self._synopsis_path, new_content,
                              **_codec_write_kwargs(encoding, eol))
            logger.info(
                "SummaryMemory: wrote synopsis section for %s → %s",
                chapter_file, self._synopsis_path,
            )
        except OSError as exc:
            logger.error(
                "SummaryMemory: cannot write %s: %s", self._synopsis_path, exc,
            )


def _read_in_own_codec(path: Path, *, lossy_ok: bool = False):
    """``(text with LF line ends, candidate codecs, the file's line end)``.

    AUDIT3-9: the codec is the one ``/edit`` and the Coder choose from the
    file's bytes (``tools.actions._edit_write_encodings``): UTF-8 when the
    bytes are UTF-8 (a BOM stays a ``\\ufeff`` character, as before), else
    the legacy codec that describes them. Strict: a byte none of them reads
    raises ``UnicodeError`` — unless *lossy_ok*, for text that is only read,
    never written back.
    """
    raw = Path(path).read_bytes()
    # REL-2: a UTF-16 file (PowerShell's `>` writes one) was probed as cp1251, which decodes
    # almost any byte, and the new section went in as single bytes between the UTF-16 ones.
    # Its BOM names it; the BOM stays a `\ufeff` character and is written back the same way.
    for bom, codec in ((b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if raw.startswith(bom) and not raw.startswith(b"\xff\xfe\x00\x00"):
            text = raw.decode(codec, errors="replace" if lossy_ok else "strict")
            eol = "\r\n" if "\r\n" in text else "\n"
            return text.replace("\r\n", "\n"), (codec,), eol
    try:
        from tools.actions import _edit_write_encodings
        encodings = tuple(_edit_write_encodings(str(path))) or ("utf-8",)
    except Exception:  # noqa: BLE001 — no probe: UTF-8, as before
        encodings = ("utf-8",)
    if encodings[0] != "utf-8" and _mostly_utf8(raw):
        # CC review fixes 50: UTF-8 with a broken byte or two is UTF-8. Read as
        # the legacy codec the probe falls back to, its Cyrillic was mojibake no
        # legacy codec of the list could take a new section in — and the
        # synopsis was never updated again. Lossy for those bytes only, as
        # every read was before AUDIT3-9.
        logger.warning("%s is UTF-8 with %d undecodable byte(s) — read as UTF-8, "
                       "they become U+FFFD", path, raw.decode("utf-8", "replace").count("\ufffd"))
        text = raw.decode("utf-8", errors="replace")
        eol = "\r\n" if "\r\n" in text else "\n"
        return text.replace("\r\n", "\n"), ("utf-8",), eol
    try:
        text = raw.decode(encodings[0])
    except UnicodeError:
        if not lossy_ok:
            raise
        text = raw.decode("utf-8", errors="replace")
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), encodings, eol


def _mostly_utf8(raw: bytes) -> bool:
    """True when *raw* is UTF-8 text with a few undecodable bytes (CC review fixes 50).

    Valid multi-byte characters must be at least twice the broken bytes: a
    cp1251 or cp1252 file decoded as UTF-8 breaks on nearly every non-ASCII
    letter and makes a valid sequence only by accident (a capital letter before
    "ё" or "»"), so it stays with its own codec.
    """
    text = raw.decode("utf-8", errors="replace")
    broken = text.count("\ufffd") - raw.count("\ufffd".encode("utf-8"))
    valid = sum(1 for ch in text if ord(ch) > 127 and ch != "\ufffd")
    return broken > 0 and valid >= 2 * broken


def _codec_write_kwargs(encoding: str, eol: str) -> dict:
    """``atomic_write_text`` keywords for a file in *encoding* with *eol* ends.

    A plain UTF-8, LF file is written exactly as before (no keywords — the
    platform's newline translation and all); only a legacy codec or a CRLF
    file gets ``newline=""`` and its codec, so its bytes come back as they were.
    """
    if encoding == "utf-8" and eol == "\n":
        return {}
    return {"newline": "", "encoding": encoding}


def _first_codec_holding(encodings, text: str):
    """The first of *encodings* that can store *text*, or None."""
    for encoding in encodings:
        try:
            text.encode(encoding)
            return encoding
        except (UnicodeError, LookupError):
            continue
    return None


# ── Factory ───────────────────────────────────────────────────────────────────

def _default_llm_settings(
    config: configparser.ConfigParser,
    task_mode: str = "creative",
) -> "LlmSettings":
    """Build the shared-provider :class:`LlmSettings` exactly as
    ``_make_llm_call`` used to compute its locals inline.

    GATE3-PROFILE-2. Split out so :func:`_make_llm_call` and
    :func:`tools.auto.gate_registry.build_validators` compute the *same*
    defaults the same way — the latter needs them as the innermost
    fallback of the ``<gate>_llm_profile → validator_llm_profile →
    [api_{active}]`` chain, without duplicating this parsing.

    Deliberately does NOT strip ``base_url``'s trailing slash (unlike a
    resolved named profile, where :func:`tools.auto.llm_profile.
    resolve_llm_profile` strips it): the ollama branch below passes
    ``base_url`` as-is into ``ollama_chat_url``, and stripping it here
    would be an observable, untested behaviour change for every existing
    config that predates GATE3-PROFILE-2.
    """
    from tools.auto.llm_profile import LlmSettings
    from tools.auto.utils import _cfg_mode

    active = config.get("api", "active", fallback="local")
    api_sec = f"api_{active}"

    base_url   = config.get(api_sec, "base_url",   fallback="http://localhost:11434")
    api_key    = config.get(api_sec, "api_key",    fallback="ollama")
    model      = config.get(api_sec, "model",      fallback="llama3.1:8b")
    api_format = config.get(api_sec, "api_format", fallback="ollama")
    verify_ssl = safe_getboolean(config, "api", "verify_ssl", fallback=True)

    num_ctx_str = _cfg_mode(config, "coder", "num_ctx", task_mode, fallback=None)
    if num_ctx_str is None:
        num_ctx_str = config.get(api_sec, "num_ctx", fallback="0")
    num_ctx = int(num_ctx_str)

    max_tokens_str = _cfg_mode(config, "coder", "max_tokens", task_mode, fallback="800")
    max_tokens = int(max_tokens_str)

    temperature = safe_getfloat(config, "inner_loop", "temperature", fallback=0.2)
    # Thinking output is off by default for the synopsis, so reasoning text is not written into it (fable follow-up 3).
    think = safe_getboolean(config, "summary_memory", "think", fallback=False)

    # Reasoning depth is honoured from [api]; response_format is not, since the summary and verdict callers parse free text (GATE3-PROFILE).
    think_effort_enabled = safe_getboolean(
        config, "api", "think_effort_enabled", fallback=False)
    think_effort = (
        (config.get("api", "think_effort", fallback="").strip() or None)
        if think_effort_enabled else None
    )

    return LlmSettings(
        base_url=base_url,
        api_key=api_key,
        model=model,
        api_format=api_format,
        verify_ssl=verify_ssl,
        think=think,
        temperature=temperature,
        max_tokens=max_tokens,
        num_ctx=num_ctx,
        think_effort_enabled=think_effort_enabled,
        think_effort=think_effort,
    )


def _make_llm_call(
    config: configparser.ConfigParser,
    task_mode: str = "creative",
    settings: "LlmSettings | None" = None,
) -> LlmCall:
    """Build a ``(system, user) -> str`` callable from *config*.

    Uses the same API profile / format / SSL logic as the rest of the
    pipeline.  The LLM call is non-streaming (blocking), matching the
    validator pattern.

    GATE3-PROFILE-2: *settings* is optional. When absent (every call site
    that hasn't opted into a per-validator profile yet), it is built from
    *config* via :func:`_default_llm_settings` — the exact same reads,
    in the exact same order, that used to live inline here — so a config
    without any ``*_llm_profile`` key produces a byte-for-byte identical
    request to before this change. When present (GATE3-PROFILE-2 callers
    that resolved a ``<gate>_llm_profile``/``validator_llm_profile``),
    *settings* is used as-is and *config* is only consulted for the
    request timeout, which is not a per-provider setting.
    """
    import tools.llm_stream as _llm_stream

    if settings is None:
        settings = _default_llm_settings(config, task_mode)

    base_url   = settings.base_url
    api_key    = settings.api_key
    model      = settings.model
    api_format = settings.api_format
    verify_ssl = settings.verify_ssl
    num_ctx    = settings.num_ctx
    max_tokens = settings.max_tokens
    temperature = settings.temperature
    think      = settings.think

    response_format = settings.response_format
    think_effort    = settings.think_effort

    timeout = safe_getint(config, "loop", "timeout_seconds", fallback=300)
    # The [loop] retry budget is read the same way as the timeout, with the request defaults when keys are absent (RUN-10).
    retry_kwargs = _llm_stream.retry_kwargs_from_config(config)

    ssl_context: ssl.SSLContext | None = _llm_stream.make_unverified_context() if not verify_ssl else None

    def _call(system: str, user: str) -> str:
        # The call goes through build_chat_request like every other caller, so the same settings apply (GATE3-PROFILE audit).
        url, headers, payload = _llm_stream.build_chat_request(
            base_url=base_url, api_key=api_key, model=model,
            api_format=api_format, temperature=temperature,
            max_tokens=max_tokens, system=system, user_msg=user,
            num_ctx=num_ctx,
            # think is passed as a real bool, so think_effort is not dead when thinking is on; every other caller passes a bool too.
            think=think,
            response_format=response_format, think_effort=think_effort,
        )
        # <think> is stripped before the synopsis parser, so reasoning is never saved (AUTO-FIX).
        return _llm_stream.strip_think(
            _llm_stream.request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=timeout,
                api_format=api_format,
                ssl_context=ssl_context,
                **retry_kwargs,
            ) or ""
        )

    return _call


def make_summary_memory(
    config: configparser.ConfigParser,
    base_dir: "str | Path" = ".",
    *,
    task_mode: str = "creative",
    synopsis_path: str = "synopsis.md",
) -> SummaryMemory:
    """Build a :class:`SummaryMemory` from *config*.

    Reads ``[auto] max_compression_passes`` and ``max_fidelity_rounds``;
    reads creative-mode token budget from ``[coder]`` via ``_cfg_mode``.

    GATE3-PROFILE-3: also resolves ``[summary] llm_profile`` — SummaryMemory
    runs once per task (not once per attempt, like the five Gate-3
    validators), so it gets a single profile key rather than the
    ``<gate>_llm_profile`` fan-out, falling back straight to
    ``[api_{active}]`` when unset (there is no shared
    ``validator_llm_profile``-style middle step for a call site of one).
    Resolution happens here, via :func:`~tools.auto.llm_profile.
    resolve_llm_profile`, and is deliberately NOT wrapped in a
    try/except: a misconfigured profile must raise here, at construction,
    before any task runs — never be discovered mid-run. A config without
    ``llm_profile`` resolves straight back to the same
    :func:`_default_llm_settings` defaults ``_make_llm_call`` has always
    used, so default behaviour is unchanged, field for field.
    """
    from tools.auto.utils import _cfg_mode
    from tools.auto.llm_profile import resolve_llm_profile

    max_passes  = safe_getint(config, "auto", "max_compression_passes", fallback=2)
    max_fidelity = safe_getint(config, "auto", "max_fidelity_rounds",   fallback=2)

    num_ctx_str = _cfg_mode(config, "coder", "num_ctx", task_mode, fallback=None)
    if num_ctx_str is None:
        active = config.get("api", "active", fallback="local")
        num_ctx_str = config.get(f"api_{active}", "num_ctx", fallback="0")
    num_ctx = int(num_ctx_str)

    max_tokens = int(_cfg_mode(config, "coder", "max_tokens", task_mode, fallback="800"))

    defaults = _default_llm_settings(config, task_mode)
    settings, _ = resolve_llm_profile(
        config, "summary", "llm_profile", defaults=defaults,
    )
    llm_call = _make_llm_call(config, task_mode=task_mode, settings=settings)

    return SummaryMemory(
        llm_call,
        max_compression_passes=max_passes,
        max_fidelity_rounds=max_fidelity,
        num_ctx=num_ctx,
        max_tokens=max_tokens,
        base_dir=base_dir,
        synopsis_path=synopsis_path,
    )
