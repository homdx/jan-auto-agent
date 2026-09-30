"""FL-4 — `scripts/check_test_clocks.py`, the three wall-clock shapes FL-1 removed by hand.

Round 84's FL-1 fixed thirty-odd wall-clock constants across the contest tests
under one assumption a loaded box does not honour: that a test's own timing is
what the box will give it. The rules it applied were recorded nowhere, so the
next `assert elapsed < 5` next to a one-second window brings family C1 straight
back — and the author is right that it passes on their machine. That is the
whole character of the bug.

The fixtures are written into `tmp_path` and fed to `scan()`, so the shapes are
proven against files the live suite never sees. The last test in here keeps the
promise on the real tree: the check reports zero unexplained findings over
`tests/` and `tests_bugfix/` as they stand.
"""

from __future__ import annotations

import ast
import inspect
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_test_clocks.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_test_clocks as ctc  # noqa: E402

# ── fixtures: the three shapes, and what they must not fire on ────────────────

C1_BOUND = """
import time


def test_the_probe_beats_the_pause(monkeypatch):
    '''The 1 s window fired, not the 60 s pause.'''
    cfg = make_config(idle_event_timeout_sec=1)
    started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
"""

C5_SLEEP = """
import time


def test_the_turn_survives_the_silence_window(monkeypatch):
    '''Twice the silence window, queued.'''
    cfg = make_config(idle_event_timeout_sec=1)
    time.sleep(2.2)
    run = harness.go()
    assert run.idle_status == "idle"
"""

C5_PULSED = """
import time


def test_the_turn_is_pulsed_through_the_window(monkeypatch):
    '''The pulse emits the idle itself, so the box can only make it longer.'''
    cfg = make_config(idle_event_timeout_sec=1)
    for _ in range(12):
        harness.pulse(0.5)
    assert harness.state == "idle"
"""

HANG_TIMEOUT = """
import subprocess


def test_the_probe_reports_its_config(tmp_path):
    out = subprocess.run(["probe", "--once"], timeout=15, capture_output=True)
    assert out.returncode == 0
"""

HANG_EXPIRY_ASSERTED = """
import subprocess


def test_the_probe_does_time_out(tmp_path):
    '''The expiry is the assertion, so the bound is not scaffolding.'''
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(["probe", "--hang"], timeout=15)
"""

HANG_ABOVE_FLOOR = """
import subprocess
import threading


def test_the_run_is_left_to_run(monkeypatch):
    barrier = threading.Barrier(2)
    barrier.wait(60)
    subprocess.run(["probe", "--slow"], timeout=120)
    assert harness.finished is True
"""

MARKED_CLAIM = """
import time


def test_the_bound_is_marked_claim(monkeypatch):
    '''The wall time is the assertion.'''
    cfg = make_config(idle_event_timeout_sec=1)
    started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < 5.0  # clock: claim
"""

MARKED_CLAIM_ABOVE = """
import time


def test_the_marker_sits_on_the_line_above(monkeypatch):
    cfg = make_config(idle_event_timeout_sec=1)
    elapsed = time.monotonic() - started
    # clock: claim
    assert elapsed < 5.0
"""

MARKED_GUARD_NARROW = """
import subprocess


def test_the_guard_sits_under_the_floor(tmp_path):
    out = subprocess.run(["probe", "--once"], timeout=15)  # clock: guard
    assert out.returncode == 0
"""

SHORT_BOUNDS = """
import time


def test_the_deliberately_short_bounds_are_the_claim(monkeypatch):
    '''Firing is the assertion at these scales; widening them destroys the test.'''
    cfg = make_config(idle_event_timeout_sec=1)
    started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < 0.1
    assert elapsed < 0.3
    assert elapsed < 0.5
    assert elapsed < 1.0
"""

WIDE_MARGIN = """
import time


def test_the_slow_path_gets_a_real_margin(monkeypatch):
    cfg = make_config(idle_event_timeout_sec=1)
    started = time.monotonic()
    elapsed = time.monotonic() - started
    assert elapsed < 100.0
"""

UNPARSEABLE = """
def test_this_file_will_not_parse(
    assert elapsed < 5.0
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """One fixture on disk; the check reads files, never literals."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _kinds(findings: list[ctc.Finding]) -> list[str]:
    return [finding.kind for finding in findings]


def _single(findings: list[ctc.Finding], kind: str) -> ctc.Finding:
    assert _kinds(findings) == [kind], findings
    return findings[0]


# ── C1: no margin above the window ───────────────────────────────────────────


def test_c1_fires_on_a_bound_just_above_its_window(tmp_path):
    """`< 5` against a 1 s window has five times of slack and no margin to speak of."""
    findings = ctc.scan([_write(tmp_path, "test_c1.py", C1_BOUND)])
    finding = _single(findings, "C1")
    assert finding.value == 5.0
    assert finding.window == 1.0
    assert finding.unexplained
    assert "vs" in (finding.note or "")


