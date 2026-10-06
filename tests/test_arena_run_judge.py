"""tests/test_arena_run_judge.py — AR-64: `arena run judge NN[.K]` sets the worktrees up and starts the cross judge."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.arena import cli, judging

INI = """[contest]
out_dir = contest-out

[contest.agent.a]
model = test/a
"""

BASE = "fc4ef1e8becb588fe8b857cd2a705999a25068a7"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(cwd: Path, *words: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                           *words], capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Recorder:
    """Stands in for `judging.RUN`: records every command line, answers with codes."""

    def __init__(self, codes=None):
        self.lines: list[list[str]] = []
        self.codes = list(codes or [])

    def __call__(self, line, **kwargs):
        self.lines.append([str(w) for w in line])
        code = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(line, code, stdout="", stderr="")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    _write(repo / "contest.ini", INI)
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(judging, "round_alive", lambda *_a, **_k: False)
    monkeypatch.setattr(judging, "LOADAVG", lambda: (0.1, 0.1, 0.1))
    return repo


@pytest.fixture
def rec(monkeypatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(judging, "RUN", rec)
    return rec


def _round(repo: Path, name: str = "184", base: str = BASE) -> Path:
    folder = repo / "contest-out" / name
    _write(folder / "entrants.json", json.dumps({"base": base, "entrants": {}}))
    return folder


def _judge(capsys, *argv):
    code = cli.main(["run", "judge", *argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_the_minimal_command_sets_up_the_trees_then_judges_with_the_cross_phase(repo, rec, capsys):
    folder = _round(repo)
    code, _, _ = _judge(capsys, "184")
    assert code == 0
    setup, judge = rec.lines
    assert setup[1].endswith("contest-bench/harness/setup_worktrees.py")
    assert setup[2:] == [str(folder / "entrants.json"), "--wt", str(folder / "wt"),
                         "--repo", str(repo)]
    assert judge[1].endswith("scripts/judge_epic_round.py")
    assert judge[2:] == ["--round", "184", "--base", BASE, "--runs", str(folder / "wt"),
                         "--cross", "--jobs", "1", "--cross-out", str(folder)]


def test_every_option_reaches_the_judge_and_the_passthrough_goes_last(repo, rec, capsys):
    folder = _round(repo)
    code, _, _ = _judge(capsys, "184", "--ideal", "a0b46f6", "--jobs", "2",
                        "--cell-timeout", "120")
    assert code == 0
    setup, judge = rec.lines
    assert setup[-2:] == ["--ideal", "a0b46f6"]
    assert judge[-9:] == ["--cross", "--jobs", "2", "--cross-out", str(folder),
                          "--ideal", "a0b46f6", "--cell-timeout", "120"]
    code = cli.main(["run", "judge", "184", "--no-cross", "--", "--tests", "--csv", "x.csv"])
    assert code == 0
    assert rec.lines[-1][-3:] == ["--tests", "--csv", "x.csv"]


def test_no_cross_is_the_score_table_alone(repo, rec, capsys):
    folder = _round(repo)
    (folder / "wt").mkdir()
    code, _, _ = _judge(capsys, "184", "--no-cross", "--jobs", "4", "--ideal", "x")
    assert code == 0
    (judge,) = [line for line in rec.lines if "judge_epic_round.py" in line[1]]
    assert "--cross" not in judge and "--jobs" not in judge and "--ideal" not in judge
    assert judge[-2:] == ["--runs", str(folder / "wt")]


def test_an_existing_wt_is_reused_and_a_missing_ideal_tree_is_set_up(repo, rec, capsys):
    folder = _round(repo)
    (folder / "wt" / "base").mkdir(parents=True)
    _judge(capsys, "184")
    assert len(rec.lines) == 1 and "judge_epic_round.py" in rec.lines[0][1]
    rec.lines.clear()
    _judge(capsys, "184", "--ideal", "abc123")
    assert "setup_worktrees.py" in rec.lines[0][1] and rec.lines[0][-2:] == ["--ideal", "abc123"]


def test_a_leg_is_judged_in_its_own_folder_with_the_round_number(repo, rec, capsys):
    folder = _round(repo, "184.2")
    code, _, _ = _judge(capsys, "184.2")
    assert code == 0
    assert rec.lines[-1][rec.lines[-1].index("--cross-out") + 1] == str(folder)
    assert rec.lines[-1][rec.lines[-1].index("--round") + 1] == "184"


def test_dry_run_prints_the_lines_and_runs_nothing(repo, rec, capsys):
    _round(repo)
    code, out, _ = _judge(capsys, "184", "--ideal", "a0b46f6", "--dry-run")
    assert code == 0 and rec.lines == []
    lines = out.splitlines()
    assert len(lines) == 2
    assert "setup_worktrees.py" in lines[0] and "judge_epic_round.py --round 184" in lines[1]


@pytest.mark.parametrize("argv, why", [
    (["x"], "'x' is not NN or NN.K"),
    (["184", "--jobs", "0"], "--jobs is 1 or more"),
])
def test_a_bad_argument_is_a_one_line_refusal(repo, rec, capsys, argv, why):
    _round(repo)
    code, _, err = _judge(capsys, *argv)
    assert code == 2 and why in err and rec.lines == []
    assert len(err.strip().splitlines()) == 1


def test_a_round_without_entrants_json_exits_1_and_says_where_it_looked(repo, rec, capsys):
    code, _, err = _judge(capsys, "184")
    assert code == 1 and "no entrants.json for round 184" in err and "contest-out" in err
    assert rec.lines == []


def test_a_running_round_is_refused(repo, rec, capsys, monkeypatch):
    _round(repo)
    monkeypatch.setattr(judging, "round_alive", lambda *_a, **_k: True)
    code, _, err = _judge(capsys, "184")
    assert code == 2 and "round 184 is running" in err and rec.lines == []


def test_an_entrants_file_without_a_base_is_refused(repo, rec, capsys):
    _write(repo / "contest-out" / "184" / "entrants.json", "{}")
    code, _, err = _judge(capsys, "184")
    assert code == 2 and "has no base" in err and rec.lines == []


def test_the_judges_exit_code_is_the_commands_and_a_failed_setup_stops_it(repo, monkeypatch, capsys):
    _round(repo)
    rec = Recorder(codes=[3])
    monkeypatch.setattr(judging, "RUN", rec)
    code, _, _ = _judge(capsys, "184")
    assert code == 3 and len(rec.lines) == 1
    rec = Recorder(codes=[0, 5])
    monkeypatch.setattr(judging, "RUN", rec)
    code, _, _ = _judge(capsys, "184")
    assert code == 5 and len(rec.lines) == 2


def test_a_loaded_box_gets_one_note_and_is_not_refused(repo, rec, capsys, monkeypatch):
    _round(repo)
    monkeypatch.setattr(judging, "LOADAVG", lambda: (99.0, 50.0, 20.0))
    monkeypatch.setattr(judging.os, "cpu_count", lambda: 8)
    code, _, err = _judge(capsys, "184")
    assert code == 0 and len(rec.lines) == 2
    assert err.count("\n") == 1 and "load 99.0 on 8 cores" in err


def test_a_quiet_box_prints_no_note(repo, rec, capsys):
    _round(repo)
    code, _, err = _judge(capsys, "184")
    assert code == 0 and err == ""


# ── `ideal` moves to the ref named now ───────────────────────────────────────
@pytest.fixture
def clones(tmp_path):
    """A repo with two commits and a `wt/ideal` detached at the first."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _write(main / "f.txt", "one")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "one")
    first = _git(main, "rev-parse", "HEAD")
    _write(main / "f.txt", "two")
    _git(main, "commit", "-qam", "two")
    second = _git(main, "rev-parse", "HEAD")
    folder = tmp_path / "round"
    _git(main, "worktree", "add", "-q", "--detach", str(folder / "wt" / "ideal"), first)
    return main, folder, first, second


