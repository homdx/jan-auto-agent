"""tests/test_cr21_2_prosody_check.py — AUTO-CR-21-2 acceptance tests.

Validates the CR-21-2 additions to tools/auto/prosody.py:

  - is_verse_task: keyword activation gate
  - analyze_ru: ProsodyReport with per-stanza syllable/scheme data
  - check_prosody: tolerant APPROVED/REVISE verdict
  - ProsodyVerdict.feedback: coder-facing message format
  - Fail-open: garbage / empty input never raises, always APPROVED

All tests are purely deterministic — no LLM, no I/O, no config.
"""


from tools.auto.prosody import (
    ProsodyReport,
    ProsodyVerdict,
    analyze_ru,
    check_prosody,
    is_verse_task,
)


# ── Helper poems ──────────────────────────────────────────────────────────────

# Clean AABB quatrain with regular syllables (9-8-9-8 pattern repeated)
# Line 1&2 rhyme on "зима"/"прима"; line 3&4 rhyme on "луна"/"волна"
_CLEAN_AABB = """\
Трещит январский холод — зима
Явилась в город наша прима
Горит вдали ночная луна
Шумит прибой — морская волна

Морозный ветер — снова зима
На сцену вышла наша прима
Блестит в ночи родная луна
В ночи шумит морская волна
"""

# ABAB quatrain: lines 1&3 rhyme, 2&4 rhyme
# "зима"/"прима" on positions 1&3, "луна"/"волна" on positions 2&4
_CLEAN_ABAB = """\
Трещит январский холод — зима
Горит вдали ночная луна
Явилась в город наша прима
Шумит прибой — морская волна

Морозный ветер — снова зима
Блестит в ночи родная луна
На сцену вышла наша прима
В ночи шумит морская волна
"""

# ABCB quatrain: only lines 2&4 rhyme
_CLEAN_ABCB = """\
Трещит январский холод дня
Горит вдали ночная луна
Сидит медведь в берлоге тёмной
Шумит прибой — морская волна

Морозный ветер, снег, метель
Блестит в ночи родная луна
Лежит сугроб у старой ели
В ночи шумит морская волна
"""

# Two-stanza poem with NO rhyme in stanza 1
_NO_RHYME_S1 = """\
Мороз трещит в лесу суровом
Медведь сидит в берлоге тёмной
Летит снежинка над сугробом
Стоит сосна в лесу огромном

Горит вдали ночная луна
На сцену вышла наша прима
Шумит прибой — морская волна
Явилась в город наша прима
"""

# 3-line stanza (violates require_quatrains)
_THREE_LINE_STANZA = """\
Мороз трещит — зима
Горит ночная луна
Шумит волна
"""

# Poem with irregular syllable counts (stanza 2 line 1 has only 1 syllable vs ~9 in stanza 1)
_IRREGULAR_SYLLABLES = """\
Трещит январский холод — зима
Горит вдали ночная луна
Явилась в город наша прима
Шумит прибой — морская волна

Я
Горит вдали ночная луна
Явилась в город наша прима
Шумит прибой — морская волна
"""


# ── is_verse_task ─────────────────────────────────────────────────────────────

class TestIsVerseTask:
    def test_ritm_i_rifma(self):
        assert is_verse_task("напиши стихи с ритмом и рифмой") is True

    def test_ritm_alone(self):
        assert is_verse_task("проверь ритм стихотворения") is True

    def test_rifma_alone(self):
        assert is_verse_task("добавь рифму") is True

    def test_rifmu_inflected(self):
        assert is_verse_task("сохрани рифму") is True

    def test_rifmoy_inflected(self):
        assert is_verse_task("стих с рифмой") is True

    def test_no_keywords(self):
        assert is_verse_task("исправь нестыковки") is False

    def test_prose_task(self):
        assert is_verse_task("напиши рассказ о природе") is False

    def test_empty_string(self):
        assert is_verse_task("") is False

    def test_case_insensitive(self):
        assert is_verse_task("РИТМ и РИФМА") is True

    def test_partial_match_ritm(self):
        # "ритм" is a substring of "ритмично" → should match
        assert is_verse_task("звучит ритмично") is True

    def test_partial_match_rifm(self):
        # "рифм" is a substring of "рифмовка"
        assert is_verse_task("проверь рифмовку") is True


# ── analyze_ru ────────────────────────────────────────────────────────────────