def test_c1_is_silent_on_the_deliberately_short_bounds(tmp_path):
    """0.1 / 0.3 / 0.5 / 1.0 s: firing is the claim, so there is nothing to widen."""
    assert ctc.scan([_write(tmp_path, "test_short.py", SHORT_BOUNDS)]) == []


def test_c1_is_silent_on_a_bound_that_has_the_margin(tmp_path):
    """A hundred times the window is the C1 answer, not a finding."""
    assert ctc.scan([_write(tmp_path, "test_margin.py", WIDE_MARGIN)]) == []


# ── C5: the span is a sleep, not a beat count ─────────────────────────────────


def test_c5_fires_when_the_span_comes_from_a_sleep(tmp_path):
    """Surviving the window is bought with wall time, so the sleep is the proof."""
    findings = ctc.scan([_write(tmp_path, "test_c5.py", C5_SLEEP)])
    finding = _single(findings, "C5")
    assert finding.value == 2.2
    assert finding.window == 1.0
    assert "wall time" in ctc.REASONS["C5"] or "sleep interval" in ctc.REASONS["C5"]


def test_c5_is_silent_when_the_beats_are_counted(tmp_path):
    """A pulse loop drives the turn, so a starved box can only make it longer."""
    assert ctc.scan([_write(tmp_path, "test_c5_pulsed.py", C5_PULSED)]) == []


# ── HANG: scaffolding under the floor ────────────────────────────────────────


def test_hang_fires_on_a_subprocess_timeout_nothing_asserts_on(tmp_path):
    """A 15 s bound on a one-second run is scaffolding dressed as an assertion."""
    findings = ctc.scan([_write(tmp_path, "test_hang.py", HANG_TIMEOUT)])
    finding = _single(findings, "HANG")
    assert finding.value == 15.0
    assert finding.window is None
    assert finding.unexplained
    assert "expiry" in ctc.REASONS["HANG"]


def test_hang_is_silent_when_the_expiry_is_the_claim(tmp_path):
    """`with pytest.raises(TimeoutExpired)` is the assertion the bound is about."""
    assert ctc.scan([_write(tmp_path, "test_hang_asserted.py", HANG_EXPIRY_ASSERTED)]) == []


def test_hang_is_silent_at_and_above_the_floor(tmp_path):
    """`barrier.wait(60)` clears the floor; 120 s is where scaffolding belongs."""
    assert ctc.scan([_write(tmp_path, "test_hang_wide.py", HANG_ABOVE_FLOOR)]) == []


def test_hang_ignores_waits_that_return_a_boolean(tmp_path):
    """`Thread.join` and `Event.wait` cannot kill the run, so the test owns them."""
    body = """
        import threading


        def test_the_thread_owns_its_own_deadline(monkeypatch):
            thread.join(15)
            event.wait(15)
            assert harness.finished is True
    """
    assert ctc.scan([_write(tmp_path, "test_hang_bool.py", body)]) == []


# ── markers ───────────────────────────────────────────────────────────────────


def test_a_claim_marker_answers_the_finding(tmp_path):
    findings = ctc.scan([_write(tmp_path, "test_claim.py", MARKED_CLAIM)])
    finding = _single(findings, "C1")
    assert finding.marker == "claim"
    assert finding.unexplained is False
    assert finding.status == "explained"


def test_a_marker_on_the_line_above_counts(tmp_path):
    findings = ctc.scan([_write(tmp_path, "test_claim_above.py", MARKED_CLAIM_ABOVE)])
    finding = _single(findings, "C1")
    assert finding.marker == "claim"
    assert finding.unexplained is False


def test_a_guard_marker_clears_the_floor():
    """`guard` says the bound is scaffolding, so at the floor it answers itself."""
    finding = ctc.Finding(
        kind="HANG",
        rel="tests/test_x.py",
        line=7,
        value=60.0,
        window=None,
        text="barrier.wait(60)",
        marker="guard",
    )
    assert finding.unexplained is False
    assert finding.status == "explained"


def test_a_guard_marker_under_the_floor_still_finds(tmp_path):
    """`guard` claims the bound is scaffolding, so it must be at the floor."""
    findings = ctc.scan([_write(tmp_path, "test_guard_narrow.py", MARKED_GUARD_NARROW)])
    finding = _single(findings, "HANG")
    assert finding.marker == "guard"
    assert finding.unexplained is True
    assert "floor" in finding.problem
    assert str(finding)  # the row still renders


# ── the CLI: --check fails, the plain run only reports ────────────────────────


