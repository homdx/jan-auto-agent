"""tools/auto/prosody.py — AUTO-CR-21: Deterministic Russian rhythm/rhyme gate.

Phase 1 (CR-21-1): Pure prosody primitives — no LLM, no I/O, no config.

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
"""

from __future__ import annotations

import re

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
