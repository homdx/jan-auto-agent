#!/usr/bin/env python3
"""FL-3 — run the stress command N times and turn the logs into the flake table.

The only tool in this repository that answers "is the suite reproducibly
green". ``pytest`` answers "was it green once", and
``scripts/sync_test_tiers.py --check`` answers "is every file tiered"; the gap
between those two is exactly where FL-1 hid for weeks, and until now everything
about closing it was a shell line retyped by the operator and read off the
terminal:

* the command itself existed in two incompatible forms — FL-1's ``-n 4`` loop
  over both roots and ``e500d40``'s four suites at once — and neither was
  checked in, so every round re-invented it and may re-invent it weaker;
* the repetition count was a judgement call made after the fact, so a
  candidate that ran it three times and saw green concluded it was fixed
  (FL-1 needed 19 runs to see 8 reds);
* the result was read by eye, out of a suite that prints thousands of lines,
  with a different victim every time — which is exactly the signal that says
  "independent races" and exactly the signal a human scrolling past loses.

This is the bench as ``contest-bench/fl1/RUNBOOK.md`` §4 and
``POSTMORTEM-FL-1.md`` Appendix B.1 define it. The two rules that make a run of
this bench valid are enforced here rather than documented, because a documented
rule is the kind of thing the next round forgets:

* the two test roots are **never** combined into one pytest invocation. Each
  subprocess gets exactly one root, and ``Invocation`` refuses to be built
  otherwise (``validate_plan`` refuses a plan that disagrees with the shape),
  so a weaker invocation cannot be produced by editing this file;
* two **passes** never overlap. The concurrency inside a pass is the input the
  margins have to survive; a second pass on top of it is noise that invalidates
  both. Passes run in a plain sequential loop and nothing here can overlap them.

The output is the table the operator used to build with
``grep -h '^FAILED' | sort | uniq -c | sort -rn``: how many passes each test
failed in, out of how many. The verdict matters more than the arithmetic — a
test that fails 3/20 is the finding, and a test that fails 20/20 is a plain
failure, not a flake, so nobody goes hunting a race that is not there. Both
forms are labelled differently rather than averaged together.

This deliberately does not classify failures into root-cause families. That is
a reading task, the families are ticket-specific, and a wrong automatic label
is worse than none.

Usage
-----
    python3 scripts/stress_suite.py                        # stress shape, 12 passes
    python3 scripts/stress_suite.py --passes 20 --shape serial
    python3 scripts/stress_suite.py --passes 3 --keep-logs /tmp/fl
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The bar POSTMORTEM-FL-1 and RUNBOOK §3 set: 12 passes of the stress shape is
#: 48 suite runs. ``e500d40`` looked finished below that and was not. Under the
#: bar the script says so on one line, green or not — a 2-pass run is a coin
#: that has not landed yet.
BAR_PASSES = 12

#: pytest-timeout's default here: the value KC-38's acceptance gate uses, since
#: ``pytest.ini`` sets none. Passed through, never raised to make a pass green.
DEFAULT_TIMEOUT = 180

#: Default workers per suite, per shape: e500d40's 32 workers (4 x ``-n 8``)
#: and FL-1's ``-n 4`` loop.
DEFAULT_WORKERS = {"stress": 8, "serial": 4}

#: The two roots. Never one invocation.
DEFAULT_ROOTS = ("tests", "tests_bugfix")

#: The stress shape runs each root twice at once — the ~4x oversubscription on
#: 8 cores that every margin in FL-1 had to survive.
DEFAULT_SUITES_PER_ROOT = 2

#: argv entries that could otherwise be mistaken for a test root. Tier
#: directories count too, since ``.smoke_tests`` and ``.regression_tests`` are
#: just symlink views onto ``tests/``.
TIER_DIRS = frozenset({".smoke_tests", ".regression_tests"})

#: A pytest-timeout kill under the default ``signal`` method: the worker is
#: signalled and the summary line names the test as ``Failed: Timeout >N s``.
#: ``pytest-timeout`` 2.x prints the same line with parentheses,
#: ``Failed: Timeout (>5.0s) from pytest-timeout``; both forms are read.
RE_TIMEOUT_FAILED = re.compile(r"Timeout\s*\(?>\s*\d+(?:\.\d+)?\s*s")

#: The other face of the same kill. With ``timeout_method = thread`` the worker
#: ``os._exit``s, so xdist reports a node that died rather than a test that
#: failed, and often no ``FAILED`` line at all. Both forms count as *killed*,
#: never as a plain failure.
RE_CRASHED_WORKER = re.compile(
    r"node down|replacing crashed worker|crashed worker|worker\s+\w+\s+crashed|"
    r"\*\*\* Killed \d+ workers|INTERNALERROR>\s*\n?Crashed"
)

#: The stack dump a kill leaves behind. It is kept as evidence, not a verdict:
#: reading that dump is what identified FL-1's family A, and pytest-timeout's
#: thread method dumps every live frame under a ``~~~~ Stack of ...`` header.
RE_STACK_DUMP = re.compile(
    r"Timeout\s*\(?>\s*\d+(?:\.\d+)?\s*s|"
    r"Stack of <|Current thread|Thread 0x|~{5,}.*~{5,}"
)

RE_FAILED_LINE = re.compile(r"^FAILED\s+(\S+)")


class MalformedInvocation(ValueError):
    """The invocation plan is not a stress run.

    Raised rather than printed, because a plan that combines the two roots
    invalidates every pass it would have produced and must not be allowed to
    produce them.
    """


@dataclass(frozen=True)
class Shape:
    """Which bench a pass has to look like.

    ``stress`` — each root repeated ``suites_per_root`` times, every suite
    spawned before any of them is waited on. The e500d40 shape, the one the
    margins were set against.
    ``serial`` — each root once, one after another, at ``-n 4``. FL-1's own
    loop: cheaper, and the one that first exposed the flake.
    """

    name: str
    roots: tuple
    workers: int
    suites_per_root: int = 1

    @property
    def concurrent(self) -> bool:
        return self.name == "stress"


@dataclass(frozen=True)
class Invocation:
    """One pytest subprocess: one root, one log, its own return code.

    Never both roots — that is the single most expensive way to misread a
    stress run, so the check lives here and a weaker invocation cannot be
    built without raising.
    """

    index: int
    root: str
    argv: tuple
    log: Path
    roots: tuple = DEFAULT_ROOTS

    def __post_init__(self) -> None:
        if self.index < 0 or not self.root or not self.argv:
            raise MalformedInvocation(
                "invocation %r has an unusable index, root or argv" % (self,)
            )
        hits = root_hits(self.argv, self.roots)
        if len(hits) != 1 or hits[0] != self.root:
            raise MalformedInvocation(
                "invocation %d names %d test roots (%r) for root %r; a suite run "
                "names exactly one" % (self.index, len(hits), hits, self.root)
            )


@dataclass(frozen=True)
class FailureParse:
    """What one pytest log says, read with no assumption about its shape.

    ``failures`` are the node ids the ``-rf`` summary named. ``timeout_lines``
    and ``crashed_lines`` are the two faces of a kill (``RE_TIMEOUT_FAILED`` /
    ``RE_CRASHED_WORKER``); ``stack_dump_lines`` is the kept evidence a timeout
    left, which is why the logs are kept at all.
    """

    failures: tuple
    timeout_lines: tuple
    crashed_lines: tuple
    stack_dump_lines: tuple

    @property
    def killed(self) -> bool:
        return bool(self.timeout_lines or self.crashed_lines)

    @property
    def killed_by(self) -> str:
        """Which face of the kill this was. Empty for a plain failure."""
        if self.crashed_lines:
            return "crashed xdist worker"
        if self.timeout_lines:
            return "pytest-timeout (%s)" % ("signal" if self.failures else "no test named")
        return ""


@dataclass(frozen=True)
class SuiteRun:
    """One root's slice of one pass."""

    index: int
    root: str
    returncode: int
    failures: tuple
    killed: bool
    killed_by: str
    stack_dump_lines: tuple
    log: Path
    log_error: str = ""

    @property
    def red(self) -> bool:
        """Non-zero exit, or a named failure, or a kill, or an unreadable log.

        A pytest exit of 0 cannot be red: with ``-rf`` a failure always leaves a
        ``FAILED`` line, so the exit code is only ever the second opinion.
        """
        return bool(self.returncode != 0 or self.failures or self.killed or self.log_error)

    @property
    def unnamed(self) -> bool:
        """Red, and pytest named no test in its summary at all.

        That is a pass that has to be read from the log, never a green one.
        """
        return bool(self.red and not self.failures)

    @property
    def note(self) -> str:
        if self.log_error:
            return "log unreadable (%s)" % self.log_error
        if self.failures:
            return "%d failed" % len(self.failures)
        if self.killed:
            return "killed"
        if self.red:
            return "no test named"
        return "green"


