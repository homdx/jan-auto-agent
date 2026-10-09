"""Bug 206: the suite slot and `is_pytest_command` read a pytest run the same way.

Before: `policy.is_full_suite_command` wanted `-m pytest` right after the
interpreter, knew no project runner and cut `PYTEST_ADDOPTS="-q -x"` in two, so
`python3 -u -m pytest tests`, `uv run pytest tests` and a quoted env skipped the
round's suite slot; and `pytest tests -kfoo` / `-mslow` took it.
"""

from __future__ import annotations

import pytest

from tools.auto.utils import is_pytest_command, pytest_argv_start, split_command
from tools.contest.policy import _pytest_argvs, is_full_suite_command

PREFIXES = [
    "python3 -u -m pytest",
    "python -X dev -m pytest",
    "uv run pytest",
    "poetry run pytest",
    'PYTEST_ADDOPTS="-q -x" pytest',
    "FOO=1 pytest",
    "pytest",
]


# 33 — the recogniser starts at the right token
@pytest.mark.parametrize("prefix", PREFIXES)
def test_a_whole_root_behind_any_prefix_takes_the_slot(prefix):
    assert is_full_suite_command(f"{prefix} tests") is True
    assert is_pytest_command(f"{prefix} tests") is True


@pytest.mark.parametrize("command", [
    "timeout 1500 python3 -m pytest tests -q",
    "cd /repo && uv run python -u -m pytest tests_bugfix -n 4",
    "python3 -X dev -W ignore -m pytest tests -q",
    "python -m pytest",
    # a subshell, a group and a negation hold the run like a bare command does
    "( pytest tests )",
    "(pytest tests)",
    "{ pytest tests; }",
    "! pytest tests",
])
def test_a_whole_root_in_any_command_position_takes_the_slot(command):
    assert is_full_suite_command(command) is True
    assert is_pytest_command(command) is True


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("args", ["tests/test_a.py", "tests -k foo", "tests/test_a.py::t"])
def test_a_targeted_run_behind_any_prefix_does_not(prefix, args):
    assert is_full_suite_command(f"{prefix} {args}") is False


@pytest.mark.parametrize("command", [
    "pip install pytest",
    "echo pytest tests",
    "uv pip install pytest",
    "poetry add pytest",
    'PYTEST_ADDOPTS="-q -x" echo pytest tests',
    "python3 -u -c 'import pytest' tests",
    'python3 -c "import pytest"',
    "python3 -u -m pip install pytest",
    "uv run echo pytest tests",
    "ls pytest",
])
def test_a_non_suite_command_does_not(command):
    assert is_full_suite_command(command) is False


def test_a_quoted_env_value_stays_one_word():
    assert _pytest_argvs('PYTEST_ADDOPTS="-q -x" pytest tests') == [["tests"]]


# 34 — attached selectors name a subset
@pytest.mark.parametrize("selector", [
    "-kfoo", "-mslow", "-k=foo", "-m=slow", "-k foo", "-m slow", "-m 'not slow'",
    "--deselect=tests/test_a.py::t", "--deselect tests/test_a.py::t",
])
def test_an_attached_selector_names_a_subset(selector):
    assert is_full_suite_command(f"pytest tests {selector}") is False
    assert is_full_suite_command(f"python3 -m pytest {selector}") is False


@pytest.mark.parametrize("option", ["-q", "-x", "-n 4", "-n4", "--tb=short", "--maxfail=1", "-ra",
                                    "--markers-not-a-flag"])
def test_other_options_do_not(option):
    assert is_full_suite_command(f"pytest tests {option}") is True


# the drift guard of ticket 163, over one shared table
TABLE = [
    "pytest tests", "python3 -u -m pytest tests", "python -X dev -m pytest tests",
    "uv run pytest tests", "poetry run pytest tests", "pipenv run pytest",
    'PYTEST_ADDOPTS="-q -x" pytest tests', "FOO=1 pytest tests",
    "timeout 900 python3 -m pytest tests -n 4", "env A=1 nice -n 5 pytest .smoke_tests",
    "/opt/venv/bin/pytest tests", "/usr/bin/python3.10 -m pytest tests",
    "cd sub && pytest tests 2>&1 | tail -5", "{ pytest tests; }", "( pytest tests )",
    "pytest tests/test_a.py", "pytest tests -kfoo", "pytest tests -mslow",
    "pip install pytest", "echo pytest tests", "python3 -c 'import pytest'", "ls pytest",
    "! pytest tests", "python3 -X dev -W ignore -m pytest tests -q",
    "python3 -m pytestx tests", "bash -lc 'pytest tests'", "ls", "",
]


@pytest.mark.parametrize("command", TABLE)
def test_the_slot_is_taken_only_by_a_pytest_command(command):
    if is_full_suite_command(command):
        assert is_pytest_command(command), command


@pytest.mark.parametrize("command", [c for c in TABLE if c and "|" not in c and "&&" not in c])
def test_both_read_the_same_argument_list(command):
    parts = split_command(command, True)
    seg = [p for p in parts if p not in ("{", "}", "(", ")", ";", "!")]
    start = pytest_argv_start(seg)
    expected = [] if start is None else [seg[start:]]
    assert _pytest_argvs(command) == expected
    assert (start is not None) == is_pytest_command(command)
