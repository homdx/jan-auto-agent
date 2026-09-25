"""tests/test_contest_harvest.py — KC-5: gates + harvest, the runner's verdict.

Before this change the round's checks lived only in a script under `scripts/`,
so `tools/` could not use them without a `sys.path` hack, and the runner (KC-6)
had no way to tell an agent what to fix. KC-5 moves the scorecard into
`tools/contest/gates.py` (`judge` renamed `judge_worktree`, everything else
verbatim) and adds `tools/contest/harvest.py`, which pairs that row with the
agent's own claim of being done — the last `runs/<agent>/PROGRESS.csv` row,
written by `scripts/append_task.py` — into a `READY`/`REWORK` verdict whose
reasons are sentences an agent can act on.

Every case runs against temp repos and worktrees only (never this checkout),
per the ticket's acceptance list. The golden case runs the pre-KC-5 copy of the
script, frozen at `tests/fixtures/judge_epic_round_pre_kc5.py`, against the new
wrapper on the same temp round and compares stdout and CSV bytes.

Knowledge label: KC-5 regression test.
"""

from __future__ import annotations

import csv
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import tools.contest.gates as gates_mod  # noqa: E402
import tools.contest.harvest as harvest_mod  # noqa: E402

from tools.contest.gates import (  # noqa: E402
    BRIDGE,
    declared_files,
    extract_shrink,
    judge_worktree,
    run_tests,
    run_tests_detail,
    ticket_for_round,
)
from tools.contest.harvest import (  # noqa: E402
    REASON_CODES,
    TEXT_LIMIT,
    Harvest,
    Reason,
    harvest,
    rework_message,
)
from tools.contest.workspace import Workspace  # noqa: E402

JUDGE_SCRIPT = REPO_ROOT / "scripts" / "judge_epic_round.py"
JUDGE_BEFORE = REPO_ROOT / "tests" / "fixtures" / "judge_epic_round_pre_kc5.py"

_BRIDGE_SRC = """\
class CollectBridge:
    def _shrink(self, text):
        return text[:10]
"""

_BRIDGE_CHANGED = """\
class CollectBridge:
    def _shrink(self, text):
        return text[:40]
"""

_TICKET = """\
# R1 — probe the bridge

**File:** `tools/auto/probe.py`

**Also touches:** `tests/test_probe.py`
"""

_COLUMNS = ["ticket", "finding", "outcome", "commit", "note"]


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    """A temp repo whose base commit carries the bridge, a ticket and a test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "kc5@example.com")
    _git(repo, "config", "user.name", "KC5")
    (repo / "tools" / "auto").mkdir(parents=True)
    (repo / "tools" / "auto" / "collect_bridge.py").write_text(_BRIDGE_SRC, encoding="utf-8")
    (repo / "epic-tasks").mkdir()
    (repo / "epic-tasks" / "01-r1.md").write_text(_TICKET, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n",
                                                 encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def round_(tmp_path):
    """`(repo, base_sha, ticket_path)` — the tree every agent starts from."""
    repo, base = _make_repo(tmp_path)
    return repo, base, repo / "epic-tasks" / "01-r1.md"


def _worktree(repo: Path, base_sha: str, tmp_path: Path, agent: str = "a") -> Workspace:
    """A fresh worktree at *base_sha* on its own branch, as a `Workspace`."""
    path = tmp_path / f"wt-{agent}"
    _git(repo, "worktree", "add", "-q", str(path), base_sha)
    _git(path, "checkout", "-q", "-b", f"contest/01/{agent}", base_sha)
    return Workspace(agent=agent, path=path, branch=f"contest/01/{agent}",
                     base_sha=base_sha, kind="worktree")


def _edit(ws: Workspace, rel: str, text: str) -> None:
    path = ws.path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(ws: Workspace, message: str) -> str:
    _git(ws.path, "add", "-A")
    _git(ws.path, "commit", "-q", "-m", message)
    return _git(ws.path, "rev-parse", "HEAD")


def _record(ws: Workspace, ticket: str, outcome: str = "FIXED", commit: str = "",
            note: str = "") -> None:
    """Append one PROGRESS.csv row, the way `append_task.py` does."""
    ws.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    existed = ws.progress_csv.is_file() and ws.progress_csv.stat().st_size > 0
    with open(ws.progress_csv, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLUMNS)
        if not existed:
            w.writeheader()
        w.writerow({"ticket": ticket, "finding": "", "outcome": outcome,
                    "commit": commit, "note": note})


def _stage_probe(ws: Workspace) -> None:
    """The ticket's declared files, edited but not committed yet."""
    _edit(ws, "tools/auto/probe.py", "PROBE = 1\n")
    _edit(ws, "tests/test_probe.py", "def test_probe():\n    assert True\n")


def _accepting(ws: Workspace) -> str:
    """One commit: declared files only, a test shipped, `_shrink` untouched."""
    _stage_probe(ws)
    return _commit(ws, "probe the bridge")


def _codes(h: Harvest) -> list[str]:
    return [r.code for r in h.reasons]