@dataclass(frozen=True)
class PassOutcome:
    """Everything one pass produced: its runs and what they say together."""

    index: int
    runs: tuple

    @property
    def red(self) -> bool:
        return any(run.red for run in self.runs)

    @property
    def killed(self) -> bool:
        return any(run.killed for run in self.runs)

    @property
    def unnamed(self) -> bool:
        return any(run.unnamed for run in self.runs)

    @property
    def failed_nodes(self) -> frozenset:
        nodes = set()
        for run in self.runs:
            nodes.update(run.failures)
        return frozenset(nodes)

    def summary(self) -> str:
        """One line per pass: how many suites were green, and why not.

        The reason comes from each suite's own note, so a pass killed by
        pytest-timeout, a run that errored without naming a test, and a log
        that could not be read are all visible on the line rather than only in
        the table at the end.
        """
        green = sum(1 for run in self.runs if not run.red)
        text = "%d/%d green" % (green, len(self.runs))
        notes = []
        for run in self.runs:
            if run.red:
                note = "%s: %s" % (run.root, run.note)
                if note not in notes:
                    notes.append(note)
        if notes:
            text += "  (red: %s)" % "; ".join(notes)
        return text


@dataclass(frozen=True)
class TableRow:
    """One test's tally across every pass, with the verdict attached."""

    node: str
    fails: int
    passes: int

    @property
    def always(self) -> bool:
        """Fails in every pass it was run in: a plain failure, not a race."""
        return bool(self.passes and self.fails == self.passes)

    @property
    def verdict(self) -> str:
        return "FAILING EVERY PASS" if self.always else "FLAKE"


