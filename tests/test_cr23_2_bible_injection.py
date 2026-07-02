"""tests/test_cr23_2_bible_injection.py — AUTO-CR-23-2: Always-inject the bible.

Three tests matching the acceptance criteria in the epic:

  test_bible_present_in_context
  test_bible_survives_tight_budget
  test_no_bible_file_unchanged
"""
from __future__ import annotations

from pathlib import Path


from tools.auto.context_assembler import ContextAssembler, _DROP_MARKER


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_synopsis(tmp_path: Path, sections: dict[str, str]) -> Path:
    """Write a synopsis.md with proper <!-- BEGIN/END --> markers."""
    lines: list[str] = []
    for name, body in sections.items():
        lines.append(f"<!-- BEGIN {name} -->")
        lines.append(body)
        lines.append(f"<!-- END {name} -->")
    synopsis = tmp_path / "synopsis.md"
    synopsis.write_text("\n".join(lines), encoding="utf-8")
    return synopsis


def _make_chapter(tmp_path: Path, name: str, content: str) -> str:
    """Write a chapter file and return its relative filename."""
    (tmp_path / name).write_text(content, encoding="utf-8")
    return name


def _make_bible(tmp_path: Path, text: str) -> Path:
    bible = tmp_path / "story_bible.md"
    bible.write_text(text, encoding="utf-8")
    return bible


def _assembler(tmp_path: Path, num_ctx: int = 8192, max_tokens: int = 2048, **kw) -> ContextAssembler:
    return ContextAssembler(num_ctx=num_ctx, max_tokens=max_tokens, base_dir=tmp_path, **kw)


# ── main acceptance-criterion tests ──────────────────────────────────────────

