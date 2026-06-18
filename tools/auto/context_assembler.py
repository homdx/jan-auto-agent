"""tools/auto/context_assembler.py — AUTO-CR-4: Budget-aware Context Assembler.

Implements the "continue from the short version" rule for creative mode: the
prompt for chapter *N* is built from the **compressed synopsis** of chapters
``1..N-1`` plus the **full text** of the immediately preceding chapter
``N-1``, fitted to the model's token budget (``num_ctx_creative`` /
``max_tokens_creative`` from AUTO-CR-3).

The model never re-reads every prior chapter in full; it reads the verified
synopsis (written by AUTO-CR-5's ``SummaryMemory``) plus the immediately
preceding chapter, which keeps voice/continuity intact without needing more
context than a small local model (``llama3.1:8b``) can hold.

Public surface
--------------
    from tools.auto.context_assembler import ContextAssembler

    assembler = ContextAssembler(num_ctx=8192, max_tokens=2048, base_dir=".")
    context = assembler.build_creative_context(
        target_file="chapter_07.md",
        all_chapter_files=["chapter_01.md", ..., "chapter_06.md"],
    )

``build_creative_context`` never raises: missing files, a missing/malformed
``synopsis.md``, or an over-budget chapter all degrade gracefully (fail-open),
matching the rest of the creative pipeline's error handling philosophy.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Matches "chapter_07", "chapter_7", "Chapter_07.md", etc. — the number is
# whatever digits follow "chapter_", case-insensitive.
_CHAPTER_RE = re.compile(r"chapter[_\-\s]?(\d+)", re.IGNORECASE)

# Conservative token-estimate ratio: ~4 characters per token. This slightly
# *over*-counts tokens for typical English prose (favoring fewer chars per
# token budget), which is the conservative direction for a budget we must
# not exceed.
_CHARS_PER_TOKEN = 4

# Fixed overhead reserved for the task framing injected around this context
# (TASK ID / TITLE / INSTRUCTION / CITED LOCATION / target file listing,
# plus the closing "produce the chapter now" instruction) in coder.py's
# user prompt template. Approximate, intentionally generous.
_INSTRUCTION_OVERHEAD_TOKENS = 300

# Markers used by AUTO-CR-5's SummaryMemory.update() to key sections in
# synopsis.md. Parsing here is read-only and independent of CR-5 — if
# synopsis.md does not exist yet (CR-5 not wired up / first run), this
# assembler still works using only the previous chapter's full text.
_SECTION_RE = re.compile(
    r"<!--\s*BEGIN\s+(?P<name>\S+)\s*-->(?P<body>.*?)<!--\s*END\s+(?P=name)\s*-->",
    re.DOTALL,
)

_DROP_MARKER = "… [older synopsis omitted]"


def _chapter_number(filename: str) -> "int | None":
    """Extract the integer chapter number from a filename, or ``None`` if it
    doesn't match the ``chapter_<N>`` pattern."""
    m = _CHAPTER_RE.search(Path(filename).name)
    return int(m.group(1)) if m else None


def _order_chapters(all_chapter_files: "list[str]") -> "list[tuple[int, str]]":
    """Return ``(number, filename)`` pairs sorted ascending by chapter number.

    Filenames that don't match the ``chapter_<N>`` pattern are silently
    ignored — they can't be ordered, so they can't participate in
    continuity assembly.
    """
    numbered = [
        (n, f) for f in all_chapter_files
        if (n := _chapter_number(f)) is not None
    ]
    numbered.sort(key=lambda t: t[0])
    return numbered


