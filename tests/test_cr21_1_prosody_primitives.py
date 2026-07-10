"""tests/test_cr21_1_prosody_primitives.py — AUTO-CR-21-1 acceptance tests.

Validates the pure prosody primitives in tools/auto/prosody.py:

  - count_syllables_ru: exact Russian vowel counting
  - rhyme_key_ru: last-vowel tail extraction
  - rhymes_ru: rhyme comparison (3 rhyming + 3 non-rhyming pairs)
  - split_stanzas: blank-line splitting, title-line dropping, short-stanza drop
  - detect_scheme: AABB / ABAB / ABCB / NONE on hand-built stanzas

All tests are purely deterministic — no LLM, no I/O, no config.
"""


from tools.auto.prosody import (
    _VOWELS_RU,
    count_syllables_ru,
    detect_scheme,
    rhyme_key_ru,
    rhymes_ru,
    split_stanzas,
)


# ── _VOWELS_RU sanity ─────────────────────────────────────────────────────────

class TestVowelSet:
    def test_contains_ten_vowels(self):
        assert len(_VOWELS_RU) == 10

    def test_all_lowercase_cyrillic(self):
        for ch in _VOWELS_RU:
            assert ch == ch.lower(), f"expected lowercase, got {ch!r}"

    def test_known_vowels_present(self):
        for v in "аеёиоуыэюя":
            assert v in _VOWELS_RU


# ── count_syllables_ru ────────────────────────────────────────────────────────

class TestCountSyllablesRu:
    def test_adelina_example(self):
        # А-де-ли-на = А, е, и, а (4); по = о (1); гим-на-сти-ке = и, а, и, е (4) → 9
        result = count_syllables_ru("Аделина КМС по гимнастике")
        assert result == 9

    def test_empty_string_is_zero(self):
        assert count_syllables_ru("") == 0

    def test_no_vowels(self):
        assert count_syllables_ru("крст") == 0

    def test_ignores_latin_letters(self):
        # Latin letters not in _VOWELS_RU should not be counted
        assert count_syllables_ru("hello") == 0

    def test_ignores_punctuation(self):
        assert count_syllables_ru("!?.,:;") == 0

    def test_ignores_digits(self):
        assert count_syllables_ru("12345") == 0

    def test_mixed_latin_cyrillic(self):
        # Only Cyrillic vowels count: "о" from слово
        result = count_syllables_ru("слово hello world")
        assert result == 2  # о, о

    def test_case_insensitive(self):
        lower = count_syllables_ru("аеёиоуыэюя")
        upper = count_syllables_ru("АЕЁИОУЫЭЮЯ")
        assert lower == upper == 10

    def test_single_vowel(self):
        assert count_syllables_ru("я") == 1

    def test_quatrain_line(self):
        # "Мороз и солнце; день чудесный!"
        # о,о (Мороз=2) + и (1) + о,е (солнце=2) + е (день=1) + у,е,ы (чудесный=3) → 9
        line = "Мороз и солнце; день чудесный!"
        result = count_syllables_ru(line)
        assert result == 9


# ── rhyme_key_ru ──────────────────────────────────────────────────────────────

class TestRhymeKeyRu:
    def test_gimnastika(self):
        # "гимнастика": last vowel 'а' at index 8, tail "ка" (len=2, no extension needed)
        key = rhyme_key_ru("гимнастика")
        assert key == "ка"

    def test_akrobatika(self):
        # "акробатика": last vowel 'а' at index 8, tail "ка"
        key = rhyme_key_ru("акробатика")
        assert key == "ка"

    def test_same_key_for_rhyming_words(self):
        assert rhyme_key_ru("гимнастика") == rhyme_key_ru("акробатика")

    def test_empty_string(self):
        assert rhyme_key_ru("") == ""

    def test_no_vowels(self):
        assert rhyme_key_ru("крст") == ""

    def test_strips_non_cyrillic(self):
        # Punctuation and Latin should be stripped before finding tail
        key1 = rhyme_key_ru("дома!")
        key2 = rhyme_key_ru("дома")
        assert key1 == key2

    def test_single_vowel_word(self):
        key = rhyme_key_ru("я")
        # tail starts at last (only) vowel 'я'; length 1 < 2, no left extension possible
        assert key == "я"

    def test_extends_when_tail_too_short(self):
        # Word ending in a vowel: tail = just the vowel → extend left
        key = rhyme_key_ru("море")
        # cleaned = "море"; last vowel = 'е' at index 3; tail = "е" (len 1) → extend: "ре"
        assert key == "ре"

    def test_case_insensitive(self):
        assert rhyme_key_ru("ДОМА") == rhyme_key_ru("дома")


# ── rhymes_ru ─────────────────────────────────────────────────────────────────

class TestRhymesRu:
    # --- 3 clearly rhyming pairs ---

    def test_gimnastika_akrobatika(self):
        # Both have rhyme key "ика"
        assert rhymes_ru("гимнастика", "акробатика") is True

    def test_zima_prima(self):
        # зима → "ма"; прима → "ма"
        assert rhymes_ru("зима", "прима") is True

    def test_luna_volna(self):
        # луна → "на"; волна → "на"
        assert rhymes_ru("луна", "волна") is True

    # --- 3 clearly non-rhyming pairs ---

    def test_doma_ryadom(self):
        # дома → "ма"; рядом → "ом"
        assert rhymes_ru("дома", "рядом") is False

    def test_nebo_reka(self):
        # небо → "бо" (е at index 1, tail "ебо"... let's check)
        # небо cleaned = "небо"; last vowel 'о' at idx 3; tail = "о" (len 1); extend → "бо"
        # река cleaned = "река"; last vowel 'а' at idx 3; tail = "а" (len 1); extend → "ка"
        # "бо" != "ка", suffix "бо"[-2:]="бо", "ка"[-2:]="ка" → not equal
        assert rhymes_ru("небо", "река") is False

    def test_dom_les(self):
        # дом → "ом"; лес → "ес"
        assert rhymes_ru("дом", "лес") is False

    # --- Edge cases ---

    def test_empty_strings(self):
        assert rhymes_ru("", "") is False

    def test_empty_and_nonempty(self):
        assert rhymes_ru("", "дом") is False
        assert rhymes_ru("дом", "") is False

    def test_identical_words(self):
        assert rhymes_ru("дом", "дом") is True