def known_roots(roots) -> frozenset:
    """The argv strings that could be a test root on this run."""
    return frozenset(roots) | TIER_DIRS


def root_hits(argv, roots) -> list:
    """The argv entries that are test roots, and only exactly-named ones.

    Exact match on purpose: with ``tests`` configured, an argv entry of
    ``tests_foo`` is not a hit for ``tests``.
    """
    return [entry for entry in argv if entry.strip().rstrip("/") in known_roots(roots)]


def suite_argv(root: str, workers: int, timeout: int) -> list:
    """One root's pytest command line: exactly one root, ``-n``, ``--timeout``.

    ``--tb=long -rf`` per pass, so the traceback in each log is what tells the
    reader which family a failure belongs to — FL-1's own Diagnosis section is
    entirely about reading those tracebacks. ``-q`` stays from ``pytest.ini``'s
    addopts, and ``-n`` here overrides its ``-n auto``.
    """
    if workers < 1:
        raise MalformedInvocation("--workers must be a positive integer, got %r" % workers)
    if timeout <= 0:
        raise MalformedInvocation("--timeout must be positive, got %r" % timeout)
    return [
        sys.executable,
        "-m",
        "pytest",
        root,
        "-n",
        str(workers),
        "--timeout=%d" % timeout,
        "--tb=long",
        "-rf",
    ]


