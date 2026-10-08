"""Ticket 212: the test cache's parser, summary line, progress line and gate-model fallback."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tools.contest.testcache import (
    PytestRun, Summary, classify_with_llm, parse_pytest, progress_line, summarise,
)

DATA = Path(__file__).resolve().parent / "data" / "sandbox_pytest_commands.txt"


def run(roots, flags=()):
    """A PytestRun from plain iterables."""
    return PytestRun(frozenset(roots), frozenset(flags))


T, B = "tests", "tests_bugfix"

#: Every non-empty line of the corpus, in file order, with the runs a reader
#: of that command finds in it. `.smoke_tests` + `.regression_tests` = `tests`;
#: `-n`, `--timeout`, `tail` do not count; `-q`, `--tb`, `--durations` do.
EXPECTED = [
    ("timeout 1200000 python3 -m pytest .smoke_tests/ -q 2>&1 | tail -20", [run([T], ["-q"])]),
    ("timeout 1200000 python3 -m pytest .regression_tests/ -q 2>&1 | tail -20", [run([T], ["-q"])]),
    ("timeout 1200000 python3 -m pytest tests_bugfix -n 4 -q 2>&1 | tail -20", [run([B], ["-q"])]),
    ("python3 -m pytest tests/test_sandbox_full_suite_900.py -q",
     [run(["tests/test_sandbox_full_suite_900.py"], ["-q"])]),
    ("python3 -m pytest .smoke_tests/ -q --tb=no 2>&1 | tail -20", [run([T], ["-q", "--tb=no"])]),
    ("python3 -m pytest .smoke_tests/ .regression_tests/ -q --tb=no 2>&1 | tail -20",
     [run([T], ["-q", "--tb=no"])]),
    ("python3 -m pytest tests_bugfix -n 4 -q --tb=no 2>&1 | tail -20", [run([B], ["-q", "--tb=no"])]),
    ("python3 -m pytest .smoke_tests/ .regression_tests/ -q 2>&1 | tail -30", [run([T], ["-q"])]),
    ("python3 -m pytest tests_bugfix -n 4 -q 2>&1 | tail -30", [run([B], ["-q"])]),
    ("python3 -m pytest tests/test_sandbox_full_suite_900.py -q 2>&1 | tail -10",
     [run(["tests/test_sandbox_full_suite_900.py"], ["-q"])]),
    ("cat pytest.ini 2>/dev/null || cat pyproject.toml 2>/dev/null | head -50", []),
    ("python3 -m pytest .smoke_tests/ .regression_tests/ tests_bugfix -q -n 4 2>&1 | tail -20",
     [run([T, B], ["-q"])]),
    ("time python3 -m pytest .smoke_tests/ .regression_tests/ tests_bugfix -q -n 4 2>&1 | tail -20",
     [run([T, B], ["-q"])]),
    ("python3 -m pytest .smoke_tests/ && python3 -m pytest .regression_tests/ && python3 -m pytest tests_bugfix/",
     [run([T]), run([T]), run([B])]),
    ("pytest -n auto tests/test_run_suite.py", [run(["tests/test_run_suite.py"])]),
    ("python3 -m pytest .smoke_tests/ .regression_tests/ -q -n 4 --durations=0",
     [run([T], ["-q", "--durations=0"])]),
    ("python3 -m pytest .smoke_tests/ .regression_tests/ -q -n 4 --durations=0 --timeout=1200000",
     [run([T], ["-q", "--durations=0"])]),
    ('cd "$PWD" && { time python3 -m pytest .smoke_tests/ .regression_tests/ -q -n 4 ; } 2>&1 | tail -30',
     [run([T], ["-q"])]),
    ("cd /home/renat/Project/opensource/github/agent-offline/rounds/901-laguna-s-2-1 && "
     "/usr/bin/time -v python3 -m pytest .smoke_tests/ .regression_tests/ tests_bugfix -q -n 4 2>&1 | tail -30",
     [run([T, B], ["-q"])]),
    # tests/smoke_tests does not exist in this repo: kept literally, not folded into `tests`
    ('python3 -m pytest tests/smoke_tests tests/regression_tests -q -n 4 --collect-only && echo "full suite -n 4 completed"',
     [run(["tests/smoke_tests", "tests/regression_tests"], ["-q", "--collect-only"])]),
    ("python3 -m pytest tests -q -n 4", [run([T], ["-q"])]),
    ("python3 -m pytest tests -q -n 4 --timeout 300000", [run([T], ["-q"])]),
    ("python3 -m pytest tests -q -n 4 --timeout 600000", [run([T], ["-q"])]),
    ("cd /home/renat/Project/opensource/github/agent-offline/rounds/901-sensenova-6-7-flash-lite && "
     "{ time (python3 -m pytest .smoke_tests/ .regression_tests/ -q -n 4 && python3 -m pytest tests_bugfix -q -n 4); } 2>&1 | tail -60",
     [run([T], ["-q"]), run([B], ["-q"])]),
    ("ls tests/ | head -40; echo ---; cat pytest.ini; echo ---; ls .smoke_tests .regression_tests tests_bugfix | head -40", []),
    ("""python3 -c "import xdist, pytest; print('xdist', xdist.__version__, 'pytest', pytest.__version__)"; """
     "ls tests/ | grep -c '^test_'; ls tests_bugfix | head; ls tests/*policy* 2>/dev/null | head -30", []),
    ('T0=$(date +%s); python3 -m pytest .smoke_tests/ .regression_tests/ -q -n 4; RC=$?; T1=$(date +%s); '
     'echo "RUN1_TIERS rc=$RC wall=$((T1-T0))s"; exit 0',
     [run([T], ["-q"])]),
    ('ls tests/ | head -50 && echo "---" && ls .smoke_tests/ | head -20 && echo "---" && ls .regression_tests/ | head -20 '
     '&& echo "---" && ls tests_bugfix/ | head -30 && echo "---" && cat pytest.ini', []),
    ("time python3 -m pytest .smoke_tests .regression_tests tests_bugfix -q -n 4", [run([T, B], ["-q"])]),
    ('cat pytest.ini 2>/dev/null || cat setup.cfg 2>/dev/null || cat pyproject.toml 2>/dev/null | grep -A 10 pytest '
     '|| echo "No pytest config found"', []),
    ("time python3 -m pytest .smoke_tests/ .regression_tests/ tests_bugfix -q -n 4", [run([T, B], ["-q"])]),
]


def corpus_lines() -> list:
    return [ln for ln in DATA.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_expected_table_covers_every_corpus_line_in_order():
    """The table is the corpus: same lines, same order, nothing skipped."""
    assert [c for c, _ in EXPECTED] == corpus_lines()
    assert len(corpus_lines()) == 31 and len(EXPECTED) == 31


@pytest.mark.parametrize("command,expected", EXPECTED, ids=[str(i) for i in range(len(EXPECTED))])
def test_corpus_command_parses_to_written_runs(command, expected):
    """Each real agent command parses to exactly the runs written beside it."""
    assert parse_pytest(command) == expected


def test_normalisation_equivalences():
    """-n does not matter; the tier roots equal tests; -x, -k and trailing slashes do."""
    assert parse_pytest("pytest -n 4 tests") == parse_pytest("pytest -n 1 tests") == parse_pytest("pytest -nauto tests")
    assert parse_pytest("pytest .smoke_tests .regression_tests") == parse_pytest("pytest tests/")
    assert parse_pytest("pytest ./tests/") == parse_pytest("pytest tests")
    assert parse_pytest("pytest tests -x") != parse_pytest("pytest tests")
    assert parse_pytest("pytest tests -k a") != parse_pytest("pytest tests -k b")
    assert parse_pytest("pytest tests -p no:cacheprovider -W ignore --color=no --no-header --timeout=9") \
        == parse_pytest("pytest tests")
    assert parse_pytest("pytest tests -xvq")[0].flags == frozenset({"-x", "-v", "-q"})
    assert parse_pytest("pytest tests --maxfail 2")[0].flags == frozenset({"--maxfail=2"})
    assert parse_pytest("pytest tests -vv") != parse_pytest("pytest tests -v")


@pytest.mark.parametrize("command,roots", [
    ("pytest tests", ["tests"]),
    ("py.test tests", ["tests"]),
    ("/opt/venv/bin/pytest tests", ["tests"]),
    ("python -m pytest tests", ["tests"]),
    ("nice -n 5 pytest tests", ["tests"]),
    ("env PYTHONPATH=. pytest tests", ["tests"]),
    ("PYTHONPATH=. timeout 600 pytest tests", ["tests"]),
    ("(cd x; pytest tests)", ["tests"]),
    ("{ pytest tests; } | tee out.log", ["tests"]),
    ('bash -c "cd x && pytest tests"', ["tests"]),
    ("pytest tests 2>&1 >out.txt", ["tests"]),
])
def test_wrapper_forms(command, roots):
    """Wrappers, groups and prefixes are seen through."""
    assert parse_pytest(command) == [run(roots)]


@pytest.mark.parametrize("command", [
    "", "   ", "echo pytest tests", "ls pytest tests", "make test", "bash run_tests.sh",
    "pytest 'unterminated", 'echo "$(pytest tests)"', "python3 -c 'import pytest'",
])
def test_not_pytest_or_unreadable_is_empty_never_raises(command):
    """Commands that do not run pytest, or cannot be read, give [] — no exception."""
    assert isinstance(parse_pytest(command), list)


@pytest.mark.parametrize("command", ["echo pytest tests", "make test", "bash run_tests.sh", "ls pytest tests", ""])
def test_non_runs_are_empty(command):
    """The command word decides: only pytest in command position is a run."""
    assert parse_pytest(command) == []


def test_non_string_input_is_empty():
    """Garbage in is [] out."""
    assert parse_pytest(None) == [] and parse_pytest(5) == [] and parse_pytest("a\x00b") == []


# ── summarise ──────────────────────────────────────────────────────────────


def test_summarise_passed_and_skipped():
    """The -q summary of a green run."""
    s = summarise("....\n7262 passed, 52 skipped in 94.31s (0:01:34)\n", 0)
    assert (s.passed, s.skipped, s.failed, s.errors) == (7262, 52, 0, 0)
    assert s.duration_s == pytest.approx(94.31) and s.failed_names == []
    assert s.raw_line.startswith("7262 passed")


def test_summarise_failed_and_errors_with_names():
    """A red run keeps its counts and failing names (at most 10)."""
    names = "\n".join(f"FAILED tests/test_a.py::test_{i} - assert 0" for i in range(14))
    out = f"{names}\nERROR tests/test_b.py::test_x - boom\n=== 14 failed, 1 error, 3 passed in 1.50s ===\n"
    s = summarise(out, 1)
    assert (s.failed, s.errors, s.passed) == (14, 1, 3)
    assert len(s.failed_names) == 10 and s.failed_names[0] == "tests/test_a.py::test_0"


def test_summarise_plural_errors_and_last_summary_wins():
    """'2 errors' counts, and a chained run's second summary is the one kept."""
    out = "=== 1 passed in 0.1s ===\n=== 2 failed, 2 errors in 0.2s ===\n"
    s = summarise(out, 1)
    assert (s.failed, s.errors, s.passed) == (2, 2, 0)