class TestBibleInjection:

    def test_bible_present_in_context(self, tmp_path: Path):
        """With a story_bible.md, the built context starts with 'STORY FACTS'
        and contains a bible fact."""
        ch1_text = "Chapter one prose. " * 5
        ch2_text = "Chapter two prose. " * 5
        _make_chapter(tmp_path, "chapter_01.md", ch1_text)
        _make_chapter(tmp_path, "chapter_02.md", ch2_text)
        _make_synopsis(tmp_path, {
            "chapter_01.md": "- Hero arrives in town",
            "chapter_02.md": "- Hero discovers map",
        })
        _make_bible(tmp_path, "• The hero wears a green jacket\n• Aisha is the hero's sister")

        assembler = _assembler(tmp_path)
        context = assembler.build_creative_context(
            target_file="chapter_03.md",
            all_chapter_files=["chapter_01.md", "chapter_02.md"],
        )

        assert context.startswith("STORY FACTS (must not contradict):")
        assert "green jacket" in context
        # Bible block must come before the synopsis block.
        assert context.index("STORY FACTS") < context.index("STORY SO FAR")

    def test_bible_survives_tight_budget(self, tmp_path: Path):
        """A tiny num_ctx that forces the synopsis to drop older sections
        still includes the bible block in full."""
        # Five prior chapters, each with a sizeable synopsis section, so a
        # tight budget must drop some of them.
        synopsis_sections: dict[str, str] = {}
        for i in range(1, 6):
            synopsis_sections[f"chapter_0{i}.md"] = f"- Fact A ch{i}\n- Fact B ch{i}\n" * 5
        _make_synopsis(tmp_path, synopsis_sections)

        for i in range(1, 6):
            _make_chapter(tmp_path, f"chapter_0{i}.md", f"Ch{i} prose. ")

        bible_fact = "The hero's eyes are amber and her promise to the king is unbroken"
        _make_bible(tmp_path, f"• {bible_fact}")

        # Tight enough that the common path's synopsis fill has to drop the
        # oldest sections (chapter_01/02), but not so tight that the
        # pathological zero-budget branch kicks in instead.
        assembler = _assembler(tmp_path, num_ctx=950, max_tokens=500)
        context = assembler.build_creative_context(
            target_file="chapter_06.md",
            all_chapter_files=[f"chapter_0{i}.md" for i in range(1, 6)],
        )

        # The bible fact must survive regardless of what happens to the
        # synopsis.
        assert "STORY FACTS (must not contradict):" in context
        assert bible_fact in context

        # The tight budget should still be forcing a synopsis drop — this
        # documents that the bible takes priority over synopsis sections,
        # not that the budget stopped being tight.
        assert _DROP_MARKER in context

    def test_no_bible_file_unchanged(self, tmp_path: Path):
        """No story_bible.md → output identical to pre-CR-23 behaviour."""
        ch1_text = "Chapter one prose. " * 5
        ch2_text = "Chapter two prose. " * 5
        _make_chapter(tmp_path, "chapter_01.md", ch1_text)
        _make_chapter(tmp_path, "chapter_02.md", ch2_text)
        _make_synopsis(tmp_path, {
            "chapter_01.md": "- Hero arrives in town",
            "chapter_02.md": "- Hero discovers map",
        })

        assembler = _assembler(tmp_path)  # no story_bible.md written
        context = assembler.build_creative_context(
            target_file="chapter_03.md",
            all_chapter_files=["chapter_01.md", "chapter_02.md"],
        )

        assert "STORY FACTS" not in context
        assert context.startswith("STORY SO FAR (synopsis):")
        assert "PREVIOUS CHAPTER (verbatim)" in context

    def test_empty_bible_file_unchanged(self, tmp_path: Path):
        """An empty (or whitespace-only) story_bible.md behaves the same as
        a missing one."""
        _make_chapter(tmp_path, "chapter_01.md", "Some prose.")
        _make_bible(tmp_path, "   \n\n  ")

        assembler = _assembler(tmp_path)
        context = assembler.build_creative_context(
            target_file="chapter_02.md",
            all_chapter_files=["chapter_01.md"],
        )

        assert "STORY FACTS" not in context

    def test_bible_capped_to_budget_chars(self, tmp_path: Path):
        """The injected bible is capped at bible_budget_chars."""
        _make_chapter(tmp_path, "chapter_01.md", "Some prose.")
        long_bible = "• fact " + ("x" * 5000)
        _make_bible(tmp_path, long_bible)

        assembler = _assembler(tmp_path, bible_budget_chars=100)
        context = assembler.build_creative_context(
            target_file="chapter_02.md",
            all_chapter_files=["chapter_01.md"],
        )

        # Header + at most 100 chars of bible content.
        bible_section = context.split("STORY FACTS (must not contradict):\n", 1)[1]
        # Whatever comes next (synopsis/prev-chapter) is separated by a
        # blank line; isolate just the bible portion.
        bible_text = bible_section.split("\n\n", 1)[0]
        assert len(bible_text) <= 100

    def test_first_chapter_no_predecessors_still_empty(self, tmp_path: Path):
        """Even with a bible present, chapter_01 (no predecessors) still
        returns '' — there is nothing to continue from yet."""
        _make_bible(tmp_path, "• Some fact")

        assembler = _assembler(tmp_path)
        context = assembler.build_creative_context(
            target_file="chapter_01.md",
            all_chapter_files=[],
        )
        assert context == ""

    def test_bible_alone_when_core_empty(self, tmp_path: Path):
        """If the synopsis/prev-chapter core degrades to '', but the bible
        is present, the bible block alone is still returned (never silently
        dropped to '')."""
        # chapter_01 exists but is unreadable in practice we simulate by
        # giving an enormous previous chapter with no synopsis at all, and a
        # budget so tiny that even the floor-based degrade path can't
        # produce a synopsis block; the bible should still come through.
        huge = "word " * 20000
        _make_chapter(tmp_path, "chapter_01.md", huge)
        _make_bible(tmp_path, "• Hero's name is Aisha")

        # max_tokens >= num_ctx forces the pathological zero-budget branch.
        assembler = _assembler(tmp_path, num_ctx=100, max_tokens=200)
        context = assembler.build_creative_context(
            target_file="chapter_02.md",
            all_chapter_files=["chapter_01.md"],
        )

        assert "STORY FACTS (must not contradict):" in context
        assert "Hero's name is Aisha" in context
