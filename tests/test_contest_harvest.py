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
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
        "tests_failed",
    }


def test_blocking_reasons_stay_within_the_sentence_budget(round_, tmp_path):
    """Every reason is one sentence ≤ TEXT_LIMIT chars, `tests_failed` excepted."""
    repo, base, ticket = round_
    wt = _worktree(repo, base, tmp_path)
    _stage_probe(wt)
    _edit(wt, "tools/auto/other.py", "OTHER = 1\n")
    _commit(wt, "probe, plus other")
    _record(wt, ticket.name, outcome="SKIPPED", commit="")

    h = harvest(wt, ticket)
    assert len(_codes(h)) >= 3
    for r in h.reasons:
        if r.code == "tests_failed":
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