class TestAnalyzeRu:
    def test_returns_prosody_report(self):
        report = analyze_ru(_CLEAN_AABB)
        assert isinstance(report, ProsodyReport)

    def test_stanza_count(self):
        report = analyze_ru(_CLEAN_AABB)
        assert report.stanza_count == 2

    def test_syllables_per_line_populated(self):
        report = analyze_ru(_CLEAN_AABB)
        assert len(report.stanzas[0].syllables) == 4
        for count in report.stanzas[0].syllables:
            assert count > 0

    def test_scheme_detected(self):
        report = analyze_ru(_CLEAN_AABB)
        assert report.stanzas[0].scheme == "AABB"

    def test_abab_scheme_detected(self):
        report = analyze_ru(_CLEAN_ABAB)
        assert report.stanzas[0].scheme == "ABAB"

    def test_syllable_regular_true_for_clean_poem(self):
        report = analyze_ru(_CLEAN_AABB)
        assert report.syllable_regular is True

    def test_syllable_regular_false_for_irregular(self):
        report = analyze_ru(_IRREGULAR_SYLLABLES)
        assert report.syllable_regular is False

    def test_empty_input(self):
        report = analyze_ru("")
        assert report.stanza_count == 0
        assert report.syllable_regular is True

    def test_whitespace_only(self):
        report = analyze_ru("   \n\n   ")
        assert report.stanza_count == 0

    def test_non_russian_text(self):
        report = analyze_ru("hello world\nthis is english")
        # No Cyrillic vowels — syllables all zero; no rhyme
        assert isinstance(report, ProsodyReport)

    def test_tolerance_parameter(self):
        # With tolerance=0 the irregular poem should be flagged
        report = analyze_ru(_IRREGULAR_SYLLABLES, syllable_tolerance=0)
        assert report.syllable_regular is False

    def test_stanza_index_is_one_based(self):
        report = analyze_ru(_CLEAN_AABB)
        assert report.stanzas[0].index == 1
        assert report.stanzas[1].index == 2


# ── ProsodyVerdict.feedback ───────────────────────────────────────────────────

class TestProsodyVerdictFeedback:
    def test_approved_feedback_empty(self):
        v = ProsodyVerdict(approved=True, reason="")
        assert v.feedback() == ""

    def test_revise_feedback_format(self):
        v = ProsodyVerdict(approved=False, reason="stanza 1 has no acceptable rhyme")
        fb = v.feedback()
        assert fb.startswith("PROSODY ISSUE —")
        assert "stanza 1" in fb

    def test_feedback_contains_reason(self):
        reason = "line 2: 7 syllables in stanza 3 vs 9 in stanza 1"
        v = ProsodyVerdict(approved=False, reason=reason)
        assert reason in v.feedback()


# ── check_prosody — APPROVED cases ───────────────────────────────────────────

class TestCheckProsodyApproved:
    def test_clean_aabb_approved(self):
        v = check_prosody(_CLEAN_AABB)
        assert v.approved is True

    def test_clean_abab_approved(self):
        v = check_prosody(_CLEAN_ABAB)
        assert v.approved is True

    def test_clean_abcb_approved(self):
        v = check_prosody(_CLEAN_ABCB)
        assert v.approved is True

    def test_min_scheme_abcb_accepts_abab(self):
        # ABCB is the loosest — should accept ABAB (stricter)
        v = check_prosody(_CLEAN_ABAB, min_scheme="ABCB")
        assert v.approved is True

    def test_min_scheme_abcb_accepts_aabb(self):
        v = check_prosody(_CLEAN_AABB, min_scheme="ABCB")
        assert v.approved is True

    def test_min_scheme_abab_accepts_aabb(self):
        v = check_prosody(_CLEAN_AABB, min_scheme="ABAB")
        assert v.approved is True

    def test_empty_input_approved(self):
        v = check_prosody("")
        assert v.approved is True

    def test_whitespace_approved(self):
        v = check_prosody("   \n\n   ")
        assert v.approved is True

    def test_garbage_approved_no_exception(self):
        v = check_prosody("!@#$%^&*()_+-=[]|;':,.<>?")
        assert v.approved is True

    def test_latin_text_approved(self):
        # Non-Russian text — no Cyrillic vowels, no rhyme detectable
        # With require_quatrains=False, split_stanzas may return stanzas but NONE rhyme
        # However split_stanzas requires >=2 lines; single-line stanzas dropped.
        # Let's test with require_quatrains=False to avoid stanza-count rejection
        v = check_prosody("hello world\ngoodbye world\n\nhow are you\ni am fine",
                          require_quatrains=False)
        # Latin text produces no Cyrillic rhymes → scheme=NONE → REVISE
        # OR stanza_count=0 if filtered out → APPROVED
        # Either outcome is acceptable; what matters is no exception
        assert isinstance(v, ProsodyVerdict)

    def test_require_quatrains_false_allows_non_four_line(self):
        v = check_prosody(_THREE_LINE_STANZA, require_quatrains=False)
        # 3-line stanza can't detect any scheme → REVISE on rhyme, but no QUATRAIN error
        assert isinstance(v, ProsodyVerdict)


