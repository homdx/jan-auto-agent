"""tools/auto/story_bible.py — AUTO-CR-23: Story Bible Store.

Maintains a small, persisted, size-capped list of *durable, must-not-contradict
facts* (character names, fixed attributes, current location/situation,
relationships, promises/goals) for a long creative work.

Design is deliberately simple (AUTO-CR-23 spec):
- Modelled on ``tools/auto/summary_memory.py``: same LLM-call style,
  ``_clean_bullet_list`` reuse, fail-open everywhere, language lock via
  ``tools.auto.utils.detect_language``.
- **No** entity graph, no NER, no tiered rolling synopsis re-summarisation.
  Those are explicitly out of scope for this CR.

Honest limits (per spec):
- LLM fact-extraction can miss a fact.
- Deduplication is string-normalised (lower-cased, punctuation/space-stripped),
  NOT semantic — a fact phrased differently will not be deduped.
- A fact not in the bible cannot be enforced.
- An 8B model can still drift on details the bible does not hold.
- The bible must therefore hold only *must-not-contradict* facts
  (state/attributes/relationships/promises), not every story detail.

Public surface
--------------
    from tools.auto.story_bible import StoryBible, make_story_bible

    bible = make_story_bible(config, base_url=…, api_key=…, model=…,
                             api_format=…, base_dir=…)
    if bible is not None:
        bible.update(chapter_text)
    facts = bible.load()   # → str, injected into prompt

Spec reference: AUTO-CR-23-1
"""
from __future__ import annotations

import configparser
import logging
import re
import string
from pathlib import Path
from typing import Callable

from tools.auto.summary_memory import _clean_bullet_list  # reuse existing helper

logger = logging.getLogger(__name__)

# ── LlmCall type alias ────────────────────────────────────────────────────────

LlmCall = Callable[[str, str], str]  # (system, user) -> response_text

# ── Prompts ───────────────────────────────────────────────────────────────────

_BIBLE_SYSTEM = (
    "Extract ONLY durable, must-not-contradict facts from this chapter: "
    "character names and fixed attributes (age, persistent appearance/clothing), "
    "relationships, the current location/situation, and any promise/goal set up. "
    "One short bullet per fact. "
    "NO events, NO dialogue, NO prose, NO parentheses. "
    "Keep each fact a single plain statement."
)

_COMPACT_SYSTEM = (
    "Merge duplicates and shorten; keep every distinct fact; output bullets only."
)

# ── helpers ───────────────────────────────────────────────────────────────────

_STRIP_TABLE = str.maketrans("", "", string.punctuation + string.whitespace)


def _normalise(text: str) -> str:
    """Lowercase + strip all punctuation and whitespace — for dedup comparison."""
    return text.lower().translate(_STRIP_TABLE)


def _parse_bullets(text: str) -> list[str]:
    """Return raw bullet strings (without leading marker) from *text*."""
    marker = re.compile(r"^\s*[•\-\*]|\d+[.)]\s+")
    result: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = marker.sub("", line, count=1).strip()
        if cleaned:
            result.append(cleaned)
    return result


# ── StoryBible ────────────────────────────────────────────────────────────────

