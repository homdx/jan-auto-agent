"""tests/test_coder_forkbomb_whitespace.py — sibling of bug #33 in coder.py.

Bug
---
``Coder._check_content_safety`` (the content-level guard that scans
LLM-generated file content before it reaches disk) blocks the fork-bomb
fragment via a plain-substring entry in ``_BLOCKED_ALWAYS``:
``("fork bomb shell", ":|:&")``. Bash allows arbitrary whitespace between
these tokens once wrapped in a function definition (the classic
``:(){:|:&};:``), and any whitespace variant — e.g. ``:|: &`` or the full
``:() { :|: & };:`` — is equally dangerous once written to a file that is
later executed by the Executor, but was never matched by the exact
substring. This is the same class of bug as #33
(``tools/auto/executor.py::_check_command_safety``), found independently in
a second, duplicate guard over the same threat.

Reproduction (against the ORIGINAL, unfixed code)
---------------------------------------------------
    >>> Coder._check_content_safety(":|:&", task_mode="code")
    (False, "blocked content pattern ':|:&' (fork bomb shell)")   # blocked
    >>> Coder._check_content_safety(":|: &", task_mode="code")
    (True, '')                                                     # BYPASSED

Fix
---
Replaced the plain-substring check for this one pattern with a dedicated
whitespace-tolerant regex (``:\\s*\\|\\s*:\\s*&``) run unconditionally before
the literal-pattern loop, in the same ``_BLOCKED_ALWAYS`` scope (checked in
every task_mode: code, docs, creative). The existing pre-existing-content
grandfathering mechanism is preserved: an edit to a file that already
legitimately contained the pattern is still permitted.

Counterfactual verification
----------------------------
On the UNFIXED code, ``test_whitespace_variants_are_blocked`` FAILS for
every whitespace variant — reproducing the bypass above — while the
canonical/bare-fragment case and all existing tests in
``test_coder_safety_domain.py`` continue to pass unchanged (the fragment
detection itself, not just the whitespace tolerance, was already correct
and must not regress).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.auto.coder import Coder  # noqa: E402


class TestForkBombFragmentStillBlocked:
    """The original, never-broken bare-fragment case must still be blocked,
    in every task_mode (this guard is unconditional)."""

    @pytest.mark.parametrize("task_mode", ["code", "docs", "creative"])
    def test_bare_fragment_blocked(self, task_mode: str) -> None:
        safe, reason = Coder._check_content_safety(":|:&", task_mode=task_mode)
        assert safe is False
        assert "fork bomb" in reason


class TestForkBombWhitespaceVariantsBlocked:
    """Whitespace variants of the fork-bomb fragment — previously
    bypassed — must now be blocked, in every task_mode."""

    @pytest.mark.parametrize("variant", [
        ":|: &",                     # space before &
        ": | :&",                    # space around the pipe
        ":() { :|: & };:",           # full wrapper with internal spacing
        ":  |  :  &",                # heavy spacing throughout
        ":|:\t&",                    # tab variant
        ":|:\n&",                    # newline variant
    ])
    @pytest.mark.parametrize("task_mode", ["code", "docs", "creative"])
    def test_whitespace_variants_are_blocked(self, variant: str, task_mode: str) -> None:
        safe, reason = Coder._check_content_safety(variant, task_mode=task_mode)
        assert safe is False, (
            f"whitespace variant of the fork-bomb fragment was not blocked "
            f"in task_mode={task_mode!r}: {variant!r}"
        )
        assert "fork bomb" in reason


class TestForkBombGrandfathering:
    """A file that already legitimately contains the pattern (e.g. this very
    test file, or a security document quoting it) must remain editable —
    the grandfathering mechanism must still apply to the regex-based check."""

    def test_preexisting_whitespace_variant_is_grandfathered(self) -> None:
        existing = "# warning, never write: :|: &\n"
        safe, _reason = Coder._check_content_safety(
            ":|: &\n", task_mode="code", existing_content=existing,
        )
        assert safe is True

    def test_new_occurrence_in_clean_file_still_blocked(self) -> None:
        safe, _reason = Coder._check_content_safety(
            ":|: &\n", task_mode="code", existing_content="def add(a, b):\n    return a + b\n",
        )
        assert safe is False


class TestForkBombNoFalsePositives:
    """The whitespace-tolerant regex must not flag unrelated code."""

    @pytest.mark.parametrize("benign_content", [
        "def add(a, b):\n    return a + b\n",
        "func() { echo hi; }\n",
        "if (x) { print(1) }\n",
        "x: int = 1\ny: int = 2\n",
    ])
    def test_benign_content_not_blocked(self, benign_content: str) -> None:
        safe, reason = Coder._check_content_safety(benign_content, task_mode="code")
        assert safe is True, f"benign content incorrectly blocked: {reason}"
