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

AUTO-CR-23-2 adds a third, higher-priority block: the **story bible**
(durable, must-not-contradict facts written by AUTO-CR-23-1's
``StoryBible``). Its budget is reserved *before* the synopsis/previous-
chapter budget is computed, so the bible is always present in the assembled
context — even when the window is so tight that old synopsis sections get
dropped. If there is no bible file (or it is empty), this degrades to
exactly the pre-CR-23 behaviour.

Public surface
--------------
    from tools.auto.context_assembler import ContextAssembler

    assembler = ContextAssembler(num_ctx=8192, max_tokens=2048, base_dir=".")
    context = assembler.build_creative_context(
        target_file="chapter_07.md",
        all_chapter_files=["chapter_01.md", ..., "chapter_06.md"],
    )

``build_creative_context`` never raises: missing files, a missing/malformed
``synopsis.md``, a missing/malformed ``story_bible.md``, or an over-budget
chapter all degrade gracefully (fail-open), matching the rest of the creative
pipeline's error handling philosophy.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from tools.auto.utils import chars_per_token

logger = logging.getLogger(__name__)

# Matches "chapter_07", "chapter_7", "Chapter_07.md", etc. — the number is
# whatever digits follow "chapter_", case-insensitive.
_CHAPTER_RE = re.compile(r"chapter[_\-\s]?(\d+)", re.IGNORECASE)

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

# AUTO-CR-23-2: default cap on how much of story_bible.md is injected per
# prompt. Small and fixed — the bible itself is already kept small by
# AUTO-CR-23-1 (story_bible_max_chars), this is just the per-prompt ceiling.
_DEFAULT_BIBLE_BUDGET_CHARS = 700

_BIBLE_HEADER = "STORY FACTS (must not contradict):"


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
    bible_path:
        Relative path (from ``base_dir``) to the story bible file written
        by AUTO-CR-23-1's ``StoryBible``. Defaults to ``"story_bible.md"``.
    bible_budget_chars:
        Hard cap on how many characters of the bible are injected per
        prompt. Defaults to ``700`` (AUTO-CR-23-2) — small and fixed, since
        AUTO-CR-23-1 already keeps the bible file itself small.
    """

    def __init__(
        self,
        num_ctx: int,
        max_tokens: int,
        base_dir: "str | Path",
        synopsis_path: str = "synopsis.md",
        bible_path: str = "story_bible.md",
        bible_budget_chars: int = _DEFAULT_BIBLE_BUDGET_CHARS,
    ) -> None:
        # Tolerate 0 / missing values (e.g. num_ctx=0 meaning "server
        # default") with sane fallbacks rather than producing a zero or
        # negative budget.
        self._num_ctx = int(num_ctx) if num_ctx else 4096
        self._max_tokens = int(max_tokens) if max_tokens else 800
        self._base_dir = Path(base_dir)
        self._synopsis_path = self._base_dir / synopsis_path
        self._bible_path = self._base_dir / bible_path
        self._bible_budget_chars = max(0, int(bible_budget_chars))

    # ── Public API ───────────────────────────────────────────────────────

    def build_creative_context(
        self,
        target_file: str,
        all_chapter_files: "list[str]",
    ) -> str:
        """Return the assembled context block for *target_file*.

        Budget permitting, contains:
          * ``STORY FACTS (must not contradict)`` — durable facts from
            AUTO-CR-23-1's ``story_bible.md`` (AUTO-CR-23-2). Its budget is
            reserved *first*, before anything else below, so this block is
            always present — even when the synopsis has to drop sections to
            fit. Omitted entirely if the bible file is missing/empty, in
            which case the rest of this method is byte-for-byte unchanged
            from before AUTO-CR-23.
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

        # AUTO-CR-23-2: reserve the bible's own budget FIRST — before the
        # synopsis/previous-chapter budget below is computed — so the bible
        # survives even when the window is tight enough to drop synopsis
        # sections. One read, one prepend, one budget subtraction; if there
        # is no bible file (or it's empty) this is a no-op.
        bible_block = self._read_bible_block()
        bible_cost = (len(bible_block) + 2) if bible_block else 0  # +2 = "\n\n" join

        # Read the previous chapter once, up front: it doubles as the
        # sample used to detect the chars-per-token ratio for the budget
        # below (Cyrillic tokenizes far denser than the ~4 chars/token
        # default — see chars_per_token()), and _assemble_core needs its
        # text anyway. Falls back to the synopsis sections as the sample
        # when there's no previous chapter text (e.g. unreadable file).
        prev_num, prev_file = prior[-1]
        prev_text = self._read_chapter_text(prev_file)
        sample_text = prev_text or "\n".join(sections.values())

        total_budget = self._budget_chars(sample_text)
        budget_chars = max(total_budget - bible_cost, 0)

        core = self._assemble_core(target_file, prior, sections, budget_chars, prev_text)

        if not bible_block:
            return core
        if not core:
            return bible_block
        return f"{bible_block}\n\n{core}"

    # ── Story bible (AUTO-CR-23-2) ───────────────────────────────────────

    def _read_bible_block(self) -> str:
        """Read ``story_bible.md`` and format it as the
        ``STORY FACTS (must not contradict)`` block, capped to
        ``bible_budget_chars``.

        Returns ``""`` if the file is missing, unreadable, or empty —
        callers must treat that as "behave exactly as before AUTO-CR-23".
        """
        try:
            text = self._bible_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        if not text:
            return ""
        if len(text) > self._bible_budget_chars:
            text = text[: self._bible_budget_chars]
        return f"{_BIBLE_HEADER}\n{text}"

    # ── Synopsis + previous-chapter assembly (pre-CR-23 logic) ──────────

    def _assemble_core(
        self,
        target_file: str,
        prior: "list[tuple[int, str]]",
        sections: "dict[str, str]",
        budget_chars: int,
        prev_text: str,
    ) -> str:
        """Assemble the synopsis + previous-chapter blocks for *budget_chars*.

        This is the original (pre-AUTO-CR-23) ``build_creative_context``
        body, unchanged, except that *budget_chars* is now passed in already
        net of the bible's reserved cost rather than computed here, and
        *prev_text* is passed in (already read by the caller) rather than
        re-read here.
        """
        if budget_chars <= 0:
            # Pathological config (max_tokens >= num_ctx) or the bible ate the
            # whole budget. Degrade gracefully instead of returning no context:
            # prefer the synopsis, falling back to a truncated tail of the
            # previous chapter only if there's no synopsis yet.
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
            if not prev_text:
                return ""
            tail = prev_text[-floor:]
            if len(prev_text) > floor:
                tail = "… [earlier text omitted]\n" + tail
            return self._format_prev_chapter_block(prev_file, tail)

        prev_num, prev_file = prior[-1]  # highest-numbered chapter < target
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

    def _budget_chars(self, sample_text: str = "") -> int:
        """Char budget available for context, after reserving output tokens
        and a fixed instruction-framing overhead.

        Token estimate uses ``chars_per_token(sample_text)`` rather than a
        fixed ``4`` — Cyrillic text tokenizes much denser than Latin, so a
        fixed English-tuned ratio silently overflows num_ctx for Russian
        chapters. *sample_text* should be a representative excerpt of the
        content actually being budgeted (e.g. the previous chapter).
        """
        budget_tokens = self._num_ctx - self._max_tokens - _INSTRUCTION_OVERHEAD_TOKENS
        return int(max(budget_tokens, 0) * chars_per_token(sample_text))

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
