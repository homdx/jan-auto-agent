"""tests/test_stress_suite.py — FL-3: `scripts/stress_suite.py`, the flake table.

FL-1's whole failure mode was that "is the suite reproducibly green" had no
artifact behind it: the command existed only as a shell line retyped by the
operator, the repetition count was a judgement call, and the result was read
off the terminal with a different victim every time. These are the acceptance
checks for the tool that closes that, exercised with a fake spawner so the
whole file runs in a second and never touches the real suite:

* a green run exits 0 with an empty table, and still says a short run is
  below the 12-pass bar;
* a coin-flip test is named with a count strictly between 1 and N and its
  traceback is in the kept log, so a red run is a finding and not a rerun;
* a test that fails every pass is labelled as a plain failure, so nobody
  hunts a race that is not there;
* the two roots never share one pytest invocation and two passes never
  overlap in time — proven by recording the invocations, not by running;
* `--timeout` is printed and passed through, and both faces of a
  pytest-timeout kill under xdist read as *killed*, with the stack dump kept;
* a red pytest that names no test is red, never green;
* broken artifacts degrade to a red pass and never raise into a run.
"""

from __future__ import annotations

import os
import random
import sys
import time
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import stress_suite as ss  # noqa: E402


class FakeRun:
    """Everything ``run_pass`` needs from a ``Popen``, and nothing else."""

    def __init__(self, argv, returncode):
        self.argv = argv
        self.returncode = returncode
        self.spawned = 0.0
        self.finished = 0.0
        self.killed = False

    def wait(self):
        self.finished = time.monotonic()
        return self.returncode

    def kill(self):
        self.killed = True


class Bench:
    """A fake spawner: writes the log, records the argv and the wall clock.

    ``make_log(argv, run_index)`` returns ``(returncode, text)``. Runs land in
    ``self.runs`` in spawn order, so a stress pass is a contiguous block of
    them — which is what makes the two shapes checkable without running any
    pytest at all.
    """

    def __init__(self, make_log):
        self.runs = []
        self.make_log = make_log

    def __call__(self, argv, log):
        code, text = self.make_log(list(argv), len(self.runs))
        log.parent.mkdir(parents=True, exist_ok=True)
        raw = text if isinstance(text, bytes) else text.encode("utf-8")
        log.write_bytes(raw)
        run = FakeRun(argv=list(argv), returncode=code)
        run.spawned = time.monotonic()
        self.runs.append(run)
        return run


def run_main(argv, bench, capsys, logs_dir):
    """Run the script end to end with a fake spawner and return its output."""
    code = ss.main(argv + ["--keep-logs", str(logs_dir)], spawner=bench)
    return code, capsys.readouterr().out


GREEN = "123 passed in 42.0s"
FAIL_PLAIN = "FAILED tests/test_x.py::test_plain - AssertionError: boom\n1 failed in 4.0s"
FAIL_TIMEOUT = (
    "FAILED tests/test_x.py::test_slow - Failed: Timeout (>5.0s) from pytest-timeout.\n"
    "1 failed in 5.1s"
)
FAIL_TIMEOUT_PARENS = (
    "FAILED tests/test_x.py::test_slow - Failed: Timeout >180.0 s\n1 failed in 181.0s"
)
FAIL_CRASHED_WORKER = "*** Crashed worker gw3\nreplacing crashed worker gw3\n1 failed in 9.0s"
STACK_DUMP = (
    '~~~~ Stack of <unknown> (129581369521728) ~~~~\n'
    '  File "tests/test_x.py", line 4, in test_slow\n'
)
NO_TEST_NAMED = "ERROR: usage: pytest [-h] [target_or_file] ... \nno tests ran in 0.1s"


def test_a_green_run_exits_zero_with_an_empty_table_and_says_it_is_below_the_bar(capsys, tmp_path):
    """Acceptance 1: the exit code is the claim, and a short run says so."""
    bench = Bench(lambda argv, index: (0, GREEN))
    code, out = run_main(["--passes", "2"], bench, capsys, tmp_path / "logs")

    assert code == 0
    assert "no rows" in out
    assert "RESULT: GREEN" in out
    # A short run is not evidence either way, and the script has to say that.
    assert "below the 12-pass bar" in out
    assert "12 passes of the stress shape = 48 suite runs" in out
    # The timeout is reported, never raised silently.
    assert "timeout=180s" in out
    assert bench.runs[0].argv.count("tests") == 1


