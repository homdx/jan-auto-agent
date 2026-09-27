#!/usr/bin/env python3
"""FL-4 — the three wall-clock shapes FL-1 removed by hand, recorded as a check.

Round 84's FL-1 fixed thirty-odd wall-clock constants across the contest tests
under one assumption that a loaded box does not honour: that a test's own
timing is what the box will give it. The fix was a set of rules applied by hand
at every call site, and nothing in the repository recorded the rules or noticed
when a new test broke them. This is that record.

Advisory, fast, reads source, changes nothing. No subprocess, no server, no
provider: the whole tree is parsed as AST and a run costs about two seconds.

The three rules
---------------
C1 — widen the slow path, do not tighten the bound. A test proving "the 0.5 s
    window fired, not the 60 s pause" proves strictly more and has 19.5 s of
    slack, bounded at 20 s under a 120 s deadline — and a green run returns in
    the same half second either way. So a bound that sits just above the window
    it is meant to outlast has no margin worth having and belongs wider, not
    narrower. The deliberately short ones (0.1 / 0.3 / 0.5 / 1.0 s) are
    load-bearing — firing *is* the claim there — and the check leaves them
    alone.
C5 — for a test that must *survive* a window rather than trip it, tolerance for
    a starved box == window == how long the test runs, and that tolerance is
    bought with wall time or not at all. There is no margin to widen and no beat
    interval that substitutes. So its span has to come from a beat *count* — the
    pulse emits the idle itself, so a starved box can only make the turn longer,
    never end it before the beats do — never from a ``time.sleep`` interval.
    The two tests of this shape are among the slowest in the suite and their
    docstrings say why; the check reports them, it does not shorten them.
HANG — a bound that is not the claim is scaffolding, and scaffolding belongs at
    or above the guard floor (60 s). Only the shapes that can turn a slow box
    into a dead run are reported: a ``subprocess`` ``timeout`` raises
    ``TimeoutExpired`` (a far worse diagnostic than the ``--timeout`` stack dump
    that would have followed), and a ``barrier.wait`` raises
    ``BrokenBarrierError``. A ``Thread.join`` or ``Event.wait`` returns a
    boolean and cannot kill the run, so the test's own assertions — and pytest's
    ``--timeout`` — own those.

How an author answers a finding
-------------------------------
A short marker comment on the finding's line (or the line directly above it)
says which category the number is in::

    assert elapsed < 20.0                # clock: claim
    subprocess.run(..., timeout=120)     # clock: guard

``claim`` says "this wall time is the assertion" — including C5's, where the
sleep is the only proof there is. ``guard`` additionally requires the value to
be at or above the guard floor, so a ``guard`` under 60 s is still a finding:
either widen it or stop marking it. The point is not to be clever about intent
— the check cannot know it — but to make the choice explicit at the moment it is
made, and to make a reviewer see it in the diff.

Usage
-----
    python3 scripts/check_test_clocks.py            # print the table, exit 0
    python3 scripts/check_test_clocks.py --check    # exit 1 on an unexplained finding
    python3 scripts/check_test_clocks.py DIR [...]  # scan DIR(s) instead of tests/ + tests_bugfix/

Fail-open throughout: a missing root, an unreadable file or a file that will not
parse is reported and skipped, never raised into a run.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = (REPO_ROOT / "tests", REPO_ROOT / "tests_bugfix")

# ── the three families, as named constants ───────────────────────────────────

C1 = "C1"
C5 = "C5"
HANG = "HANG"

MARKER_CLAIM = "claim"
MARKER_GUARD = "guard"

#: Bounds at or under this are the claim itself: 0.1 / 0.3 / 0.5 / 1.0 s, where
#: firing *is* the assertion and weakening it destroys the test.
CLAIM_SHORT_S = 1.0

#: Scaffolding lives at or above here. ``barrier.wait(60)`` clears it.
GUARD_FLOOR_S = 60.0

#: A bound within this multiple of the window it must outlast has no margin:
#: the 0.5 s window bounded at 20 s has forty times, at 5 s it has five.
C1_NEAR_S = 8.0

#: A zero window is not a starved-box tolerance, so C5 needs a real one.
C5_WINDOW_MIN_S = 1.0
C5_WINDOW_MAX_S = 120.0

MARK_RE = re.compile(r"clock:\s*(claim|guard)\b")
_WINDOW_KEY_RE = re.compile(r"idle|window|silence", re.IGNORECASE)
_BOUND_KEY_RE = re.compile(r"(timeout|deadline)\s*$")
_WAIT_SHAPE_RE = re.compile(r"(wait_idle|wait|sleep)$")
_BEAT_NAME_RE = re.compile(r"beat", re.IGNORECASE)
_CLOCK_FUNCS = ("monotonic", "time")
_SLEEP_SUFFIX = ".sleep"
_HANG_FUNCS = frozenset({
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
})
_BARRIER_RE = re.compile(r"barrier$", re.IGNORECASE)

REASONS = {
    C1: "no margin above the window it must outlast — widen the slow path",
    C5: "the span comes from a sleep interval, not a beat count",
    HANG: "under the guard floor and nothing asserts the expiry",
}


@dataclass(frozen=True)
class Finding:
    """One wall-clock site the check cannot classify.

    ``window`` is the silence window the site is measured against, or ``None``
    when the rule does not need one.
    """

    kind: str
    rel: str
    line: int
    value: float
    window: float | None
    text: str
    marker: str | None = None
    note: str | None = None

    @property
    def unexplained(self) -> bool:
        """True when the site still needs an answer in the source.

        ``claim`` always answers. ``guard`` answers only at or above the floor:
        a ``guard`` under 60 s is a hang guard dressed up as a marker, so it
        stays a finding until the bound is widened or the marker is removed.
        """
        if self.marker == MARKER_GUARD:
            return self.value < GUARD_FLOOR_S
        return self.marker is None

    @property
    def problem(self) -> str:
        if self.marker == MARKER_GUARD and self.value < GUARD_FLOOR_S:
            return (
                f"marked {MARKER_GUARD} but {self.value:g} s sits under the "
                f"{GUARD_FLOOR_S:g} s floor — widen it or stop marking it"
            )
        return "unmarked"

    @property
    def status(self) -> str:
        return self.problem if self.unexplained else "explained"

    def __str__(self) -> str:
        window = "-" if self.window is None else f"{self.window:g} s"
        marker = self.marker or "-"
        note = f" ({self.note})" if self.note else ""
        return (
            f"{self.kind:5s} {self.rel}:{self.line:<6} {self.value:9g} s {window:>8s}"
            f"{note:34s}  {marker:6s} {self.status:40s} |  {self.text.strip()}"
        )


@dataclass
class _Buckets:
    """Every node of one test function, sorted by the node kinds the rules use."""

    calls: list[ast.Call]
    compares: list[ast.Compare]
    assigns: list[ast.Assign]
    asserts: list[ast.Assert]
    names: list[ast.Name]

    def add(self, node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            self.calls.append(node)
        elif isinstance(node, ast.Compare):
            self.compares.append(node)
        elif isinstance(node, ast.Assign):
            self.assigns.append(node)
        elif isinstance(node, ast.Assert):
            self.asserts.append(node)
        elif isinstance(node, ast.Name):
            self.names.append(node)


# ── small AST helpers ────────────────────────────────────────────────────────


def _dotted(node: ast.AST) -> str | None:
    """``a.b.c`` as a string, ``None`` when *node* is not a dotted name."""
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ".".join(reversed(parts)) if parts else None


def _tail(node: ast.AST) -> str:
    """The last component of a dotted name, for receiver-side matching."""
    dotted = _dotted(node)
    return dotted.rsplit(".", 1)[-1] if dotted else ""


def _num(node: ast.AST) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    return None


def _is_clock_call(node: ast.AST) -> bool:
    """``time.monotonic()`` / ``time.time()`` — a no-arg read of the clock."""
    if not isinstance(node, ast.Call) or node.args or node.keywords:
        return False
    dotted = _dotted(node.func)
    if dotted is None:
        return False
    return dotted in ("time.monotonic", "time.time") or dotted.rsplit(".", 1)[-1] in _CLOCK_FUNCS


def _mentions_clock(node: ast.AST, clock_names: set[str]) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _is_clock_call(sub):
            return True
        if isinstance(sub, ast.Name) and sub.id in clock_names:
            return True
    return False


def _is_raises(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _dotted(node.func) in ("pytest.raises", "raises")


def _collect(tree: ast.AST) -> tuple[dict[int, _Buckets], dict[int, set[int]], dict[int, set[int]]]:
    """One DFS over a file, scoped to its ``def test_*`` functions.

    Returns ``buckets`` keyed by function id, plus the ids of every node that
    sits inside an ``assert``'s test and inside a ``pytest.raises`` block. A
    nested ``def`` under a test keeps the test's scope — a hook the test hands
    to another thread is still the test's code.
    """
    buckets: dict[int, _Buckets] = {}
    asserted: dict[int, set[int]] = {}
    raises: dict[int, set[int]] = {}
    frontier: list[tuple[ast.AST, int | None, bool, bool]] = [(tree, None, False, False)]
    while frontier:
        node, scope, in_test, in_raises = frontier.pop()
        if isinstance(node, ast.FunctionDef):
            scope = node.name if node.name.startswith("test_") else scope
        if scope is not None:
            group = buckets.get(scope)
            if group is None:
                group = buckets[scope] = _Buckets([], [], [], [], [])
                asserted[scope] = set()
                raises[scope] = set()
            group.add(node)
            if in_test:
                asserted[scope].add(id(node))
            if in_raises:
                raises[scope].add(id(node))
        if isinstance(node, ast.Assert):
            for child in ast.iter_child_nodes(node):
                frontier.append((child, scope, in_test or child is node.test, in_raises))
        elif isinstance(node, ast.With) and any(
            _is_raises(item.context_expr) for item in node.items
        ):
            for child in ast.iter_child_nodes(node):
                frontier.append((child, scope, in_test, True))
        else:
            for child in ast.iter_child_nodes(node):
                frontier.append((child, scope, in_test, in_raises))
    return buckets, asserted, raises


def _module_windows(tree: ast.AST) -> dict[str, float]:
    """Module-level ``*window* = <seconds>`` constants, by name."""
    out: dict[str, float] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value, targets = node.value, list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            value, targets = node.value, [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and _WINDOW_KEY_RE.search(target.id):
                number = _num(value)
                if number is not None:
                    out[target.id] = number
    return out


def _windows(buckets: _Buckets, module_windows: dict[str, float]) -> dict[float, str]:
    """Configured silence windows in reach of the function, value → spelling."""
    found: dict[float, str] = {}
    used = {name.id for name in buckets.names}
    for name, value in module_windows.items():
        if name in used:
            found.setdefault(value, f"{name}={value:g} s")
    for assign in buckets.assigns:
        for target in assign.targets:
            if isinstance(target, ast.Name) and _WINDOW_KEY_RE.search(target.id):
                value = _num(assign.value)
                if value is not None:
                    found.setdefault(value, f"{target.id}={value:g} s")
    for call in buckets.calls:
        for kw in call.keywords:
            if kw.arg and _WINDOW_KEY_RE.search(kw.arg):
                value = _num(kw.value)
                if value is not None:
                    found.setdefault(value, f"{kw.arg}={value:g} s")
    return found


def _clock_names(buckets: _Buckets) -> set[str]:
    """Names bound to a wall-clock reading (``elapsed`` and friends)."""
    names: set[str] = set()
    for assign in buckets.assigns:
        for target in assign.targets:
            if isinstance(target, ast.Name) and _mentions_clock(assign.value, names):
                names.add(target.id)
    return names


def _clock_bounds(buckets: _Buckets, clock_names: set[str]) -> list[tuple[ast.Compare, float]]:
    """Every ``< clock-span`` / ``clock-span <`` numeric bound."""
    bounds: list[tuple[ast.Compare, float]] = []
    for compare in buckets.compares:
        operands = [compare.left] + list(compare.comparators)
        for index, op in enumerate(compare.ops):
            if not isinstance(op, (ast.Lt, ast.LtE)):
                continue
            left, right = operands[index], operands[index + 1]
            if _mentions_clock(left, clock_names):
                value = _num(right)
                if value is not None:
                    bounds.append((compare, value))
            elif _mentions_clock(right, clock_names):
                value = _num(left)
                if value is not None:
                    bounds.append((compare, value))
    return bounds


def _hang_calls(buckets: _Buckets) -> list[tuple[ast.Call, float]]:
    """The scaffolding bounds that can kill the run: a subprocess ``timeout``
    and a ``barrier.wait``. ``Thread.join`` and ``Event.wait`` return a boolean
    and are left to the test's own assertions.
    """
    calls: list[tuple[ast.Call, float]] = []
    for call in buckets.calls:
        dotted = _dotted(call.func)
        if dotted is None:
            continue
        if dotted in _HANG_FUNCS:
            for kw in call.keywords:
                if kw.arg == "timeout":
                    value = _num(kw.value)
                    if value is not None:
                        calls.append((call, value))
        elif dotted.endswith(".wait") and len(call.args) == 1 and _BARRIER_RE.search(
            _tail(call.func.value)
        ):
            value = _num(call.args[0])
            if value is not None:
                calls.append((call, value))
    return calls


def _wait_bound_keywords(buckets: _Buckets) -> list[tuple[ast.Call, float, str]]:
    """Numeric ``timeout=`` / ``deadline=`` kwargs on calls that *are* waits.

    ``request_completion(..., timeout=5)`` and ``FaqAgent(timeout=10)`` are not
    the shape: an HTTP client's own timeout, and a config value read once and
    asserted nowhere. Only a call whose name says it waits.
    """
    bounds: list[tuple[ast.Call, float, str]] = []
    for call in buckets.calls:
        dotted = _dotted(call.func)
        if dotted is None or not _WAIT_SHAPE_RE.search(_tail(call.func)):
            continue
        for kw in call.keywords:
            if not kw.arg or not _BOUND_KEY_RE.search(kw.arg):
                continue
            if _WINDOW_KEY_RE.search(kw.arg):
                continue  # the window itself is not a bound on itself
            value = _num(kw.value)
            if value is not None:
                bounds.append((call, value, f"{kw.arg}={value:g} s"))
    return bounds


def _beats_counted(buckets: _Buckets) -> bool:
    """True when the test's span comes from a beat count instead of a sleep."""
    for call in buckets.calls:
        dotted = _dotted(call.func)
        if dotted == "range" or (dotted is not None and dotted.endswith(".pulse")):
            return True
        if any(kw.arg in ("beats", "every") for kw in call.keywords):
            return True
    return any(_BEAT_NAME_RE.search(name.id) for name in buckets.names)


