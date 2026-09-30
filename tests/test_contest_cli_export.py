"""tests/test_contest_cli_export.py — KC-7: the round's folder is the one `contest-bench` reads.

The KC-16 harness comes over by import from `tests/test_contest_cli.py`: a temp
repo with a committed `epic-tasks/` and a two-agent roster, `KiloServer.spawn`
onto `FakeKiloServer`, so no `kilo` binary and no provider is ever touched. The
new surface is `tools/contest/export.py` — `write_entrants`, `write_summary`,
`round_patches`, `render_table` — reached through `cli.main`, the way an
operator reaches it: `run --dry-run`, `run --ticket NN`, `status --ticket NN`.

Every test here fails at `5d4d834`: `tools/contest/export.py` did not exist, and
neither did the `--dry-run` flag, the `status` subcommand, nor intake's
`**File:**` / `**Symbol:**` refusal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The KC-16 harness, reused: the sandbox, the fake, the helpers and the fixtures.
from test_contest_cli import (  # noqa: E402
    ROUND,
    SCENARIO_ONE_READY,
    TEST,
    TICKET_01,
    THING_CHANGED,
    _claim,
    _git,
    _one_ready_turn,
    _permission_outside,
    _plan,
    _table,
    _ticket,
    _write,
    Sandbox,
    gate_key,       # noqa: F401
    run_fake,
    sandbox,        # noqa: F401
    spawn_holder,   # noqa: F401
    work_no_test,
    work_ready,
)

from tools.contest import cli, export  # noqa: E402
from tools.contest.kilo_client import KiloServer  # noqa: E402
from tools.contest.runner import RoundState  # noqa: E402

# the intake tests start `kilo serve` (the stub) on the port
# kilo_client._free_port picked and let go of — see test_contest_cli.py
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

TICKET_01_BODY = _ticket("01", "first")
TICKET_01_BODY_QUEUED = _ticket("01", "first", "queued")


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _git_check(args, cwd=None) -> subprocess.CompletedProcess:
    """One subprocess, unchecked: the caller reads its exit code and its output."""
    return subprocess.run(args, cwd=str(cwd) if cwd is not None else None,
                          capture_output=True, text=True)


def _read_entrants(out: Path) -> dict:
    return json.loads((out / export.ENTRANTS_FILE).read_text(encoding="utf-8"))


def _resolve(repo: Path, entry: dict) -> Path:
    """Resolve one entrant's `source` the way `setup_worktrees.py` does: against the repo."""
    source = Path(entry["source"])
    return (repo / source).resolve() if not source.is_absolute() else source


def _table_lines(text: str) -> list:
    return [line for line in text.splitlines() if line.startswith("|")]


def _no_server(monkeypatch) -> None:
    """`KiloServer.spawn` / `.attach` and the gate client all raise: a dry run must
    not reach either."""
    def refuse(*args, **kwargs):
        raise AssertionError("a dry run must not start a server or ask the gate")

    monkeypatch.setattr(KiloServer, "spawn", staticmethod(refuse))
    monkeypatch.setattr(KiloServer, "attach", staticmethod(refuse))
    monkeypatch.setattr("tools.contest.policy.request_completion", refuse)
    monkeypatch.setattr("tools.llm_stream.request_completion", refuse)


#: The KC-16 ready/gave-up scenario plus one permission ask on the first turn:
#: the ask is recorded as a `gate-failed` decision, so SUMMARY's gate section
#: has a line to read.
SCENARIO_ONE_READY_AND_AN_ASK = {
    "turns": [
        {"on_prompt": _one_ready_turn, "events": ["busy", "idle"],
         "permission": _permission_outside("/var/lib/*")},
        {"on_prompt": work_no_test, "events": ["busy", "idle"]},
    ],
}


# ─────────────────────────────────────────────────────────────────────────────
# run --dry-run
# ─────────────────────────────────────────────────────────────────────────────