def _with_origin(repo: Path, tmp_path: Path) -> Path:
    """KC-20: the base branch pushed to a bare `origin` under *tmp_path* — the
    way every real round starts, with the base already on a remote."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "HEAD")
    return bare


def _blocking(h: Harvest) -> list[str]:
    return [r.code for r in h.reasons if r.blocking]


def _reason(h: Harvest, code: str) -> Reason:
    return next(r for r in h.reasons if r.code == code)


# ─────────────────────────────────────────────────────────────────────────────
# 1. The golden case: the script's stdout and CSV are byte-identical
# ─────────────────────────────────────────────────────────────────────────────


def _run(script: Path, tmp_path: Path, tasks: Path, base: str, trees: list[Workspace],
         tag: str):
    """Run a judge script over several worktrees; return (rc, stdout, csv bytes)."""
    csv_path = tmp_path / f"{tag}.csv"
    argv = [sys.executable, str(script), "--round", "1", "--tasks", str(tasks),
            "--base", base]
    for wt in trees:
        argv += ["--worktree", f"{wt.agent}={wt.path}"]
    proc = subprocess.run(argv + ["--csv", str(csv_path)],
                          capture_output=True, text=True)
    stdout = proc.stdout.replace(str(csv_path), "<CSV>")
    return proc.returncode, stdout, csv_path.read_bytes() if csv_path.exists() else b""


def test_judge_script_stdout_and_csv_unchanged(round_, tmp_path):
    """Same inputs, same bytes: the move changed nothing the operator sees."""
    repo, base, ticket = round_
    a = _worktree(repo, base, tmp_path, "a")
    _accepting(a)
    b = _worktree(repo, base, tmp_path, "b")
    _accepting(b)
    _edit(b, "tools/auto/other.py", "OTHER = 1\n")  # a FAIL row and an off-ticket note
    _commit(b, "second commit")

    before = _run(JUDGE_BEFORE, tmp_path, ticket.parent, base, [a, b], "before")
    after = _run(JUDGE_SCRIPT, tmp_path, ticket.parent, base, [a, b], "after")

    assert after[0] == before[0] == 0
    assert after[1] == before[1], "stdout must be byte-identical"
    assert after[2] == before[2], "the --csv scorecard must be byte-identical"
    assert "scorecard -> <CSV>" in after[1]
    assert "FAIL" in after[1] and "touched off-ticket: tools/auto/other.py" in after[1]
    assert b"off_ticket" in after[2]


def test_judge_worktree_row_matches_the_pre_kc5_judge(round_, tmp_path):
    """`judge_worktree` is the old `judge`, renamed — same row for the same input."""
    spec = importlib.util.spec_from_file_location("judge_before", JUDGE_BEFORE)
    before = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(before)

    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _edit(wt, "tools/auto/other.py", "OTHER = 1\n")
    _commit(wt, "second commit")

    declared = list(declared_files(ticket))
    assert judge_worktree(wt.agent, str(wt.path), base, declared, False) == \
        before.judge(wt.agent, str(wt.path), base, declared, False)


def test_judge_worktree_reports_a_non_worktree(round_, tmp_path):
    repo, base, ticket = round_
    path = tmp_path / "not-a-worktree"
    path.mkdir()
    row = judge_worktree("a", str(path), base, ["tools/auto/probe.py"], False)
    assert row["gate"] == "FAIL" and row["notes"] == "not a git worktree"
    assert "commits" not in row


# ─────────────────────────────────────────────────────────────────────────────
# 2. `declared_files` and the moved helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_declared_files_reads_file_and_also_touches(round_):
    repo, base, ticket = round_
    assert declared_files(ticket) == ("tools/auto/probe.py", "tests/test_probe.py")


def test_declared_files_drops_a_placeholder(tmp_path):
    p = tmp_path / "02-x.md"
    p.write_text("# X\n\n**File:** `—`\n\n**Also touches:** `a.py`\n", encoding="utf-8")
    assert declared_files(p) == ("a.py",)


def test_declared_files_raises_for_a_missing_ticket(tmp_path):
    with pytest.raises(FileNotFoundError):
        declared_files(tmp_path / "nope.md")


def _ticket(tmp_path, file_line, also_line=None):
    p = tmp_path / "03-y.md"
    lines = ["# Y", "", f"**File:** {file_line}"]
    if also_line is not None:
        lines += ["", f"**Also touches:** {also_line}"]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_declared_files_reads_every_backticked_path_on_the_file_line(tmp_path):
    """KC-17: KC-6's verbatim lines — `(new)` after the span is not a path."""
    p = _ticket(tmp_path, "`tools/contest/runner.py` (new)",
                "`tests/test_contest_runner.py` (new), `tests/_kilo_fake.py`")
    assert declared_files(p) == ("tools/contest/runner.py",
                                 "tests/test_contest_runner.py", "tests/_kilo_fake.py")


def test_declared_files_drops_symbols_on_the_file_line(tmp_path):
    """KC-14's line: the symbols in the brackets are backticked but not paths."""
    p = _ticket(tmp_path, "`tools/contest/harvest.py` (`_is_ancestor`, `harvest`)")
    assert declared_files(p) == ("tools/contest/harvest.py",)


def test_declared_files_keeps_three_file_paths_in_order(tmp_path):
    """KC-5's line names three paths; all three, in the line's order."""
    p = _ticket(tmp_path, "`tools/contest/gates.py` (new), `tools/contest/harvest.py` "
                          "(new), `scripts/judge_epic_round.py`")
    assert declared_files(p) == ("tools/contest/gates.py", "tools/contest/harvest.py",
                                 "scripts/judge_epic_round.py")


def test_declared_files_bare_file_line_is_one_path(tmp_path):
    assert declared_files(_ticket(tmp_path, "tools/x.py")) == ("tools/x.py",)
    assert declared_files(_ticket(tmp_path, "—")) == ()


def test_declared_files_drops_config_keys_on_also_touches(tmp_path):
    """KC-10's line: a `key = value` span is backticked but not a path."""
    p = _ticket(tmp_path, "`tools/contest/runner.py`",
                "`contest.ini` (`compact_at_percent = 80`), `tests/_kilo_fake.py`")
    assert declared_files(p) == ("tools/contest/runner.py", "contest.ini",
                                 "tests/_kilo_fake.py")


def test_harvest_does_not_call_the_file_line_path_off_ticket(round_, tmp_path):
    """The live case: a ticket written the epic way declares its primary file."""
    repo, base, _ = round_
    ticket = repo / "epic-tasks" / "04-z.md"
    ticket.write_text("# Z\n\n**File:** `tools/auto/probe.py` (`probe`)\n\n"
                      "**Also touches:** `tests/test_probe.py` (new)\n", encoding="utf-8")
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    assert "off_ticket_files" not in _codes(harvest(wt, ticket))

    _edit(wt, "tools/auto/other.py", "OTHER = 1\n")
    _git(wt.path, "add", "tools/auto/other.py")
    _git(wt.path, "commit", "-q", "--amend", "--no-edit")
    _record(wt, ticket.name, outcome="DONE", commit=_git(wt.path, "rev-parse", "HEAD"))
    h = harvest(wt, ticket)
    assert _reason(h, "off_ticket_files").text.endswith("declared list: tools/auto/other.py")


def test_ticket_for_round_still_finds_and_titles(round_):
    repo, base, ticket = round_
    name, title, declared = ticket_for_round(str(ticket.parent), 1)
    assert name == "01-r1.md"
    assert title == "R1 — probe the bridge"
    assert declared == ["tools/auto/probe.py", "tests/test_probe.py"]
    assert ticket_for_round(str(ticket.parent), 99) == (None, None, [])


