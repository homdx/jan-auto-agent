"""tests_bugfix/test_bugfix_collect_check_module_precedence.py — `--check`
must beat a malformed `--module` in `parse_collect_args`.

`action_from_flags`'s own docstring lays out one precedence table shared by
both collect entry points, most to least specific:

    check   — read-only; a freshness report must never silently become
              a write, whatever else was also asked for.
    module  — the most specific write request: one named file.
    rebuild — ...
    refresh — ...

Before the fix, `parse_collect_args` resolved `--module`'s value via
`_module_path_from(argv)` unconditionally whenever `--module` appeared in
argv, *before* `action_from_flags` ever ran and got a chance to apply that
precedence. `_module_path_from` raises `CollectCliError` for a `--module`
with no value (or one immediately followed by what looks like another
flag) — so `parse_collect_args(["--check", "--module"])` raised
"--module requires a path argument" instead of returning the `check`
action, even though `--check` should have short-circuited before
`--module`'s value was ever inspected.

This directly broke `--check`'s documented "never becomes a write, whatever
else was also asked for" guarantee at the argv-parsing layer: a user (or a
script) invoking `/collect --check --module` — say, fat-fingering a
trailing `--module` meant for a *different* invocation, or a shell history
splice — got a crash instead of the harmless freshness report `--check`
promises.

  AC-1  `parse_collect_args(["--check", "--module"])` returns the `check`
        action instead of raising.
  AC-2  `parse_collect_args(["--check", "--module="])` (empty value form)
        likewise returns `check` rather than raising.
  AC-3  `--check` combined with a *valid* `--module <path>` still returns
        `check` (module_path is not surfaced — check wins outright, same
        as `action_from_flags` already does when both are truthy).
  AC-4  A malformed `--module` WITHOUT `--check` still raises
        `CollectCliError` — this fix must not make `--module` validation
        disappear, only defer it behind `--check`.
"""

from __future__ import annotations

import pytest

from tools.collect.cli import CollectCliError, parse_collect_args


class TestCheckBeatsMalformedModule:
    """Bug reproduction: --check must short-circuit before --module's own
    value is ever inspected/validated."""

    def test_check_with_valueless_module_returns_check(self):
        """Before the fix this raised CollectCliError instead of returning
        the check action."""
        result = parse_collect_args(["--check", "--module"])
        assert result == {"action": "check", "module_path": None, "drop_summaries": False}

    def test_check_with_empty_equals_module_returns_check(self):
        result = parse_collect_args(["--check", "--module="])
        assert result == {"action": "check", "module_path": None, "drop_summaries": False}

    def test_check_with_module_looking_like_a_flag_returns_check(self):
        """`--module --no-llm` would normally be rejected by
        `_module_path_from` as "looks like a flag rather than a path" —
        but --check must win before that validation ever runs."""
        result = parse_collect_args(["--check", "--module", "--no-llm"])
        assert result == {"action": "check", "module_path": None, "drop_summaries": False}

    def test_check_with_valid_module_still_returns_check(self):
        """check outranks module even when the module value is perfectly
        valid — matches action_from_flags's own precedence."""
        result = parse_collect_args(["--check", "--module", "pkg/a.py"])
        assert result["action"] == "check"


class TestMalformedModuleStillRaisesWithoutCheck:
    """The fix must defer --module validation behind --check, not remove
    it: without --check present, a malformed --module is still an error."""

    def test_valueless_module_without_check_still_raises(self):
        with pytest.raises(CollectCliError, match="--module requires a path argument"):
            parse_collect_args(["--module"])

    def test_empty_equals_module_without_check_still_raises(self):
        with pytest.raises(CollectCliError, match="--module requires a path argument"):
            parse_collect_args(["--module="])

    def test_flag_looking_module_without_check_still_raises(self):
        with pytest.raises(CollectCliError, match="looks like a flag"):
            parse_collect_args(["--module", "--no-llm"])

    def test_valid_module_without_check_still_returns_module(self):
        result = parse_collect_args(["--module", "pkg/a.py"])
        assert result == {"action": "module", "module_path": "pkg/a.py", "drop_summaries": False}