def plan_pass(shape: Shape, timeout: int) -> list:
    """The ``(index, root)`` pairs one pass must run, in order, one root each.

    ``stress`` interleaves the repeats rather than running them in blocks, so
    the four suites of the e500d40 shape start together — which is what makes
    the two ``tests`` runs collide the way the bench intends. ``serial`` runs
    each root once, in the order given, one at a time.
    """
    if shape.suites_per_root < 1:
        raise MalformedInvocation(
            "--suites-per-root must be a positive integer, got %r" % shape.suites_per_root
        )
    if not shape.roots:
        raise MalformedInvocation("--roots must name at least one test root")

    repeats = shape.suites_per_root if shape.concurrent else 1
    pairs = []
    for _repeat in range(repeats):
        for root in shape.roots:
            pairs.append((len(pairs), root))
    for _index, root in pairs:
        argv = suite_argv(root, shape.workers, timeout)
        hits = root_hits(argv, shape.roots)
        if len(hits) != 1 or hits[0] != root:
            raise MalformedInvocation(
                "a suite run must name exactly one root, got %r in %r" % (hits, argv)
            )
    return pairs


def validate_plan(invocations: list, shape: Shape) -> None:
    """Refuse a plan that is not the shape it claims to be.

    ``plan_pass`` already guarantees one root per run, and this is what makes
    the guarantee enforceable on an invocation list built anywhere else: the
    roots must be exactly the shape's roots in the shape's order, and every
    argv must name exactly one of them.
    """
    expected = []
    repeats = shape.suites_per_root if shape.concurrent else 1
    for _repeat in range(repeats):
        expected.extend(shape.roots)
    got = [invocation.root for invocation in invocations]
    if got != expected:
        raise MalformedInvocation(
            "plan does not match shape %r: got %r, expected %r" % (shape.name, got, expected)
        )
    for invocation in invocations:
        hits = root_hits(invocation.argv, shape.roots)
        if len(hits) != 1 or hits[0] != invocation.root:
            raise MalformedInvocation(
                "invocation %d names %d test roots (%r) for root %r; the roots are "
                "never combined into one pytest invocation"
                % (invocation.index, len(hits), hits, invocation.root)
            )


def parse_failures(log_text) -> FailureParse:
    """Read one pytest log. Never raises, on any input whatsoever.

    A log is an artifact and artifacts break: truncated mid-write by a killed
    worker, written in an encoding this box cannot read, or never written at
    all. Each of those is a pass that still has to be reported, never an
    exception into a run.
    """
    if log_text is None:
        text = ""
    elif isinstance(log_text, bytes):
        text = log_text.decode("utf-8", errors="replace")
    elif not isinstance(log_text, str):
        text = str(log_text)
    else:
        text = log_text

    failures = []
    seen = set()
    timeout_lines = []
    crashed_lines = []
    dump_lines = []

    for line in text.splitlines():
        stripped = line.strip()
        match = RE_FAILED_LINE.match(line)
        if match:
            node = match.group(1)
            if node not in seen:
                seen.add(node)
                failures.append(node)
        if RE_TIMEOUT_FAILED.search(stripped):
            timeout_lines.append(stripped)
        if RE_CRASHED_WORKER.search(stripped):
            crashed_lines.append(stripped)
        if RE_STACK_DUMP.search(stripped):
            dump_lines.append(stripped)
    return FailureParse(tuple(failures), tuple(timeout_lines), tuple(crashed_lines), tuple(dump_lines))


def read_log(path: Path) -> tuple:
    """Read a log, degrading to an empty log plus a reason instead of raising."""
    try:
        return path.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as exc:
        return "", "%s: %s" % (type(exc).__name__, exc)