def test_check_exits_one_on_an_unmarked_finding(tmp_path, capsys):
    _write(tmp_path, "test_c1.py", C1_BOUND)
    assert ctc.main(["--check", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "unexplained finding" in out
    assert "claim" in out and "guard" in out  # it says how to answer


def test_check_exits_zero_on_an_empty_root(tmp_path, capsys):
    assert ctc.main(["--check", str(tmp_path / "missing")]) == 0
    assert "all findings explained" in capsys.readouterr().out


def test_check_exits_zero_when_every_finding_is_answered(tmp_path, capsys):
    _write(tmp_path, "test_claim.py", MARKED_CLAIM)
    assert ctc.main(["--check", str(tmp_path)]) == 0
    assert "all findings explained" in capsys.readouterr().out


def test_the_plain_run_prints_the_table_and_exits_zero(tmp_path, capsys):
    _write(tmp_path, "test_c1.py", C1_BOUND)
    assert ctc.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "KIND" in out and "WHERE" in out and "STATUS" in out
    assert "C1" in out and "unmarked" in out
    assert "claim" in out and "guard" in out


def test_the_plain_run_says_so_when_there_is_nothing(tmp_path, capsys):
    assert ctc.main([str(tmp_path / "nowhere")]) == 0
    assert "no wall-clock findings" in capsys.readouterr().out


# ── the rules are written down ────────────────────────────────────────────────


def test_the_three_rules_are_in_the_docstring():
    doc = ctc.__doc__ or ""
    for rule in ("C1", "C5", "HANG"):
        assert rule in doc, f"{rule} is not written down"
    assert "widen the slow path" in doc
    assert "tolerance for" in doc and "how long the test runs" in doc
    assert "wall time or not at all" in doc
    assert "guard floor" in doc


def test_the_named_constants_carry_the_numbers():
    """The rules are constants, so a reader can change one number, not a sentence."""
    assert (ctc.CLAIM_SHORT_S, ctc.GUARD_FLOOR_S) == (1.0, 60.0)
    assert ctc.C1_NEAR_S == 8.0
    assert set(ctc.REASONS) == {"C1", "C5", "HANG"}


def test_findings_render_as_a_stable_table():
    finding = ctc.Finding(
        kind="C1",
        rel="tests/test_x.py",
        line=7,
        value=5.0,
        window=1.0,
        text="    assert elapsed < 5.0",
        note="vs idle_event_timeout_sec=1 s",
    )
    assert finding.line == 7 and "C1" in str(finding) and "unmarked" in str(finding)


# ── fail-open and cheap ───────────────────────────────────────────────────────


def test_an_absent_root_is_reported_not_raised(tmp_path, capsys):
    assert ctc.scan([tmp_path / "no-such-dir"]) == []
    assert "no such file" in capsys.readouterr().err


def test_an_unparseable_file_is_skipped(tmp_path):
    assert ctc.scan_file(_write(tmp_path, "test_broken.py", UNPARSEABLE)) == []


def test_a_file_that_raises_is_skipped(tmp_path, monkeypatch):
    """A report must never take a run down with it."""

    def refuse(self, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    assert ctc.scan_file(_write(tmp_path, "test_locked.py", C1_BOUND)) == []


def test_scan_survives_a_file_that_blows_up(tmp_path, monkeypatch):
    """One bad file must not swallow the findings of the ones beside it."""
    real_scan = ctc.scan_file

    def blow_up(path):
        if path.name == "test_bad.py":
            raise RuntimeError("boom")
        return real_scan(path)

    monkeypatch.setattr(ctc, "scan_file", blow_up)
    _write(tmp_path, "test_bad.py", C1_BOUND)
    _write(tmp_path, "test_good.py", C1_BOUND)
    findings = ctc.scan([tmp_path])
    assert len(findings) == 1
    assert "good" in findings[0].rel


def _imports(path: Path) -> set[str]:
    """Every module the file imports, so the check's reach is asserted, not read."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_the_check_starts_nothing(tmp_path):
    """It reads source: no subprocess, no socket, no server, no provider."""
    banned = {"subprocess", "socket", "http", "requests", "urllib", "shutil", "signal"}
    assert _imports(SCRIPT) & banned == set(), _imports(SCRIPT) & banned
    for name in banned:
        assert not hasattr(ctc, name), f"the check imports {name}"
    assert inspect.getmodule(ctc.scan_file).__name__ == "check_test_clocks"
    assert ctc.scan([_write(tmp_path, "test_c1.py", C1_BOUND)])


@pytest.fixture(scope="module")
def tree_findings():
    """`tests/` and `tests_bugfix/` as they stand, scanned once for the two
    tests below that read the real tree — several CPU-seconds a scan under
    `-n 8`. Their xdist_group keeps both on the one worker that holds it."""
    return ctc.scan(ctc.DEFAULT_ROOTS)


@pytest.mark.xdist_group(name="check_test_clocks_tree")
def test_scan_of_the_tree_is_deterministic_and_fast(tree_findings):
    """The whole tree, read-only, in a couple of seconds of CPU.

    CPU, not wall: the scan is one thread with no subprocess, so its
    process_time is its own cost. The wall clock under `tests -n 8` next to a
    live round read 10.3 and 10.8 s — the box's load, not the scan's."""
    started = time.process_time()
    second = ctc.scan(ctc.DEFAULT_ROOTS)
    assert second == tree_findings
    assert time.process_time() - started < 10.0


@pytest.mark.xdist_group(name="check_test_clocks_tree")
def test_the_check_is_silent_on_the_tree_as_it_stands(tree_findings):
    """FL-4's promise on the base tree: zero unexplained findings."""
    unexplained = [finding for finding in tree_findings if finding.unexplained]
    assert unexplained == [], [str(finding) for finding in unexplained]