def _asserts_a_state(buckets: _Buckets) -> bool:
    """True when the test asserts on a terminal state string (``"idle"`` and kin)."""
    for node in buckets.asserts:
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.strip():
                return True
    return False


def _sleep_intervals(buckets: _Buckets) -> list[tuple[ast.Call, float]]:
    sleeps: list[tuple[ast.Call, float]] = []
    for call in buckets.calls:
        if len(call.args) != 1:
            continue
        dotted = _dotted(call.func)
        if dotted is None or not dotted.endswith(_SLEEP_SUFFIX):
            continue
        value = _num(call.args[0])
        if value is not None:
            sleeps.append((call, value))
    return sleeps


def _expiry_is_claim(call: ast.Call, scope: int, asserted: set[int], raises: set[int]) -> bool:
    """True when the call's own expiry is what the test asserts on."""
    return id(call) in asserted or id(call) in raises


def _reference_window(value: float, windows: dict[float, str]) -> tuple[float, str] | None:
    """The window a bound sits just above — C1's trigger.

    The window must be *below* the bound: the claim is "this window fired, not
    the slow path", so the bound sits above the window it is outlasting. A bound
    below a configured window is not the shape, and a bound above a window by
    more than ``C1_NEAR_S`` times has the margin the rule asks for.
    """
    candidates = [w for w in windows if w <= value <= C1_NEAR_S * w]
    if not candidates:
        return None
    window = max(candidates)
    return window, windows[window]