class StoryBible:
    """Persisted, size-capped fact store for a long creative work.

    Parameters
    ----------
    llm_call:
        ``(system, user) -> str`` — any callable wrapping the LLM.
    base_dir:
        Project root directory.  ``path`` is resolved relative to this.
    path:
        Relative path (from *base_dir*) to the bible file.
    max_chars:
        Hard cap on the size of the persisted bible.  If the merged bible
        exceeds this a single compaction LLM call is attempted.  Never raises.
    """

    def __init__(
        self,
        llm_call: LlmCall,
        base_dir: "str | Path" = ".",
        *,
        path: str = "story_bible.md",
        max_chars: int = 2000,
    ) -> None:
        self._llm = llm_call
        self._base_dir = Path(base_dir)
        self._path = self._base_dir / path
        self._max_chars = max(100, int(max_chars))

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self) -> str:
        """Return the current bible text, or ``""`` if the file does not exist."""
        try:
            return self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def extract(self, chapter_text: str) -> list[str]:
        """Ask the LLM to extract durable facts from *chapter_text*.

        Returns a list of plain-text fact strings (without bullet markers).
        Returns ``[]`` on LLM error (fail-open).
        """
        from tools.auto.utils import detect_language, language_instruction

        lang_instr = language_instruction(detect_language(chapter_text))
        system = _BIBLE_SYSTEM + (("\n" + lang_instr) if lang_instr else "")
        user = f"CHAPTER:\n{chapter_text}"
        try:
            reply = self._llm(system, user) or ""
        except Exception as exc:
            logger.warning("StoryBible.extract: LLM error — %s", exc)
            return []

        bullets_text = _clean_bullet_list(reply)
        return _parse_bullets(bullets_text)

    def update(self, chapter_text: str) -> None:
        """Extract facts from *chapter_text*, merge into bible file, compact if needed.

        Algorithm (deterministic merge):
        1. Extract new bullets via LLM.
        2. Load existing bible bullets.
        3. Append only new bullets whose normalised form does not already exist.
        4. If merged text > ``max_chars`` → one bounded compaction LLM call.
        5. Write result.  Never raises.
        """
        try:
            self._do_update(chapter_text)
        except Exception as exc:  # noqa: BLE001 — fail-open contract
            logger.error("StoryBible.update: unexpected error — %s", exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _do_update(self, chapter_text: str) -> None:
        new_facts = self.extract(chapter_text)
        if not new_facts:
            logger.debug("StoryBible.update: no new facts extracted — skipping write.")
            return

        existing_text = self.load()
        existing_bullets = _parse_bullets(existing_text)
        existing_norms = {_normalise(b) for b in existing_bullets}

        merged: list[str] = list(existing_bullets)
        added = 0
        for fact in new_facts:
            if _normalise(fact) not in existing_norms:
                merged.append(fact)
                existing_norms.add(_normalise(fact))
                added += 1

        logger.debug(
            "StoryBible.update: %d existing + %d new = %d total bullets.",
            len(existing_bullets), added, len(merged),
        )

        merged_text = "\n".join(f"• {b}" for b in merged)

        if len(merged_text) > self._max_chars:
            merged_text = self._compact(merged_text)

        self._write(merged_text)

    def _compact(self, text: str) -> str:
        """One bounded compaction call.  Returns *text* unchanged on failure."""
        logger.info(
            "StoryBible._compact: bible (%d chars) exceeds max_chars=%d — compacting.",
            len(text), self._max_chars,
        )
        try:
            reply = self._llm(_COMPACT_SYSTEM, text) or ""
        except Exception as exc:
            logger.warning("StoryBible._compact: LLM error — %s — keeping uncompacted.", exc)
            return text

        compacted = _clean_bullet_list(reply)
        if not compacted:
            logger.warning("StoryBible._compact: LLM returned unusable reply — keeping uncompacted.")
            return text

        logger.info(
            "StoryBible._compact: compacted from %d to %d chars.",
            len(text), len(compacted),
        )
        return compacted

    def _write(self, text: str) -> None:
        """Write *text* to the bible file.  Creates parent dirs as needed."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(text, encoding="utf-8")
            logger.info("StoryBible: wrote %d chars to %s", len(text), self._path)
        except OSError as exc:
            logger.error("StoryBible: cannot write %s: %s", self._path, exc)


# ── Factory ───────────────────────────────────────────────────────────────────

def make_story_bible(
    config: configparser.ConfigParser,
    *,
    base_url: str,
    api_key: str,
    model: str,
    api_format: str,
    base_dir: "str | Path" = ".",
) -> "StoryBible | None":
    """Return a :class:`StoryBible` when ``[validator_agent] story_bible_creative``
    is ``true``, else ``None``.

    Parameters are taken from the caller (controller/pipeline) rather than
    re-parsed from config so the bible uses the same API endpoint as the rest
    of the pipeline.
    """
    enabled = config.getboolean("validator_agent", "story_bible_creative", fallback=False)
    if not enabled:
        logger.debug("make_story_bible: story_bible_creative=false — bible disabled.")
        return None

    max_chars = config.getint("validator_agent", "story_bible_max_chars", fallback=2000)

    llm_call = _build_llm_call(
        base_url=base_url,
        api_key=api_key,
        model=model,
        api_format=api_format,
        config=config,
    )

    return StoryBible(
        llm_call,
        base_dir=base_dir,
        max_chars=max_chars,
    )


def _build_llm_call(
    *,
    base_url: str,
    api_key: str,
    model: str,
    api_format: str,
    config: configparser.ConfigParser,
) -> LlmCall:
    """Build a simple blocking ``(system, user) -> str`` callable."""
    import ssl
    import tools.llm_stream as _llm_stream

    verify_ssl = config.getboolean("api", "verify_ssl", fallback=True)
    temperature = config.getfloat("inner_loop", "temperature", fallback=0.1)
    timeout = config.getint("loop", "timeout_seconds", fallback=300)
    max_tokens = config.getint("validator_agent", "max_tokens", fallback=200)

    ssl_context: ssl.SSLContext | None = None
    if not verify_ssl:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    if api_format == "ollama":
        url = _llm_stream.ollama_chat_url(base_url)
    else:
        url = f"{base_url.rstrip('/')}/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    def _call(system: str, user: str) -> str:
        if api_format == "ollama":
            payload: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
        else:
            payload = {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        return (
            _llm_stream.request_completion(
                url=url,
                headers=headers,
                payload=payload,
                timeout=timeout,
                api_format=api_format,
                ssl_context=ssl_context,
            )
            or ""
        )

    return _call