class ContextAssembler:
    """Builds the budget-fitted creative-mode context block.

    Parameters
    ----------
    num_ctx:
        The model's total context window in tokens (e.g.
        ``[coder] num_ctx_creative`` from AUTO-CR-3).
    max_tokens:
        The reserved output budget in tokens (e.g.
        ``[coder] max_tokens_creative``).
    base_dir:
        Repository root. Chapter files and ``synopsis_path`` are resolved
        relative to this.
    synopsis_path:
        Relative path (from ``base_dir``) to the running synopsis file
        written by AUTO-CR-5's ``SummaryMemory``. Defaults to
        ``"synopsis.md"``.
    """

    def __init__(
        self,
        num_ctx: int,
        max_tokens: int,
        base_dir: "str | Path",
        synopsis_path: str = "synopsis.md",
    ) -> None:
        # Tolerate 0 / missing values (e.g. num_ctx=0 meaning "server
        # default") with sane fallbacks rather than producing a zero or
        # negative budget.
        self._num_ctx = int(num_ctx) if num_ctx else 4096
        self._max_tokens = int(max_tokens) if max_tokens else 800
        self._base_dir = Path(base_dir)
        self._synopsis_path = self._base_dir / synopsis_path

    # ── Public API ───────────────────────────────────────────────────────

    def build_creative_context(
        self,
        target_file: str,
        all_chapter_files: "list[str]",
    ) -> str:
        """Return the assembled context block for *target_file*.

        Budget permitting, contains:
          * ``STORY SO FAR (synopsis)`` — verified synopsis sections for
            chapters ``1..N-1``, newest first, oldest dropped first if the
            budget is exceeded (marked with ``"… [older synopsis omitted]"``).
          * ``PREVIOUS CHAPTER (verbatim)`` — the full text of the
            highest-numbered chapter below *target_file* (most critical for
            voice/continuity).

        If even the previous chapter's full text doesn't fit the budget, it
        is folded into the synopsis fill instead (using its synopsis section
        rather than full text) and a warning is logged — this is how the
        assembler degrades gracefully at small windows (e.g. ``num_ctx=4096``).

        For the first chapter (no predecessors), or when *target_file* isn't
        a recognizable ``chapter_<N>`` filename, returns ``""`` — just the
        task framing, no errors.
        """
        target_num = _chapter_number(target_file)
        if target_num is None:
            return ""

        ordered = _order_chapters(all_chapter_files)
        prior = [(n, f) for n, f in ordered if n < target_num]
        if not prior:
            return ""

        sections = self._read_synopsis_sections()

        budget_chars = self._budget_chars()
        if budget_chars <= 0:
            # Pathological config (max_tokens >= num_ctx). Rather than silently
            # returning NO context (which makes the model invent a chapter in
            # the wrong language with nothing to continue), degrade gracefully:
            # prefer the compact synopsis of the story so far; only if there is
            # no synopsis yet, fall back to a truncated tail of the previous
            # chapter so there is always *some* anchor.
            prev_num, prev_file = prior[-1]
            logger.warning(
                "ContextAssembler: zero/negative budget for %s "
                "(num_ctx=%d, max_tokens=%d) — degrading (synopsis, else truncated prev).",
                target_file, self._num_ctx, self._max_tokens,
            )
            floor = 2000
            synopsis_block = self._fit_synopsis(sections, prior, floor)
            if synopsis_block:
                return synopsis_block
            prev_text = self._read_chapter_text(prev_file)
            if not prev_text:
                return ""
            tail = prev_text[-floor:]
            if len(prev_text) > floor:
                tail = "… [earlier text omitted]\n" + tail
            return self._format_prev_chapter_block(prev_file, tail)

        prev_num, prev_file = prior[-1]  # highest-numbered chapter < target
        prev_text = self._read_chapter_text(prev_file)
        prev_block = self._format_prev_chapter_block(prev_file, prev_text)

        if prev_text and len(prev_block) <= budget_chars:
            # Common path: previous chapter fits in full. Fill whatever
            # budget remains with synopsis of everything older than it.
            remaining = budget_chars - len(prev_block) - 2  # joining newline
            synopsis_block = self._fit_synopsis(sections, prior[:-1], remaining)
            parts = [p for p in (synopsis_block, prev_block) if p]
            return "\n\n".join(parts)

        # Degrade path: chapter N-1's full text alone exceeds the budget (or
        # is unreadable). Fold it into the synopsis fill — using its synopsis
        # section instead of full text — alongside everything older.
        logger.warning(
            "ContextAssembler: previous chapter %s (%d chars) does not fit "
            "the %d-char budget for target %s — degrading to its synopsis "
            "section instead of full text.",
            prev_file, len(prev_text), budget_chars, target_file,
        )
        return self._fit_synopsis(sections, prior, budget_chars)

    # ── Budget math ──────────────────────────────────────────────────────

    def _budget_chars(self) -> int:
        """Char budget available for context, after reserving output tokens
        and a fixed instruction-framing overhead. Token estimate = chars/4."""
        budget_tokens = self._num_ctx - self._max_tokens - _INSTRUCTION_OVERHEAD_TOKENS
        return max(budget_tokens, 0) * _CHARS_PER_TOKEN

    # ── File / synopsis reading ──────────────────────────────────────────

    def _read_chapter_text(self, filename: str) -> str:
        path = self._base_dir / filename
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("ContextAssembler: cannot read %s: %s", filename, exc)
            return ""

    @staticmethod
    def _format_prev_chapter_block(filename: str, text: str) -> str:
        return f"PREVIOUS CHAPTER (verbatim) — {filename}:\n{text}"

    def _read_synopsis_sections(self) -> "dict[str, str]":
        """Parse ``synopsis.md`` into ``{chapter_filename: section_text}``
        using AUTO-CR-5's ``<!-- BEGIN/END chapter_NN.md -->`` marker format.

        Fail-open: returns ``{}`` if the file is missing or has no parseable
        sections, so this assembler works even before AUTO-CR-5 has ever run.
        """
        try:
            raw = self._synopsis_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        sections: "dict[str, str]" = {}
        for m in _SECTION_RE.finditer(raw):
            name = m.group("name").strip()
            body = m.group("body").strip()
            if name and body:
                sections[name] = body
        return sections

    # ── Synopsis budget fill ─────────────────────────────────────────────

    def _fit_synopsis(
        self,
        sections: "dict[str, str]",
        chapters: "list[tuple[int, str]]",
        budget_chars: int,
    ) -> str:
        """Fill *budget_chars* with synopsis sections for *chapters*, newest
        first, stopping (and dropping all older ones) at the first section
        that doesn't fit. Drops are marked with ``"… [older synopsis omitted]"``.

        Chapters with no synopsis section available (e.g. CR-5 hasn't run
        for them yet) are silently skipped — they neither consume budget nor
        trigger a drop marker on their own.
        """
        if budget_chars <= 0 or not chapters:
            return ""

        newest_first = sorted(chapters, key=lambda t: t[0], reverse=True)

        included: "list[str]" = []
        used = 0
        dropped = False
        for _, fname in newest_first:
            body = sections.get(fname)
            if not body:
                continue
            block = f"## {fname}\n{body}"
            cost = len(block) + (2 if included else 0)  # joining "\n\n"
            if used + cost > budget_chars:
                dropped = True
                break
            included.append(block)
            used += cost

        if not included:
            return ""

        # Restore chronological (oldest → newest) order for narrative flow.
        included.reverse()
        body_text = "\n\n".join(included)
        if dropped:
            body_text = f"{_DROP_MARKER}\n\n{body_text}"
        return f"STORY SO FAR (synopsis):\n{body_text}"
