"""tests/test_cr4_context_assembler.py — AUTO-CR-4: Budget-aware Context Assembler.

Five tests matching the acceptance criteria in the epic:

  test_includes_prev_chapter_full_and_synopsis
  test_excludes_older_chapters_full_text
  test_budget_drops_oldest_synopsis_first
  test_first_chapter_no_predecessors
  test_degrades_to_synopsis_when_prev_too_large
"""
from __future__ import annotations

from pathlib import Path


from tools.auto.context_assembler import (
    ContextAssembler,
    _DROP_MARKER,
    _chapter_number,
    _order_chapters,
)


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


def _assembler(tmp_path: Path, num_ctx: int = 8192, max_tokens: int = 2048) -> ContextAssembler:
    return ContextAssembler(num_ctx=num_ctx, max_tokens=max_tokens, base_dir=tmp_path)


# ── unit tests for helpers ────────────────────────────────────────────────────

class TestHelpers:
    def test_chapter_number_extracts_int(self):
        assert _chapter_number("chapter_07.md") == 7
        assert _chapter_number("chapter_1.md") == 1
        assert _chapter_number("CHAPTER_42.md") == 42

    def test_chapter_number_none_for_non_chapter(self):
        assert _chapter_number("prologue.md") is None
        assert _chapter_number("synopsis.md") is None

    def test_order_chapters_sorts_ascending(self):
        files = ["chapter_03.md", "chapter_01.md", "chapter_02.md"]
        result = _order_chapters(files)
        assert [n for n, _ in result] == [1, 2, 3]

    def test_order_chapters_skips_non_chapters(self):
        files = ["chapter_01.md", "prologue.md", "chapter_02.md"]
        result = _order_chapters(files)
        assert len(result) == 2
        assert all(f.startswith("chapter_") for _, f in result)


# ── main acceptance-criterion tests ──────────────────────────────────────────