def test_extract_shrink_sees_a_change(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    assert extract_shrink(str(wt.path), "HEAD") == extract_shrink(str(wt.path), base)
    _edit(wt, BRIDGE, _BRIDGE_CHANGED)
    _commit(wt, "shrink wider")
    assert extract_shrink(str(wt.path), "HEAD") != extract_shrink(str(wt.path), base)


def test_run_tests_summary_and_tail_agree(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    summary, tail = run_tests_detail(str(wt.path))
    assert run_tests(str(wt.path)) == summary
    assert summary.startswith("tests:PASS")
    assert "✗" not in summary and tail == []

    # The moved `run_tests` still yields the old module's summary, token for token.
    spec = importlib.util.spec_from_file_location("judge_before", JUDGE_BEFORE)
    before = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(before)
    assert before.run_tests(str(wt.path)) == summary

    _edit(wt, "tests/test_probe.py",
          "def test_probe():\n    assert False, 'boom-marker-42'\n")
    _commit(wt, "break the test")
    summary, tail = run_tests_detail(str(wt.path))
    assert "✗" in summary
    assert tail and any("boom-marker-42" in line for line in tail)


def test_run_tests_counts_failures_when_the_ini_silences_the_stats_line(round_, tmp_path):
    """KC-26: `-qq` prints no `N failed in Xs` line; the count comes from the
    short summary instead of collapsing to `0✗`, and the tail names its root."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _edit(wt, "pytest.ini", "[pytest]\naddopts = -qq\n")
    _edit(wt, "tests/test_probe.py",
          "def test_probe():\n    assert False, 'boom-marker-42'\n"
          "def test_other():\n    assert False\n")
    _commit(wt, "break two tests under -qq")
    summary, tail = run_tests_detail(str(wt.path))
    assert summary.split()[0] == "tests:2✗"
    assert tail[0] == "--- tests"
    assert any("boom-marker-42" in line for line in tail)


def test_run_tests_keeps_a_tail_per_failing_root(round_, tmp_path):
    """Two roots fail: both tails are kept, each under its own `--- <root>` line —
    not only the last root's."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _edit(wt, "tests/test_probe.py", "def test_probe():\n    assert False, 'first-root-7'\n")
    _edit(wt, "tests_bugfix/test_b.py", "def test_b():\n    assert False, 'second-root-9'\n")
    _commit(wt, "break both roots")
    summary, tail = run_tests_detail(str(wt.path))
    assert summary.startswith("tests:1✗ tests_bugfix:1✗")
    first, second = tail.index("--- tests"), tail.index("--- tests_bugfix")
    assert first < second
    assert any("first-root-7" in l for l in tail[first:second])
    assert any("second-root-9" in l for l in tail[second:])


_FLAKY = """\
import pathlib
FLAG = pathlib.Path(__file__).with_suffix(".flag")

def test_flaky():
    if FLAG.exists():
        return
    FLAG.write_text("seen")
    assert False, "first run only"
"""


def test_run_tests_reruns_the_failed_tests_alone_and_marks_a_flake(round_, tmp_path):
    """A test that fails once and passes on its serial rerun is `PASS*1`, named
    in the tail, and not a `tests_failed` reason — a timing test under round
    load is not a failure of the tree."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _stage_probe(wt)
    _edit(wt, "tests/test_flaky.py", _FLAKY)
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "probe, plus a flaky test"))
    summary, tail = run_tests_detail(str(wt.path))
    assert summary.split()[0] == "tests:PASS*1"
    assert "✗" not in summary
    assert len(tail) == 1 and "flaky" in tail[0] and "tests/test_flaky.py::test_flaky" in tail[0]

    (wt.path / "tests" / "test_flaky.flag").unlink()
    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "READY" and "tests_failed" not in _codes(h)
    assert h.facts["tests_run"].startswith("tests:PASS*1")


def test_run_tests_reports_a_non_zero_exit_without_a_failed_test(round_, tmp_path):
    """pytest dying before any test (a collection error here) is `✗` with the
    exit code, never a silent `PASS`."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _edit(wt, "tests/test_probe.py", "import does_not_exist_zz\n")
    _commit(wt, "collection error")
    summary, tail = run_tests_detail(str(wt.path))
    assert "✗" in summary.split()[0] and summary.split()[0] != "tests:PASS"
    assert tail[0] == "--- tests"


def _tier(ws: Workspace, root: str, *names: str, target: str = "tests") -> None:
    """*root* as a symlink view: one relative link per name, into *target*."""
    d = ws.path / root
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        (d / name).symlink_to(Path("..") / target / name)


def _roots_run(monkeypatch) -> list:
    """Record the first argument of every `_pytest` call — the root it ran."""
    import tools.contest.gates as gates
    ran = []
    real = gates._pytest

    def spy(cwd, *args):
        ran.append(args[0])
        return real(cwd, *args)

    monkeypatch.setattr(gates, "_pytest", spy)
    return ran


def test_run_tests_does_not_rerun_a_tier_of_links_into_tests(round_, tmp_path, monkeypatch):
    """FL-8: `.smoke_tests` / `.regression_tests` made only of links into
    `tests/` are the files `tests` just ran — no second pytest, one token each."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _stage_probe(wt)
    _tier(wt, ".smoke_tests", "test_base.py")
    _tier(wt, ".regression_tests", "test_probe.py")
    (wt.path / ".smoke_tests" / "__pycache__").mkdir()
    _commit(wt, "probe, tiered")
    ran = _roots_run(monkeypatch)
    summary, tail = run_tests_detail(str(wt.path))
    assert summary == ("tests:PASS tests_bugfix:absent "
                       ".smoke_tests:links-to-tests .regression_tests:links-to-tests")
    assert ran == ["tests"] and tail == []


def test_run_tests_still_runs_a_tier_that_is_not_only_links_into_tests(round_, tmp_path, monkeypatch):
    """FL-8: a real file in a tier, or a link out of `tests/`, keeps the old
    run — only a pure view is skipped."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _tier(wt, ".smoke_tests", "test_base.py")
    _edit(wt, ".smoke_tests/test_real.py", "def test_real():\n    assert False, 'real-file-3'\n")
    _edit(wt, "tests_bugfix/test_b.py", "def test_b():\n    assert True\n")
    _tier(wt, ".regression_tests", "test_b.py", target="tests_bugfix")
    _commit(wt, "a tier with a real file, a tier linking elsewhere")
    ran = _roots_run(monkeypatch)
    summary, tail = run_tests_detail(str(wt.path))
    assert [r for r in ran if "::" not in r] == [   # `::` — the flake rerun
        "tests", "tests_bugfix", ".smoke_tests", ".regression_tests"]
    assert ".smoke_tests:1✗" in summary and ".regression_tests:PASS" in summary
    assert any("real-file-3" in line for line in tail)


def test_run_tests_reports_the_tier_check_when_the_tree_has_one(round_, tmp_path):
    """FL-8: the skipped tiers are traded for `sync_test_tiers.py --check` —
    `tiers:PASS`, or `tiers:✗` with its output in the tail."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _edit(wt, "scripts/sync_test_tiers.py",
          "import sys\nassert sys.argv[1:] == ['--check']\nsys.exit(0)\n")
    _commit(wt, "a passing tier check")
    summary, tail = run_tests_detail(str(wt.path))
    assert summary.split()[-1] == "tiers:PASS" and tail == []

    _edit(wt, "scripts/sync_test_tiers.py",
          "import sys\nprint('.smoke_tests: missing link test_new.py')\nsys.exit(1)\n")
    _commit(wt, "a failing tier check")
    summary, tail = run_tests_detail(str(wt.path))
    assert summary.split()[-1] == "tiers:✗"
    assert tail[0] == "--- tiers" and any("missing link test_new.py" in l for l in tail)


# ─────────────────────────────────────────────────────────────────────────────
# 3. `harvest`: the claim and the facts become a verdict
# ─────────────────────────────────────────────────────────────────────────────


def test_harvest_without_a_progress_row_reworks(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)

    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert _codes(h) == ["no_progress_row"]
    assert _blocking(h) == ["no_progress_row"]
    assert h.facts["commits"] == 1
    assert h.elapsed > 0


def test_harvest_accepts_a_done_row_on_a_clean_commit(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    sha = _accepting(wt)
    _record(wt, ticket.name, outcome="DONE", commit=sha)

    h = harvest(wt, ticket)
    assert h.verdict == "READY"
    assert _blocking(h) == []
    assert h.commit == sha
    assert h.facts["commits"] == 1 and h.facts["shrink"] == "same"
    assert h.facts["test_files"] == 1 and h.facts["pushed"] == "no"


def test_harvest_accepts_fixed_because_append_task_writes_it(round_, tmp_path):
    """`append_task.py` rewrites `--outcome DONE` onto FIXED; both mean done."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="FIXED", commit=_accepting(wt))
    assert harvest(wt, ticket).verdict == "READY"


@pytest.mark.parametrize("outcome", ["SKIPPED", "ALREADY-OK", "WIP"])
def test_harvest_rejects_a_non_done_outcome(round_, tmp_path, outcome):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome=outcome, commit=_accepting(wt))

    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert "progress_not_done" in _codes(h)


def test_harvest_flags_a_done_row_without_a_commit(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    sha = _accepting(wt)
    _record(wt, ticket.name, outcome="DONE", commit="")

    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert "no_commit" in _codes(h)
    assert h.commit is None  # only the agent's own claim is ever reported
    assert h.facts["sha"] == sha[:7]  # the branch head is still in the facts


def test_harvest_flags_a_commit_off_the_branch(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)

    # A commit on a side line of history: it exists, but is not on the branch.
    _git(wt.path, "checkout", "-q", "--detach")
    _edit(wt, "tools/auto/side.py", "SIDE = 1\n")
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-q", "-m", "side")
    side = _git(wt.path, "rev-parse", "HEAD")
    _git(wt.path, "checkout", "-q", wt.branch)

    _record(wt, ticket.name, outcome="DONE", commit=side)
    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert "commit_not_on_branch" in _codes(h)
    assert side[:12] in _reason(h, "commit_not_on_branch").text


def test_harvest_flags_a_bogus_commit_sha(round_, tmp_path):
    """An unknown sha is certainly not on the branch."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit="0" * 40)
    _accepting(wt)

    assert "commit_not_on_branch" in _codes(harvest(wt, ticket))


def test_harvest_flags_every_symbolic_claim_as_not_a_sha(round_, tmp_path):
    """`HEAD`, `@`, a branch and a tag all resolve in git, and none of them is a claim.

    A symbolic name pins nothing: it resolves to something else on every branch
    it is read from, so the claim must be written as a sha.
    """
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _git(wt.path, "tag", "kc-14-tag")

    for claim in ("HEAD", "@", "HEAD~0", wt.branch, "kc-14-tag"):
        _record(wt, ticket.name, outcome="DONE", commit=claim)  # last row wins
        h = harvest(wt, ticket)

        assert h.verdict == "REWORK"
        assert _codes(h) == ["commit_not_on_branch"]
        text = _reason(h, "commit_not_on_branch").text
        assert claim in text
        assert "rev-parse" in text
        assert len(text) <= TEXT_LIMIT
        assert h.commit is None
        assert h.facts["commits"] == 1  # the worktree itself is fine


def test_harvest_accepts_a_short_sha_and_reports_the_full_one(round_, tmp_path):
    """A 7-char prefix is a claim; `Harvest.commit` is the 40-char sha it names."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    full = _accepting(wt)
    _record(wt, ticket.name, outcome="DONE", commit=full[:7])

    h = harvest(wt, ticket)
    assert h.verdict == "READY"
    assert h.commit == full
    assert len(h.commit) == 40


def test_harvest_accepts_an_uppercase_sha_and_stores_it_lowercase(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    full = _accepting(wt)
    _record(wt, ticket.name, outcome="DONE", commit=full.upper())

    h = harvest(wt, ticket)
    assert h.verdict == "READY"
    assert h.commit == full.lower()


def test_harvest_flags_a_sub_7_char_prefix(round_, tmp_path):
    """git resolves 6 chars; a claim must pin one commit, not a range."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    full = _accepting(wt)
    _record(wt, ticket.name, outcome="DONE", commit=full[:6])

    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert _codes(h) == ["commit_not_on_branch"]
    text = _reason(h, "commit_not_on_branch").text
    assert full[:6] in text
    assert "rev-parse" in text
    assert h.commit is None


def test_harvest_off_branch_sha_is_rejected_with_no_commit(round_, tmp_path):
    """A real sha off the branch keeps KC-5's sentence — and `Harvest.commit` is
    None, not the sha it resolves to: a rejected claim carries no commit."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _git(wt.path, "checkout", "-q", "-b", "side", base)
    _edit(wt, "side.txt", "side\n")
    side = _commit(wt, "side")
    _git(wt.path, "checkout", "-q", wt.branch)

    for claim in (side, side[:7], side.upper()):
        _record(wt, ticket.name, outcome="DONE", commit=claim)
        h = harvest(wt, ticket)
        assert _codes(h) == ["commit_not_on_branch"]
        text = _reason(h, "commit_not_on_branch").text
        assert side[:7] in text and "not an ancestor of HEAD" in text and "rebase" in text
        assert h.commit is None


def test_harvest_not_a_sha_sentence_stays_within_the_budget(round_, tmp_path):
    """The claim is quoted as written but cut to a sha's length: a 300-char
    row value must not push the sentence past `TEXT_LIMIT`."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _record(wt, ticket.name, outcome="DONE", commit="x" * 300)

    h = harvest(wt, ticket)
    text = _reason(h, "commit_not_on_branch").text
    assert text.startswith("commit " + "x" * 40 + " is not a sha")
    assert len(text) <= TEXT_LIMIT
    assert h.commit is None


def test_harvest_flags_two_commits(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _edit(wt, "tools/auto/probe.py", "PROBE = 2\n")
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "another commit"))

    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert "commits_ne_1" in _codes(h)
    assert "2 commits" in _reason(h, "commits_ne_1").text


def test_harvest_flags_no_test_shipped(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _edit(wt, "tools/auto/probe.py", "PROBE = 1\n")
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "probe, no test"))

    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert "no_test_file" in _codes(h)


def test_harvest_flags_a_changed_shrink(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _stage_probe(wt)
    _edit(wt, BRIDGE, _BRIDGE_CHANGED)
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "widen the shrink"))

    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert h.facts["shrink"] == "CHANGED"
    assert "shrink_changed" in _codes(h)
    assert BRIDGE in _reason(h, "shrink_changed").text


def test_harvest_flags_a_pushed_commit(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(wt.path, "remote", "add", "origin", str(bare))
    _git(wt.path, "push", "-q", "origin", wt.branch)

    _record(wt, ticket.name, outcome="DONE", commit=_git(wt.path, "rev-parse", "HEAD"))
    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert "pushed" in _codes(h)


def test_harvest_zero_commits_no_pushed_when_base_on_origin(round_, tmp_path):
    """KC-20: a worktree with no commits must not be flagged as pushed even when
    the base itself has been pushed to origin."""
    repo, base, ticket = round_
    _with_origin(repo, tmp_path)
    # A worktree with zero agent commits sits at the base.
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=base)
    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert "commits_ne_1" in _codes(h)
    assert "pushed" not in _codes(h)


def test_judge_worktree_zero_commits_not_pushed(round_, tmp_path):
    """KC-20: `judge_worktree` directly returns pushed=no when commits==0."""
    repo, base, ticket = round_
    _with_origin(repo, tmp_path)
    wt = _worktree(repo, base, tmp_path)
    declared = list(declared_files(ticket))
    row = judge_worktree(wt.agent, str(wt.path), base, declared, False)
    assert row["commits"] == 0
    assert row["pushed"] == "no"


def test_harvest_one_unpushed_commit_no_pushed_when_base_on_origin(round_, tmp_path):
    """KC-20: one agent commit above a base that lives on origin is fine."""
    repo, base, ticket = round_
    _with_origin(repo, tmp_path)
    wt = _worktree(repo, base, tmp_path)
    sha = _accepting(wt)
    # Do NOT push this commit.
    _record(wt, ticket.name, outcome="DONE", commit=sha)
    h = harvest(wt, ticket)
    assert h.verdict == "READY"
    assert "pushed" not in _codes(h)


def test_harvest_one_pushed_commit_flags_pushed_when_base_on_origin(round_, tmp_path):
    """KC-20: pushing the agent commit still reports pushed."""
    repo, base, ticket = round_
    _with_origin(repo, tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _git(wt.path, "push", "-q", "origin", wt.branch)
    _record(wt, ticket.name, outcome="DONE", commit=_git(wt.path, "rev-parse", "HEAD"))
    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    assert "pushed" in _codes(h)


def test_harvest_reports_off_ticket_files_without_stopping(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _stage_probe(wt)
    _edit(wt, "tools/auto/other.py", "OTHER = 1\n")
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "probe, plus other"))

    h = harvest(wt, ticket)
    assert h.verdict == "READY"
    off = _reason(h, "off_ticket_files")
    assert off.blocking is False
    assert off.text == "touched 1 file(s) outside the ticket's declared list: tools/auto/other.py"


def test_harvest_only_runs_tests_when_asked(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))

    h = harvest(wt, ticket)
    assert h.verdict == "READY"
    assert h.facts["tests_run"] == "—"
    assert "tests_failed" not in _codes(h)


def test_harvest_run_tests_reports_the_failure_tail(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _edit(wt, "tests/test_probe.py",
          "def test_probe():\n    assert False, 'boom-marker-42'\n")
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "break the test"))

    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "REWORK"
    failed = _reason(h, "tests_failed")
    assert h.facts["tests_run"].startswith("tests:") and "✗" in h.facts["tests_run"]
    assert "boom-marker-42" in failed.text
    assert "the tests do not pass" in failed.text


def test_harvest_last_row_wins(round_, tmp_path):
    """The claim is the last row for the ticket, not the first."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _record(wt, ticket.name, outcome="SKIPPED", commit="", note="first attempt")
    _record(wt, ticket.name, outcome="DONE", commit=_git(wt.path, "rev-parse", "HEAD"))

    assert harvest(wt, ticket).verdict == "READY"


def test_harvest_ignores_rows_for_other_tickets(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _record(wt, "99-other.md", outcome="DONE", commit=_git(wt.path, "rev-parse", "HEAD"))

    assert "no_progress_row" in _codes(harvest(wt, ticket))


def test_harvest_reworks_when_the_path_is_not_a_worktree(round_, tmp_path):
    repo, base, ticket = round_
    path = tmp_path / "loose-dir"
    path.mkdir()
    ws = Workspace(agent="a", path=path, branch="contest/01/a", base_sha=base,
                   kind="worktree")

    h = harvest(ws, ticket)
    assert h.verdict == "REWORK"
    assert _codes(h) == ["no_progress_row", "commits_ne_1"]
    assert "not a git worktree" in _reason(h, "commits_ne_1").text


# ─────────────────────────────────────────────────────────────────────────────
# 4. The verdict's shape, and the message the runner sends
# ─────────────────────────────────────────────────────────────────────────────


def test_reason_codes_are_the_tickets_list():
    assert set(REASON_CODES) == {
        "no_progress_row", "progress_not_done", "no_commit", "commit_not_on_branch",
        "commits_ne_1", "pushed", "no_test_file", "shrink_changed", "off_ticket_files",
        "tests_failed", "uncommitted_files",
    }


def test_blocking_reasons_stay_within_the_sentence_budget(round_, tmp_path):
    """Every reason is one sentence ≤ TEXT_LIMIT chars, the two that carry a
    payload excepted: `tests_failed` the pytest tail, `uncommitted_files` the
    `git status` lines."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _stage_probe(wt)
    _edit(wt, "tools/auto/other.py", "OTHER = 1\n")
    _commit(wt, "probe, plus other")
    _record(wt, ticket.name, outcome="SKIPPED", commit="")

    h = harvest(wt, ticket)
    assert len(_codes(h)) >= 3
    for r in h.reasons:
        if r.code in ("tests_failed", "uncommitted_files"):
            continue
        assert len(r.text) <= TEXT_LIMIT, f"{r.code}: {len(r.text)} chars"
        assert r.text == r.text.strip()


def test_reason_and_harvest_are_frozen():
    r = Reason("pushed", "HEAD is reachable from a remote")
    with pytest.raises(Exception):
        r.code = "other"  # type: ignore[misc]
    h = Harvest(verdict="READY", reasons=(r,), commit="abc", facts={"commits": 1},
                elapsed=0.01)
    with pytest.raises(Exception):
        h.verdict = "REWORK"  # type: ignore[misc]
    assert h.facts == {"commits": 1}


def test_off_ticket_files_is_the_only_non_blocker(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _stage_probe(wt)
    _edit(wt, "tools/auto/other.py", "OTHER = 1\n")
    _commit(wt, "probe, plus other")
    _record(wt, ticket.name, outcome="SKIPPED", commit="")

    h = harvest(wt, ticket)
    assert {r.code for r in h.reasons if not r.blocking} == {"off_ticket_files"}


def test_rework_message_lists_every_blocking_reason(round_, tmp_path):
    """Two reasons → both sentences, the attempt counter, `append_task.py`."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _accepting(wt)
    _edit(wt, "tools/auto/probe.py", "PROBE = 2\n")
    _commit(wt, "another commit")  # no PROGRESS.csv at all: two blocking reasons

    h = harvest(wt, ticket)
    texts = [r.text for r in h.reasons if r.blocking]
    assert len(texts) == 2

    msg = rework_message(h, attempt=2, max_rework=3)
    for t in texts:
        assert t in msg
    assert "Attempt 2 of 3" in msg
    assert "append_task.py" in msg
    assert "**one** commit" in msg
    assert "Ground rules" in msg
    assert "_shrink" in msg
    assert "tests/test_probe.py" not in msg  # no ticket text is repeated


def test_rework_message_puts_non_blocking_notes_under_also_noted(round_, tmp_path):
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _stage_probe(wt)
    _edit(wt, "tools/auto/other.py", "OTHER = 1\n")
    _commit(wt, "probe, plus other")
    _record(wt, ticket.name, outcome="SKIPPED", commit="")

    h = harvest(wt, ticket)
    assert h.verdict == "REWORK"
    msg = rework_message(h, attempt=1, max_rework=2)
    assert "Also noted:" in msg
    assert msg.index("Also noted:") > msg.index(_reason(h, "progress_not_done").text)
    assert msg.index("Also noted:") < msg.index("Ground rules")
    assert _reason(h, "off_ticket_files").text in msg


def test_rework_message_has_no_also_noted_without_a_note():
    h = Harvest(verdict="REWORK",
                reasons=(Reason("no_test_file", "01-r1.md shipped no test file"),),
                commit="abc", facts={}, elapsed=0.0)
    msg = rework_message(h, attempt=1, max_rework=2)
    assert "- 01-r1.md shipped no test file" in msg
    assert "Also noted" not in msg
    assert msg.index("Attempt 1 of 2") < msg.index("- 01-r1.md") < msg.index("Ground rules")


# ─────────────────────────────────────────────────────────────────────────────
# 5. KC-60: the roots run on the commit, and the tree is left exactly as it was
# ─────────────────────────────────────────────────────────────────────────────

#: The base repo's own noise filter. The harvest never carries a list of its
#: own — `git status` applies this file and reports nothing it matches.
IGNORED = "runs/\n__pycache__/\n.pytest_cache/\nscratch/\n"

#: The committed check that makes a missing tier link a failing test. It reads
#: the tree git has, so it passes in a tree that carries the link and fails in
#: one that does not.
TIER_LINKS = """\
from pathlib import Path


def test_every_test_file_has_a_link_in_the_tier():
    root = Path(__file__).resolve().parent.parent
    tier = root / ".smoke_fast"
    missing = [p.name for p in sorted((root / "tests").glob("test_*.py"))
               if not (tier / p.name).is_symlink()]
    assert not missing, ".smoke_fast has no link for: " + ", ".join(missing)
"""


def _write(path: Path, text: str) -> None:
    """One file with its parents, for a repo path rather than a `Workspace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_ignored_repo(tmp_path: Path) -> tuple[Path, str, Path]:
    """A repo whose base carries a `.gitignore`, so `runs/` is the repo's own
    scratch rather than an uncommitted file."""
    repo, _base = _make_repo(tmp_path)
    _write(repo / ".gitignore", IGNORED)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ignore the round's scratch")
    return repo, _git(repo, "rev-parse", "HEAD"), repo / "epic-tasks" / "01-r1.md"


def _make_tier_repo(tmp_path: Path) -> tuple[Path, str, Path]:
    """A repo whose base carries a `.smoke_fast` tier — a name that exists nowhere
    in this repo — one committed relative link and one real coverage check. The
    tier is not a pure link view, so it is a root that runs instead of a view
    that is skipped."""
    repo, _base, ticket = _make_ignored_repo(tmp_path)
    _write(repo / ".smoke_fast" / "test_links.py", TIER_LINKS)
    (repo / ".smoke_fast" / "test_base.py").symlink_to(
        Path("..") / "tests" / "test_base.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "a .smoke_fast tier")
    return repo, _git(repo, "rev-parse", "HEAD"), ticket


def _tree_state(ws: Workspace) -> tuple[str, str, dict]:
    """`(git status, git stash list, {path: mtime})` — what the harvest must leave."""
    mtimes: dict[str, int] = {}
    for p in sorted(ws.path.rglob("*")):
        if ".git" in p.parts:      # git's own bookkeeping, not the tree's files
            continue
        mtimes[str(p.relative_to(ws.path))] = p.lstat().st_mtime_ns
    return _git(ws.path, "status", "--porcelain"), _git(ws.path, "stash", "list"), mtimes


def _registered_worktrees(repo: Path) -> set[Path]:
    """The worktree paths git has registered, resolved: a throwaway is gone when
    this set is unchanged."""
    return {Path(line[len("worktree "):]).resolve()
            for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
            if line.startswith("worktree ")}


def test_harvest_runs_the_roots_on_the_commit_not_the_tree_around_it(tmp_path, monkeypatch):
    """The commit lacks the tier link that only the untracked file makes pass. The
    roots run on the commit, so the verdict is `tests_failed` and
    `uncommitted_files` names the link — the green worktree is not what is scored."""
    monkeypatch.setattr(gates_mod, "TEST_ROOTS", ["tests", ".smoke_fast"])
    repo, base, ticket = _make_tier_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)

    _edit(wt, "tools/auto/probe.py", "PROBE = 1\n")
    _edit(wt, "tests/test_probe.py", "def test_probe():\n    assert True\n")
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "probe, plus a new test"))
    _tier(wt, ".smoke_fast", "test_probe.py")      # never `git add`ed

    green, _ = run_tests_detail(str(wt.path))      # the hole: the tree is green
    assert "✗" not in green

    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "REWORK"
    assert h.facts["tests_run"].startswith("tests:PASS ")
    assert ".smoke_fast:1✗" in h.facts["tests_run"]      # one failure: the link is missing
    uncommitted = _reason(h, "uncommitted_files")
    assert uncommitted.blocking is False
    assert ".smoke_fast/test_probe.py" in uncommitted.text
    assert _reason(h, "tests_failed").blocking is True
    # The reason reaches the rework prompt, so the REWORK says why.
    msg = rework_message(h, attempt=1, max_rework=2)
    assert "- the tests do not pass:" in msg
    assert "Also noted:" in msg
    assert "- ?? .smoke_fast/test_probe.py is not in the commit you handed in" in msg


def test_uncommitted_files_leaves_ignored_paths_out(tmp_path):
    """The noise filter is the repo's own `.gitignore`: an ignored file is not
    listed, and the non-blocking note keeps a green commit `READY`."""
    repo, base, ticket = _make_ignored_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    _write(wt.path / "scratch" / "junk.txt", "noise\n")
    _write(wt.path / "stray.py", "x = 1\n")

    h = harvest(wt, ticket, run_tests=True)
    text = _reason(h, "uncommitted_files").text
    assert "stray.py" in text
    assert "scratch" not in text and "junk.txt" not in text
    assert h.verdict == "READY"          # the commit is green; the note is non-blocking


def test_uncommitted_files_never_names_the_runners_own_queue_file(round_, tmp_path):
    """A repo that does not ignore the runner's ground: the claim the harvest
    reads (`ws.progress_csv`) is untracked there, and it is still not named —
    telling the agent to commit the runner's file would be the wrong advice."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    assert "PROGRESS.csv" in _git(wt.path, "status", "--porcelain", "--untracked-files=all")
    _write(wt.path / "stray.py", "x = 1\n")

    h = harvest(wt, ticket, run_tests=True)
    text = _reason(h, "uncommitted_files").text
    assert text.startswith("?? stray.py is not in the commit you handed in")
    assert "PROGRESS.csv" not in text


def test_an_uncommitted_fix_does_not_turn_the_verdict_ready(tmp_path):
    """The other way round: the commit has the failing test, the worktree has the
    fix. The roots run on the commit, so it stays `REWORK`."""
    repo, base, ticket = _make_ignored_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _edit(wt, "tools/auto/probe.py", "PROBE = 1\n")
    _edit(wt, "tests/test_probe.py", "def test_probe():\n    assert False, 'boom-kc60-42'\n")
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "probe, with a broken test"))
    _edit(wt, "tests/test_probe.py", "def test_probe():\n    assert True\n")   # not committed

    green, _ = run_tests_detail(str(wt.path))      # the tree is green
    assert "✗" not in green

    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "REWORK"
    assert _blocking(h) == ["tests_failed"]
    assert "boom-kc60-42" in _reason(h, "tests_failed").text
    assert "tests/test_probe.py" in _reason(h, "uncommitted_files").text


def test_harvest_leaves_the_worktree_byte_for_byte_alone(tmp_path):
    """No stash, no clean, no checkout: `git status`, `git stash list` and every
    file's mtime are the same after the harvest as before it."""
    repo, base, ticket = _make_ignored_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    _write(wt.path / "stray.py", "x = 1\n")

    before = _tree_state(wt)
    h = harvest(wt, ticket, run_tests=True)
    assert _tree_state(wt) == before
    assert h.verdict == "READY"          # the stray file is a note, not a change


def test_the_roots_run_in_a_throwaway_checkout_of_the_commit(tmp_path, monkeypatch):
    """Not in the agent's tree at all: a detached worktree at the claimed commit,
    removed again, with neither git's registration nor its directory left."""
    repo, base, ticket = _make_ignored_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    before = _registered_worktrees(repo)

    seen: list[tuple[str, str]] = []
    real = harvest_mod.run_tests_detail

    def roots_on_the_commit(cwd):
        seen.append((cwd, _git(cwd, "rev-parse", "HEAD")))
        return real(cwd)

    monkeypatch.setattr(harvest_mod, "run_tests_detail", roots_on_the_commit)

    h = harvest(wt, ticket, run_tests=True)
    assert h.verdict == "READY"
    (target, head) = seen[0]
    assert target != str(wt.path)                    # not the agent's tree
    assert head == _git(wt.path, "rev-parse", "HEAD")  # the commit it scores
    assert not Path(target).exists()                 # the checkout is gone
    assert _registered_worktrees(repo) == before     # and git's record of it


def test_the_throwaway_checkout_is_gone_when_the_roots_raise(tmp_path, monkeypatch):
    """`finally`, not just the happy path: an exception out of the roots still
    drops the checkout, and the exception still propagates."""
    repo, base, ticket = _make_ignored_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    before = _registered_worktrees(repo)

    seen: list[str] = []

    def roots_that_die(cwd):
        seen.append(cwd)
        raise RuntimeError("boom-kc60")

    monkeypatch.setattr(harvest_mod, "run_tests_detail", roots_that_die)

    with pytest.raises(RuntimeError, match="boom-kc60"):
        harvest(wt, ticket, run_tests=True)
    assert seen and not Path(seen[0]).exists()
    assert _registered_worktrees(repo) == before


def test_uncommitted_files_names_at_most_ten_and_stays_non_blocking(tmp_path):
    """A worktree with a pile of leftovers: ten paths are named, the rest is a
    count, and the verdict is untouched."""
    repo, base, ticket = _make_ignored_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    for i in range(15):
        _write(wt.path / f"stray_{i:02d}.py", "x = 1\n")

    h = harvest(wt, ticket, run_tests=True)
    r = _reason(h, "uncommitted_files")
    assert r.blocking is False and h.verdict == "READY"
    assert r.text.count("?? stray_") == 10
    assert "(+5 more)" in r.text
    assert "15 worktree changes" in r.text


def test_run_tests_false_checks_out_nothing_and_names_nothing(tmp_path, monkeypatch):
    """The roots off: no checkout is made, no root is run, and `uncommitted_files`
    is not reported — the old path does not move."""
    repo, base, ticket = _make_ignored_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    _write(wt.path / "stray.py", "x = 1\n")
    before = _registered_worktrees(repo)

    def no_roots(cwd):
        raise AssertionError(f"run_tests=False must not run the roots in {cwd}")

    monkeypatch.setattr(harvest_mod, "run_tests_detail", no_roots)

    h = harvest(wt, ticket)
    assert h.verdict == "READY"
    assert h.facts["tests_run"] == "—"
    assert "uncommitted_files" not in _codes(h)
    assert _registered_worktrees(repo) == before


def test_harvest_reads_the_tree_without_rewriting_its_index(tmp_path):
    """`git status` refreshes a stale index as a side effect; the harvest's read
    of the tree must not. A tracked file whose mtime moved would make a plain
    `git status` rewrite the index — `--no-optional-locks` leaves it as it was."""
    repo, base, ticket = _make_ignored_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _record(wt, ticket.name, outcome="DONE", commit=_accepting(wt))
    index = Path(_git(wt.path, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    probe = wt.path / "tools" / "auto" / "probe.py"
    os.utime(probe, ns=(probe.stat().st_atime_ns, probe.stat().st_mtime_ns + 5_000_000_000))
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    harvest(wt, ticket, run_tests=True)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


@pytest.mark.parametrize("kind", ["loose", "absent"])
def test_harvest_runs_no_roots_outside_a_git_worktree(tmp_path, round_, kind, monkeypatch):
    """No repo to check out from: no root runs anywhere — not in the loose folder
    either — and the verdict says the tests could not run, rather than scoring
    roots that never ran. Nothing is reported that was not learned, nothing raises."""
    repo, base, ticket = round_
    path = tmp_path / kind
    if kind == "loose":
        path.mkdir()
    ws = Workspace(agent="a", path=path, branch="contest/01/a", base_sha=base,
                   kind="worktree")

    def no_roots(cwd):
        raise AssertionError(f"no checkout, so no root may run (asked for {cwd})")

    monkeypatch.setattr(harvest_mod, "run_tests_detail", no_roots)
    h = harvest(ws, ticket, run_tests=True)
    assert h.verdict == "REWORK"
    assert "commits_ne_1" in _codes(h)
    assert "not a git worktree" in _reason(h, "tests_failed").text
    assert h.facts["tests_run"] == "checkout✗"
    assert "uncommitted_files" not in _codes(h)


def test_a_checkout_that_cannot_be_made_is_not_a_pass(tmp_path, monkeypatch):
    """KC-60's hole must not reopen when git stumbles: if the commit cannot be
    checked out, the roots do not fall back to the agent's tree (which is green
    here only because of an untracked file) — the verdict is `tests_failed` with
    git's own words, and no directory or registration is left behind."""
    monkeypatch.setattr(gates_mod, "TEST_ROOTS", ["tests", ".smoke_fast"])
    repo, base, ticket = _make_tier_repo(tmp_path)
    wt = _worktree(repo, base, tmp_path)
    _edit(wt, "tools/auto/probe.py", "PROBE = 1\n")
    _edit(wt, "tests/test_probe.py", "def test_probe():\n    assert True\n")
    _record(wt, ticket.name, outcome="DONE", commit=_commit(wt, "probe, plus a new test"))
    _tier(wt, ".smoke_fast", "test_probe.py")      # the tree is green, the commit is not
    before = _registered_worktrees(repo)

    # `git worktree add` refuses a target that is already a non-empty folder.
    scratch = tmp_path / "scratch-kc60"
    (scratch / "commit").mkdir(parents=True)
    (scratch / "commit" / "occupied").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(harvest_mod.tempfile, "mkdtemp", lambda prefix="": str(scratch))

    ran: list[str] = []
    monkeypatch.setattr(harvest_mod, "run_tests_detail",
                        lambda cwd: ran.append(cwd) or ("tests:PASS", []))

    h = harvest(wt, ticket, run_tests=True)
    assert ran == []                                 # not in the agent's tree, not anywhere
    assert h.verdict == "REWORK"
    text = _reason(h, "tests_failed").text
    assert text.startswith(f"the tests could not run on commit {_git(wt.path, 'rev-parse', 'HEAD')[:12]}: ")
    assert "already exists" in text
    assert h.facts["tests_run"] == "checkout✗"
    assert not scratch.exists()
    assert _registered_worktrees(repo) == before
