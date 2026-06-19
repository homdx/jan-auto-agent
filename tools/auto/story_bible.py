"""tools/auto/story_bible.py — AUTO-CR-23 / AUTO-CR-24: Story Bible Store.

Maintains a small, persisted, size-capped list of *durable, must-not-contradict
facts* (character names, fixed attributes, relationships, promises/goals) for a
long creative work.

Design is deliberately simple (AUTO-CR-23 spec):
- Modelled on ``tools/auto/summary_memory.py``: same LLM-call style,
  ``_clean_bullet_list`` reuse, fail-open everywhere, language lock via
  ``tools.auto.utils.detect_language``.
- **No** entity graph, no NER, no tiered rolling synopsis re-summarisation.
  Those are explicitly out of scope for this CR.

AUTO-CR-24-1: extracted facts are verified against the source chapter before
being merged, using the proven ``SummaryFidelityVerifier`` pattern.  Controlled
by ``[validator_agent] story_bible_verify`` (default: false for back-compat).

AUTO-CR-24-2: the extract prompt requests ONLY immutable / slowly-changing
facts.  Transient state (current location, what characters are doing/wearing
right now) is explicitly excluded — per-scene state is the continuity gate's
job, read from the previous chapter, not this store's.

AUTO-CR-24-3: compaction is deterministic (stronger dedup, drop-substring
merge).  No LLM call in the compaction path, so it can never silently distort
or drop a fact via a bad model reply.  Slightly-over-cap is preferred over
fact loss; only a hard ceiling (2x ``max_chars``) triggers dropping the
oldest bullets, and that drop is always logged.

Honest limits (per spec):
- LLM fact-extraction can miss a fact.
- Deduplication is string-normalised (lower-cased, punctuation/space-stripped),
  NOT semantic — a fact phrased differently will not be deduped.
- A fact not in the bible cannot be enforced.
- An 8B model can still drift on details the bible does not hold.
- The bible must therefore hold only *must-not-contradict* facts
  (attributes/relationships/promises), not every story detail.

Public surface
--------------
    from tools.auto.story_bible import StoryBible, make_story_bible

    bible = make_story_bible(config, base_url=…, api_key=…, model=…,
                             api_format=…, base_dir=…)
    if bible is not None:
        bible.update(chapter_text)
    facts = bible.load()   # → str, injected into prompt

Spec reference: AUTO-CR-23-1, AUTO-CR-24-1, AUTO-CR-24-2, AUTO-CR-24-3
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
    "Extract ONLY immutable or slowly-changing facts from this chapter: "
    "character names, relationships, fixed attributes (age, КМС, profession, "
    "defining physical traits), world rules, and long-term promises/goals. "
    "One short bullet per fact. "
    "NO events, NO dialogue, NO prose, NO parentheses. "
    "Keep each fact a single plain statement. "
    "Do NOT record where characters currently are, what they are doing or wearing "
    "in this scene, or anything that changes scene-to-scene. "
    "Record only PERMANENT attributes. Do NOT turn a momentary description into "
    "a permanent trait (e.g. 'seemed darker in the sunset' is NOT 'dark hair'). "
    "Preserve negations and qualifiers exactly (e.g. 'secret cargo', 'not "
    "allowed to know', 'does not work')."
)

# AUTO-CR-24-3: compaction is now deterministic — no LLM call in this path.
# (Old AUTO-CR-23 _COMPACT_SYSTEM prompt removed; see StoryBible._compact.)

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


def _dedup_substrings(bullets: list[str]) -> list[str]:
    """Drop bullets whose normalised form is an exact duplicate of, or a
    substring of, another (longer or equal) bullet.  Order-preserving;
    among substring/duplicate pairs the longer (more informative) bullet
    wins, first-seen wins ties.
    """
    norms = [_normalise(b) for b in bullets]
    keep = [True] * len(bullets)

    for i, norm_i in enumerate(norms):
        if not keep[i] or not norm_i:
            continue
        for j, norm_j in enumerate(norms):
            if i == j or not keep[j]:
                continue
            if norm_i == norm_j:
                # Exact duplicate — keep the first occurrence only.
                if j > i:
                    keep[j] = False
                continue
            if norm_i in norm_j:
                # bullet i is a substring of the longer bullet j — drop i.
                keep[i] = False
                break

    return [b for b, k in zip(bullets, keep) if k]


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
        exceeds this, deterministic compaction (dedup) is attempted; never
        calls the LLM and never raises.
    """

    def __init__(
        self,
        llm_call: LlmCall,
        base_dir: "str | Path" = ".",
        *,
        path: str = "story_bible.md",
        max_chars: int = 2000,
        verify: bool = False,
        max_fidelity_rounds: int = 1,
    ) -> None:
        self._llm = llm_call
        self._base_dir = Path(base_dir)
        self._path = self._base_dir / path
        self._max_chars = max(100, int(max_chars))
        self._verify = bool(verify)
        self._fidelity: "SummaryFidelityVerifier | None" = None
        if self._verify:
            from tools.auto.summary_memory import SummaryFidelityVerifier
            self._fidelity = SummaryFidelityVerifier(
                llm_call, max_fidelity_rounds=max(1, int(max_fidelity_rounds))
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self) -> str:
        """Return the current bible text, or ``""`` if the file does not exist."""
        try:
            return self._path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def extract(self, chapter_text: str, known_facts: str = "") -> list[str]:
        """Ask the LLM to extract durable facts from *chapter_text*.

        ``known_facts`` (AUTO-CR-25-1): when non-empty, the already-recorded
        bible is prepended to the user message so the model stops re-emitting
        known facts in paraphrased form. The deterministic substring/exact
        dedup in ``_do_update``/``_compact`` remains as a backstop regardless.

        Returns a list of plain-text fact strings (without bullet markers).
        Returns ``[]`` on LLM error (fail-open).
        """
        from tools.auto.utils import detect_language, language_instruction

        lang_instr = language_instruction(detect_language(chapter_text))
        system = _BIBLE_SYSTEM + (("\n" + lang_instr) if lang_instr else "")
        if known_facts:
            user = (
                "KNOWN FACTS (already recorded — do NOT repeat these or "
                f"restate them in other words):\n{known_facts}\n\n"
                f"CHAPTER:\n{chapter_text}"
            )
        else:
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
        4. If merged text > ``max_chars`` → deterministic compaction (dedup;
           no LLM call — AUTO-CR-24-3).
        5. Write result.  Never raises.
        """
        try:
            self._do_update(chapter_text)
        except Exception as exc:  # noqa: BLE001 — fail-open contract
            logger.error("StoryBible.update: unexpected error — %s", exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _do_update(self, chapter_text: str) -> None:
        new_facts = self.extract(chapter_text, known_facts=self.load())
        if not new_facts:
            logger.debug("StoryBible.update: no new facts extracted — skipping write.")
            return

        # AUTO-CR-24-1: verify extracted facts against the source chapter.
        if self._fidelity is not None:
            raw_bullet_text = "\n".join(f"• {f}" for f in new_facts)
            try:
                verified_text = self._fidelity.verify_and_fix(chapter_text, raw_bullet_text)
                if verified_text and verified_text.strip():
                    verified_facts = _parse_bullets(verified_text)
                    if verified_facts:
                        new_facts = verified_facts
                    else:
                        logger.warning(
                            "StoryBible.update: fidelity check returned no usable bullets "
                            "— falling back to raw extracted facts."
                        )
                else:
                    logger.warning(
                        "StoryBible.update: fidelity check returned empty — "
                        "falling back to raw extracted facts."
                    )
            except Exception as exc:
                logger.warning(
                    "StoryBible.update: fidelity check error (%s) — "
                    "falling back to raw extracted facts (fail-open).", exc
                )
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
        """Deterministic compaction.  Never calls the LLM (AUTO-CR-24-3).

        1. Stronger dedup: drop bullets whose normalised form is an exact
           duplicate, or a substring, of another bullet (merges near-dupes).
        2. If still over ``max_chars``: keep everything and log a WARNING —
           a slightly-over-cap bible is safer than silently losing an anchor.
        3. Only if a hard ceiling (2x ``max_chars``) is exceeded: drop the
           OLDEST bullets until back under the ceiling, logging what was
           dropped.
        """
        bullets = _parse_bullets(text)
        deduped = _dedup_substrings(bullets)
        deduped_text = "\n".join(f"• {b}" for b in deduped)

        logger.info(
            "StoryBible._compact: deterministic dedup %d -> %d bullets "
            "(%d -> %d chars).",
            len(bullets), len(deduped), len(text), len(deduped_text),
        )

        if len(deduped_text) <= self._max_chars:
            return deduped_text

        hard_ceiling = self._max_chars * 2
        if len(deduped_text) <= hard_ceiling:
            logger.warning(
                "StoryBible._compact: bible is %d chars after dedup, still over "
                "max_chars=%d; consider raising story_bible_max_chars. Keeping "
                "all facts (no silent loss).",
                len(deduped_text), self._max_chars,
            )
            return deduped_text

        # Hard ceiling exceeded — drop the oldest bullets only, as a last
        # resort, and log exactly what was dropped.
        kept: list[str] = []
        dropped: list[str] = []
        # Walk from newest to oldest, keep what fits, then restore order.
        running_len = 0
        for bullet in reversed(deduped):
            line_len = len(f"• {bullet}\n")
            if running_len + line_len <= hard_ceiling:
                kept.append(bullet)
                running_len += line_len
            else:
                dropped.append(bullet)
        kept.reverse()
        dropped.reverse()

        logger.warning(
            "StoryBible._compact: hard ceiling (%d chars) exceeded after dedup "
            "(%d chars) — dropped %d oldest fact(s): %s",
            hard_ceiling, len(deduped_text), len(dropped), dropped,
        )

        return "\n".join(f"• {b}" for b in kept)

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
    verify = config.getboolean("validator_agent", "story_bible_verify", fallback=False)
    max_fidelity_rounds = config.getint(
        "validator_agent", "story_bible_max_fidelity_rounds", fallback=1
    )

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
        verify=verify,
        max_fidelity_rounds=max_fidelity_rounds,
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
