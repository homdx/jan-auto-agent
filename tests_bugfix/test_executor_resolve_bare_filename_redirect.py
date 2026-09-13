"""tests_bugfix/test_executor_resolve_bare_filename_redirect.py

Regression test for a bug in Executor._resolve_bare_filename (tools/auto/
executor.py, found in the verified-bugs audit #6): the bare-filename rewrite
could not tell a "contains shell operators" command apart from one with
redirections.

`_SHELL_OPS` only covers &&, ||, ;, |, & — so a redirect operator
(> / <, >>, 2>&1) passed the candidate scan untouched, and a command with no
`&&||;` that DID match a bare target filename took the cleanup branch:

    parts[idx] = full_path
    rewritten = shlex.join(parts)

shlex.join shell-quotes every token that is not a single safe word, so the
redirect operator became a LITERAL argument:

    "python gen_report.py > /tmp/report.html"         (target tools/gen_report.py)
    →  "python tools/gen_report.py '>' /tmp/report.html"

Executed with shell=True, the `>` is passed to the child as an argv entry
instead of redirecting stdout — the output file is never created, and any
downstream "does the file exist" check fails spuriously. Same for `2>&1`
(quoted as '2>&1') and `>>` (quoted as '>>').

The fix routes ANY command containing a metacharacter shlex.join would quote
(&&, ||, ;, |, &, >, <) through the existing word-boundary re.sub rewrite,
which substitutes only the bare-filename token and leaves every other byte
of the original command verbatim.
"""

from __future__ import annotations

from tools.auto.executor import Executor


def test_resolve_bare_filename_preserves_stdout_redirect():
    command = "python gen_report.py > /tmp/report.html"
    target_files = ["tools/gen_report.py"]
    rewritten = Executor._resolve_bare_filename(command, target_files)
    assert rewritten == "python tools/gen_report.py > /tmp/report.html"


def test_resolve_bare_filename_preserves_append_redirect():
    command = "python bench.py >> /tmp/out.log"
    target_files = ["tools/bench.py"]
    rewritten = Executor._resolve_bare_filename(command, target_files)
    assert rewritten == "python tools/bench.py >> /tmp/out.log"


def test_resolve_bare_filename_preserves_stderr_merge():
    command = "python run.py 2>&1"
    target_files = ["tools/run.py"]
    rewritten = Executor._resolve_bare_filename(command, target_files)
    assert rewritten == "python tools/run.py 2>&1"


def test_resolve_bare_filename_preserves_pipe():
    command = "python x.py | tee out.txt"
    target_files = ["tools/x.py"]
    rewritten = Executor._resolve_bare_filename(command, target_files)
    assert rewritten == "python tools/x.py | tee out.txt"


def test_resolve_bare_filename_preserves_input_redirect():
    command = "python summarize.py < input.txt"
    target_files = ["tools/summarize.py"]
    rewritten = Executor._resolve_bare_filename(command, target_files)
    assert rewritten == "python tools/summarize.py < input.txt"


def test_resolve_bare_filename_compound_command_still_works():
    command = "pytest notes.txt && echo done"
    target_files = ["src/pkg/notes.txt"]
    rewritten = Executor._resolve_bare_filename(command, target_files)
    assert rewritten == "pytest src/pkg/notes.txt && echo done"


def test_resolve_bare_filename_no_meta_still_joins_cleanly():
    command = "bash generateAllureReport.sh"
    target_files = ["dockerfiles/allure-generator/generateAllureReport.sh"]
    rewritten = Executor._resolve_bare_filename(command, target_files)
    assert rewritten == "bash dockerfiles/allure-generator/generateAllureReport.sh"