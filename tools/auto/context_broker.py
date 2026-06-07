"""tools/auto/context_broker.py — Pull-model context resolver for the autonomous loop.

The *push* model that existed before this module forced both the Coder and the
Gate-2 Validator to guess which surrounding code they needed.  They received a
fixed char-budget slice and had no way to ask for more.  This module adds the
*pull* side: when the Gate-2 validator signals ``missing_context`` in its
rejection, the :class:`ContextBroker` resolves those symbol names from the AST
index that ``block_extractor`` already exposes, and returns the full source
blocks so the *next* attempt starts with the context the validator asked for.
(The coder resolves its own missing context inside ``generate()`` via the
in-generate context probe, so it does not go through this broker.)

Design
------
* Thin router over ``tools.block_extractor``: ``extract_block``,
  ``find_references``, ``get_context_lines``.
* Searches ``target_files`` first (cheapest and most likely hit), then falls
  back to a depth-first scan of all project Python files in ``base_dir``.
* Returns a formatted string ready to be injected into the coder prompt as a
  ``PREFETCHED CONTEXT`` section — not as failure feedback.

Public surface::

    from tools.auto.context_broker import ContextBroker

    broker = ContextBroker()

    # Resolve a list of symbol names and return a prompt-ready string.
    snippet = broker.fetch(
        symbols      = ["Config", "_resolve_path"],
        target_files = ["tools/auto/coder.py"],
        base_dir     = Path("."),
    )

    # Low-level: just the resolved {symbol: block} dict.
    resolved = broker.resolve(symbols, target_files, base_dir)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# Extensions we know block_extractor handles well.
_SEARCHABLE_EXTS = frozenset({".py", ".js", ".ts", ".tsx", ".jsx", ".go",
                               ".java", ".rs", ".c", ".cpp", ".h", ".hpp"})


class ContextBroker:
    """Resolve symbol names to their full source blocks.

    Parameters
    ----------
    max_block_chars:
        Hard cap per resolved block so a single giant class cannot flood the
        prompt.  Defaults to 3 000 chars — enough for most functions / small
        classes while keeping total context manageable.
    max_symbols:
        Upper limit on the number of symbols resolved per call to prevent
        runaway requests.  Extra symbols are silently dropped.
    """

    def __init__(
        self,
        max_block_chars: int = 3_000,
        max_symbols: int = 8,
    ) -> None:
        self._max_block_chars = max_block_chars
        self._max_symbols = max_symbols
        # Only PROJECT-SCAN (Pass-2) hits are cached. They live in files the current
        # task does not edit (dependencies), so they stay valid across attempts, and
        # the rglob scan that finds them is the expensive part. Target-file (Pass-1)
        # hits are never cached — the coder rewrites those files every attempt.
        self._resolved_cache: dict[str, str] = {}

    def reset_cache(self) -> None:
        self._resolved_cache.clear()

    # ── Public API ────────────────────────────────────────────────────────────

    def resolve(
        self,
        symbols: Sequence[str],
        target_files: Sequence[str],
        base_dir: Path,
    ) -> dict[str, str]:
        """Return a mapping of *symbol_name* → *source_block*.

        Symbols are looked up in ``target_files`` first.  Any symbol not found
        there triggers a depth-first search of all searchable files in
        ``base_dir``.  Symbols still not found after the full search are
        omitted from the result (the caller should not treat a missing symbol
        as an error — the block may simply not exist or use a name the model
        hallucinated).
        """
        from tools.block_extractor import extract_block  # local import — optional dep

        # Deduplicate while preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for sym in symbols:
            if sym not in seen:
                seen.add(sym); ordered.append(sym)

        # Cache hits are free — they don't count against _max_symbols.
        resolved = {s: self._resolved_cache[s] for s in ordered if s in self._resolved_cache}
        remaining = [s for s in ordered if s not in self._resolved_cache][: self._max_symbols]

        # ── Pass 1: search target files (fast path) ───────────────────────────
        for rel in target_files:
            if not remaining:
                break
            abs_path = base_dir / rel
            source, ext = self._read_file(abs_path)
            if source is None:
                continue
            found_now: list[str] = []
            for sym in remaining:
                block = extract_block(source, sym, ext)
                if block.strip():
                    resolved[sym] = self._cap(block)
                    found_now.append(sym)
            for sym in found_now:
                remaining.remove(sym)

        # ── Pass 2: scan whole project (fallback) ─────────────────────────────
        if remaining:
            for abs_path in self._iter_project_files(base_dir, target_files):
                if not remaining:
                    break
                source, ext = self._read_file(abs_path)
                if source is None:
                    continue
                found_now = []
                for sym in remaining:
                    block = extract_block(source, sym, ext)
                    if block.strip():
                        rel = str(abs_path.relative_to(base_dir))
                        capped_block = self._cap(block)
                        resolved[sym] = capped_block
                        self._resolved_cache[sym] = capped_block   # fallback hit → safe to cache
                        logger.debug(
                            "ContextBroker: found %r in %s (fallback search)",
                            sym, rel,
                        )
                        found_now.append(sym)
                for sym in found_now:
                    remaining.remove(sym)

        if remaining:
            logger.debug(
                "ContextBroker: could not resolve symbol(s): %s",
                ", ".join(remaining),
            )

        return resolved

    def fetch(
        self,
        symbols: Sequence[str],
        target_files: Sequence[str],
        base_dir: Path,
    ) -> str:
        """Resolve *symbols* and return a formatted prompt-ready string.

        Returns an empty string when nothing was resolved (so callers can
        safely include the result in a prompt without extra checks).
        """
        resolved = self.resolve(symbols, target_files, base_dir)
        return self.format_for_prompt(resolved)

    @staticmethod
    def format_for_prompt(resolved: dict[str, str]) -> str:
        """Format a ``{symbol: block}`` dict as a ``PREFETCHED CONTEXT`` section."""
        if not resolved:
            return ""
        lines = ["PREFETCHED CONTEXT (symbols you requested from the previous attempt):"]
        for sym, block in resolved.items():
            lines.append(f"\n### {sym}\n{block.rstrip()}")
        return "\n".join(lines) + "\n"

    # ── Private helpers ───────────────────────────────────────────────────────

    def _cap(self, block: str) -> str:
        """Truncate *block* to ``_max_block_chars`` with a notice."""
        if len(block) <= self._max_block_chars:
            return block
        excess = len(block) - self._max_block_chars
        return (
            block[: self._max_block_chars]
            + f"\n… [+{excess} chars truncated by ContextBroker]\n"
        )

    @staticmethod
    def _read_file(abs_path: Path) -> tuple[str | None, str]:
        """Read *abs_path* and return ``(source, extension)``."""
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            return source, abs_path.suffix.lower()
        except OSError:
            return None, ""

    @staticmethod
    def _iter_project_files(
        base_dir: Path,
        already_searched: Sequence[str],
    ):
        """Yield project source files, skipping already-searched ones and
        well-known noise directories."""
        skip_dirs = frozenset({
            "__pycache__", ".git", ".hg", ".svn",
            "node_modules", "venv", ".venv", "dist", "build", ".tox",
        })
        searched_abs = frozenset(
            str((base_dir / r).resolve()) for r in already_searched
        )
        for path in sorted(base_dir.rglob("*")):
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.suffix.lower() not in _SEARCHABLE_EXTS:
                continue
            if not path.is_file():
                continue
            if str(path.resolve()) in searched_abs:
                continue
            yield path