def _outlived_window(value: float, windows: list[float]) -> float | None:
    """The largest window a sleep of *value* seconds outlasts — C5's trigger."""
    candidates = [w for w in windows if value > w]
    return max(candidates) if candidates else None


def _source(lines: list[str], node: ast.AST) -> str:
    line = getattr(node, "lineno", 0)
    if not line or not 0 < line <= len(lines):
        return ""
    return lines[line - 1]


def _markers(lines: list[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for index, line in enumerate(lines, start=1):
        match = MARK_RE.search(line)
        if match:
            out[index] = match.group(1)
    return out


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ── the scan ─────────────────────────────────────────────────────────────────


def scan_file(path: Path) -> list[Finding]:
    """Findings in one file. Never raises: unreadable or unparseable is "no data"."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError, RecursionError):
        return []
    lines = text.splitlines()
    markers = _markers(lines)
    module_windows = _module_windows(tree)
    buckets_by_fn, asserted_by_fn, raises_by_fn = _collect(tree)
    rel = _relative(path)

    findings: list[Finding] = []
    seen: set[tuple[str, int, float]] = set()

    def add(kind: str, node: ast.AST, value: float, window: float | None, note: str | None) -> None:
        line = getattr(node, "lineno", 0)
        key = (kind, line, value)
        if line <= 0 or key in seen:
            return
        seen.add(key)
        findings.append(
            Finding(
                kind=kind,
                rel=rel,
                line=line,
                value=value,
                window=window,
                text=_source(lines, node),
                marker=markers.get(line) or markers.get(line - 1),
                note=note,
            )
        )

    for scope, buckets in buckets_by_fn.items():
        windows = _windows(buckets, module_windows)
        clock_names = _clock_names(buckets)

        # C1: a wall-clock bound sitting just above a configured window.
        for node, value in _clock_bounds(buckets, clock_names):
            if value <= CLAIM_SHORT_S:
                continue
            reference = _reference_window(value, windows)
            if reference:
                add(C1, node, value, reference[0], f"vs {reference[1]}")

        # C1 in a keyword: a wait's own timeout bound, close to its window.
        for node, value, _spelled in _wait_bound_keywords(buckets):
            if value <= CLAIM_SHORT_S:
                continue
            reference = _reference_window(value, windows)
            if reference:
                add(C1, node, value, reference[0], f"vs {reference[1]}")

        # HANG: scaffolding that can kill the run, under the floor, unasserted.
        for call, value in _hang_calls(buckets):
            if value <= CLAIM_SHORT_S or value >= GUARD_FLOOR_S:
                continue
            if _expiry_is_claim(call, scope, asserted_by_fn[scope], raises_by_fn[scope]):
                continue
            add(HANG, call, value, None, REASONS[HANG])

        # C5: survives-the-window whose span is a sleep, not a beat count.
        if not windows or _beats_counted(buckets) or not _asserts_a_state(buckets):
            continue
        c5_windows = [w for w in windows if C5_WINDOW_MIN_S <= w <= C5_WINDOW_MAX_S]
        for node, value in _sleep_intervals(buckets):
            window = _outlived_window(value, c5_windows)
            if window is not None:
                add(C5, node, value, window, f"over a {window:g} s window")

    return sorted(findings, key=lambda f: (f.rel, f.line, f.kind))


def scan(paths: Iterable[Path]) -> list[Finding]:
    """Every finding under *paths* (files or directories), fail-open."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.py") if not p.is_symlink()))
        elif path.is_file():
            files.append(path)
        else:
            print(f"  ! {path}: no such file or directory, skipped", file=sys.stderr)
    findings: list[Finding] = []
    for path in files:
        try:
            findings.extend(scan_file(path))
        except Exception as exc:  # a report must never take a run down with it
            print(f"  ! {path}: {type(exc).__name__}: {exc}, skipped", file=sys.stderr)
    return findings


