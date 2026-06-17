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
import logging
import re
import ssl
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ── constants ─────────────────────────────────────────────────────────────────

_CHARS_PER_TOKEN = 4
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
    "No prose, no commentary, no preamble — bullet lines only."
)

_SYSTEM_FIDELITY = (
    "You are a careful fact-checker. "
    "Compare the SUMMARY with the SOURCE text. "
    "For each fact in the SOURCE that the SUMMARY OMITS, CONTRADICTS, or DISTORTS, "
    "output one line beginning exactly with 'FIX: ' followed by the correction. "
    "If the summary is fully faithful, reply with exactly: OK"
)

# ── LlmCall type alias ────────────────────────────────────────────────────────

LlmCall = Callable[[str, str], str]   # (system, user) -> response_text


# ── helpers ───────────────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


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


def _apply_fixes(summary: str, fixes: list[str]) -> str:
    """Append each *FIX* correction as a new bullet line to *summary*."""
    if not fixes:
        return summary
    fix_lines = "\n".join(f"- [corrected] {f}" for f in fixes)
    return summary.rstrip() + "\n" + fix_lines


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
        """
        current = summary
        for rnd in range(1, self._max_rounds + 1):
            user_msg = (
                f"SOURCE:\n{chapter_text}\n\n"
                f"SUMMARY:\n{current}"
            )
            try:
                reply = self._llm(_SYSTEM_FIDELITY, user_msg) or ""
            except Exception as exc:
                logger.warning(
                    "SummaryFidelityVerifier: LLM error on round %d: %s — treating as OK.",
                    rnd, exc,
                )
                break

            reply = reply.strip()
            if not reply:
                logger.warning(
                    "SummaryFidelityVerifier: empty reply on round %d — treating as OK.", rnd,
                )
                break

            first_line = reply.splitlines()[0].strip().upper()
            if first_line == "OK":
                logger.debug("SummaryFidelityVerifier: OK on round %d.", rnd)
                break

            fixes = [
                line[len("FIX:"):].strip()
                for line in reply.splitlines()
                if line.strip().upper().startswith("FIX:")
            ]
            if fixes:
                logger.info(
                    "SummaryFidelityVerifier: round %d — applying %d fix(es).", rnd, len(fixes),
                )
                current = _apply_fixes(current, fixes)
            else:
                # Reply was neither "OK" nor any "FIX:" lines — fail-open.
                logger.warning(
                    "SummaryFidelityVerifier: round %d reply unparseable — treating as OK. "
                    "Reply preview: %.120s", rnd, reply,
                )
                break

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
        _input_tokens = (
            max(0, (int(num_ctx) if num_ctx else 4096))
            - (int(max_tokens) if max_tokens else 800)
            - _INSTRUCTION_OVERHEAD_TOKENS
            - _SUMMARISE_OVERHEAD_TOKENS
        )
        self._chunk_budget_chars = max(200, _input_tokens * _CHARS_PER_TOKEN)

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
        self._write_section(chapter_file, verified)

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

        fits_in_budget = len(text) <= self._chunk_budget_chars

        if fits_in_budget:
            # Single pass.
            result = self._summarise_once(text)
            if result:
                return result
            # LLM returned nothing — return the text as-is (fail-open).
            logger.warning("SummaryMemory: summarise returned empty — using input.")
            return text

        # Chunked pass: split, summarise each chunk, then merge.
        chunks = _chunk_paragraphs(text, self._chunk_budget_chars)
        logger.info(
            "SummaryMemory: chapter too large (%d chars > %d budget) — "
            "splitting into %d chunks (pass %d/%d).",
            len(text), self._chunk_budget_chars,
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
            return text[:self._chunk_budget_chars]

        merged = "\n".join(chunk_summaries)
        # Recurse: try to compress the merged chunk-summaries into one summary.
        return self._compress(merged, passes_used + 1)

    def _summarise_once(self, text: str) -> str:
        """Make a single summarisation LLM call.  Returns ``\"\"`` on failure."""
        user_msg = f"CHAPTER TEXT:\n{text}"
        try:
            reply = self._llm(_SYSTEM_SUMMARISE, user_msg) or ""
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
        if pattern.search(existing):
            new_content = pattern.sub(section_text, existing)
        else:
            separator = "\n\n" if existing.strip() else ""
            new_content = existing.rstrip() + separator + section_text + "\n"

        try:
            self._synopsis_path.parent.mkdir(parents=True, exist_ok=True)
            self._synopsis_path.write_text(new_content, encoding="utf-8")
            logger.info(
                "SummaryMemory: wrote synopsis section for %s → %s",
                chapter_file, self._synopsis_path,
            )
        except OSError as exc:
            logger.error(
                "SummaryMemory: cannot write %s: %s", self._synopsis_path, exc,
            )


# ── Factory ───────────────────────────────────────────────────────────────────

def _make_llm_call(
    config: configparser.ConfigParser,
    task_mode: str = "creative",
) -> LlmCall:
    """Build a ``(system, user) -> str`` callable from *config*.

    Uses the same API profile / format / SSL logic as the rest of the
    pipeline.  The LLM call is non-streaming (blocking), matching the
    validator pattern.
    """
    from tools.auto.utils import _cfg_mode
    import tools.llm_stream as _llm_stream

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
    timeout     = config.getint("loop", "timeout_seconds", fallback=300)

    ssl_context: ssl.SSLContext | None = None
    if not verify_ssl:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode    = ssl.CERT_NONE

    if api_format == "ollama":
        url = _llm_stream.ollama_chat_url(base_url)
    else:
        url = f"{base_url.rstrip('/')}/chat/completions"

    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    def _call(system: str, user: str) -> str:
        if api_format == "ollama":
            _opts: dict = {"temperature": temperature, "num_predict": max_tokens}
            if num_ctx:
                _opts["num_ctx"] = num_ctx
            payload: dict = {
                "model":    model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "options": _opts,
            }
        else:
            payload = {
                "model":       model,
                "temperature": temperature,
                "max_tokens":  max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            }
        return _llm_stream.request_completion(
            url=url,
            headers=headers,
            payload=payload,
            timeout=timeout,
            api_format=api_format,
            ssl_context=ssl_context,
        ) or ""

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
    """
    from tools.auto.utils import _cfg_mode

    max_passes  = config.getint("auto", "max_compression_passes", fallback=2)
    max_fidelity = config.getint("auto", "max_fidelity_rounds",   fallback=2)

    num_ctx_str = _cfg_mode(config, "coder", "num_ctx", task_mode, fallback=None)
    if num_ctx_str is None:
        active = config.get("api", "active", fallback="local")
        num_ctx_str = config.get(f"api_{active}", "num_ctx", fallback="0")
    num_ctx = int(num_ctx_str)

    max_tokens = int(_cfg_mode(config, "coder", "max_tokens", task_mode, fallback="800"))

    llm_call = _make_llm_call(config, task_mode=task_mode)

    return SummaryMemory(
        llm_call,
        max_compression_passes=max_passes,
        max_fidelity_rounds=max_fidelity,
        num_ctx=num_ctx,
        max_tokens=max_tokens,
        base_dir=base_dir,
        synopsis_path=synopsis_path,
    )