def spawn(argv: list, log: Path, *, cwd: Path) -> subprocess.Popen:
    """Start one pytest with its own log, line-buffered.

    Line-buffered so a pass that is killed leaves everything it got that far;
    that is the difference between a readable ``--tb=long`` traceback and a
    dead pipe.
    """
    handle = open(log, "w", encoding="utf-8", buffering=1)
    return subprocess.Popen(argv, cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT)


def _wait(process) -> None:
    """Reap one subprocess. Never raises: a broken process is a red pass."""
    try:
        process.wait()
    except Exception:
        pass


def _stop(process) -> None:
    try:
        process.kill()
    except Exception:
        pass


def run_pass(
    pass_index: int,
    total: int,
    invocations: list,
    concurrent: bool,
    spawner: Optional[Callable] = None,
) -> PassOutcome:
    """Run one pass: spawn its suites, wait for all of them, read the logs.

    A concurrent shape spawns every suite before waiting on any, so the load
    inside the pass is real; a serial shape runs them one at a time. This
    function cannot overlap with another pass — the loop in ``main`` is what
    keeps that true, and it is a loop.

    ``spawner`` is the only process boundary in this file; the tests replace
    it so the bench can be checked in seconds without running the suite.
    """
    if pass_index < 1 or total < pass_index:
        raise MalformedInvocation("pass_index must be between 1 and %d" % total)
    if not invocations:
        raise MalformedInvocation("a pass needs at least one suite run")

    spawn_one = spawner if spawner is not None else _default_spawner(REPO_ROOT)
    live = []
    reaped = set()

    def reap(process):
        _wait(process)
        reaped.add(id(process))

    try:
        for invocation in invocations:
            process = spawn_one(list(invocation.argv), invocation.log)
            live.append((invocation, process))
            if not concurrent:
                reap(process)
        for _invocation, process in live:
            # A serial pass already reaped every suite in the loop above; only
            # reap once, so a suite's finish stays before the next one spawns.
            if id(process) not in reaped:
                reap(process)
    finally:
        # A pass is never abandoned half-spawned: on a box this script
        # saturates, three orphaned pytest workers left behind are the whole
        # bench re-run twice.
        for _invocation, process in live:
            if id(process) not in reaped:
                _stop(process)

    runs = []
    for invocation, process in live:
        text, error = read_log(invocation.log)
        parsed = parse_failures(text)
        runs.append(
            SuiteRun(
                index=invocation.index,
                root=invocation.root,
                returncode=int(getattr(process, "returncode", 0) or 0),
                failures=parsed.failures,
                killed=parsed.killed,
                killed_by=parsed.killed_by,
                stack_dump_lines=parsed.stack_dump_lines,
                log=invocation.log,
                log_error=error,
            )
        )

    outcome = PassOutcome(index=pass_index, runs=tuple(runs))
    label = outcome.summary()
    if outcome.killed:
        causes = sorted({run.killed_by for run in outcome.runs if run.killed})
        label += "  (killed: %s)" % "; ".join(causes)
    print("[pass %d/%d] %s  ->  %s" % (pass_index, total, time.strftime("%H:%M:%S"), label))
    return outcome


def _default_spawner(cwd: Path):
    """The real process boundary: pytest in the repo root, one log each."""

    def spawn_one(argv: list, log: Path) -> subprocess.Popen:
        return spawn(argv, log, cwd=cwd)

    return spawn_one