def test_summarise_cut_by_tail_is_none():
    """A tail that dropped the summary line is not a result."""
    assert summarise("...F..\nsome traceback\n", 1) is None
    assert summarise("", 0) is None and summarise(None, 0) is None


def test_summarise_killed_run_is_none():
    """The tool killed the call: even a summary-looking line before it is not recorded."""
    out = "5 passed in 1.0s\n\nterminated command after exceeding timeout 120000 ms\n"
    assert summarise(out, None) is None


# ── progress_line ──────────────────────────────────────────────────────────


def test_progress_line_shape():
    """One line, starts with a 10-char bar, counts and 'not re-run'."""
    s = Summary(passed=7262, skipped=52, duration_s=94.0, raw_line="x")
    line = progress_line(s, age_s=120.0, agent="agnes-2-5-flash", wall_s=94.2)
    assert "\n" not in line
    assert line.startswith("[##########]")
    assert "7262 passed" in line and "52 skipped" in line and "not re-run" in line
    assert "agnes-2-5-flash" in line and "94 s" in line


def test_progress_line_failures_are_served_with_names():
    """A red run is cached too: at most 10 names, still one line, wall_s may be None."""
    s = Summary(failed=2, passed=1, failed_names=[f"t::n{i}" for i in range(12)], duration_s=3.0)
    line = progress_line(s, age_s=5.0, agent="a\nb", wall_s=None)
    assert "\n" not in line and "2 failed" in line and "t::n9" in line and "t::n10" not in line