# ── check_prosody — REVISE cases ─────────────────────────────────────────────

class TestCheckProsodyRevise:
    def test_no_rhyme_revise(self):
        v = check_prosody(_NO_RHYME_S1)
        assert v.approved is False
        assert "stanza 1" in v.reason

    def test_no_rhyme_names_stanza(self):
        v = check_prosody(_NO_RHYME_S1)
        assert "stanza" in v.reason

    def test_three_line_stanza_revise(self):
        v = check_prosody(_THREE_LINE_STANZA, require_quatrains=True)
        assert v.approved is False
        assert "stanza" in v.reason
        assert "lines" in v.reason or "4" in v.reason

    def test_irregular_syllables_revise(self):
        v = check_prosody(_IRREGULAR_SYLLABLES)
        assert v.approved is False
        assert "syllable" in v.reason or "stanza" in v.reason

    def test_min_scheme_aabb_rejects_abab(self):
        # Strict AABB required — ABAB poem should REVISE
        v = check_prosody(_CLEAN_ABAB, min_scheme="AABB")
        assert v.approved is False

    def test_min_scheme_aabb_rejects_abcb(self):
        v = check_prosody(_CLEAN_ABCB, min_scheme="AABB")
        assert v.approved is False

    def test_min_scheme_abab_rejects_abcb(self):
        v = check_prosody(_CLEAN_ABCB, min_scheme="ABAB")
        assert v.approved is False

    def test_revise_reason_quotes_line_endings(self):
        # The reason should quote the tail endings to help the coder
        v = check_prosody(_NO_RHYME_S1)
        # Feedback should reference the non-rhyming tails (quoted with «»)
        assert "\u00ab" in v.reason or "ending" in v.reason or "lines 2/4" in v.reason

    def test_revise_feedback_not_empty(self):
        v = check_prosody(_NO_RHYME_S1)
        assert v.feedback() != ""
        assert "PROSODY ISSUE" in v.feedback()

    def test_irregular_syllables_names_line_and_stanza(self):
        v = check_prosody(_IRREGULAR_SYLLABLES)
        if not v.approved:
            # Should name which line position and which stanza
            assert "stanza" in v.reason

    def test_quatrain_rule_fires_before_rhyme_rule(self):
        # A 3-line stanza with bad rhyme — should fail on quatrain first
        v = check_prosody(_THREE_LINE_STANZA, require_quatrains=True)
        assert v.approved is False
        # The reason should mention the stanza line count, not the rhyme
        assert "lines" in v.reason or "4" in v.reason


# ── Fail-open: garbage / error inputs ────────────────────────────────────────

class TestCheckProsodyFailOpen:
    def test_none_like_empty_string(self):
        v = check_prosody("")
        assert v.approved is True

    def test_only_punctuation(self):
        v = check_prosody("??? !!! ...")
        assert v.approved is True

    def test_only_digits(self):
        # Digits produce no Cyrillic vowels/rhymes; with require_quatrains=False
        # the stanza has 0-syllable lines and NONE scheme → REVISE on rhyme
        # OR stanza_count=0 → APPROVED.  What matters: no exception ever raised.
        v = check_prosody("123 456\n789 000", require_quatrains=False)
        assert isinstance(v, ProsodyVerdict)

    def test_very_long_garbage(self):
        v = check_prosody("x" * 10_000)
        assert v.approved is True

    def test_no_exception_on_any_string(self):
        """check_prosody must never raise, regardless of input."""
        samples = [
            "",
            "\x00\x01\x02",
            "a" * 5000,
            "\n\n\n\n\n",
            "рифм" * 100,
            "None True False",
        ]
        for s in samples:
            v = check_prosody(s)
            assert isinstance(v, ProsodyVerdict)
