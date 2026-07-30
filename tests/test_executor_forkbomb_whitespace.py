"""tests/test_executor_forkbomb_whitespace.py — Bug #33: fork-bomb whitespace bypass.

Bug
---
``Executor._BLOCKED_COMMAND_PATTERNS`` used to contain the fork bomb as an
exact-substring literal: ``":(){:|:&};:"``. That literal is a bash *function
definition*, and bash allows arbitrary whitespace between its tokens (inside
the parens/braces, before the opening brace, between the pipe and ampersand,
etc). Any whitespace variant of the canonical string — e.g.
``":() { :|:& };:"`` — is equally dangerous bash but was never matched by the
literal, silently defeating a security control on trivial input variation.

Reproduction (against the ORIGINAL, unfixed code)
---------------------------------------------------
    >>> Executor._check_command_safety(":(){:|:&};:")
    (False, "blocked pattern ':(){:|:&};:' in acceptance_check")   # blocked
    >>> Executor._check_command_safety(":() { :|:& };:")
    (True, '')                                                     # BYPASSED

Fix
---
Replaced the exact-substring literal with a dedicated, whitespace-tolerant
regular expression matched against the fixed token sequence of a fork bomb
(colon, open-paren, close-paren, open-brace, colon, pipe, colon, ampersand,
close-brace, semicolon, colon), checked before the literal-pattern loop.

Counterfactual verification
----------------------------
On the UNFIXED code (``git show HEAD:tools/auto/executor.py`` prior to this
patch), ``test_whitespace_variants_are_blocked`` FAILS for every whitespace
variant except the canonical string itself — reproducing the exact bug above.
On the FIXED code, all cases pass.

Test-writing lesson carried over from the knowledge file: the canonical
(never-broken) case is asserted SEPARATELY from the whitespace variants, and
only on the security property (``safe is False``), not on the exact wording
of the reason string — the old code's message for the canonical case already
differs in phrasing from the new regex-based message, which would otherwise
look like a false-alarm "regression" during counterfactual testing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.auto.executor import Executor  # noqa: E402


class TestForkBombCanonicalStillBlocked:
    """The original, never-broken exact-literal case must still be blocked."""

    def test_canonical_fork_bomb_blocked(self) -> None:
        safe, _reason = Executor._check_command_safety(":(){:|:&};:")
        assert safe is False


class TestForkBombWhitespaceVariantsBlocked:
    """Whitespace variants of the fork bomb — previously bypassed — must now
    be blocked. Each of these is valid, equally-dangerous bash."""

    @pytest.mark.parametrize("variant", [
        ":() { :|:& };:",           # space before brace, inside braces
        ": () {: | :& };:",         # space after leading colon, around pipe
        ":(){ :|: & };:",           # space before closing brace, before &
        ":  (  )  {  :  |  :  &  }  ;  :",   # heavy spacing throughout
        ":()\t{:|:&};:",            # tab variant
        ":()\n{:|:&};:",            # newline variant
    ])
    def test_whitespace_variants_are_blocked(self, variant: str) -> None:
        safe, reason = Executor._check_command_safety(variant)
        assert safe is False, (
            f"whitespace variant of the fork bomb was not blocked: {variant!r}"
        )
        assert reason  # a non-empty reason is returned alongside the block


class TestForkBombNoFalsePositives:
    """The whitespace-tolerant regex must not flag unrelated brace/paren
    syntax that merely happens to contain some of the same characters."""

    @pytest.mark.parametrize("benign_cmd", [
        "echo hello",
        "pytest",
        "terraform apply",
        "if (x) { print(1) }",
        "def f(): pass",
        "python -c \"print(1)\"",
        "func() { echo hi; }",   # ordinary shell function, not a fork bomb
    ])
    def test_benign_command_not_blocked(self, benign_cmd: str) -> None:
        safe, _reason = Executor._check_command_safety(benign_cmd)
        assert safe is True, f"benign command was incorrectly blocked: {benign_cmd!r}"