def flake_table(outcomes: list) -> str:
    """The output: per test, how many passes it failed in, out of how many.

    Fails everywhere is labelled as a plain failure so nobody hunts a race
    that is not there, and a flake stands alone as the finding. Nothing here
    guesses whether a failure is a race — that is the reading task.
    """
    passes = len(outcomes)
    counts = {}
    for outcome in outcomes:
        for node in outcome.failed_nodes:
            counts[node] = counts.get(node, 0) + 1

    rows = [
        TableRow(node, count, passes)
        for node, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    width = max([len("test")] + [len(row.node) for row in rows])
    count_width = max([len("fail/pass")] + [len("%d/%d" % (row.fails, row.passes)) for row in rows])

    lines = ["%-*s  %*s  %s" % (width, "test", count_width, "fail/pass", "verdict")]
    if rows:
        for row in rows:
            lines.append(
                "%-*s  %*s  %s"
                % (width, row.node, count_width, "%d/%d" % (row.fails, row.passes), row.verdict)
            )
    else:
        lines.append("%-*s" % (width, "no rows"))
    lines.append("")

    always = [row for row in rows if row.always]
    flakes = [row for row in rows if not row.always]
    killed = sum(1 for outcome in outcomes if outcome.killed)
    unnamed = sum(1 for outcome in outcomes if outcome.unnamed)
    broken = sum(1 for outcome in outcomes for run in outcome.runs if run.log_error)

    lines.append(
        "tests named in a FAILED line: %d across %d pass(es)" % (len(rows), passes)
    )
    if always:
        lines.append(
            "  FAILING EVERY PASS: %d test(s) failed in all %d passes, a plain failure"
            % (len(always), passes)
        )
    if flakes:
        lines.append(
            "  FLAKE: %d test(s) failed in fewer than all %d passes" % (len(flakes), passes)
        )
    lines.append(
        "  killed by pytest-timeout: %d pass(es); the stack dump stays in the log" % killed
    )
    lines.append(
        "  red with no test named: %d pass(es); pytest errored without naming a test"
        % unnamed
    )
    lines.append("  unreadable logs: %d" % broken)
    return "\n".join(lines)


def below_bar_note(passes: int) -> str:
    """One line whenever the run did not reach the bar, green or not."""
    if passes <= 0:
        raise MalformedInvocation("--passes must be a positive integer, got %r" % passes)
    if passes >= BAR_PASSES:
        return ""
    return (
        "note: %d pass(es) is below the %d-pass bar (%d passes of the stress shape = "
        "%d suite runs); green here is not evidence of a finished fix"
        % (passes, BAR_PASSES, BAR_PASSES, BAR_PASSES * 4)
    )


def resolve_log_dir(keep_logs: Optional[str]) -> Path:
    """Where the per-pass logs go: a temp dir by default, printed on stdout."""
    if keep_logs:
        directory = Path(keep_logs).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
    else:
        directory = Path(tempfile.mkdtemp(prefix="stress-suite-"))
    if not directory.is_dir():
        raise MalformedInvocation("--keep-logs is not a directory: %r" % keep_logs)
    return directory


def build_invocations(shape: Shape, pass_index: int, log_dir: Path, timeout: int) -> list:
    """Materialise one pass as invocations, one log per suite run."""
    if pass_index < 1:
        raise MalformedInvocation("pass numbers are 1-based, got %r" % pass_index)
    invocations = []
    for index, root in plan_pass(shape, timeout):
        invocations.append(
            Invocation(
                index=pass_index * 1000 + index,
                root=root,
                argv=tuple(suite_argv(root, shape.workers, timeout)),
                log=log_dir / ("pass%03d_%02d_%s.log" % (pass_index, index, root)),
                roots=shape.roots,
            )
        )
    validate_plan(invocations, shape)
    return invocations


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Run the stress command N times and print the flake table.",
    )
    ap.add_argument(
        "--shape",
        choices=tuple(DEFAULT_WORKERS),
        default="stress",
        help="stress: %d concurrent suites at -n %d each (default); "
        "serial: -n %d over each root, one at a time"
        % (
            DEFAULT_SUITES_PER_ROOT * len(DEFAULT_ROOTS),
            DEFAULT_WORKERS["stress"],
            DEFAULT_WORKERS["serial"],
        ),
    )
    ap.add_argument(
        "--passes",
        type=int,
        default=BAR_PASSES,
        help="how many passes to run; the bar is %d (default)" % BAR_PASSES,
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="pytest-timeout seconds, printed and passed through, never raised "
        "(default %d)" % DEFAULT_TIMEOUT,
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=None,
        help="workers per suite; defaults to %d for stress, %d for serial"
        % (DEFAULT_WORKERS["stress"], DEFAULT_WORKERS["serial"]),
    )
    ap.add_argument(
        "--roots",
        nargs="+",
        default=list(DEFAULT_ROOTS),
        help="test roots, never combined into one pytest invocation (default: %s)"
        % " ".join(DEFAULT_ROOTS),
    )
    ap.add_argument(
        "--suites-per-root",
        type=int,
        default=DEFAULT_SUITES_PER_ROOT,
        help="stress shape only: how many times each root runs at once (default %d, "
        "giving 4 concurrent suites)" % DEFAULT_SUITES_PER_ROOT,
    )
    ap.add_argument(
        "--keep-logs",
        default="",
        help="directory for the per-pass logs; a temp dir by default, whose path is printed",
    )
    ap.add_argument("--cwd", default="", help="run pytest from here (default: the repo root)")
    return ap.parse_args(argv)


