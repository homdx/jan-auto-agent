"""Regression: max_versions <= 0 must not crash PromptStore.push().

main.py's _build_agents() constructs PromptStore with config=None (so
__init__'s max(1, ...) clamp never runs) and then overwrites
``self.prompt_store.max_versions = self._getint(...)`` at line ~223.
Before the fix that assignment was unclamped — a present-but-zero
``max_versions = 0`` in agents.ini is a valid int (not malformed, so
_getint's ValueError guard doesn't catch it) and reached push() unclamped.
push() appends the new entry, then ``while len(stack) > 0: stack.pop(0)``
empties the stack, and ``data[…]["current_version"] = stack[-1]["version"]``
raises IndexError on the empty list.

PromptStore.__init__ already clamps with max(1, ...) (lines 67-72), but
that branch only runs when max_versions is passed to __init__ — the
override-after-construction path in main.py bypassed it entirely. The fix
mirrors the clamp at the override site: ``max(1, self._getint(...))``.

These tests pin the invariant: push() must not crash for any
max_versions >= 1, and max_versions = 0 (the unclamped value) must crash
so the bug is visible if the clamp is ever removed from both sites.
"""

from pathlib import Path

import pytest

from tools.prompt_store import PromptStore


def _store(tmp_path: Path, max_versions: int) -> PromptStore:
    return PromptStore(
        store_path=tmp_path / "prompts.json",
        max_versions=max_versions,
    )


def test_init_clamps_zero_to_one(tmp_path):
    """PromptStore.__init__ already clamps max_versions=0 to 1."""
    ps = _store(tmp_path, max_versions=0)
    assert ps.max_versions == 1


def test_init_clamps_negative_to_one(tmp_path):
    ps = _store(tmp_path, max_versions=-3)
    assert ps.max_versions == 1


def test_push_works_with_clamped_max_versions_one(tmp_path):
    """max_versions=1 (the value max(1, 0) produces) must not crash."""
    ps = _store(tmp_path, max_versions=1)
    ps.push("validator_agent", "first prompt", 0.72)
    assert ps.get_current("validator_agent") == "first prompt"
    # push again — oldest is evicted, stack stays at 1
    ps.push("validator_agent", "second prompt", 0.85)
    assert ps.get_current("validator_agent") == "second prompt"
    assert len(ps._load()["validator_agent"]["stack"]) == 1


def test_push_works_with_typical_max_versions(tmp_path):
    ps = _store(tmp_path, max_versions=3)
    for i in range(5):
        ps.push("validator_agent", f"prompt v{i}", 0.5 + i * 0.1)
    stack = ps._load()["validator_agent"]["stack"]
    assert len(stack) == 3  # capped at max_versions
    assert ps.get_current("validator_agent") == "prompt v4"


def test_unclamped_zero_crashes_push(tmp_path):
    """max_versions=0 set AFTER construction (the main.py override path)
    must crash push() with IndexError — this documents the bug the main.py
    fix prevents. If this test ever stops raising, the clamp in push() or
    __init__ was strengthened and the main.py clamp may be redundant."""
    ps = PromptStore(store_path=tmp_path / "prompts.json", max_versions=3)
    # Simulate the unclamped override that main.py used to do:
    ps.max_versions = 0
    with pytest.raises(IndexError):
        ps.push("validator_agent", "prompt", 0.72)


def test_main_py_clamp_expression():
    """Verify the main.py fix is present: the override uses max(1, ...).

    Reads the source so the test fails if someone removes the clamp.
    """
    import re

    src = (Path(__file__).resolve().parent.parent / "main.py").read_text("utf-8")
    # The fix: self.prompt_store.max_versions = max(1, self._getint(...))
    pattern = re.compile(
        r"self\.prompt_store\.max_versions\s*=\s*max\s*\(\s*1\s*,"
    )
    assert pattern.search(src), (
        "main.py no longer clamps prompt_store.max_versions with max(1, ...) — "
        "a zero/negative max_versions in agents.ini will crash push() with IndexError"
    )
