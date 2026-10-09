"""The `run` line a contest hint prints is shell-quoted, so it pastes back as the same argv."""

import shlex

from tools.contest import cli


def test_run_line_quotes_an_argument_with_spaces_and_parentheses():
    line = cli._run_line(["run", "--ticket", "3", "--brief", "fix the parser (urgent)",
                          "--models", "a,b"], 7)
    # split back to exactly the original argv, with only the ticket number replaced
    assert shlex.split(line) == ["python3", "-m", "tools.contest", "run", "--ticket", "7",
                                 "--brief", "fix the parser (urgent)", "--models", "a,b"]


def test_run_line_keeps_ticket_equals_form_and_replaces_the_number():
    line = cli._run_line(["run", "--ticket=3", "--brief", "x y"], 9)
    assert shlex.split(line) == ["python3", "-m", "tools.contest", "run", "--ticket=9",
                                 "--brief", "x y"]