def test_plan_ideal_move_names_the_commit_only_when_the_tree_is_elsewhere(clones):
    main, folder, first, second = clones
    assert judging.plan_ideal_move(main, folder, first) is None
    assert judging.plan_ideal_move(main, folder, second) == second
    assert judging.plan_ideal_move(main, folder, None) is None
    assert judging.plan_ideal_move(main, folder / "nowhere", second) is None
    with pytest.raises(judging.RoundError, match="not a commit"):
        judging.plan_ideal_move(main, folder, "no-such-ref")


def test_a_second_ideal_moves_the_existing_tree_before_the_judge_runs(repo, monkeypatch, capsys, clones):
    main, folder, first, second = clones
    _write(folder / "entrants.json", json.dumps({"base": BASE, "entrants": {}}))
    monkeypatch.setattr(judging, "round_folder", lambda *_a, **_k: folder)
    monkeypatch.setattr(judging, "load_config", lambda _r: None)
    monkeypatch.setattr(cli, "REPO_ROOT", main)
    rec = Recorder()
    monkeypatch.setattr(judging, "RUN", rec)
    code, out, _ = _judge(capsys, "184", "--ideal", second)
    assert code == 0
    assert rec.lines[0][:3] == ["git", "-C", str(folder / "wt" / "ideal")]
    assert rec.lines[0][-3:] == ["checkout", "-q", "--detach"] or rec.lines[0][-2] == "--detach"
    assert rec.lines[0][-1] == second
    assert "judge_epic_round.py" in rec.lines[1][1] and f"ideal -> {second[:12]}" in out
    # dry run reports the move and does it never
    rec.lines.clear()
    code, out, _ = _judge(capsys, "184", "--ideal", second, "--dry-run")
    assert code == 0 and rec.lines == [] and "checkout -q --detach" in out
