"""tests/test_cr22_2_flexible_verse.py — AUTO-CR-22-2 acceptance tests.

Covers the CR-22-2 widening of the prosody gate's trigger
(:func:`is_verse_task`) and the new independent rhyme/rhythm requirement
split (:func:`verse_requirements`), plus the corresponding
``check_prosody(..., require_rhyme=, require_rhythm=)`` behaviour.

All tests are purely deterministic — no LLM, no I/O, no config.
"""

from __future__ import annotations

from tools.auto.prosody import (
    check_prosody,
    is_verse_task,
    verse_requirements,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

# Two quatrains, NONE rhyme scheme (no AABB/ABAB/ABCB anywhere), but
# positionally syllable-regular (8-8-8-8 in both stanzas) — i.e. "blank
# verse": regular rhythm, no rhyme.
_BLANK_VERSE_TWO_STANZAS = """\
Тихо падает первый снег
Город молчит под фонарём
Где-то вдали гудит мотор
Ветер качает старый дом

Утром проснётся серый кот
Выйдет на улицу гулять
Где-то вдали гудит завод
Листья летят, кружась, кружась
"""

# Same first stanza alone (single stanza, NONE scheme) — used to confirm the
# rhythm rule cannot fire with nothing to compare against.
_SINGLE_STANZA_NO_RHYME = """\
Тихо падает первый снег
Город молчит под фонарём
Где-то вдали гудит мотор
Ветер качает старый дом
"""

# Clean AABB quatrain — rhymes (used for the rhyme-required regression check
# in the negative direction: take an unrhymed quatrain instead).
_UNRHYMED_SINGLE_QUATRAIN = _SINGLE_STANZA_NO_RHYME


# ── is_verse_task: trigger widening ───────────────────────────────────────────

def test_trigger_poem_nouns():
    for text in (
        "напиши стихотворение про осень",
        "сочини четверостишие",
        "белый стих",
        "верлибр",
        "сонет",
        "напиши небольшую поэму",
        "сочини двустишие",
    ):
        assert is_verse_task(text), text


def test_no_false_positive():
    for text in (
        "рассказ про морскую стихию",
        "ветер стих к утру",
        "буря стихать начала",
        "буря утихла под утро",
        "стихийное явление природы",
    ):
        assert not is_verse_task(text), text


# ── verse_requirements: independent rhyme/rhythm ──────────────────────────────

def test_requirements_blank_verse():
    assert verse_requirements("стихи с ритмом, без рифмы") == (False, True)


def test_requirements_rhyme_only():
    require_rhyme, _ = verse_requirements("стихи с рифмой")
    assert require_rhyme is True


def test_requirements_generic_poem():
    assert verse_requirements("напиши стихотворение") == (True, True)


# ── check_prosody: behaviour under require_rhyme / require_rhythm ────────────

def test_blank_verse_passes_without_rhyme():
    """Unrhymed but syllable-regular two-stanza text, require_rhyme=False →
    APPROVED (would be REVISE under the old always-rhyme rule)."""
    verdict = check_prosody(
        _BLANK_VERSE_TWO_STANZAS, require_rhyme=False, require_rhythm=True,
    )
    assert verdict.approved is True


def test_rhyme_still_enforced_when_requested():
    """The same unrhymed text, rhyme required → REVISE."""
    verdict = check_prosody(
        _UNRHYMED_SINGLE_QUATRAIN, require_rhyme=True, require_rhythm=True,
    )
    assert verdict.approved is False


def test_single_stanza_rhythm_noop():
    """One stanza, require_rhythm=True → APPROVED (nothing to compare)."""
    verdict = check_prosody(
        _SINGLE_STANZA_NO_RHYME, require_rhyme=False, require_rhythm=True,
    )
    assert verdict.approved is True
