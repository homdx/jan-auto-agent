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
import json
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
    "character names, relationships, fixed attributes (age, occupation, "
    "defining physical traits), world rules, and long-term promises/goals. "
    "One short bullet per fact. "
    "NO events, NO dialogue, NO prose, NO parentheses. "
    "Keep each fact a single plain statement. "
    "Do NOT record where characters currently are, what they are doing or wearing "
    "in this scene, or anything that changes scene-to-scene. "
    "Record only PERMANENT attributes. Do NOT turn a momentary description into "
    "a permanent trait (e.g. 'seemed darker in the sunset' is NOT 'dark hair'). "
    "Preserve negations and qualifiers exactly (e.g. 'secret cargo', 'not "
    "allowed to know', 'does not work'). "
    "Record each main character's gender when the text makes it clear (e.g. "
    "write a line like '<имя персонажа> — женщина' / '<имя персонажа> — мужчина')."
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


_GENDER_FEMALE_RE = re.compile(
    r"женщин|женского|девушк|female|\bона\b", re.IGNORECASE
)
_GENDER_MALE_RE = re.compile(
    r"мужчин|мужского|парень|male|\bон\b", re.IGNORECASE
)

_AGE_RE = re.compile(r"(\d+)\s*(?:лет|год)", re.IGNORECASE)
# AUTO-BUG: no human age is >= 120, but a calendar-year reference like
# "в 1990 году" / "к 2020 году" / "с 1985 года" matches _AGE_RE too — "год"
# is an unanchored prefix of "году"/"года", so "1990 году" was being read as
# age=1990. That false "age" then fed straight into the immutable-fact
# conflict gate below, so a bullet stating a character's BIRTH YEAR and a
# later bullet stating their AGE (two compatible facts) were flagged as a
# contradiction and one of them silently dropped. Cap plausible human age.
_MAX_PLAUSIBLE_AGE = 119

# AUTO-BUG-2 (extended): a second example of a guarded, narrowly but
# deterministically detectable "immutable-ish" attribute besides
# gender/age -- recovery/health status. Full generalisation to arbitrary
# contradicting facts would need semantic (LLM) comparison; this mirrors
# the same narrow, false-positive-resistant pattern as gender/age instead
# of attempting that.
_RECOVERY_FULL_RE = re.compile(
    r"полностью (?:выздоровел|восстановил(?:ся|ась)|поправил(?:ся|ась))", re.IGNORECASE,
)
_RECOVERY_PARTIAL_RE = re.compile(
    r"(?:постепенно|частично) восстанавлива|обрывками|не (?:полностью|до конца) "
    r"(?:восстанов|поправ)|рука не слушается|речь (?:ещё )?путается",
    re.IGNORECASE,
)


def _recovery_status_of(bullet: str) -> "str | None":
    """Return 'full', 'partial', or None if *bullet* asserts a recovery status."""
    if _RECOVERY_FULL_RE.search(bullet):
        return "full"
    if _RECOVERY_PARTIAL_RE.search(bullet):
        return "partial"
    return None


_AGE_UNKNOWN_RE = re.compile(r"не\s+указан|неизвестен", re.IGNORECASE)
_AGE_WORDS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
}
_AGE_WORD_RE = re.compile(
    r"\b(" + "|".join(_AGE_WORDS) + r")\b", re.IGNORECASE
)

_NAME_RE = re.compile(r"\b[А-ЯЁ][а-яёА-ЯЁ]{2,}\b")
# Common non-name capitalised words that should not be treated as entity names.
# Includes pronouns/demonstratives and generic role nouns that are capitalised
# only because they start a sentence (bugfix: these were being misread as
# character names, e.g. "Она сказала..." -> name "Она", "Главный герой..."
# -> name "Главный" — which corrupted the entity-scoping in
# _conflicts_with_established below).
_NAME_STOPWORDS = {
    "Капитан", "Команда", "Корабль", "Мостик", "Возраст",
    "Она", "Его", "Ему", "Себя", "Этот", "Эта", "Это",
    "Главный", "Главная", "Герой", "Героиня", "Рассказчик", "Рассказчица",
    "Персонаж", "Персонажи",
}


