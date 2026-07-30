"""tests/test_executor_forkbomb_whitespace.py — the fork bomb blocklist
entry was defeated by a single extra space.

The fork bomb entry in _BLOCKED_COMMAND_PATTERNS was an EXACT-substring
literal (":(){:|:&};:"), but that structure is a bash function definition
— arbitrary whitespace is valid syntax anywhere inside it. Any of these are
equally valid, equally dangerous, and NONE matched the literal:

    ":(){ :|:& };:"          (spaces inside the braces)
    ":() { :|:& };:"         (space before the brace)
    ": ( ) { : | : & } ; :"  (spaces everywhere)

A successful fork bomb can hang or crash the ENTIRE host, not just fail the
one sandboxed task — the highest-severity failure mode this defense-in-depth
blocklist exists to catch, defeated by a single extra space in the acceptance
check string. Fixed with a whitespace-tolerant regex, verified not to
false-positive on unrelated brace/paren syntax (a Python function def, an
`if (x) {...}` block).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.executor import Executor


class TestForkBombWhitespaceVariants:
    def test_canonical_form_was_already_blocked(self):
        """The exact literal was never broken -- the OLD mechanism already
        blocks this one, just with different message wording. Kept
        separate from the whitespace-variant cases below so a wording
        assertion on THIS case can't accidentally validate nothing, the
        way a first draft of this test did."""
        safe, reason = Executor._check_command_safety(":(){:|:&};:")
        assert safe is False

    @pytest.mark.parametrize("command", [
        ":(){ :|:& };:",
        ":() { :|:& };:",
        ":(){ : | : & };:",
        ": ( ) { : | : & } ; :",
        "echo hi; :(){ :|:& };:",
        "ECHO HI && :(){ :|:& };:",
    ])
    def test_whitespace_variant_is_blocked(self, command):
        """BUGFIX: confirmed on unfixed code that EVERY one of these
        returned (True, '') -- completely unblocked, not merely blocked
        with different wording. A first draft of this test parametrized
        the canonical (never-broken) form together with these variants
        under one shared wording assertion, which happened to pass for
        the canonical case regardless of whether the real fix was present
        -- checked directly and split apart before trusting this test."""
        safe, reason = Executor._check_command_safety(command)
        assert safe is False, f"variant not blocked: {command!r}"
        assert "fork bomb" in reason

    @pytest.mark.parametrize("command", [
        "def f(): pass",
        "if (x) { y() }; z",
        "function foo() { return 1; }",
        "pytest -k 'test_thing'",
    ])
    def test_unrelated_brace_syntax_not_flagged(self, command):
        """Sanity: the regex must not become trigger-happy on ordinary
        brace/paren-containing shell or code snippets."""
        safe, reason = Executor._check_command_safety(command)
        assert safe is True, f"false positive: {reason}"

    def test_other_blocklist_entries_still_work(self):
        """Sanity: adding the regex check must not disturb the existing
        word-boundary literal matching for everything else."""
        safe, reason = Executor._check_command_safety("sudo rm -rf /")
        assert safe is False
        safe2, _ = Executor._check_command_safety("pytest tests/test_confirm.py")
        assert safe2 is True