# ── classify_with_llm ──────────────────────────────────────────────────────


def test_classify_good_json():
    """A good answer is a parse, normalised like a mechanical one."""
    ans = '```json\n{"runs_tests": true, "roots": [".smoke_tests/", "tests_bugfix"], "flags": ["-x", "-k slow"]}\n```'
    got = classify_with_llm("make test", None, completion_fn=lambda s, u: ans)
    assert got == run([T, B], ["-x", "-k=slow"])


def test_classify_not_a_test_run_is_none():
    """The model says it is not a test run: None."""
    assert classify_with_llm("ls", None, completion_fn=lambda s, u: '{"runs_tests": false}') is None


@pytest.mark.parametrize("answer", ["garbage", "", "{not json}", '{"runs_tests": true, "roots": "tests"}',
                                    '{"runs_tests": true, "roots": [3]}', "[1, 2]"])
def test_classify_garbage_is_none(answer):
    """Unreadable or malformed answers are None."""
    assert classify_with_llm("make test", None, completion_fn=lambda s, u: answer) is None


def test_classify_exception_is_none():
    """A failing transport is None, not an exception."""
    def boom(system, user):
        raise RuntimeError("429")
    assert classify_with_llm("make test", None, completion_fn=boom) is None


def test_classify_slow_is_none_within_budget():
    """A late answer is abandoned at the budget."""
    def slow(system, user):
        time.sleep(2.0)
        return '{"runs_tests": true, "roots": ["tests"], "flags": []}'
    started = time.monotonic()
    assert classify_with_llm("make test", None, budget_s=0.2, completion_fn=slow) is None
    assert time.monotonic() - started < 1.5


def test_classify_without_settings_or_command_is_none():
    """No gate model configured, or an empty command: None."""
    assert classify_with_llm("make test", None) is None
    assert classify_with_llm("", None, completion_fn=lambda s, u: "{}") is None
