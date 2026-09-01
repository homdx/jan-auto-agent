"""tests/test_executor_resolve_bare_filename_backslash.py

Regression test for a bug in Executor._resolve_bare_filename (tools/auto/
executor.py): the compound-command (contains &&, ||, or ;) rewrite path
used to call

    re.sub(pattern, full_path, command, count=1)

passing full_path as re.sub's *string* replacement argument. re.sub
interprets backslash sequences in a string replacement as backreferences
(\\1, \\g<n>, ...). The pattern here has no capture groups, so a target path
containing an ordinary backslash (e.g. a Windows-style path) raised
`re.error: invalid group reference` instead of substituting cleanly.

This mirrors an identical bug already found and fixed in
tools/auto/summary_memory.py (see its "Bugfix" comment) — the fix here is
the same: use a callable replacement, since re.sub never interprets
backslash escapes in a function's return value.
"""

from __future__ import annotations

from tools.auto.executor import Executor


def test_resolve_bare_filename_with_backslash_in_path_compound_command():
    """A target path containing a backslash must not raise re.error.

    On POSIX, Path(...).name only splits on '/', so the backslash must sit
    in a directory component (before the final '/') for the basename to
    still match "notes.txt" while the full replacement string carries a
    backslash-digit sequence through re.sub.
    """
    command = "pytest notes.txt && echo done"
    target_files = [r"some\1weird\dir/notes.txt"]

    # Previously this raised: re.error: invalid group reference 1
    rewritten = Executor._resolve_bare_filename(command, target_files)

    assert rewritten == r"pytest some\1weird\dir/notes.txt && echo done"


def test_resolve_bare_filename_with_group_like_backslash_sequence():
    """A path containing \\g<...>-shaped text must also not be interpreted
    as a backreference."""
    command = "pytest notes.txt ; echo done"
    target_files = [r"weird\g<name>dir/notes.txt"]

    rewritten = Executor._resolve_bare_filename(command, target_files)

    assert rewritten == r"pytest weird\g<name>dir/notes.txt ; echo done"


def test_resolve_bare_filename_normal_path_unaffected():
    """Sanity check: ordinary (non-backslash) paths still rewrite correctly
    on the compound-command path."""
    command = "pytest notes.txt && echo done"
    target_files = ["src/pkg/notes.txt"]

    rewritten = Executor._resolve_bare_filename(command, target_files)

    assert rewritten == "pytest src/pkg/notes.txt && echo done"
