"""tools/auto/prosody.py — AUTO-CR-21: Deterministic Russian rhythm/rhyme gate.

Phase 1 (CR-21-1): Pure prosody primitives — no LLM, no I/O, no config.
Phase 2 (CR-21-2): Prosody report, tolerant verdict, keyword activation gate.

All functions are deterministic and operate only on Unicode strings.  They are
designed to be imported and unit-tested in complete isolation from the rest of
the pipeline.

Public surface (CR-21-1)::

    _VOWELS_RU
    count_syllables_ru(line) -> int
    rhyme_key_ru(line) -> str
    rhymes_ru(a, b) -> bool
    split_stanzas(text) -> list[list[str]]
    detect_scheme(stanza) -> str

Public surface (CR-21-2)::

    is_verse_task(text) -> bool
    ProsodyReport          (dataclass)
    analyze_ru(text, *, syllable_tolerance) -> ProsodyReport
    ProsodyVerdict         (dataclass)
    check_prosody(text, *, min_scheme, syllable_tolerance, require_quatrains) -> ProsodyVerdict

Public surface (CR-22-2)::

    verse_requirements(text) -> tuple[bool, bool]   # (require_rhyme, require_rhythm)
    check_prosody(..., require_rhyme=True, require_rhythm=True) -> ProsodyVerdict
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Vowels ────────────────────────────────────────────────────────────────────

_VOWELS_RU: str = "аеёиоуыэюя"


# ── Syllable counting ─────────────────────────────────────────────────────────

def count_syllables_ru(line: str) -> int:
    """Return the number of syllables in *line* (Russian vowel count).

    Non-Cyrillic characters (Latin letters, digits, punctuation, spaces) are
    ignored.  Case-insensitive.

    >>> count_syllables_ru("Аделина КМС по гимнастике")
    9
    >>> count_syllables_ru("")
    0
    """
    return sum(ch in _VOWELS_RU for ch in line.lower())


# ── Rhyme key ─────────────────────────────────────────────────────────────────

_CYRILLIC_RE = re.compile(r"[^а-яёА-ЯЁ]")


def rhyme_key_ru(line: str) -> str:
    """Return the rhyme-bearing tail of *line* for Russian clausal rhyme.

    Algorithm:
    1. Lowercase and strip everything that is not a Cyrillic letter.
    2. Find the index of the **last** vowel in that string.
    3. Take a slice from that index to the end.
    4. If the tail is shorter than 2 characters, extend left by one letter.
    5. Return the tail; return ``""`` when the line contains no vowel.

    >>> rhyme_key_ru("гимнастика")
    'ика'
    >>> rhyme_key_ru("акробатика")
    'ика'
    >>> rhyme_key_ru("")
    ''
    """
    cleaned = _CYRILLIC_RE.sub("", line.lower())
    if not cleaned:
        return ""

    # Find last vowel index
    last_vowel_idx = -1
    for i, ch in enumerate(cleaned):
        if ch in _VOWELS_RU:
            last_vowel_idx = i

    if last_vowel_idx == -1:
        return ""  # no vowel found

    tail = cleaned[last_vowel_idx:]

    # Extend left if the tail is shorter than 2 chars
    if len(tail) < 2 and last_vowel_idx > 0:
        tail = cleaned[last_vowel_idx - 1:]

    return tail


# ── Rhyme comparison ──────────────────────────────────────────────────────────

def rhymes_ru(a: str, b: str) -> bool:
    """Return True when *a* and *b* rhyme in Russian.

    Two lines rhyme when either:
    - their :func:`rhyme_key_ru` values are equal, **or**
    - both keys are non-empty and share the same last-2-character suffix.

    >>> rhymes_ru("гимнастика", "акробатика")
    True
    >>> rhymes_ru("дома", "рядом")
    False
    """
    key_a = rhyme_key_ru(a)
    key_b = rhyme_key_ru(b)

    if not key_a or not key_b:
        return False

    if key_a == key_b:
        return True

    # Last-2-character suffix match
    if len(key_a) >= 2 and len(key_b) >= 2 and key_a[-2:] == key_b[-2:]:
        return True

    return False


# ── Stanza splitting ──────────────────────────────────────────────────────────

def split_stanzas(text: str) -> list[list[str]]:
    """Split *text* into stanzas separated by blank lines.

    Rules:
    - A stanza is a list of non-empty (after strip) lines.
    - Stanzas are separated by one or more blank lines.
    - A leading title stanza (exactly 1 line with no terminal punctuation)
      is silently dropped.
    - Stanzas with fewer than 2 lines are discarded (not useful for analysis).

    >>> stanzas = split_stanzas("строка 1\\nстрока 2\\n\\nстрока 3\\nстрока 4")
    >>> len(stanzas)
    2
    >>> stanzas[0]
    ['строка 1', 'строка 2']
    """
    stanzas: list[list[str]] = []
    current: list[str] = []

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped:
            current.append(stripped)
        else:
            if current:
                stanzas.append(current)
                current = []

    if current:
        stanzas.append(current)

    # Drop leading title: a stanza of exactly 1 line with no terminal punctuation
    if stanzas and len(stanzas[0]) == 1:
        title_line = stanzas[0][0]
        terminal_punct = set(".,!?;:…—–")
        if not (title_line and title_line[-1] in terminal_punct):
            stanzas = stanzas[1:]

    # Discard stanzas with fewer than 2 lines (not useful for analysis)
    stanzas = [s for s in stanzas if len(s) >= 2]

    return stanzas


# ── Rhyme-scheme detection ────────────────────────────────────────────────────

def detect_scheme(stanza: list[str]) -> str:
    """Detect the rhyme scheme of a 4-line *stanza*.

    Returns one of: ``"AABB"``, ``"ABAB"``, ``"ABCB"``, or ``"NONE"``.
    First match in that order wins.

    For stanzas that are not exactly 4 lines, the function still attempts
    detection on the first 4 lines; if fewer than 4 lines are supplied it
    returns ``"NONE"``.

    >>> detect_scheme(["дома", "зима", "трава", "слова"])
    'AABB'
    """
    if len(stanza) < 4:
        return "NONE"

    l1, l2, l3, l4 = stanza[0], stanza[1], stanza[2], stanza[3]

    # AABB: lines 1&2 rhyme AND lines 3&4 rhyme
    if rhymes_ru(l1, l2) and rhymes_ru(l3, l4):
        return "AABB"

    # ABAB: lines 1&3 rhyme AND lines 2&4 rhyme
    if rhymes_ru(l1, l3) and rhymes_ru(l2, l4):
        return "ABAB"

    # ABCB: only lines 2&4 rhyme
    if rhymes_ru(l2, l4):
        return "ABCB"

    return "NONE"


# ═══════════════════════════════════════════════════════════════════════════════
# CR-21-2 — Prosody report + verdict + keyword gate
# ═══════════════════════════════════════════════════════════════════════════════

# ── Keyword activation gate ───────────────────────────────────────────────────

def is_verse_task(text: str) -> bool:
    """Return True if *text* signals a Russian verse/poem task.

    AUTO-CR-22-2: widened from the original bare ``ритм``/``рифм`` substring
    check to also recognise poem nouns, so a request like
    "напиши стихотворение про осень" (no ритм/рифм keyword at all) still
    activates the gate. Activates on any of:

    * substrings ``"ритм"`` / ``"рифм"``;
    * poem stems: ``стихотворен``, ``четверостиш``, ``двустиш``, ``верлибр``,
      ``сонет``, ``поэм``;
    * the phrase ``"белый стих"``;
    * whole-word forms of ``стих`` (стих, стихи, стиха, стиху, стихов,
      стихам, стихах, стихе) and ``стиш`` diminutives (стишок, стишки,
      стишка) — matched with word boundaries so it does **not** false-fire
      on unrelated words sharing the same prefix (стихия, стихийн-,
      стихать, утихать, стихл-, etc.).

    This is the **only** activation switch for the prosody gate. When it
    returns False the gate is a no-op — non-verse creative tasks are never
    penalised.

    >>> is_verse_task("напиши стихи с ритмом и рифмой")
    True
    >>> is_verse_task("напиши стихотворение про осень")
    True
    >>> is_verse_task("исправь нестыковки")
    False
    >>> is_verse_task("рассказ про морскую стихию")
    False
    """
    lower = text.lower()
    if "ритм" in lower or "рифм" in lower:
        return True
    if any(stem in lower for stem in _VERSE_STEMS):
        return True
    if "белый стих" in lower:
        return True
    if _VERSE_WORD_RE.search(lower) or _VERSE_DIM_RE.search(lower):
        return True
    return False


# Poem-noun stems — any substring match activates the gate.
_VERSE_STEMS: tuple[str, ...] = (
    "стихотворен", "четверостиш", "двустиш", "верлибр", "сонет", "поэм",
)

# Whole-word forms of стих / стиш — word-boundary anchored so the shared
# "стих" prefix in стихия / стихийн- / стихать / утихать / стихл- never
# matches (those words have no boundary right after the prefix or its
# listed endings; see is_verse_task's docstring). The suffix group is
# REQUIRED (not optional): the bare word "стих" is also the masculine past
# tense of "стихнуть" (to die down/abate, e.g. "ветер стих к утру") — an
# unsuffixed match would misfire on that verb, so only inflected noun forms
# (стихи, стиха, стиху, стихов, стихам, стихах, стихе) trigger the gate.
_VERSE_WORD_RE = re.compile(r"\bстих(и|а|у|ов|ам|ах|е)\b")
_VERSE_DIM_RE  = re.compile(r"\bстиш(ок|ки|ка)\b")


def verse_requirements(text: str) -> tuple[bool, bool]:
    """Return ``(require_rhyme, require_rhythm)`` for a verse task's *text*.

    AUTO-CR-22-2: lets a prompt request rhyme and rhythm **independently**,
    so blank verse (белый стих / верлибр — regular rhythm, no rhyme) and a
    rhyme-only request both work, instead of ``check_prosody`` always
    demanding both.

    Rules
    -----
    * ``no_rhyme``  — true when the text says "без рифм", "белый стих", or
      "верлибр" (these forms explicitly disclaim rhyme).
    * ``rhyme_kw``  — true when "рифм" appears anywhere in the text.
    * ``rhythm_kw`` — true when "ритм" appears anywhere in the text.
    * ``generic``   — a verse task (:func:`is_verse_task`) that mentions
      neither keyword (e.g. plain "стихотворение") — defaults to both.

    ``require_rhyme  = (rhyme_kw or generic) and not no_rhyme``
    ``require_rhythm = rhythm_kw or generic or no_rhyme``

    Examples
    --------
    >>> verse_requirements("стихи с ритмом, без рифмы")
    (False, True)
    >>> verse_requirements("напиши стихотворение")
    (True, True)
    >>> verse_requirements("белый стих")
    (False, True)
    """
    t = text.lower()
    no_rhyme  = ("без рифм" in t) or ("белый стих" in t) or ("верлибр" in t)
    rhyme_kw  = "рифм" in t
    rhythm_kw = "ритм" in t
    generic   = is_verse_task(t) and not (rhyme_kw or rhythm_kw)

    require_rhyme  = (rhyme_kw or generic) and not no_rhyme
    require_rhythm = rhythm_kw or generic or no_rhyme
    return require_rhyme, require_rhythm


# ── ProsodyReport ─────────────────────────────────────────────────────────────

@dataclass
class StanzaInfo:
    """Per-stanza analysis results (internal helper used by ProsodyReport)."""
    index: int               # 1-based stanza number
    lines: list[str]
    syllables: list[int]     # syllable count per line
    scheme: str              # one of AABB / ABAB / ABCB / NONE


@dataclass
class ProsodyReport:
    """Full prosody analysis of a multi-stanza Russian poem.

    Attributes
    ----------
    stanza_count:
        Number of analysed stanzas (those with >= 2 lines).
    stanzas:
        Per-stanza breakdown — syllable counts and detected rhyme scheme.
    syllable_regular:
        True when every line position's syllable count stays within
        ``syllable_tolerance`` across all stanzas (positional regularity).
    """
    stanza_count: int
    stanzas: list[StanzaInfo] = field(default_factory=list)
    syllable_regular: bool = True


# ── analyze_ru ────────────────────────────────────────────────────────────────

def analyze_ru(text: str, *, syllable_tolerance: int = 2) -> ProsodyReport:
    """Analyse *text* as a Russian poem and return a :class:`ProsodyReport`.

    The analysis:
    1. Splits text into stanzas via :func:`split_stanzas`.
    2. Counts syllables per line and detects the rhyme scheme per stanza.
    3. Checks *positional* syllable regularity: for each line position N,
       the syllable counts across stanzas must stay within
       ``syllable_tolerance`` of the first stanza's count at that position.

    Empty or non-Russian input returns an empty report with
    ``syllable_regular=True`` (fail-open).
    """
    raw_stanzas = split_stanzas(text)
    if not raw_stanzas:
        return ProsodyReport(stanza_count=0, stanzas=[], syllable_regular=True)

    stanza_infos: list[StanzaInfo] = []
    for idx, lines in enumerate(raw_stanzas, start=1):
        syllables = [count_syllables_ru(line) for line in lines]
        scheme = detect_scheme(lines)
        stanza_infos.append(StanzaInfo(index=idx, lines=lines, syllables=syllables, scheme=scheme))

    # Positional syllable regularity check
    # Use the first stanza as reference for each line position
    syllable_regular = True
    ref = stanza_infos[0].syllables
    for info in stanza_infos[1:]:
        for pos, count in enumerate(info.syllables):
            if pos < len(ref):
                if abs(count - ref[pos]) > syllable_tolerance:
                    syllable_regular = False
                    break
        if not syllable_regular:
            break

    return ProsodyReport(
        stanza_count=len(stanza_infos),
        stanzas=stanza_infos,
        syllable_regular=syllable_regular,
    )


# ── ProsodyVerdict ────────────────────────────────────────────────────────────

# Acceptance sets for min_scheme parameter
_SCHEME_ACCEPTS: dict[str, set[str]] = {
    "ABCB": {"AABB", "ABAB", "ABCB"},   # loosest — accepts all rhyming schemes
    "ABAB": {"AABB", "ABAB"},
    "AABB": {"AABB"},
}


@dataclass
class ProsodyVerdict:
    """Outcome of :func:`check_prosody`.

    Attributes
    ----------
    approved:
        True when the poem passes all enabled checks (or the gate failed
        open due to an internal error).
    reason:
        Short machine-readable reason when ``approved`` is False.
    """
    approved: bool
    reason: str

    def feedback(self) -> str:
        """Return a coder-facing feedback string, mirroring FactValidator style.

        Returns an empty string when approved.
        """
        if self.approved:
            return ""
        return f"PROSODY ISSUE — {self.reason}"


# ── check_prosody ─────────────────────────────────────────────────────────────

def check_prosody(
    text: str,
    *,
    min_scheme: str = "ABCB",
    syllable_tolerance: int = 2,
    require_quatrains: bool = True,
    require_rhyme: bool = True,
    require_rhythm: bool = True,
) -> ProsodyVerdict:
    """Check *text* for rhythm and/or rhyme compliance.

    Parameters
    ----------
    text:
        The poem text to check.
    min_scheme:
        The minimum acceptable rhyme scheme, one of ``"ABCB"`` (loosest,
        default), ``"ABAB"``, or ``"AABB"`` (strictest).  Schemes *better*
        than the minimum are also accepted (see ``_SCHEME_ACCEPTS``).
    syllable_tolerance:
        How many syllables a line may deviate from the reference stanza
        before the rhythm check fires.  Default is 2.
    require_quatrains:
        When True, any analysed stanza that is not exactly 4 lines causes
        a REVISE verdict. Independent of ``require_rhyme``/``require_rhythm``.
    require_rhyme:
        AUTO-CR-22-2. When False, the rhyme-scheme rule (Rule 2) is skipped
        entirely — lets blank verse / verlibre pass without a rhyme scheme.
    require_rhythm:
        AUTO-CR-22-2. When False, the syllable-regularity rule (Rule 3) is
        skipped entirely. When True but only a single stanza is present,
        there is nothing to compare against, so the rule is satisfied
        (no REVISE) rather than failing open or closed arbitrarily.

    Returns
    -------
    ProsodyVerdict
        ``approved=True`` when all enabled checks pass.  ``approved=False``
        with a specific ``reason`` when a violation is detected.  **Never
        raises** -- any internal exception returns
        ``ProsodyVerdict(True, "")`` (fail-open).
    """
    try:
        return _check_prosody_inner(
            text,
            min_scheme=min_scheme,
            syllable_tolerance=syllable_tolerance,
            require_quatrains=require_quatrains,
            require_rhyme=require_rhyme,
            require_rhythm=require_rhythm,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        logger.warning("check_prosody: internal error — %s", exc)
        return ProsodyVerdict(approved=True, reason="")


def _check_prosody_inner(
    text: str,
    *,
    min_scheme: str,
    syllable_tolerance: int,
    require_quatrains: bool,
    require_rhyme: bool = True,
    require_rhythm: bool = True,
) -> ProsodyVerdict:
    """Inner implementation — may raise; wrapped by :func:`check_prosody`."""
    if not text or not text.strip():
        return ProsodyVerdict(approved=True, reason="")

    report = analyze_ru(text, syllable_tolerance=syllable_tolerance)

    if report.stanza_count == 0:
        return ProsodyVerdict(approved=True, reason="")

    accepts = _SCHEME_ACCEPTS.get(min_scheme, _SCHEME_ACCEPTS["ABCB"])

    # ── Rule 1: require quatrains ─────────────────────────────────────────────
    if require_quatrains:
        for info in report.stanzas:
            if len(info.lines) != 4:
                reason = (
                    f"stanza {info.index} has {len(info.lines)} lines "
                    f"(expected 4 lines per quatrain); "
                    f"revise so that each stanza contains exactly 4 lines"
                )
                return ProsodyVerdict(approved=False, reason=reason)

    # ── Rule 2: rhyme check (AUTO-CR-22-2: skipped when rhyme not required) ───
    if require_rhyme:
        for info in report.stanzas:
            if info.scheme not in accepts:
                lines = info.lines
                tail2 = rhyme_key_ru(lines[1]) if len(lines) > 1 else ""
                tail4 = rhyme_key_ru(lines[3]) if len(lines) > 3 else ""
                reason = (
                    f"stanza {info.index} has no acceptable rhyme "
                    f"(detected scheme: {info.scheme}, required: >={min_scheme}); "
                    f"lines 2/4 endings: \u00ab{tail2}\u00bb / \u00ab{tail4}\u00bb; "
                    f"revise so that at least lines 2 and 4 rhyme"
                )
                return ProsodyVerdict(approved=False, reason=reason)

    # ── Rule 3: syllable regularity (AUTO-CR-22-2: skipped when rhythm not
    # required; with a single stanza there's nothing to compare against, so
    # it's treated as satisfied — analyze_ru already leaves
    # syllable_regular=True in that case). ──────────────────────────────────
    if require_rhythm and not report.syllable_regular:
        ref = report.stanzas[0].syllables
        for info in report.stanzas[1:]:
            for pos, count in enumerate(info.syllables):
                if pos < len(ref) and abs(count - ref[pos]) > syllable_tolerance:
                    reason = (
                        f"line {pos + 1}: {count} syllables in stanza {info.index} "
                        f"vs {ref[pos]} in stanza 1; "
                        f"keep them within \u00b1{syllable_tolerance}"
                    )
                    return ProsodyVerdict(approved=False, reason=reason)

    return ProsodyVerdict(approved=True, reason="")


# ═══════════════════════════════════════════════════════════════════════════════
# CR-21-3 — ProsodyValidator wrapper + config factory (inner-loop wiring)
# ═══════════════════════════════════════════════════════════════════════════════

class ProsodyValidator:
    """Thin wrapper exposing the Gate-3-style ``.check()`` contract.

    Mirrors :class:`tools.auto.fact_validator.FactValidator`'s surface so the
    inner loop can treat it identically: ``.check(task, text) -> ProsodyVerdict``
    and a ``.max_prosody_revisions`` cap.

    ``check`` only activates when the task's goal/instruction contains the
    ``ритм``/``рифм`` keyword (:func:`is_verse_task`); otherwise it is a no-op
    that always returns an approved verdict.
    """

    def __init__(
        self,
        *,
        max_prosody_revisions: int = 2,
        min_scheme: str = "ABCB",
        syllable_tolerance: int = 2,
        require_quatrains: bool = True,
    ):
        self.max_prosody_revisions = max(0, int(max_prosody_revisions))
        self._min_scheme = min_scheme
        self._syllable_tolerance = int(syllable_tolerance)
        self._require_quatrains = bool(require_quatrains)

    def check(self, task: dict, text: str) -> ProsodyVerdict:
        """Return a :class:`ProsodyVerdict` for *text* given *task*.

        No-op (always approved) unless the task's goal+instruction contains
        a verse signal (:func:`is_verse_task` — keywords or poem nouns).
        AUTO-CR-22-2: once active, rhyme and rhythm are required
        *independently*, per :func:`verse_requirements` — so blank verse
        (rhythm only) and rhyme-only requests are both handled correctly
        instead of always demanding both.  Never raises (delegates to
        ``check_prosody``, which is itself fail-open).
        """
        try:
            goal        = task.get("goal", "") or ""
            instruction = task.get("instruction", "") or ""
            combined    = f"{goal} {instruction}"
            if not is_verse_task(combined):
                return ProsodyVerdict(approved=True, reason="")
            require_rhyme, require_rhythm = verse_requirements(combined)
            return check_prosody(
                text,
                min_scheme=self._min_scheme,
                syllable_tolerance=self._syllable_tolerance,
                require_quatrains=self._require_quatrains,
                require_rhyme=require_rhyme,
                require_rhythm=require_rhythm,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            logger.warning("ProsodyValidator.check: internal error — %s", exc)
            return ProsodyVerdict(approved=True, reason="")


def make_prosody_validator(config) -> "ProsodyValidator | None":
    """Construct a :class:`ProsodyValidator` from *config*, or ``None``.

    Reads ``[validator_agent]``:
      - ``prosody_check_creative`` (bool, default False) — master switch.
      - ``max_prosody_revisions``  (int,  default 2)
      - ``prosody_min_scheme``     (str,  default "ABCB")
      - ``prosody_syllable_tolerance`` (int, default 2)

    Returns ``None`` when ``prosody_check_creative`` is falsy/absent.
    """
    try:
        enabled = config.getboolean(
            "validator_agent", "prosody_check_creative", fallback=False
        )
    except Exception:  # noqa: BLE001 — tolerate malformed config
        enabled = False

    if not enabled:
        return None

    max_rev = config.getint("validator_agent", "max_prosody_revisions", fallback=2)
    min_scheme = config.get("validator_agent", "prosody_min_scheme", fallback="ABCB")
    syllable_tolerance = config.getint(
        "validator_agent", "prosody_syllable_tolerance", fallback=2
    )

    return ProsodyValidator(
        max_prosody_revisions=max_rev,
        min_scheme=min_scheme,
        syllable_tolerance=syllable_tolerance,
    )