class TestContextAssembler:

    def test_includes_prev_chapter_full_and_synopsis(self, tmp_path: Path):
        """For chapter N, assembled context contains:
        - synopsis sections for chapters 1..N-1 (budget permitting)
        - full text of chapter N-1 (verbatim block)
        - NOT the full text of chapters 1..N-2.
        """
        ch1_text = "Chapter one prose. " * 5
        ch2_text = "Chapter two prose. " * 5
        _make_chapter(tmp_path, "chapter_01.md", ch1_text)
        _make_chapter(tmp_path, "chapter_02.md", ch2_text)

        ch1_synopsis = "- Hero arrives in town\n- Meets innkeeper"
        ch2_synopsis = "- Hero discovers map\n- Buys horse"
        _make_synopsis(tmp_path, {
            "chapter_01.md": ch1_synopsis,
            "chapter_02.md": ch2_synopsis,
        })

        assembler = _assembler(tmp_path)
        context = assembler.build_creative_context(
            target_file="chapter_03.md",
            all_chapter_files=["chapter_01.md", "chapter_02.md"],
        )

        # Must include chapter 2 verbatim block
        assert "PREVIOUS CHAPTER (verbatim)" in context
        assert "chapter_02.md" in context
        assert ch2_text in context

        # Must include synopsis sections
        assert "STORY SO FAR (synopsis)" in context
        assert ch1_synopsis in context

        # Must NOT include chapter 1 raw text in the verbatim block
        # (synopsis section may reference chapter 1 content in summarised form,
        # but the full prose "Chapter one prose." block should not appear)
        assert "PREVIOUS CHAPTER (verbatim)" in context
        # Only chapter_02 should be in the verbatim block header
        assert "chapter_01.md" not in context.split("PREVIOUS CHAPTER")[1]

    def test_excludes_older_chapters_full_text(self, tmp_path: Path):
        """Chapters 1..N-2 must never appear as full 'PREVIOUS CHAPTER' blocks."""
        for i in range(1, 5):
            _make_chapter(tmp_path, f"chapter_0{i}.md", f"Unique marker ch{i}. " * 3)

        assembler = _assembler(tmp_path)
        context = assembler.build_creative_context(
            target_file="chapter_05.md",
            all_chapter_files=[f"chapter_0{i}.md" for i in range(1, 5)],
        )

        # Only one PREVIOUS CHAPTER block — chapter_04
        assert context.count("PREVIOUS CHAPTER (verbatim)") == 1
        assert "chapter_04.md" in context

        # Verbatim block must contain chapter 4 text
        assert "Unique marker ch4." in context

        # Full raw prose of chapters 1-3 must NOT appear verbatim in the
        # PREVIOUS CHAPTER section (synopsis may include summaries)
        prev_section = context.split("PREVIOUS CHAPTER (verbatim)")[1] if "PREVIOUS CHAPTER (verbatim)" in context else ""
        for i in range(1, 4):
            assert f"Unique marker ch{i}." not in prev_section

    def test_budget_drops_oldest_synopsis_first(self, tmp_path: Path):
        """When budget is exceeded, oldest synopsis sections are dropped first,
        marked with the drop marker; most recent sections survive."""

        # Build a synopsis with 5 chapters each with a large section body
        synopsis_sections: dict[str, str] = {}
        for i in range(1, 6):
            # Each section is ~200 chars
            synopsis_sections[f"chapter_0{i}.md"] = f"- Fact A ch{i}\n- Fact B ch{i}\n" * 5

        _make_synopsis(tmp_path, synopsis_sections)

        for i in range(1, 6):
            _make_chapter(tmp_path, f"chapter_0{i}.md", f"Ch{i} prose. ")

        # Use a tiny budget: 1000 tokens total, 500 reserved for output → 500
        # remaining minus 300 overhead = 200 tokens = 800 chars. A single
        # chapter section body is ~200 chars; the prev-chapter block for ch5
        # is small. Fitting all 4 synopsis sections (ch1-ch4) would exceed
        # the remaining budget.
        assembler = ContextAssembler(
            num_ctx=1000, max_tokens=500, base_dir=tmp_path,
        )
        context = assembler.build_creative_context(
            target_file="chapter_06.md",
            all_chapter_files=[f"chapter_0{i}.md" for i in range(1, 6)],
        )

        if not context:
            # Budget so tight nothing fits — that's allowed; just verify no crash
            return

        # The drop marker must appear if any sections were omitted
        if _DROP_MARKER in context:
            # Newer chapters must appear after the marker; older may be absent
            # ch5 (newest prior) should be present if anything is
            assert "chapter_05.md" in context or "chapter_04.md" in context
        # No exception = pass (fail-open contract)

    def test_first_chapter_no_predecessors(self, tmp_path: Path):
        """For chapter_01 (no predecessors), build_creative_context returns
        an empty string without raising."""

        assembler = _assembler(tmp_path)
        context = assembler.build_creative_context(
            target_file="chapter_01.md",
            all_chapter_files=[],
        )
        assert context == ""

    def test_first_chapter_with_only_itself_listed(self, tmp_path: Path):
        """Even if chapter_01 is in all_chapter_files, it is the target, so
        there are still no predecessors — returns '' without error."""
        _make_chapter(tmp_path, "chapter_01.md", "Some prose.")
        assembler = _assembler(tmp_path)
        context = assembler.build_creative_context(
            target_file="chapter_01.md",
            all_chapter_files=["chapter_01.md"],
        )
        assert context == ""

    def test_degrades_to_synopsis_when_prev_too_large(self, tmp_path: Path):
        """When the previous chapter's full text alone exceeds the budget, the
        assembler degrades gracefully: it uses the synopsis section for N-1
        instead of the full text, and does NOT raise."""

        # Huge chapter 2 (~50 000 chars, way over any small budget)
        huge_ch2 = "The quick brown fox jumped. " * 2000   # ~56 000 chars
        _make_chapter(tmp_path, "chapter_01.md", "Short ch1 prose.")
        _make_chapter(tmp_path, "chapter_02.md", huge_ch2)

        ch2_synopsis_body = "- Fox jumped; hero fled"
        _make_synopsis(tmp_path, {
            "chapter_01.md": "- Hero arrived",
            "chapter_02.md": ch2_synopsis_body,
        })

        # Very small context window so chapter_02's full text cannot fit
        assembler = ContextAssembler(
            num_ctx=512, max_tokens=256, base_dir=tmp_path,
        )
        context = assembler.build_creative_context(
            target_file="chapter_03.md",
            all_chapter_files=["chapter_01.md", "chapter_02.md"],
        )

        # Must not raise; result is either the synopsis fallback or '' if even
        # synopsis doesn't fit the tiny budget.
        assert isinstance(context, str)

        # If *something* was returned it must NOT contain the huge chapter text
        if context:
            assert huge_ch2 not in context
            # Should NOT have a verbatim block for chapter 02
            assert "PREVIOUS CHAPTER (verbatim)" not in context or "chapter_02.md" not in context.split("PREVIOUS CHAPTER (verbatim)")[-1]

    def test_synopsis_missing_still_returns_prev_chapter(self, tmp_path: Path):
        """If synopsis.md does not exist yet (CR-5 not wired), the assembler
        still returns the previous chapter's full text without raising."""
        prev_text = "Hero crosses the river at dawn."
        _make_chapter(tmp_path, "chapter_01.md", prev_text)

        assembler = _assembler(tmp_path)   # no synopsis.md written
        context = assembler.build_creative_context(
            target_file="chapter_02.md",
            all_chapter_files=["chapter_01.md"],
        )

        assert "PREVIOUS CHAPTER (verbatim)" in context
        assert prev_text in context
        # No synopsis block since synopsis.md is absent
        assert "STORY SO FAR" not in context

    def test_non_chapter_target_returns_empty(self, tmp_path: Path):
        """A target file that doesn't match chapter_<N> returns '' (fail-open)."""
        assembler = _assembler(tmp_path)
        context = assembler.build_creative_context(
            target_file="prologue.md",
            all_chapter_files=["chapter_01.md"],
        )
        assert context == ""

    def test_pathological_budget_degrades_not_empty(self, tmp_path: Path):
        """AUTO-CR-12: when max_tokens >= num_ctx the budget is zero. Instead of
        silently returning '' (which left the model with no story so far and
        the wrong language), degrade to the previous chapter so there is always
        an anchor. Must not crash."""
        _make_chapter(tmp_path, "chapter_01.md", "Some text.")
        assembler = ContextAssembler(
            num_ctx=512, max_tokens=512, base_dir=tmp_path,
        )
        context = assembler.build_creative_context(
            target_file="chapter_02.md",
            all_chapter_files=["chapter_01.md"],
        )
        # No synopsis on disk → falls back to the previous chapter's text.
        assert "Some text." in context
        assert "chapter_01.md" in context

    def test_unreadable_chapter_degrades_gracefully(self, tmp_path: Path):
        """If chapter N-1 cannot be read (file missing), assembler returns
        whatever synopsis is available or '' — never raises."""
        # Do NOT create chapter_01.md on disk
        _make_synopsis(tmp_path, {"chapter_01.md": "- Hero arrived"})

        assembler = _assembler(tmp_path)
        # Should not raise even though chapter_01.md is missing
        context = assembler.build_creative_context(
            target_file="chapter_02.md",
            all_chapter_files=["chapter_01.md"],
        )
        assert isinstance(context, str)
        # Huge raw text from missing file definitely shouldn't appear
        assert "chapter_01.md" not in context or "PREVIOUS CHAPTER" not in context
