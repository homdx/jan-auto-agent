"""H4 — a task's own fenced code block must not terminate its section.

``_task_section_pattern`` delimits one ``### <id>: ...`` entry in
IMPROVEMENTS.md so ``_remove_from_improvements_md`` can delete it whole.

FIX-2 #8 already removed the original defect: the bare ``(?=\\n### )``
lookahead ended the entry at ANY line starting with ``### ``, including one
the task's own instruction quoted, so the entry was cut short and its tail
left behind as orphaned text. That fix keys the boundary off the renderer's
own ``\\n---\\n`` separator, accepted only when a real heading follows it.

H4 is the same defect reached through the new door. An instruction that
teaches markdown quotes markdown::

    ```markdown
    ---
    ### Example heading
    ```

Those three lines are a code sample, but they read as "separator, then a
heading" — the exact shape of a real entry terminator — so the section was
again cut off mid-fence and its tail orphaned.

The fix makes the body consume a fenced block as one atomic unit, so no
position inside a fence is ever offered as a boundary. These tests pin both
directions: a fence never terminates an entry, and real structure outside a
fence still does.
"""

from __future__ import annotations

import pytest

from tools.auto.plan_validator import _task_section_pattern

_TAIL = """### AUTO-T2: Second task
Instruction: nothing special.

---

## Manual Suggestions

- a manual note
"""


def _doc(body: str) -> str:
    return f"## Autonomous Tasks\n\n### AUTO-T1: First task\n{body}\n\n---\n\n{_TAIL}"


def _strip(doc: str, task_id: str = "AUTO-T1") -> str:
    return _task_section_pattern(task_id).sub("", doc)


@pytest.mark.parametrize(
    "label,body",
    [
        (
            "backtick fence holding a separator and a heading",
            "Instruction: render this:\n\n```markdown\n---\n### Example heading\nbody\n```\n\nAcceptance: done.",
        ),
        (
            "tilde fence holding a separator and a heading",
            "Instruction: render this:\n\n~~~markdown\n---\n### Example heading\n~~~\n\nAcceptance: done.",
        ),
        (
            "fence holding only an interior heading",
            "Instruction:\n\n```md\n### Notes:\ntext\n```\n\nAcceptance: done.",
        ),
        (
            "fence holding only a horizontal rule",
            "Instruction:\n\n```md\nabove\n---\nbelow\n```\n\nAcceptance: done.",
        ),
        (
            "two fences in one entry",
            "```md\n---\n### One\n```\n\nand\n\n```md\n---\n### Two\n```",
        ),
        (
            "a longer fence containing a shorter one",
            "````md\n```\n---\n### Inner\n```\n````",
        ),
        (
            "a tilde line inside a backtick fence is body, not a closer",
            "```md\n~~~\n---\n### Example\n~~~\n```",
        ),
        (
            "indented fence (CommonMark allows up to three spaces)",
            "Instruction:\n\n   ```md\n   ---\n   ### Example\n   ```\n\nAcceptance: done.",
        ),
    ],
)
def test_fenced_content_does_not_terminate_the_section(label: str, body: str) -> None:
    """The whole entry comes out, and nothing of it is left behind."""
    out = _strip(_doc(body))
    assert "AUTO-T1" not in out, f"{label}: entry not removed"
    assert "### Example heading" not in out, f"{label}: fenced tail orphaned"
    assert "Acceptance: done." not in out, f"{label}: entry tail orphaned"
    assert "```" not in out and "~~~" not in out, f"{label}: fence orphaned"
    # The neighbours are untouched.
    assert "### AUTO-T2: Second task" in out
    assert "## Manual Suggestions" in out
    assert "- a manual note" in out


def test_real_separator_and_heading_still_terminate_the_entry() -> None:
    """The fence handling must not make the terminator itself unreachable."""
    out = _strip(_doc("Instruction: plain prose, no code at all."))
    assert "AUTO-T1" not in out
    assert "plain prose" not in out
    assert "### AUTO-T2: Second task" in out


def test_unclosed_fence_degrades_rather_than_swallowing_the_file() -> None:
    """An unclosed fence matches no block, so the per-character path still
    finds the real terminator — the rest of the file must survive."""
    out = _strip(_doc("Instruction:\n\n```py\nnever closed"))
    assert "AUTO-T1" not in out
    assert "### AUTO-T2: Second task" in out
    assert "## Manual Suggestions" in out


def test_last_entry_before_manual_suggestions_is_still_removed() -> None:
    """The fallback lookahead (no trailing separator) still works, fence and
    all — this is the hand-edited / foreign-renderer path."""
    doc = (
        "## Autonomous Tasks\n\n"
        "### AUTO-T1: Only task\n"
        "Instruction:\n\n```md\n---\n### Example\n```\n"
        "\n## Manual Suggestions\n\n- a manual note\n"
    )
    out = _strip(doc)
    assert "AUTO-T1" not in out
    assert "### Example" not in out
    assert "## Manual Suggestions" in out
    assert "- a manual note" in out


def test_a_task_whose_id_is_absent_matches_nothing() -> None:
    """The caller's `text == original` no-op check depends on this."""
    doc = _doc("Instruction: prose.")
    assert _strip(doc, "AUTO-T99") == doc