# ── reporting ────────────────────────────────────────────────────────────────


def _report(findings: list[Finding]) -> None:
    if not findings:
        print("no wall-clock findings")
        return
    print(
        f"{'KIND':5s} {'WHERE':46s} {'BOUND':>9s} {'WINDOW':>7s}  "
        f"{'MARKER':7s} STATUS"
    )
    for finding in findings:
        window = "-" if finding.window is None else f"{finding.window:g} s"
        print(
            f"{finding.kind:5s} {finding.rel}:{finding.line:<39d} {finding.value:9g} "
            f"{window:>7s}  {finding.marker or '-':7s} {finding.status}"
        )
    print()
    unexplained = [finding for finding in findings if finding.unexplained]
    if unexplained:
        print(f"{len(unexplained)} unexplained:")
        for finding in unexplained:
            print(f"  {finding}")
        print()
    print(f"C1    {REASONS[C1]} (a bound within {C1_NEAR_S:g}x of the window it must outlast)")
    print(f"C5    {REASONS[C5]} — wall time or not at all")
    print(f"HANG  {REASONS[HANG]} (scaffolding belongs at or above {GUARD_FLOOR_S:g} s)")
    print(
        "answer: mark the line `# clock: claim`, or `# clock: guard` with a value "
        f">= {GUARD_FLOOR_S:g} s"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FL-4 — report the wall-clock shapes FL-1 removed by hand.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when any finding is unexplained; change nothing either way",
    )
    parser.add_argument(
        "roots",
        nargs="*",
        type=Path,
        help="files or directories to scan (default: tests/ and tests_bugfix/)",
    )
    args = parser.parse_args(argv)

    roots = args.roots or [root for root in DEFAULT_ROOTS if root.is_dir()]
    findings = scan(roots)
    _report(findings)

    unexplained = [finding for finding in findings if finding.unexplained]
    if args.check:
        if unexplained:
            print(
                f"{len(unexplained)} unexplained finding(s) — mark each "
                "`# clock: claim` or `# clock: guard` (guard needs >= "
                f"{GUARD_FLOOR_S:g} s)"
            )
            return 1
        print("all findings explained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