def test_dry_run_prints_the_plan_and_the_first_prompt_without_a_server(
        tmp_path, monkeypatch, capsys):
    """`--dry-run`: intake, the worktrees, the plan and the exact first prompt —
    then exit 0, with no `kilo serve`, no session and no gate call."""
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    _no_server(monkeypatch)

    code = cli.main(["run", "--ticket", "1", "--no-tests", "--dry-run"])
    captured = capsys.readouterr()

    assert code == 0
    skipped = [line for line in captured.out.splitlines() if line.startswith("dry-run: skipped")]
    assert len(skipped) == 3
    assert any("offer" in line for line in skipped)
    assert any("variant" in line for line in skipped)
    assert any("gate" in line for line in skipped)

    plan = _plan(captured.out)
    assert plan["ticket"].startswith(TICKET_01)
    assert plan["base"] == sb.base[:12]
    assert plan["gate"] == "test/gate @ 127.0.0.1"
    assert plan["out"] == str(sb.out())

    # the prompt the runner would send the first agent: the runbook's text, the
    # round's own base sha, and the agent's own scratch dir under `tmp_roots`
    assert "prompt (agent-a):" in captured.out
    assert "prompt (agent-b):" not in captured.out
    assert f"Your starting tree is commit {sb.base}" in captured.out
    assert "runs/agent-a/PROGRESS.csv" in captured.out
    assert "/nowhere/agent-a/" in captured.out

    # the worktrees are the round's own, and the round wrote nothing of its own
    assert (sb.rounds / "01-agent-a").is_dir()
    assert (sb.rounds / "01-agent-b").is_dir()
    assert not sb.out().exists()


