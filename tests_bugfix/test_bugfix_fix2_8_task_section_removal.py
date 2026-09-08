"""FIX-2 #8 — the IMPROVEMENTS.md section regex truncated a task's own entry.

``_task_section_pattern`` bounded a task entry with a bare ``\\n### ``
lookahead, which matches ANY line starting with ``### `` — including one the
task wrote inside its own instruction ("### Notes:", a quoted markdown
example). The removal stopped at that interior heading and left the rest of
the entry behind as orphaned text, so plan.json and IMPROVEMENTS.md openly
disagreed about a task that had been dropped.

The fix keys off the separator ``to_improvements_md`` itself emits after
every rendered entry (``"...{instruction}\\n\\n---\\n\\n"``) and CONSUMES it, so
the entry and its own trailing rule come out as one unit.

The separator is only accepted when a heading, the Manual Suggestions
section, or end-of-file follows it — otherwise the fix merely relocates the
bug: an instruction containing a markdown horizontal rule would end the
match at its own interior ``---``, orphaning the text after it. A real
terminator is always followed by a heading; an interior rule never is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.plan_validator import _task_section_pattern  # noqa: E402


def _entry(task_id: str, title: str, instruction: str, acc: str = "pytest -q") -> str:
    """One entry exactly as to_improvements_md() renders it."""
    return (
        f"### {task_id}: {title}\n\n"
        f"**Cluster:** c  \n"
        f"**Location:** `a.py`  \n"
        f"**Target files:** `a.py`  \n"
        f"**Dependencies:** none  \n"
        f"**Acceptance check:**\n```\n{acc}\n```\n\n"
        f"**Instruction:**\n\n{instruction}\n\n---\n\n"
    )


def _doc(*entries: str) -> str:
    return (
        "# IMPROVEMENTS.md\n## Autonomous Tasks\n\n"
        + "".join(entries)
        + "## Manual Suggestions\n\n_No manual suggestions._\n"
    )


def _remove(text: str, task_id: str) -> str:
    return _task_section_pattern(task_id).sub("", text)


PLAIN = _entry("T-1", "One", "Do it.")
OTHER = _entry("T-2", "Two", "Do the other thing.")
THIRD = _entry("T-3", "Three", "Third.")


class TestEntryIsRemovedWhole:
    def test_plain_entry_including_its_separator(self) -> None:
        assert _remove(_doc(PLAIN, OTHER), "T-1") == _doc(OTHER)

    def test_interior_hash_heading_no_longer_truncates(self) -> None:
        """The reported defect: a "### Notes:" inside the instruction."""
        entry = _entry("T-1", "One", "Do it.\n\n### Notes:\nbe careful.")
        assert _remove(_doc(entry, OTHER), "T-1") == _doc(OTHER)

    def test_interior_horizontal_rule_no_longer_truncates(self) -> None:
        """The relocated-bug guard: a markdown "---" inside the instruction
        must not be mistaken for the entry's own terminator."""
        entry = _entry("T-1", "One", "Do it.\n\n---\n\nmore prose.")
        assert _remove(_doc(entry, OTHER), "T-1") == _doc(OTHER)

    def test_interior_heading_and_rule_together(self) -> None:
        entry = _entry("T-1", "One", "Do.\n\n### Notes:\nx\n\n---\n\ntail.")
        assert _remove(_doc(entry, OTHER), "T-1") == _doc(OTHER)

    def test_interior_hash_hash_heading_no_longer_truncates(self) -> None:
        """A "## " line (a shell comment, a quoted heading) is not the
        Manual Suggestions boundary."""
        entry = _entry("T-1", "One", "Run:\n\n## build step\nmake")
        assert _remove(_doc(entry, OTHER), "T-1") == _doc(OTHER)

    def test_rule_inside_the_acceptance_check_fence(self) -> None:
        entry = _entry("T-1", "One", "Do.", acc="echo ---\nmake")
        assert _remove(_doc(entry, OTHER), "T-1") == _doc(OTHER)


class TestNeighboursAreUntouched:
    def test_middle_entry_with_interior_rule(self) -> None:
        middle = _entry("T-2", "Two", "b.\n\n---\n\nc.")
        assert _remove(_doc(PLAIN, middle, THIRD), "T-2") == _doc(PLAIN, THIRD)

    def test_last_entry_before_manual_suggestions(self) -> None:
        entry = _entry("T-1", "One", "Do.\n\n---\n\ntail.")
        assert _remove(_doc(entry), "T-1") == _doc()

    def test_absent_id_is_a_no_op(self) -> None:
        doc = _doc(PLAIN, OTHER)
        assert _remove(doc, "T-99") == doc

    def test_id_is_regex_escaped(self) -> None:
        entry = _entry("T.1", "Dot", "Do.")
        assert _remove(_doc(entry, OTHER), "T.1") == _doc(OTHER)
        # "T.1" must not match "T-1" via the regex dot wildcard.
        assert _remove(_doc(PLAIN, OTHER), "T.1") == _doc(PLAIN, OTHER)


class TestSeparatorlessFallback:
    def test_hand_edited_file_without_a_trailing_rule_still_matches(self) -> None:
        """A file not produced by to_improvements_md may lack the separator;
        the entry must still be removable rather than left in place."""
        doc = (
            "# IMPROVEMENTS.md\n## Autonomous Tasks\n\n"
            "### T-1: One\n\nhand written, no rule\n"
            "## Manual Suggestions\n\n_No manual suggestions._\n"
        )
        assert "### T-1:" not in _remove(doc, "T-1")
