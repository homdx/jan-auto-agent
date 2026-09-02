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

# Bugfix: "LlmSettings" is used in annotations here but was only imported
# inside _default_llm_settings's body. Under `from __future__ import
# annotations` that stayed dormant until typing.get_type_hints() resolved
# the string against module globals and raised NameError. A module-level
# import is safe (llm_profile.py imports nothing from this package) and a
# TYPE_CHECKING-gated one would not fix it — that block never executes.
from tools.auto.llm_profile import LlmSettings

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

_INSTRUCTION_OVERHEAD_TOKENS = 300

# Reserved headroom inside the summarisation budget for the system prompt and
# the "summarise these chunk-summaries" wrapper prompt in pass 2.
_SUMMARISE_OVERHEAD_TOKENS = 200

# Synopsis section marker format — must match ContextAssembler._SECTION_RE.
_SECTION_BEGIN = "<!-- BEGIN {name} -->"
_SECTION_END   = "<!-- END {name} -->"
_SECTION_RE = re.compile(
    r"<!--\s*BEGIN\s+(?P<name>\S+)\s*-->(?P<body>.*?)<!--\s*END\s+(?P=name)\s*-->",
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
    paragraphs = re.split(r"\n{2,}", text.strip())
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


def _strip_meta_parentheticals(fact: str) -> str:
    """Remove trailing/embedded ``(...)`` segments that are self-evaluation
    commentary rather than story facts (e.g. '(this bullet should be removed
    because...)'). Keeps parentheticals that look like genuine content.
    """
    import re as _re

    def _repl(m: "_re.Match") -> str:
        inner = m.group(1).lower()
        return "" if any(k in inner for k in _META_PAREN_MARKERS) else m.group(0)

    cleaned = _re.sub(r"\s*\(([^()]*)\)", _repl, fact)
    return cleaned.strip()


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
            # Same class of bug as the continuity validator: "output
            # {language} only, do not translate" also swallows the literal
            # "OK" sentinel on rounds where no correction is needed, so the
            # early-exit branch below can never fire for non-English books —
            # every round looks like it "needed a fix" and the loop always
            # bleeds out via max_fidelity_rounds instead of a genuine pass.
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

            first_line = reply.splitlines()[0].strip().upper()
            if first_line.startswith("OK") and len(reply) <= 4:
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
        # Budget for *input* to a single summarisation LLM call.
        # Reserve output budget + overhead; leave _SUMMARISE_OVERHEAD_TOKENS
        # of headroom for the system prompt and wrapper text.
        # Stored as a token count, not a fixed char count: the char budget
        # depends on the text being chunked (Cyrillic tokenizes ~2x denser
        # than Latin — see chars_per_token()), so it's computed per-call in
        # _chunk_budget_chars() rather than fixed at construction time.
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
        # BUGFIX (audit): no validation — an absolute chapter_file discards
        # _base entirely (pathlib's `/` operator returns the right side
        # unchanged when it's absolute), and a relative one containing
        # ".." can escape _base the same way the other base_dir-relative
        # reads in this codebase's Gate 1 stage did before their own fix
        # (see gate1_filter.py / context_broker.py / gate1_grounding.py).
        try:
            chapter_path.resolve().relative_to(_base.resolve())
        except ValueError:
            logger.error(
                "SummaryMemory.update: chapter_file %r escapes base_dir — "
                "skipping synopsis update.", chapter_file,
            )
            return
        try:
            chapter_text = chapter_path.read_text(encoding="utf-8", errors="replace")
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
        # AUTO-CR-16: final cleanup — strip any residual meta-commentary
        # parentheticals / non-bullet noise. Keep the verified text if cleaning
        # would empty it (fail-safe).
        cleaned = _clean_bullet_list(verified)
        body = cleaned or verified

        # AUTO-BUG-6 fix: previously `cleaned or verified` would happily
        # persist whatever `verified` was even when it was obviously not a
        # summary at all (observed in practice: the literal grader verdict
        # "APPROVED" ended up written into synopsis.md as a chapter's
        # "durable fact summary"). Refuse to write anything that looks like
        # a bare verdict word or is implausibly short for a summary, and
        # leave the previous section (if any) untouched instead.
        _stripped = body.strip()
        _is_bare_verdict = _stripped.upper().rstrip(".:!") in {"APPROVED", "OK", "REVISE"}
        # BUGFIX (audit, generalizes the fix above): the previous version of
        # this guard only rejected UNCLEAN content when it was a single
        # line — but _clean_bullet_list() itself already fails open
        # ("") for ANY reply that doesn't look like a fact list, single-
        # line or not (e.g. >20 lines, or a line >220 chars — its own
        # "looks like narrative prose" checks; see its docstring). When
        # the summarizer/verifier failed and `verified` was raw or
        # truncated multi-line chapter text, `cleaned` was correctly ""
        # but this guard let it through anyway because it had more than
        # one line, polluting synopsis.md with un-summarized chapter
        # text. `cleaned` being "" is _clean_bullet_list's own considered
        # verdict that the content isn't usable, regardless of line count.
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

        # Read existing synopsis.
        try:
            existing = self._synopsis_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""

        # Replace existing section if present, otherwise append.
        pattern = re.compile(
            rf"<!--\s*BEGIN\s+{re.escape(chapter_file)}\s*-->.*?<!--\s*END\s+{re.escape(chapter_file)}\s*-->",
            re.DOTALL,
        )
        #
        # Bugfix: this used to be `pattern.sub(section_text, existing)` —
        # passing section_text (which embeds the LLM-generated chapter
        # summary) as re.sub's REPLACEMENT argument. re.sub interprets
        # backslash sequences in a *string* replacement as backreferences
        # (\1, \g<name>, ...); this pattern has no capture groups, so any
        # summary containing an ordinary backslash — a footnote marker, a
        # Windows path, dialogue about a regex or LaTeX expression, even
        # just "...found it at 221\1 Baker Street." — raised
        # `re.error: invalid group reference`. Only the *replace an existing
        # section* path was affected (a fresh section is plain string
        # concatenation, below); commit_on_success.py catches the exception
        # so it doesn't crash the run, but because it fires before the file
        # write, that chapter's section was silently left at its FIRST-DRAFT
        # summary forever — exactly the staleness this file exists to
        # prevent — with only one logger.error to show for it.
        #
        # A callable replacement sidesteps this entirely: re.sub never
        # interprets backslash escapes in a function's return value, no
        # matter what it contains.
        if pattern.search(existing):
            new_content = pattern.sub(lambda _m: section_text, existing)
        else:
            separator = "\n\n" if existing.strip() else ""
            new_content = existing.rstrip() + separator + section_text + "\n"

        try:
            self._synopsis_path.parent.mkdir(parents=True, exist_ok=True)
            # Bugfix: was plain write_text() — same class as the story_bible.md
            # fix.  synopsis.md is rewritten wholesale each update (pattern.sub
            # produces a full new string, not an append), so a kill mid-write
            # truncates it silently.  The next chapter's coder then receives an
            # incomplete continuity context with no error anywhere.
            # atomic_write_text (temp-file + os.replace) is the one-line fix.
            atomic_write_text(self._synopsis_path, new_content)
            logger.info(
                "SummaryMemory: wrote synopsis section for %s → %s",
                chapter_file, self._synopsis_path,
            )
        except OSError as exc:
            logger.error(
                "SummaryMemory: cannot write %s: %s", self._synopsis_path, exc,
            )


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
    verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)

    num_ctx_str = _cfg_mode(config, "coder", "num_ctx", task_mode, fallback=None)
    if num_ctx_str is None:
        num_ctx_str = config.get(api_sec, "num_ctx", fallback="0")
    num_ctx = int(num_ctx_str)

    max_tokens_str = _cfg_mode(config, "coder", "max_tokens", task_mode, fallback="800")
    max_tokens = int(max_tokens_str)

    temperature = config.getfloat("inner_loop", "temperature", fallback=0.2)
    # AUTO-FIX (fable follow-up 3): thinking models (qwen3) prepend a
    # <think> block; if it truncates against num_predict the synopsis update
    # comes back empty, and if it doesn't, the reasoning text is WRITTEN
    # INTO synopsis.md — a long-lived artifact re-injected into every later
    # prompt, so one polluted call poisons the whole run. Same toggle as
    # gate1/architect/coder: default off, [summary_memory] think = true
    # re-enables it.
    think = config.getboolean("summary_memory", "think", fallback=False)

    # AUTO-FIX (GATE3-PROFILE audit): think_effort was absent here, so every
    # LlmSettings this function produced carried the dataclass default (None)
    # rather than what [api] says, and a named profile appeared to "override"
    # a value the default had never read. Reasoning depth is a cost/quality
    # knob that does not change the shape of the reply, so the global [api]
    # switch is safe to honour.
    #
    # response_format is deliberately NOT read from [api] here. That switch
    # forces the server to emit a JSON object, which is right for Gate 1, the
    # Coder and the Architect — and wrong for every caller of this function.
    # The Gate-3 validators (canon/fact/continuity/theme) and SummaryMemory
    # all parse FREE TEXT: an "APPROVED" / "REVISE: ..." verdict, or a prose
    # synopsis. Honouring `[api] response_format = true` here would hand them
    # a JSON object instead and break every one of them at once — and
    # agents_128k.ini already ships with that key set to true, so this would
    # have gone straight into a live profile. A caller that genuinely wants
    # JSON mode can still set response_format in its own *_llm_profile, which
    # is an explicit, per-caller opt-in rather than a global one.
    think_effort_enabled = config.getboolean(
        "api", "think_effort_enabled", fallback=False)
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

    timeout = config.getint("loop", "timeout_seconds", fallback=300)

    ssl_context: ssl.SSLContext | None = _llm_stream.make_unverified_context() if not verify_ssl else None

    def _call(system: str, user: str) -> str:
        # AUTO-FIX (GATE3-PROFILE audit): this used to hand-roll its own
        # payload, alone among the callers — Coder, Gate1Filter, Architect
        # and TaskRewriter all go through build_chat_request. Two settings
        # were silently dropped as a result:
        #
        #   * response_format / think_effort — resolve_llm_profile read them
        #     faithfully from a <gate>_llm_profile and logged the profile as
        #     applied, and then nothing used them. The same key worked in
        #     [gate1] presence_llm_profile, so an operator could verify it
        #     there and get silence here.
        #   * think = false on api_format = openai — the hand-built openai
        #     branch had no think handling at all, so suppression only ever
        #     worked for ollama. build_chat_request sends the reasoning /
        #     thinking fields both branches need.
        #
        # Verified byte-for-byte identical to the old payload for
        # (ollama, think on/off) and (openai, think on); the openai +
        # think=false case is the bug fix and is the only intended change.
        # The one further payload difference: think=True now sends an
        # explicit think:true (ollama) / thinking:enabled (openai) where the
        # old code omitted the field. That restates the server default rather
        # than changing it, and it is what makes think_effort reachable at
        # all — the builder applies effort only when thinking is enabled.
        #
        # Going through the shared builder also picks up the per-(url, model)
        # unsupported-field memoization: a provider that rejects `reasoning`
        # or `response_format` once is not asked again.
        url, headers, payload = _llm_stream.build_chat_request(
            base_url=base_url, api_key=api_key, model=model,
            api_format=api_format, temperature=temperature,
            max_tokens=max_tokens, system=system, user_msg=user,
            num_ctx=num_ctx,
            # think is passed as a real bool, never None. `None` would omit
            # the field entirely, which preserved the old ollama payload
            # byte-for-byte when think=True — but build_chat_request only
            # applies think_effort when thinking is explicitly ENABLED, so
            # `None` left think_effort dead on arrival. Keeping a
            # configurable key working beats preserving one payload key that
            # only ever restated the server default; every other caller
            # (Gate1Filter, Coder, Architect) already passes a bool here.
            think=think,
            response_format=response_format, think_effort=think_effort,
        )
        # AUTO-FIX (fable follow-up 3): strip <think> BEFORE the synopsis
        # parser — reasoning text must never be persisted into synopsis.md.
        return _llm_stream.strip_think(
            _llm_stream.request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=timeout,
                api_format=api_format,
                ssl_context=ssl_context,
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

    max_passes  = config.getint("auto", "max_compression_passes", fallback=2)
    max_fidelity = config.getint("auto", "max_fidelity_rounds",   fallback=2)

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