def test_dry_run_refuses_a_worktree_it_does_not_own(tmp_path, monkeypatch, capsys):
    """A second `--dry-run` without `--fresh` meets the worktrees it left: the
    same `intake:` refusal as every other `WorkspaceError`, exit 1, no server.
    `--dry-run --fresh` is allowed and discards the work."""
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    _no_server(monkeypatch)

    assert cli.main(["run", "--ticket", "1", "--no-tests", "--dry-run"]) == 0
    capsys.readouterr()
    # uncommitted work, the way a crashed attempt would leave it
    _write(sb.rounds / "01-agent-a" / "pkg" / "thing.py", "def thing():\n    return 2\n")

    code = cli.main(["run", "--ticket", "1", "--no-tests", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 1
    assert "intake:" in captured.err
    assert "prompt (agent-a):" not in captured.out

    assert cli.main(["run", "--ticket", "1", "--no-tests", "--dry-run", "--fresh"]) == 0
    assert "prompt (agent-a):" in capsys.readouterr().out


def test_dry_run_still_runs_the_checks_that_need_no_server(tmp_path, monkeypatch, capsys):
    """The pure checks still run: a not-open ticket is refused before the plan
    prints, no worktree is created, and no server is started."""
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    _no_server(monkeypatch)
    sb.commit_ticket(TICKET_01, TICKET_01_BODY_QUEUED)

    code = cli.main(["run", "--ticket", "1", "--no-tests", "--dry-run"])
    captured = capsys.readouterr()
    assert code == 1
    assert "is not open" in captured.err
    assert "prompt (agent-a):" not in captured.out
    assert not (sb.rounds / "01-agent-a").exists()


# ─────────────────────────────────────────────────────────────────────────────
# intake: **File:** and **Symbol:**
# ─────────────────────────────────────────────────────────────────────────────

def test_intake_refuses_a_ticket_without_a_file_line(sandbox, capsys):
    sandbox.commit_ticket(TICKET_01, TICKET_01_BODY.replace("**File:** `pkg/thing.py`\n", ""))
    code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests"])
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert code == 1
    assert len(lines) == 1
    assert TICKET_01 in lines[0] and "**File:**" in lines[0]


def test_intake_refuses_a_ticket_without_a_symbol_line(sandbox, capsys):
    sandbox.commit_ticket(TICKET_01, TICKET_01_BODY.replace("**Symbol:** `thing`\n", ""))
    code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests"])
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert code == 1
    assert len(lines) == 1
    assert TICKET_01 in lines[0] and "**Symbol:**" in lines[0]


def test_intake_names_both_missing_fields_as_two_lines(sandbox, capsys):
    body = TICKET_01_BODY.replace("**File:** `pkg/thing.py`\n", "") \
                         .replace("**Symbol:** `thing`\n", "")
    sandbox.commit_ticket(TICKET_01, body)
    code = cli.main(["run", "--ticket", "1", "--no-gate", "--no-tests"])
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert code == 1
    assert len(lines) == 2
    assert any("has no **File:**" in line for line in lines)
    assert any("has no **Symbol:**" in line for line in lines)


def test_intake_reads_the_missing_field_from_the_base_not_the_checkout(tmp_path, monkeypatch,
                                                                       capsys):
    """`bare` has the ticket without `**Symbol:**` and this checkout has it: the
    sessions read the base tree, so `--base bare` refuses either way."""
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    _git(sb.repo, "checkout", "-q", "-b", "bare")
    sb.commit_ticket(TICKET_01, TICKET_01_BODY.replace("**Symbol:** `thing`\n", ""))
    _git(sb.repo, "checkout", "-q", "main")

    code = cli.main(["run", "--ticket", "1", "--base", "bare", "--no-gate", "--no-tests"])
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if line.startswith("intake:")]
    assert code == 1
    assert any(TICKET_01 in line and "**Symbol:**" in line for line in lines)


# ─────────────────────────────────────────────────────────────────────────────
# run: entrants.json and SUMMARY.md
# ─────────────────────────────────────────────────────────────────────────────

def test_a_run_writes_entrants_and_a_summary(sandbox, capsys, spawn_holder):
    """One READY and one GAVE_UP with a commit: exit 0, the two patches, an
    `entrants.json` `setup_worktrees.py` accepts, and a `SUMMARY.md` with both
    rows and a gate section."""
    code, fake = run_fake(sandbox, SCENARIO_ONE_READY_AND_AN_ASK,
                          ["--ticket", "1", "--no-gate", "--no-tests"], spawn_holder)
    captured = capsys.readouterr()
    out = sandbox.out()

    assert code == 0
    assert (out / "agent-a.patch").is_file()
    assert (out / "agent-b.GAVE_UP.patch").is_file()
    assert (out / export.ENTRANTS_FILE).is_file()
    assert (out / export.SUMMARY_FILE).is_file()
    assert f"entrants.json: {out / export.ENTRANTS_FILE}" in captured.out
    assert f"SUMMARY.md: {out / export.SUMMARY_FILE}" in captured.out

    config = _read_entrants(out)
    assert config["base"] == sandbox.base
    assert sorted(config["entrants"]) == ["agent-a", "agent-b"]
    assert config["entrants"]["agent-a"] == {"source": "contest-out/01/agent-a.patch"}
    assert config["entrants"]["agent-b"]["source"] == "contest-out/01/agent-b.GAVE_UP.patch"
    assert config["entrants"]["agent-b"]["state"] == "GAVE_UP"
    assert "state" not in config["entrants"]["agent-a"]

    # resolve every `source` against the repo root, as `setup_worktrees.py` does
    for name, entry in config["entrants"].items():
        assert not Path(entry["source"]).is_absolute(), name
        resolved = _resolve(sandbox.repo, entry)
        assert resolved.is_file(), name
        assert resolved.suffix in (".patch", ".diff")
        assert resolved == (out / resolved.name).resolve()

    summary = (out / export.SUMMARY_FILE).read_text(encoding="utf-8")
    assert f"- ticket: {TICKET_01}" in summary
    assert f"- base: {sandbox.base}" in summary
    assert "- gate: off" in summary
    assert "started:" in summary and "ended:" in summary and "wall:" in summary
    assert "## Decisions worth a look" in summary
    assert "gate-failed" in summary
    assert "rm -v /var/lib/x" in summary
    rows = [line.split("|")[1].strip() for line in _table_lines(summary)[2:]]
    assert rows == ["agent-a", "agent-b"]
    assert f"--worktree agent-a={sandbox.rounds / '01-agent-a'}" in summary
    assert "setup_worktrees.py contest-out/01/entrants.json" in summary
    assert f"judge_epic_round.py --round {ROUND} --base {sandbox.base}" in summary
    assert "contest_reset.sh" not in summary


def test_setup_worktrees_accepts_the_entrants_a_run_wrote(sandbox, spawn_holder):
    """`contest-bench/harness/setup_worktrees.py` on the round's own
    `entrants.json`: every entrant gets a worktree with its patch applied."""
    run_fake(sandbox, SCENARIO_ONE_READY_AND_AN_ASK,
             ["--ticket", "1", "--no-gate", "--no-tests"], spawn_holder)
    out = sandbox.out()
    wt = sandbox.tmp / "bench-wt"

    result = _git_check([sys.executable,
                         str(REPO_ROOT / "contest-bench" / "harness" / "setup_worktrees.py"),
                         str(out / export.ENTRANTS_FILE), "--wt", str(wt),
                         "--repo", str(sandbox.repo)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[base] " + sandbox.base in result.stdout
    assert "[agent-a] am OK" in result.stdout
    assert "[agent-b] am OK" in result.stdout

    assert _git(wt / "base", "rev-parse", "HEAD") == sandbox.base
    for name in ("agent-a", "agent-b"):
        assert _git(wt / name, "rev-parse", "HEAD") != sandbox.base


def test_a_stalled_agent_with_only_a_diff_is_an_entrant(sandbox, capsys, spawn_holder):
    """A `STALLED` turn that died with edits and no commit exports only a KC-31
    `.diff`, and that `.diff` is the entrant's `source`, with its state beside it."""
    ini = sandbox.repo / "contest.ini"
    # KC-41 off: otherwise the runner commits the work for agent-b and it is a
    # `.STALLED.patch`, not a `.diff`. KC-9: a silence under the deadline is a
    # `continue` now; this test is about the stall's export, so no budget.
    ini.write_text(ini.read_text(encoding="utf-8").replace(
        "idle_event_timeout_sec = 900",
        "idle_event_timeout_sec = 5\ndeadline_commit = false\nmax_continues_per_attempt = 0"),
        encoding="utf-8")

    def on_prompt(directory, text):
        if Path(directory).name.endswith("agent-a"):
            work_ready(directory, text)
        else:
            _write(Path(directory) / "pkg" / "stalled.py", "STALLED = True\n")

    scenario = {"turns": [{"on_prompt": on_prompt, "events": [], "idle": False}]}
    code, fake = run_fake(sandbox, scenario, ["--ticket", "1", "--no-gate", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()
    out = sandbox.out()

    assert code == 0
    assert [row["state"] for row in _table(captured.out)] == ["READY", "STALLED"]
    assert (out / "agent-a.patch").is_file()
    assert (out / "agent-b.STALLED.diff").is_file()
    assert not (out / "agent-b.patch").exists()
    assert not (out / "agent-a.diff").exists()

    config = _read_entrants(out)
    assert sorted(config["entrants"]) == ["agent-a", "agent-b"]
    entry = config["entrants"]["agent-b"]
    assert entry["state"] == "STALLED"
    resolved = _resolve(sandbox.repo, entry)
    assert resolved.suffix == ".diff" and resolved.is_file()
    assert "pkg/stalled.py" in resolved.read_text(encoding="utf-8")

    summary = (out / export.SUMMARY_FILE).read_text(encoding="utf-8")
    assert "agent-b.STALLED.diff" in summary


def _commit_at(directory: str, message: str, date: str) -> str:
    """The KC-16 commit, at a named date — so two agents who made the same change
    end up with two different commits, one per second."""
    _git(directory, "add", "-A")
    if _git(directory, "log", "--oneline", "HEAD").splitlines():
        args = ["commit", "-q", "--amend", "--no-edit"]
    else:
        args = ["commit", "-q", "-m", message]
    env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    result = subprocess.run(["git", *args], cwd=str(directory), env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return _git(directory, "rev-parse", "HEAD")


def _ship_at(directory: str, date: str) -> str:
    """The ready work — a change, a test, one commit, the claim — at *date*."""
    _write(Path(directory) / "pkg" / "thing.py", THING_CHANGED)
    _write(Path(directory) / "tests" / "test_thing.py", TEST)
    sha = _commit_at(directory, "KC-16: thing", date)
    _claim(directory, sha)
    return sha


def _both_ship_at_different_dates(directory, text):
    if Path(directory).name.endswith("agent-a"):
        return _ship_at(directory, "2026-09-27T07:00:00Z")
    return _ship_at(directory, "2026-09-27T08:00:00Z")


def test_two_identical_patches_mark_the_second_duplicate_of(sandbox, capsys, spawn_holder):
    """Two agents who made the same change produce patches that differ only in the
    `From <sha>` and `Date:` lines: the first keeps its `source`, the second is
    `duplicate_of` the first."""
    scenario = {"turns": [{"on_prompt": _both_ship_at_different_dates,
                           "events": ["busy", "idle"]}]}
    code, fake = run_fake(sandbox, scenario, ["--ticket", "1", "--no-gate", "--no-tests"],
                          spawn_holder)
    out = sandbox.out()
    assert code == 0

    first = (out / "agent-a.patch").read_text(encoding="utf-8")
    second = (out / "agent-b.patch").read_text(encoding="utf-8")
    assert first != second, "the two commits differ in their sha and their date"
    # ... and only in those two lines
    assert [line for line in first.splitlines() if not line.startswith(("From ", "Date: "))] \
        == [line for line in second.splitlines()
            if not line.startswith(("From ", "Date: "))]

    config = _read_entrants(out)
    assert config["entrants"]["agent-a"] == {"source": "contest-out/01/agent-a.patch"}
    assert config["entrants"]["agent-b"] == {"duplicate_of": "agent-a"}

    summary = (out / export.SUMMARY_FILE).read_text(encoding="utf-8")
    assert "agent-a.patch" in summary and "agent-b.patch" in summary


def test_the_ready_patch_applies_to_the_base_and_reproduces_the_tree(sandbox, spawn_holder):
    """`git am` of the READY agent's patch onto the base, in a fresh worktree:
    it applies cleanly and leaves the branch's own tree."""
    run_fake(sandbox, SCENARIO_ONE_READY_AND_AN_ASK,
             ["--ticket", "1", "--no-gate", "--no-tests"], spawn_holder)
    out = sandbox.out()
    worktree = sandbox.rounds / "01-agent-a"
    fresh = sandbox.tmp / "am"
    fresh.mkdir()
    _git(fresh, "init", "-q", "-b", "main")
    _git(fresh, "config", "user.email", "am@example.invalid")
    _git(fresh, "config", "user.name", "am")
    _git(fresh, "fetch", "-q", str(sandbox.repo), f"{sandbox.base}:base")
    _git(fresh, "checkout", "-q", "base")

    result = _git_check(["git", "am", "-q", str(out / "agent-a.patch")], cwd=str(fresh))
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(fresh, "rev-parse", "HEAD^{tree}") == _git(worktree, "rev-parse", "HEAD^{tree}")


def test_a_round_with_no_patch_writes_a_summary_and_no_entrants(sandbox, capsys, spawn_holder):
    """Every agent gives up on a clean tree: no patch, no `.diff`, so no
    `entrants.json` — nothing to point the bench at — but still a `SUMMARY.md`,
    the round the operator most needs to read, without the bench line."""
    def nothing(directory, text):
        return None

    scenario = {"turns": [{"on_prompt": nothing, "events": ["busy", "idle"]},
                          {"on_prompt": nothing, "events": ["busy", "idle"]}]}
    code, fake = run_fake(sandbox, scenario, ["--ticket", "1", "--no-gate", "--no-tests"],
                          spawn_holder)
    captured = capsys.readouterr()
    out = sandbox.out()

    assert code == cli.EXIT_NO_READY
    assert not (out / export.ENTRANTS_FILE).exists()
    assert "entrants.json:" not in captured.out
    assert f"SUMMARY.md: {out / export.SUMMARY_FILE}" in captured.out
    summary = (out / export.SUMMARY_FILE).read_text(encoding="utf-8")
    rows = [line.split("|")[1].strip() for line in _table_lines(summary)[2:]]
    assert rows == ["agent-a", "agent-b"]
    assert "setup_worktrees.py" not in summary
    assert "judge_epic_round.py" in summary


def test_the_summary_names_the_rounds_own_ticket_and_an_edits_path(sandbox, capsys,
                                                                   spawn_holder):
    """The header is the round's ticket, never a fixed id, and a gate decision
    on an edit — no `command`, only `patterns` — names the file it was about."""
    run_fake(sandbox, SCENARIO_ONE_READY, ["--ticket", "1", "--no-gate", "--no-tests"],
             spawn_holder)
    capsys.readouterr()
    out = sandbox.out()
    decision = {"t": 1.0, "permission": "edit", "patterns": ["AGENTS.md"], "command": "",
                "layer": "gate-failed", "reply": "reject",
                "reason": "gate unavailable: empty reply (2 attempts)"}
    with (out / "agent-a" / "decisions.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(decision) + "\n")
    state = RoundState.from_dict(json.loads((out / "state.json").read_text(encoding="utf-8")))

    summary = export.write_summary(out, state, state.base_sha,
                                   export.round_patches(out, state)).read_text(encoding="utf-8")

    assert summary.splitlines()[0] == f"# Round {ROUND:02d}: {TICKET_01}"
    assert "KC-7" not in summary
    assert "- agent-a: gate-failed — reject — `AGENTS.md` — gate unavailable" in summary


# ─────────────────────────────────────────────────────────────────────────────
# status
# ─────────────────────────────────────────────────────────────────────────────

def test_status_prints_the_same_table_as_the_summary(sandbox, capsys, spawn_holder):
    run_fake(sandbox, SCENARIO_ONE_READY_AND_AN_ASK,
             ["--ticket", "1", "--no-gate", "--no-tests"], spawn_holder)
    out = sandbox.out()
    summary = (out / export.SUMMARY_FILE).read_text(encoding="utf-8")

    code = cli.main(["status", "--ticket", "1"])
    captured = capsys.readouterr()

    assert code == 0
    lines = _table_lines(captured.out)
    assert len(lines) == 4, "the header, the rule and one row per agent"
    assert _table_lines(summary) == lines
    assert captured.err == ""


def test_status_reads_an_explicit_out_dir(tmp_path, monkeypatch, capsys, spawn_holder):
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    run_fake(sb, SCENARIO_ONE_READY_AND_AN_ASK,
             ["--ticket", "1", "--no-gate", "--no-tests", "--out", str(sb.tmp / "elsewhere")],
             spawn_holder)
    capsys.readouterr()

    code = cli.main(["status", "--ticket", "1", "--out", str(sb.tmp / "elsewhere")])
    captured = capsys.readouterr()
    assert code == 0
    assert len(_table_lines(captured.out)) == 4
    # the default folder is not the one the round wrote to, and says so
    assert not (sb.out() / "state.json").exists()


def test_status_exits_one_without_a_state(tmp_path, monkeypatch, capsys):
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    code = cli.main(["status", "--ticket", "9"])
    captured = capsys.readouterr()
    assert code == 1
    assert len(captured.err.splitlines()) == 1
    assert "state.json" in captured.err and "no" in captured.err
    assert captured.out == ""


def test_status_names_an_unreadable_state(tmp_path, monkeypatch, capsys):
    sb = Sandbox(tmp_path)
    monkeypatch.chdir(sb.repo)
    _write(sb.out() / "state.json", "{ not json")
    code = cli.main(["status", "--ticket", "1"])
    captured = capsys.readouterr()
    assert code == 1
    assert "unreadable" in captured.err
    assert captured.out == ""


def test_status_help_lists_the_rounds_flags(capsys):
    with pytest.raises(SystemExit) as raised:
        cli._parser().parse_args(["status", "--help"])
    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "--ticket" in help_text and "--out" in help_text
    assert "state.json" in help_text