def _gender_of(bullet: str) -> "str | None":
    """Return ``'f'``, ``'m'``, or ``None`` if *bullet* asserts a gender."""
    if _GENDER_FEMALE_RE.search(bullet):
        return "f"
    if _GENDER_MALE_RE.search(bullet):
        return "m"
    return None


def _age_of(bullet: str) -> "int | str | None":
    """Return an int age, the sentinel ``"unknown"`` (for «не указан» /
    «неизвестен» style assertions), or ``None`` if *bullet* asserts no age.
    """
    if _AGE_UNKNOWN_RE.search(bullet):
        return "unknown"
    # AUTO-BUG fix: a bullet can contain BOTH a calendar year ("в 1990
    # году") and a genuine age ("ей было 45 лет") — scan every match and
    # take the first one that is a plausible human age instead of just the
    # first match found, so the calendar year no longer shadows the real
    # age (or gets misread as one when it's the only \d+ (лет|год) hit).
    for m in _AGE_RE.finditer(bullet):
        value = int(m.group(1))
        if value <= _MAX_PLAUSIBLE_AGE:
            return value
    w = _AGE_WORD_RE.search(bullet)
    if w:
        return _AGE_WORDS[w.group(1).lower()]
    return None


def _names_in(bullet: str) -> set[str]:
    """Return the set of capitalised Cyrillic tokens (len >= 3) in *bullet*,
    used to scope an immutable fact to one or more entities. Best-effort —
    not true NER.
    """
    return {tok for tok in _NAME_RE.findall(bullet) if tok not in _NAME_STOPWORDS}


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

    return [b for b, k in zip(bullets, keep, strict=True) if k]


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
        immutable_guard: bool = True,
        semantic_guard: bool = True,
    ) -> None:
        self._llm = llm_call
        self._base_dir = Path(base_dir)
        self._path = self._base_dir / path
        self._max_chars = max(100, int(max_chars))
        self._verify = bool(verify)
        self._immutable_guard = bool(immutable_guard)
        # Litera-sim fix: LLM-backed semantic conflict gate on merge. The
        # deterministic _find_conflict covers only gender/age/recovery; any
        # other hallucinated fact that contradicts an established one (e.g.
        # «долг полностью погашен» vs «долг за три месяца аренды») used to be
        # APPENDED alongside it, leaving the bible self-contradictory forever
        # and burning continuity-revision caps on perfectly correct chapters.
        self._semantic_guard = bool(semantic_guard)
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
            logger.warning("StoryBible.update: no new facts extracted — skipping write. (AUTO-BUG-5: if this happens every chapter, the model may not be following the bullet-list format at all.)")
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
            logger.warning("StoryBible.update: no new facts extracted — skipping write. (AUTO-BUG-5: if this happens every chapter, the model may not be following the bullet-list format at all.)")
            return

        existing_text = self.load()
        existing_bullets = _parse_bullets(existing_text)
        existing_norms = {_normalise(b) for b in existing_bullets}

        # Litera-sim fix: ONE batched semantic conflict check for all genuinely
        # new facts against the established bible, feeding the same
        # correction-attempt machinery as the deterministic guard — so a
        # hallucinated contradiction is dropped on arrival, while a REAL
        # correction (proposed consistently by _CORRECTION_THRESHOLD
        # independent chapters) still eventually replaces a bad established
        # bullet.
        # AUTO-FIX: gated on self._semantic_guard, NOT self._immutable_guard.
        # These are two independent config flags (story_bible_immutable_guard
        # / story_bible_semantic_guard) — the previous version gated this
        # call on immutable_guard alone, so setting immutable_guard=false
        # silently disabled the semantic gate too, regardless of its own
        # flag. _semantic_conflicts() already no-ops when self._semantic_guard
        # is false, so gating the call here on the same flag just skips the
        # (redundant) LLM call in that case rather than changing behavior.
        _candidates = [f for f in new_facts if _normalise(f) not in existing_norms]
        _semantic_map = (
            self._semantic_conflicts(_candidates, existing_bullets)
            if self._semantic_guard else {}
        )

        merged: list[str] = list(existing_bullets)
        added = 0
        for fact in new_facts:
            if _normalise(fact) in existing_norms:
                continue
            # AUTO-FIX: each guard now checks independently — a fact can be
            # dropped/corrected by the deterministic check, the semantic
            # check, or both, and either guard alone is enough to enter this
            # branch (previously the whole branch — including the semantic
            # lookup — was nested under immutable_guard, so semantic_guard
            # had no effect when immutable_guard was off).
            if self._immutable_guard or self._semantic_guard:
                conflict_bullet = None
                if self._immutable_guard:
                    conflict_bullet = self._find_conflict(fact, merged)
                if conflict_bullet is None and self._semantic_guard:
                    _sem = _semantic_map.get(fact)
                    # The semantic map was computed against the pre-merge
                    # bible; only honour it if the flagged bullet is still
                    # present (it may have been replaced by a correction
                    # earlier in this same loop).
                    if _sem is not None and _sem in merged:
                        conflict_bullet = _sem
                if conflict_bullet is not None:
                    if self._register_correction_attempt(fact, conflict_bullet):
                        # AUTO-BUG-2 fix: the SAME correction has now been
                        # proposed by _CORRECTION_THRESHOLD independent
                        # chapters in a row — treat it as a genuine fix to
                        # a bad first extraction rather than an attack /
                        # one-off model slip, and replace the established
                        # bullet instead of dropping the correction forever.
                        logger.warning(
                            "StoryBible: accepting correction %r -> %r after "
                            "%d consistent re-observations.",
                            conflict_bullet, fact, self._CORRECTION_THRESHOLD,
                        )
                        merged.remove(conflict_bullet)
                        existing_norms.discard(_normalise(conflict_bullet))
                    else:
                        logger.warning(
                            "StoryBible: dropped contradicting immutable fact %r "
                            "(conflicts with established %r)", fact, conflict_bullet,
                        )
                        continue
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

    def _find_conflict(self, new_fact: str, established: list[str]) -> "str | None":
        """AUTO-CR-26-2 (write-once guard), reworked for AUTO-BUG-2.

        Returns the conflicting established bullet (or None) instead of a
        bare bool, so the caller can decide whether to drop the new fact
        or -- if the same correction keeps being independently re-observed
        -- accept it as a fix and replace the established bullet.

        Bugfix retained from the original: a fact with NO detected name
        (e.g. a pronoun-only assertion like "Ei 45 let") cannot be safely
        scoped to one entity, so it is let through unchecked instead of
        being compared against every established bullet.
        """
        new_gender = _gender_of(new_fact)
        new_age = _age_of(new_fact)
        new_recovery = _recovery_status_of(new_fact)
        if new_gender is None and new_age is None and new_recovery is None:
            return None

        new_names = _names_in(new_fact)
        if not new_names:
            return None  # unscoped -- cannot safely compare to one entity

        for existing in established:
            if not (new_names & _names_in(existing)):
                continue  # scoped to a different entity -- no conflict

            if new_gender is not None:
                existing_gender = _gender_of(existing)
                if existing_gender is not None and existing_gender != new_gender:
                    return existing

            if new_age is not None:
                existing_age = _age_of(existing)
                if existing_age is not None and existing_age != new_age:
                    return existing

            if new_recovery is not None:
                existing_recovery = _recovery_status_of(existing)
                if existing_recovery is not None and existing_recovery != new_recovery:
                    return existing

        return None

    def _conflicts_with_established(self, new_fact: str, established: list[str]) -> bool:
        """Backward-compatible bool wrapper around :meth:`_find_conflict`."""
        return self._find_conflict(new_fact, established) is not None

    # ── Litera-sim fix: semantic (LLM) conflict gate ──────────────────────────
    _SEMANTIC_CONFLICT_SYSTEM = (
        "You are a strict continuity judge for a story bible. You get "
        "ESTABLISHED facts and NEW candidate facts, each numbered. A NEW fact "
        "CONFLICTS with an ESTABLISHED fact only if both cannot be true at "
        "the same time in the story (direct contradiction), e.g. one says a "
        "debt is unpaid and the other says the same debt is fully repaid. "
        "Rephrasings, additional details, or unrelated facts are NOT "
        "conflicts. Reply with one line per conflict in the exact form "
        "'CONFLICT <new_number> <established_number>' and nothing else. If "
        "there are no conflicts reply exactly 'NONE'."
    )

    def _semantic_conflicts(
        self, new_facts: list[str], established: list[str]
    ) -> dict[str, str]:
        """Return ``{new_fact: conflicting_established_bullet}`` via ONE
        batched LLM call. Fail-open: any error or unparsable reply -> ``{}``
        (merge proceeds exactly as before this gate existed).

        This is the semantic complement to the deterministic
        :meth:`_find_conflict` (gender/age/recovery). One extra LLM call per
        chapter is the accepted price for a bible that cannot silently hold
        two mutually exclusive facts.
        """
        if not self._semantic_guard or not new_facts or not established:
            return {}
        try:
            est_block = "\n".join(f"E{i + 1}. {b}" for i, b in enumerate(established))
            new_block = "\n".join(f"N{i + 1}. {b}" for i, b in enumerate(new_facts))
            user = (f"ESTABLISHED FACTS:\n{est_block}\n\n"
                    f"NEW CANDIDATE FACTS:\n{new_block}\n\n"
                    f"List conflicts, or NONE.")
            reply = (self._llm(self._SEMANTIC_CONFLICT_SYSTEM, user) or "").strip()
            conflicts: dict[str, str] = {}
            for m in re.finditer(r"CONFLICT\s+N?(\d+)\s+E?(\d+)", reply, re.IGNORECASE):
                ni, ei = int(m.group(1)) - 1, int(m.group(2)) - 1
                if 0 <= ni < len(new_facts) and 0 <= ei < len(established):
                    conflicts[new_facts[ni]] = established[ei]
            if conflicts:
                logger.warning(
                    "StoryBible: semantic conflict gate flagged %d new fact(s) "
                    "as contradicting established facts.", len(conflicts),
                )
            return conflicts
        except Exception as exc:  # noqa: BLE001 — fail-open contract
            logger.warning(
                "StoryBible: semantic conflict gate error (%s) — proceeding "
                "without it (fail-open).", exc,
            )
            return {}


    # AUTO-BUG-2: number of independent chapters that must propose the exact
    # same correction before it overrides a previously "locked" immutable
    # fact. Persisted to disk (not just in-memory) because in this pipeline
    # each chapter is typically a separate `main.py --auto` process.
    _CORRECTION_THRESHOLD = 2

    def _pending_corrections_path(self) -> Path:
        return self._path.with_suffix(self._path.suffix + ".pending.json")

    def _register_correction_attempt(self, new_fact: str, conflict_bullet: str) -> bool:
        """Track repeated attempts to correct *conflict_bullet* to *new_fact*.

        Returns True once the same correction has been independently
        proposed `_CORRECTION_THRESHOLD` times (and clears its counter),
        meaning the caller should accept it; False otherwise (counter was
        just incremented and persisted).
        """
        key = _normalise(conflict_bullet)
        proposal = _normalise(new_fact)
        path = self._pending_corrections_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            state = {}

        entry = state.get(key)
        if entry is not None and entry.get("proposal") == proposal:
            count = int(entry.get("count", 1)) + 1
        else:
            count = 1

        if count >= self._CORRECTION_THRESHOLD:
            state.pop(key, None)
            try:
                path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                logger.warning("StoryBible: could not persist pending-corrections state: %s", exc)
            return True

        state[key] = {"proposal": proposal, "count": count}
        try:
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("StoryBible: could not persist pending-corrections state: %s", exc)
        return False

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
    # AUTO-BUG-1 fix: default to enabled. The bible's continuity-anchoring
    # was designed to always run in creative mode; requiring an opt-in flag
    # that the shipped agents.ini never actually set left the whole
    # subsystem (and its story_bible_verify / story_bible_immutable_guard
    # sub-settings) silently inert out of the box.
    enabled = config.getboolean("validator_agent", "story_bible_creative", fallback=True)
    if not enabled:
        logger.warning("make_story_bible: story_bible_creative=false — bible disabled "
                        "(durable cross-chapter facts will NOT be tracked).")
        return None

    max_chars = config.getint("validator_agent", "story_bible_max_chars", fallback=2000)
    verify = config.getboolean("validator_agent", "story_bible_verify", fallback=False)
    max_fidelity_rounds = config.getint(
        "validator_agent", "story_bible_max_fidelity_rounds", fallback=1
    )
    immutable_guard = config.getboolean(
        "validator_agent", "story_bible_immutable_guard", fallback=True
    )
    # Litera-sim fix: LLM semantic conflict gate on merge (see StoryBible).
    # Default ON — one extra LLM call per chapter is the accepted price for
    # a bible that cannot hold two mutually exclusive facts.
    semantic_guard = config.getboolean(
        "validator_agent", "story_bible_semantic_guard", fallback=True
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
        immutable_guard=immutable_guard,
        semantic_guard=semantic_guard,
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
    # AUTO-FIX (fable follow-up 3): the bible generator used to borrow
    # [validator_agent] max_tokens (a cap sized for a short JSON verdict /
    # numbered critique). Raising the validator budget silently inflated the
    # bible and vice versa, and the 200-token fallback is ~13 lines — far too
    # small for a whole-story bible in Cyrillic (~2 chars/token). Give the
    # bible its own key with a creative-sized default; keep the old
    # validator_agent value as a secondary fallback so existing tuned
    # configs don't regress.
    _legacy_mt = config.getint("validator_agent", "max_tokens", fallback=200)
    max_tokens = config.getint("story_bible", "max_tokens",
                               fallback=max(1000, _legacy_mt))
    # Same context-window forwarding as everywhere else: 0 = server default.
    active_profile = config.get("api", "active", fallback="local")
    num_ctx = config.getint(f"api_{active_profile}", "num_ctx", fallback=0)
    # AUTO-FIX (fable follow-up 3): thinking models (qwen3) wrap output in
    # <think>…</think>; with a bounded num_predict the reply can truncate
    # mid-think and the bible comes back empty — or worse, reasoning lines
    # that happen to match the FACT markers get PERSISTED into
    # story_bible.md and re-injected into every later chapter prompt.
    # Mirror the gate1/architect/coder toggle: default off, re-enable via
    # [story_bible] think = true.
    think = config.getboolean("story_bible", "think", fallback=False)

    ssl_context: ssl.SSLContext | None = _llm_stream.make_unverified_context() if not verify_ssl else None

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
            _opts: dict = {"temperature": temperature, "num_predict": max_tokens}
            if num_ctx:
                _opts["num_ctx"] = num_ctx
            payload: dict = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": _opts,
            }
            if not think:
                payload["think"] = False
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
        # AUTO-FIX (fable follow-up 3): strip <think> reasoning BEFORE the
        # fact-marker parser sees the text — otherwise reasoning lines can be
        # mistaken for facts and persisted into story_bible.md.
        return _llm_stream.strip_think(
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
