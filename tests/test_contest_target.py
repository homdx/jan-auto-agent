"""tests/test_contest_target.py — KC-76: `python3 -m tools.contest run --target REPO`.

The round's repo is `--target` when given, else the CWD. A relative `--roster`
is taken from the CWD (where the operator stands), never from the target. The
target's base must carry the two scripts the agents' prompt runs. No test
starts a real `kilo` or calls a live provider: the e2e test replays the fake.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_contest_cli import (  # noqa: E402
    ROUND, SCENARIO_ONE_READY, Sandbox, _git, _write, gate_key, run_fake, sandbox,  # noqa: F401
    spawn_holder,  # noqa: F401
)
from tools.contest import cli  # noqa: E402

pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")


def _commit_scripts(sb) -> None:
    for name in cli._TARGET_FILES:
        _write(sb.repo / name, "# stub\n")
    _git(sb.repo, "add", "-A")
    _git(sb.repo, "commit", "-q", "-m", "scripts")


# ── the pure helpers ─────────────────────────────────────────────────────────

def test_target_repo_is_the_flag_resolved_and_the_cwd_without_it(tmp_path, monkeypatch):
    other = tmp_path / "somewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert cli._target_repo(None) == other.resolve()
    assert cli._target_repo(str(tmp_path / "x" / "..")) == tmp_path.resolve()


def test_relative_roster_resolves_against_the_cwd_not_the_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "target"
    assert cli._roster_path(target, "contest.ini") == tmp_path / "contest.ini"
    assert cli._roster_path(target, "/abs/r.ini") == Path("/abs/r.ini")


def test_target_failures_name_what_the_base_lacks(sandbox):
    lines = cli._target_failures(sandbox.repo, "HEAD")
    assert len(lines) == 1
    assert "scripts/next_task.py" in lines[0] and "scripts/append_task.py" in lines[0]
    assert "epic-tasks" not in lines[0].split("lacks", 1)[1].split("—")[0]
    _commit_scripts(sandbox)
    assert cli._target_failures(sandbox.repo, "HEAD") == []


def test_target_failures_refuse_a_non_git_directory_and_a_missing_one(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "not a git repository" in cli._target_failures(plain, "HEAD")[0]
    assert "not a directory" in cli._target_failures(tmp_path / "nope", "HEAD")[0]


# ── the command ──────────────────────────────────────────────────────────────

def test_run_refuses_a_target_without_the_scripts(sandbox, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["run", "--ticket", "1", "--target", str(sandbox.repo),
                     "--roster", str(sandbox.repo / "contest.ini"), "--no-tests", "--no-gate"])
    err = capsys.readouterr().err
    assert code == 1
    assert "lacks scripts/next_task.py, scripts/append_task.py" in err
    assert not (sandbox.rounds / f"{ROUND:02d}-agent-a").exists()


def test_run_on_a_target_from_another_directory(sandbox, tmp_path, monkeypatch, spawn_holder, capsys):
    """CWD elsewhere with its own contest.ini: the roster comes from the CWD, the
    tickets, the clones' base and `out_dir` from the target."""
    _commit_scripts(sandbox)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "contest.ini").write_text(
        (sandbox.repo / "contest.ini").read_text(encoding="utf-8"), encoding="utf-8")
    (sandbox.repo / "contest.ini").unlink()          # the target has NO roster of its own
    _git(sandbox.repo, "add", "-A")
    _git(sandbox.repo, "commit", "-q", "-m", "no roster")
    monkeypatch.chdir(elsewhere)

    code, _fake = run_fake(sandbox, SCENARIO_ONE_READY,
                           ["--ticket", "1", "--target", str(sandbox.repo),
                            "--no-tests", "--no-gate"], spawn_holder)
    out = capsys.readouterr().out
    assert code == 0
    assert (sandbox.repo / "contest-out" / f"{ROUND:02d}" / "state.json").is_file()
    assert not (elsewhere / "contest-out").exists()
    assert (sandbox.rounds / f"{ROUND:02d}-agent-a").is_dir()
    assert "agent-a.patch" in out


def test_run_without_target_still_uses_the_cwd(sandbox, spawn_holder, capsys):
    """Regression: no flag, no change — the sandbox's cwd is the repo."""
    code, _fake = run_fake(sandbox, SCENARIO_ONE_READY,
                           ["--ticket", "1", "--no-tests", "--no-gate"], spawn_holder)
    assert code == 0
    assert (sandbox.repo / "contest-out" / f"{ROUND:02d}" / "state.json").is_file()