def test_a_run_at_the_bar_does_not_claim_to_be_below_it(capsys, tmp_path):
    bench = Bench(lambda argv, index: (0, GREEN))
    code, out = run_main(["--passes", "12"], bench, capsys, tmp_path / "logs")

    assert code == 0
    assert "below the 12-pass bar" not in out
    # 12 passes x 4 concurrent suites = the 48 suite runs the bar means.
    assert len(bench.runs) == 48


def test_a_coin_flip_test_is_a_flake_with_a_count_strictly_between_1_and_n(capsys, tmp_path):
    """Acceptance 2: the table names it, and the kept log keeps its traceback."""
    rng = random.Random(3)
    flips = [rng.random() < 0.5 for _ in range(8)]
    red_passes = sum(flips)
    assert 1 < red_passes < 8, "the seed changed; the coin stopped being a coin"
    bench = Bench(
        lambda argv, index: (1, FAIL_PLAIN + "\n" + STACK_DUMP)
        if flips[index // 2]
        else (0, GREEN)
    )
    code, out = run_main(["--passes", "8", "--shape", "serial"], bench, capsys, tmp_path / "logs")

    assert code == 1
    assert "RESULT: RED, %d of 8 passes had a failure" % red_passes in out
    row = [line for line in out.splitlines() if "test_plain" in line]
    assert len(row) == 1
    assert "FLAKE" in row[0]
    assert "%d/8" % red_passes in row[0]
    assert "FAILING EVERY PASS" not in row[0]
    assert "FLAKE: 1 test(s) failed in fewer than all 8 passes" in out

    # The traceback survives in the kept log, not just the summary line.
    logs = sorted((tmp_path / "logs").glob("*.log"))
    assert len(logs) == 16
    kept = [path for path in logs if "line 4, in test_slow" in path.read_text()]
    assert len(kept) == 2 * red_passes


def test_a_test_that_fails_every_pass_is_labelled_distinctly_from_a_flake(capsys, tmp_path):
    """Acceptance 3: nobody hunts a race that is not there."""
    bench = Bench(lambda argv, index: (1, FAIL_PLAIN))
    code, out = run_main(["--passes", "4", "--shape", "serial"], bench, capsys, tmp_path / "logs")

    assert code == 1
    row = [line for line in out.splitlines() if "test_plain" in line]
    assert len(row) == 1
    assert "FAILING EVERY PASS" in row[0]
    assert "FLAKE" not in row[0]
    assert "4/4" in row[0]
    assert "a plain failure" in out


def test_neither_shape_combines_the_roots_and_no_two_passes_overlap(capsys, tmp_path):
    """Acceptance 4: proven from the invocations the script would make.

    The two roots are the bench's whole premise, so this records what the
    script would actually run rather than running the suite: every invocation
    names exactly one root, and inside a stress pass every suite is alive
    before any of them finishes while the passes themselves stay disjoint.
    """
    bench = Bench(lambda argv, index: (0, GREEN))
    code, out = run_main(["--passes", "3", "--shape", "stress"], bench, capsys, tmp_path / "logs")
    assert code == 0

    roots = {"tests", "tests_bugfix"}
    for run in bench.runs:
        hits = [entry for entry in run.argv if entry in roots]
        assert len(hits) == 1, run.argv
        assert not roots.issubset(set(run.argv)), run.argv
    # Two roots, each twice, four suites a pass.
    assert len(bench.runs) == 12
    assert [run.argv[3] for run in bench.runs] == ["tests", "tests_bugfix"] * 6
    assert out.count("suites/pass=4") == 1

    per_pass = [bench.runs[i : i + 4] for i in range(0, 12, 4)]
    # The stress pass is genuinely concurrent: all four are spawned before any
    # of them has finished, so the load inside the pass is real.
    for group in per_pass:
        assert max(r.spawned for r in group) <= min(r.finished for r in group)
    # ...while passes never overlap, which would invalidate both of them.
    for earlier, later in zip(per_pass, per_pass[1:]):
        assert max(r.finished for r in earlier) <= min(r.spawned for r in later)


def test_the_serial_shape_runs_the_two_roots_one_at_a_time_at_n4(capsys, tmp_path):
    """Acceptance 4: the ``-n 4`` loop, one root per invocation, in order."""
    bench = Bench(lambda argv, index: (0, GREEN))
    code, out = run_main(["--passes", "2", "--shape", "serial"], bench, capsys, tmp_path / "logs")
    assert code == 0

    assert len(bench.runs) == 4
    assert [run.argv[3] for run in bench.runs] == ["tests", "tests_bugfix"] * 2
    for run in bench.runs:
        assert run.argv[4:6] == ["-n", "4"], run.argv
    for earlier, later in zip(bench.runs, bench.runs[1:]):
        assert earlier.finished <= later.spawned


def test_a_timeout_kill_is_reported_killed_in_both_forms_and_the_dump_is_kept(capsys, tmp_path):
    """Acceptance 5: both faces of an xdist kill, and the stack dump survives."""
    bench = Bench(
        lambda argv, index: (
            (1, FAIL_TIMEOUT_PARENS + "\n" + STACK_DUMP)
            if index % 4 == 0
            else (1, FAIL_TIMEOUT + "\n" + STACK_DUMP)
            if index % 4 == 1
            else (1, FAIL_CRASHED_WORKER + "\n" + STACK_DUMP)
        )
    )
    code, out = run_main(["--passes", "1", "--shape", "stress"], bench, capsys, tmp_path / "logs")

    assert code == 1
    assert "killed by pytest-timeout: 1 pass(es)" in out
    # Both faces of the same kill, neither waved through as a plain failure.
    assert "pytest-timeout (signal)" in out
    assert "crashed xdist worker" in out
    assert "RESULT: RED" in out
    # The dump is what identifies a family, so it must still be on disk.
    logs = list((tmp_path / "logs").glob("*.log"))
    assert sum(1 for path in logs if "Stack of <unknown>" in path.read_text()) == 4


def test_a_red_pytest_that_names_no_test_is_red_and_never_green(capsys, tmp_path):
    """Requirement 6: a non-zero exit with no ``FAILED`` line is still a fail."""
    bench = Bench(lambda argv, index: (4, NO_TEST_NAMED))
    code, out = run_main(["--passes", "2", "--shape", "serial"], bench, capsys, tmp_path / "logs")

    assert code == 1
    assert "no test named" in out
    assert "red with no test named: 2 pass(es)" in out
    assert "RESULT: GREEN" not in out
    assert "no rows" in out


def test_a_broken_log_degrades_to_a_red_pass_instead_of_raising(capsys, tmp_path):
    """Fail-open: a broken artifact is reported, never raised into a run."""

    def spawn_broken(argv, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        # A directory where the log file should be: read_text() raises.
        os.mkdir(log)
        run = FakeRun(argv=list(argv), returncode=0)
        run.spawned = time.monotonic()
        return run

    code = ss.main(
        ["--passes", "1", "--shape", "serial", "--keep-logs", str(tmp_path / "logs")],
        spawner=spawn_broken,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "log unreadable" in out
    assert "unreadable logs: 2" in out
    assert "RESULT: RED" in out


def test_parse_failures_never_raises_on_any_input():
    """Fail-open: a log is an artifact, and artifacts break."""
    for text in (None, "", "not a log at all", b"\xff\xfe\x00broken", 42, []):
        parsed = ss.parse_failures(text)
        assert parsed.failures == ()
        assert not parsed.killed


def test_an_unparseable_log_is_still_green_when_pytest_was_green(capsys, tmp_path):
    """Fail-open the other way: garbage does not make a green pass red."""
    bench = Bench(lambda argv, index: (0, b"\xff\xfe\x00not really a log"))
    code, out = run_main(["--passes", "1", "--shape", "serial"], bench, capsys, tmp_path / "logs")

    assert code == 0
    assert "RESULT: GREEN" in out


def test_an_invocation_can_not_name_both_roots(tmp_path):
    """The enforcement behind acceptance 4, where it actually lives."""
    roots = ("tests", "tests_bugfix")
    with pytest.raises(ss.MalformedInvocation, match="names 2 test roots"):
        ss.Invocation(
            index=0,
            root="tests",
            argv=("python", "-m", "pytest", "tests", "tests_bugfix", "-n", "8"),
            log=tmp_path / "pass001_00_tests.log",
            roots=roots,
        )
    # A similar name is not the root: `tests_foo` is not `tests`.
    with pytest.raises(ss.MalformedInvocation, match="names 0 test roots"):
        ss.Invocation(
            index=0,
            root="tests",
            argv=("python", "-m", "pytest", "tests_foo", "-n", "8"),
            log=tmp_path / "pass001_00_tests.log",
            roots=roots,
        )


def test_validate_plan_refuses_a_plan_that_is_not_the_shape_it_claims(tmp_path):
    """A plan that drops a root, or reorders it, is not this bench."""
    shape = ss.Shape(name="stress", roots=("tests", "tests_bugfix"), workers=8, suites_per_root=2)
    invocations = ss.build_invocations(shape, 1, tmp_path, 180)
    assert [(inv.root,) for inv in invocations] == [("tests",), ("tests_bugfix",)] * 2

    with pytest.raises(ss.MalformedInvocation, match="does not match shape"):
        ss.validate_plan(invocations[:-1], shape)
    with pytest.raises(ss.MalformedInvocation, match="does not match shape"):
        ss.validate_plan(invocations[::-1], shape)
    # An argv that sneaks both roots in after the plan is shaped is refused too.
    sneaky = list(invocations)
    sneaky[-1] = types.SimpleNamespace(
        index=3,
        root="tests_bugfix",
        argv=("python", "-m", "pytest", "tests", "tests_bugfix", "-n", "8"),
    )
    with pytest.raises(ss.MalformedInvocation, match="never combined"):
        ss.validate_plan(sneaky, shape)


def test_a_shape_that_cannot_be_built_is_refused():
    """A quietly smaller bench is not a stress run, so it is an error."""
    with pytest.raises(ss.MalformedInvocation, match="suites-per-root"):
        ss.plan_pass(ss.Shape("stress", ("tests",), 8, 0), 180)
    with pytest.raises(ss.MalformedInvocation, match="test root"):
        ss.plan_pass(ss.Shape("stress", (), 8, 2), 180)
    with pytest.raises(ss.MalformedInvocation, match="workers"):
        ss.suite_argv("tests", 0, 180)
    with pytest.raises(ss.MalformedInvocation, match="timeout"):
        ss.suite_argv("tests", 8, 0)


def test_workers_roots_and_timeout_are_flags_not_edits():
    """A different box says so on the command line, not in the file."""
    shape = ss.Shape(name="stress", roots=("tests",), workers=4, suites_per_root=1)
    assert ss.plan_pass(shape, 300) == [(0, "tests")]
    # One root, one -n, the timeout, the traceback and the short summary.
    assert ss.suite_argv("tests", 4, 300) == [
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "-n",
        "4",
        "--timeout=300",
        "--tb=long",
        "-rf",
    ]


def test_bad_flags_are_refused_rather_than_making_a_quietly_smaller_bench(capsys, tmp_path):
    for argv in (["--passes", "0"], ["--workers", "0"], ["--timeout", "0"]):
        code = ss.main(argv + ["--keep-logs", str(tmp_path / "logs")])
        assert code == 2, argv
        assert "refusing to run" in capsys.readouterr().out


def test_the_two_shapes_are_named_and_default_to_the_e500d40_bench():
    """Acceptance 1: stress is four concurrent suites at -n 8, not retyped."""
    args = ss.parse_args(["--passes", "12"])
    shape = ss.make_shape(args)
    assert (shape.name, shape.workers, shape.suites_per_root) == ("stress", 8, 2)
    assert shape.roots == ("tests", "tests_bugfix")
    assert len(ss.plan_pass(shape, 180)) == 4
    assert ss.make_shape(ss.parse_args(["--shape", "serial"])).workers == 4


def test_the_bar_is_twelve_passes():
    assert ss.BAR_PASSES == 12
    assert ss.DEFAULT_TIMEOUT == 180
    assert ss.below_bar_note(12) == ""
    assert "below the 12-pass bar" in ss.below_bar_note(2)
    assert ss.below_bar_note(48) == ""
