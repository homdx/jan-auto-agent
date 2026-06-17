"""tests/test_cr6_prose_pull.py — AUTO-CR-6: Prose-aware pull
(ContextBroker + block_extractor).

Four tests matching the epic acceptance criteria:

  test_heading_section_extracted
  test_entity_paragraph_extracted_and_capped
  test_broker_searches_earlier_chapters_first
  test_code_symbol_path_unchanged (regression)
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block, extract_prose_section
from tools.auto.context_broker import ContextBroker


# ───────────────────────── block_extractor.extract_prose_section ─────────


def test_heading_section_extracted():
    """A heading query returns exactly that section (own text, sub-headings,
    up to the next heading of equal-or-higher level), nothing more."""
    source = (
        "# Chapter 1: The Beginning\n\n"
        "Alice met the Dragon in the old mill.\n\n"
        "## The Pact\n\n"
        "They swore an oath under the broken beam.\n\n"
        "## Aftermath\n\n"
        "Rain fell on the ride home.\n\n"
        "# Chapter 2: The Storm\n\n"
        "The sea rose against the cliffs.\n"
    )

    section = extract_prose_section(source, "The Pact", ".md")
    assert "## The Pact" in section
    assert "They swore an oath under the broken beam." in section
    # Stops at the next heading of equal-or-higher level — sibling and
    # next-chapter content must NOT leak in.
    assert "Aftermath" not in section
    assert "Chapter 2" not in section

    # A full-chapter heading query returns the whole chapter, including its
    # sub-headings, up to the next top-level heading.
    chapter = extract_prose_section(source, "Chapter 1: The Beginning", ".md")
    assert "The Pact" in chapter
    assert "Aftermath" in chapter
    assert "Chapter 2" not in chapter

    # Substring/fragment match also resolves (query is a fragment of the
    # heading text, case-insensitive).
    fragment = extract_prose_section(source, "the storm", ".md")
    assert "The sea rose against the cliffs." in fragment


def test_entity_paragraph_extracted_and_capped():
    """An entity query returns the paragraph(s) mentioning it, capped to a
    small number of paragraphs around the first hit."""
    paragraphs = [f"Paragraph {i} is just filler text about the weather." for i in range(8)]
    paragraphs[4] = "The Dragon appeared at dusk, scales catching the last light."
    source = "\n\n".join(paragraphs)

    result = extract_prose_section(source, "Dragon", ".txt", max_paragraphs=3)
    assert "Dragon" in result
    # Capped: never more than max_paragraphs paragraphs in the result.
    returned = [p for p in result.split("\n\n") if p.strip()]
    assert len(returned) <= 3

    # No match → fail-open empty string, same contract as extract_block.
    assert extract_prose_section(source, "Unicorn", ".txt") == ""

    # Non-prose extension → always "" regardless of content.
    assert extract_prose_section(source, "Dragon", ".py") == ""

    # Empty/whitespace query → "".
    assert extract_prose_section(source, "   ", ".md") == ""


# ───────────────────────── ContextBroker prose-pull wiring ────────────────


def test_broker_searches_earlier_chapters_first():
    """When a query matches multiple chapters, the broker resolves it
    against the lower-numbered (earlier) chapter."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "chapter_1.md").write_text(
            "# Chapter 1\n\nThe lighthouse keeper waved from the rocks.\n"
        )
        (d / "chapter_2.md").write_text(
            "# Chapter 2\n\nThe lighthouse keeper waved again, years later.\n"
        )

        broker = ContextBroker()
        resolved = broker.resolve(["lighthouse keeper"], [], d)
        assert "lighthouse keeper" in resolved
        block = resolved["lighthouse keeper"]
        assert "from the rocks" in block
        assert "years later" not in block

    # Out-of-numeric-order filenames on disk must not change the result —
    # the broker sorts by chapter number, not directory listing order.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "chapter_10.md").write_text(
            "# Chapter 10\n\nThe lighthouse keeper waved once more.\n"
        )
        (d / "chapter_2.md").write_text(
            "# Chapter 2\n\nThe lighthouse keeper waved from the rocks.\n"
        )

        broker = ContextBroker()
        resolved = broker.resolve(["lighthouse keeper"], [], d)
        assert "from the rocks" in resolved["lighthouse keeper"]


def test_code_symbol_path_unchanged():
    """Regression: a code-symbol query against a .py file still goes
    through the AST/brace path (extract_block), unaffected by the new
    prose dispatch in ContextBroker.resolve()."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text(
            "def my_helper(x):\n    return x + 1\n\nclass Other:\n    pass\n"
        )

        # Same result whether called directly or via the broker.
        direct = extract_block(
            (d / "helper.py").read_text(), "my_helper", ".py"
        )
        broker = ContextBroker()
        resolved = broker.resolve(["my_helper"], ["helper.py"], d)
        assert "my_helper" in resolved
        assert resolved["my_helper"].strip() == direct.strip()
        assert "def my_helper(x):" in resolved["my_helper"]

    # A mixed project (code + prose) must route each file through the
    # correct strategy independently.
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "chapter_1.md").write_text("# Chapter 1\n\nThe Dragon woke.\n")
        (d / "lib.py").write_text("def wake_dragon():\n    return True\n")

        broker = ContextBroker()
        resolved = broker.resolve(["Dragon", "wake_dragon"], [], d)
        assert "The Dragon woke." in resolved["Dragon"]
        assert "def wake_dragon():" in resolved["wake_dragon"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