def make_shape(args: argparse.Namespace) -> Shape:
    """Resolve the flags into a shape, refusing anything that would silently be
    a different bench.

    A wrong ``--passes`` or ``--suites-per-root`` is not a smaller run: it is a
    different claim reported as this one, so those are errors.
    """
    return Shape(
        name=args.shape,
        roots=tuple(args.roots),
        workers=args.workers if args.workers is not None else DEFAULT_WORKERS[args.shape],
        suites_per_root=args.suites_per_root,
    )


def main(argv=None, spawner: Optional[Callable] = None) -> int:
    """One run of the bench. Exit 0 only when every pass was green."""
    args = parse_args(argv)
    try:
        shape = make_shape(args)
        below = below_bar_note(args.passes)
        timeout = int(args.timeout)
        pairs = plan_pass(shape, timeout)
        cwd = Path(args.cwd).expanduser().resolve() if args.cwd else REPO_ROOT
        log_dir = resolve_log_dir(args.keep_logs or None)
    except (MalformedInvocation, ValueError, OSError) as exc:
        print("stress_suite: refusing to run: %s" % exc)
        return 2

    suites_per_pass = len(pairs)
    print(
        "stress_suite: shape=%s  suites/pass=%d  workers=%d  timeout=%ds  passes=%d"
        % (shape.name, suites_per_pass, shape.workers, timeout, args.passes)
    )
    print("roots (never combined into one invocation): %s" % " + ".join(shape.roots))
    print(
        "inside a pass, %s: %s"
        % (
            "concurrently" if shape.concurrent else "one at a time",
            " + ".join("pytest %s -n %d" % (root, shape.workers) for root in shape.roots),
        )
    )
    print("logs: %s" % log_dir)
    if below:
        print(below)
    if shape.name == "serial" and args.suites_per_root != 1:
        print("note: --suites-per-root is ignored by the serial shape; each root runs once")

    run_one = spawner if spawner is not None else _default_spawner(cwd)

    # Passes run in this loop and nowhere else. There is no concurrency here by
    # construction, so two passes cannot overlap whatever happens inside one.
    outcomes = []
    for pass_index in range(1, args.passes + 1):
        invocations = build_invocations(shape, pass_index, log_dir, timeout)
        outcomes.append(run_pass(pass_index, args.passes, invocations, shape.concurrent, run_one))

    red = sum(1 for outcome in outcomes if outcome.red)
    print("")
    print("=== flake table (%d passes, %d suite runs) ===" % (args.passes, len(outcomes) * suites_per_pass))
    print(flake_table(outcomes))
    print("logs: %s" % log_dir)
    if red:
        print("RESULT: RED, %d of %d passes had a failure" % (red, args.passes))
        return 1
    print("RESULT: GREEN, every pass was green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