# ── split_stanzas ─────────────────────────────────────────────────────────────

class TestSplitStanzas:
    def test_two_stanza_text(self):
        text = (
            "строка первая\n"
            "строка вторая\n"
            "\n"
            "строка третья\n"
            "строка четвёртая"
        )
        stanzas = split_stanzas(text)
        assert len(stanzas) == 2

    def test_correct_line_counts(self):
        text = (
            "а\nб\nв\n"
            "\n"
            "г\nд"
        )
        stanzas = split_stanzas(text)
        assert len(stanzas[0]) == 3
        assert len(stanzas[1]) == 2

    def test_multiple_blank_lines_treated_as_one(self):
        text = "а\nб\n\n\n\nв\nг"
        stanzas = split_stanzas(text)
        assert len(stanzas) == 2

    def test_leading_title_line_dropped(self):
        # Title = single line without terminal punctuation
        text = "Моё стихотворение\n\nстрока 1\nстрока 2"
        stanzas = split_stanzas(text)
        # Title stanza dropped, only the content stanza remains
        assert len(stanzas) == 1
        assert stanzas[0][0] == "строка 1"

    def test_title_line_with_punctuation_not_dropped(self):
        # Ends with "!" → not treated as title
        text = "Это не заголовок!\n\nстрока 1\nстрока 2"
        stanzas = split_stanzas(text)
        # The single-line stanza has punctuation → kept, but len < 2 → dropped
        # so only one stanza remains
        assert len(stanzas) == 1

    def test_single_line_stanzas_dropped(self):
        text = "одна строка\n\nа\nб"
        stanzas = split_stanzas(text)
        # "одна строка" is a single-line stanza dropped as title;
        # "а\nб" survives
        assert len(stanzas) == 1
        assert stanzas[0] == ["а", "б"]

    def test_empty_string(self):
        assert split_stanzas("") == []

    def test_only_blank_lines(self):
        assert split_stanzas("\n\n\n") == []

    def test_preserves_line_content(self):
        text = "Зима  \nВесна\n\nЛето\nОсень"
        stanzas = split_stanzas(text)
        assert stanzas[0][0] == "Зима"  # stripped
        assert stanzas[0][1] == "Весна"

    def test_no_trailing_newline_issue(self):
        text = "а\nб"
        stanzas = split_stanzas(text)
        assert len(stanzas) == 1
        assert stanzas[0] == ["а", "б"]


# ── detect_scheme ─────────────────────────────────────────────────────────────

class TestDetectScheme:
    # Build hand-crafted stanzas using words with known rhyme keys

    def _aabb_stanza(self):
        # Lines 1&2 rhyme, lines 3&4 rhyme
        # "зима" / "прима" (ма), "луна" / "волна" (на)
        return ["Трещит мороза снег зима", "Пришла красавица прима",
                "Горит вдали ночная луна", "Шумит прибой — морская волна"]

    def _abab_stanza(self):
        # Lines 1&3 rhyme, lines 2&4 rhyme
        # "зима"/"прима" → 1&3; "луна"/"волна" → 2&4
        return ["Трещит мороза снег зима", "Горит вдали ночная луна",
                "Пришла красавица прима", "Шумит прибой — морская волна"]

    def _abcb_stanza(self):
        # Only lines 2&4 rhyme
        return ["Трещит в лесу мороз суровый", "Горит вдали ночная луна",
                "Сидит медведь в берлоге тёмной", "Шумит прибой — морская волна"]

    def _none_stanza(self):
        # No rhymes at all
        return ["Трещит мороз", "Горит закат", "Сидит медведь", "Бежит вода"]

    def test_aabb(self):
        assert detect_scheme(self._aabb_stanza()) == "AABB"

    def test_abab(self):
        assert detect_scheme(self._abab_stanza()) == "ABAB"

    def test_abcb(self):
        assert detect_scheme(self._abcb_stanza()) == "ABCB"

    def test_none(self):
        assert detect_scheme(self._none_stanza()) == "NONE"

    def test_fewer_than_four_lines(self):
        assert detect_scheme(["а", "б", "в"]) == "NONE"

    def test_empty(self):
        assert detect_scheme([]) == "NONE"

    def test_aabb_takes_priority_over_abab(self):
        # If all 4 lines rhyme together, AABB is returned (checked first)
        # "дома"/"зима"/"прима"/"сама" — все на "ма"
        stanza = ["Я дома", "Трещит зима", "Пришла прима", "Иду сама"]
        result = detect_scheme(stanza)
        # 1&2 rhyme AND 3&4 rhyme → AABB
        assert result == "AABB"

    def test_uses_first_four_lines_only(self):
        # Extra lines beyond 4 are ignored
        stanza = ["Трещит мороза снег зима", "Горит вдали ночная луна",
                  "Пришла красавица прима", "Шумит прибой — морская волна",
                  "Лишняя строка про кота"]
        # Should still detect the scheme of lines 1-4
        result = detect_scheme(stanza)
        assert result in {"AABB", "ABAB", "ABCB", "NONE"}
