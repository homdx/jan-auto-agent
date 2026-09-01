"""tests/test_coder_creative_language_sample_sort.py — chapter-file
selection must sort NUMERICALLY, not lexicographically.

Coder._creative_language_sample's fallback path (used when the target
chapter is empty/new) picked the "latest" predecessor chapter via
`sorted(cands, reverse=True)` — sorting by full PATH STRING, not by
chapter number. For any project with 10+ unpadded chapter numbers, this
picked the WRONG predecessor: "chapter_9.md" sorts AFTER "chapter_10.md"
in reverse lexicographic order (comparing the character '9' against '1'),
so a new chapter_11 would sample chapter_9 for its language detection
instead of the true latest, chapter_10. Reproduced directly:

    chapter_9.md  = "ENGLISH: chapter nine content"
    chapter_10.md = "глава десять — русский текст"   (true latest)
    target: chapter_11.md
    sample picked: chapter_9.md's ENGLISH text          <- WRONG

Given AUTO-CR-9/16's extensive documented history of exactly this class of
language-lock bug already being carefully fixed elsewhere in this same
file, a fresh instance of it in this fallback path could genuinely lock a
new chapter to the wrong language.

architect.py's _latest_chapter_file already solves the identical problem
correctly (extract the number via regex, sort numerically) — this fix
mirrors that approach.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.coder import Coder


def _coder() -> Coder:
    return Coder.__new__(Coder)   # bypass __init__; only tests one pure method


class TestNumericChapterSort:
    def test_double_digit_beats_single_digit(self, tmp_path):
        """The exact reported case: unpadded chapter_9 vs chapter_10."""
        (tmp_path / "chapter_9.md").write_text(
            "ENGLISH: chapter nine content", encoding="utf-8"
        )
        (tmp_path / "chapter_10.md").write_text(
            "глава десять — русский текст", encoding="utf-8"
        )
        sample = _coder()._creative_language_sample(["chapter_11.md"], tmp_path)
        assert "десять" in sample
        assert "nine" not in sample.lower()

    def test_triple_digit_beats_double_digit(self, tmp_path):
        for n, text in [(9, "nine"), (10, "ten"), (99, "ninety-nine"),
                        (100, "one hundred, the real latest")]:
            (tmp_path / f"chapter_{n}.md").write_text(text, encoding="utf-8")
        sample = _coder()._creative_language_sample(["chapter_101.md"], tmp_path)
        assert "one hundred" in sample

    def test_single_digit_chapters_still_correct(self, tmp_path):
        """Sanity: the common, small-number case must be unaffected."""
        (tmp_path / "chapter_1.md").write_text("chapter one", encoding="utf-8")
        (tmp_path / "chapter_2.md").write_text(
            "chapter two — the real latest", encoding="utf-8"
        )
        sample = _coder()._creative_language_sample(["chapter_3.md"], tmp_path)
        assert "real latest" in sample

    def test_target_files_own_content_used_first_when_present(self, tmp_path):
        """The PRIMARY path (not the fallback): a target file with real
        content is used directly, before ever falling back to a
        predecessor search."""
        (tmp_path / "chapter_11.md").write_text(
            "existing content in the target itself", encoding="utf-8"
        )
        (tmp_path / "chapter_9.md").write_text("old chapter nine", encoding="utf-8")
        sample = _coder()._creative_language_sample(["chapter_11.md"], tmp_path)
        assert "existing content in the target itself" in sample

    def test_no_numbered_chapters_falls_back_to_lexicographic(self, tmp_path):
        """When NO candidate has an extractable number at all, the
        pre-existing lexicographic fallback must still run without error."""
        (tmp_path / "chapterX.md").write_text("no number here", encoding="utf-8")
        result = _coder()._creative_language_sample(["chapter_new.md"], tmp_path)
        assert "no number here" in result

    def test_empty_target_files_returns_empty_string(self, tmp_path):
        assert _coder()._creative_language_sample([], tmp_path) == ""
