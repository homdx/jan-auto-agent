"""Regression guards for AGNES-4 (round 343): the operator tools' own inputs.

Every case here failed on 4526a0a: a nested ticket listed with an empty body,
`_winners` raising on one bad row, `checks` crashing without `tests/test_calc.py`,
`main` crashing on a missing target, and `run_all.py` refusing to rerun over a
truncated `results.json`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.contest import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
TICKET = "# {title}\n\n**Status:** open\n\n**File:** x.py\n**Symbol:** y\n"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def nested_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "epic-tasks" / "sub").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    body = TICKET.format(title="Nested body") + "\nunique-nested-marker\n"
    (repo / "epic-tasks" / "sub" / "007-fix.md").write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "nested ticket")
    return repo, body


def test_tickets_reads_a_nested_ticket_body_from_the_base(nested_repo):
    repo, body = nested_repo
    tasks = repo / "epic-tasks"
    rows = cli._tickets(tasks, at="HEAD")
    assert [(number, path, status) for number, path, status, _body in rows] == [
        (7, tasks / "sub" / "007-fix.md", "open")]
    assert rows[0][3] == body.rstrip("\n")
    assert "unique-nested-marker" in rows[0][3]


def test_tickets_stays_flat_without_a_base(nested_repo):
    repo, _body = nested_repo
    assert cli._tickets(repo / "epic-tasks") == []


def _two_legs_module():
    return _load(REPO_ROOT / "2legs" / "check_2legs.py", "check_2legs_343")


class _Done:
    returncode = 1
    stdout = ""
    stderr = "stub failure\n"


def test_winners_keeps_valid_rows_and_reports_each_malformed_one(tmp_path, monkeypatch):
    mod = _two_legs_module()
    out = tmp_path / "contest-out"
    out.mkdir()
    (out / "winners.txt").write_text(
        "1 agent1 deadbeef\n"
        "\n"
        "2 agent2\n"
        "three agent3 deadbeef\n", encoding="utf-8")
    winners, malformed = mod._winners(tmp_path)
    assert winners == {1: ("agent1", "deadbeef")}
    assert len(malformed) == 3

    monkeypatch.setattr(mod, "_run", lambda cmd, cwd: _Done())
    # The report carries the malformed rows as FAIL lines, not a crash.
    rows = mod.checks(tmp_path)
    fail_lines = [msg for ok, msg in rows if not ok and "winners.txt" in msg]
    assert len(fail_lines) == 3


def test_checks_without_the_calc_test_reports_a_failing_row(tmp_path, monkeypatch):
    mod = _two_legs_module()
    monkeypatch.setattr(mod, "_run", lambda cmd, cwd: _Done())
    rows = mod.checks(tmp_path)
    assert any(not ok and "tests/test_calc.py" in msg for ok, msg in rows)


def test_main_refuses_a_missing_target_before_any_check(tmp_path, monkeypatch, capsys):
    mod = _two_legs_module()
    called = []
    monkeypatch.setattr(mod, "checks", lambda target: called.append(target) or [])
    rc = mod.main([str(tmp_path / "no-such-target")])
    err = capsys.readouterr().err
    assert rc != 0
    assert called == []
    assert "no such directory" in err
    assert "check_2legs.py" in err


def test_run_all_reruns_over_a_corrupt_results_file(tmp_path, monkeypatch):
    mod = _load(REPO_ROOT / "contest-bench" / "harness" / "run_all.py", "run_all_343")
    wt = tmp_path / "wt"
    (wt / "entrant").mkdir(parents=True)
    scenarios = tmp_path / "scenarios.py"
    scenarios.write_text("SCENARIOS = {}\n", encoding="utf-8")
    out = tmp_path / "results.json"
    out.write_text('{"entrant": {"S0_static": ', encoding="utf-8")

    class _Proc:
        stdout = '@@RESULT@@{"scenario": "S0_static", "worktree": "entrant", "checks": []}'
        stderr = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda cmd, **kw: _Proc())
    monkeypatch.setattr(sys, "argv", ["run_all.py", "--wt", str(wt),
                                      "--scenarios", str(scenarios), "--out", str(out)])
    mod.main()

    assert json.loads(out.read_text()) == {
        "entrant": {"S0_static": {"scenario": "S0_static", "worktree": "entrant",
                                  "checks": []}}}
    corrupt = out.with_name(out.name + ".corrupt")
    assert corrupt.read_text() == '{"entrant": {"S0_static": '
    assert not out.with_name(out.name + ".tmp").exists()

# ── What a narrow fix misses ─────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent.parent
_ROW = "| {} | a.py:1 | why |\n"


@pytest.mark.parametrize("rel", ["check_improvements.py", "stub-test/check_improvements.py"])
def test_only_a_heading_opens_a_false_positive_section(rel):
    """The words in a REAL section's prose do not make its rows false positives."""
    mod = _load(_ROOT / rel, "ci_prose_" + rel.replace("/", "_"))
    prose = "## Confirmed REAL\nThis is not a FALSE POSITIVE at all.\n" + _ROW.format("T9") + "## Other\n" + _ROW.format("T8")
    assert mod._parse_ground_truth_table(prose) == []
    between = "## FALSE POSITIVE a\n" + _ROW.format("T1") + "## Confirmed REAL\n" + _ROW.format("T9") + "## FALSE POSITIVE b\n" + _ROW.format("T2")
    assert [t["id"] for t in mod._parse_ground_truth_table(between)] == ["AUTO-T1", "AUTO-T2"]


def _run_all(tmp_path, *entries):
    wt = tmp_path / "wt"
    wt.mkdir(exist_ok=True)
    for name in entries:
        (wt / name).mkdir(exist_ok=True)
    scenarios = tmp_path / "scen.py"
    scenarios.write_text("SCENARIOS = {}\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_ROOT / "contest-bench/harness/run_all.py"), "--wt", str(wt),
         "--scenarios", str(scenarios), "--out", str(tmp_path / "results.json"), "--timeout", "20"],
        capture_output=True, text=True, timeout=60)


def _aside(tmp_path) -> list:
    return sorted(p.read_bytes() for p in tmp_path.glob("results.json.corrupt*"))


def test_every_damaged_results_file_is_kept_and_none_overwrites_another(tmp_path):
    damaged = [b'{"first"', b"[1, 2]", b"\xff\xfe{}", b"[3]"]
    for raw in damaged:
        (tmp_path / "results.json").write_bytes(raw)
        assert _run_all(tmp_path).returncode == 0
    assert _aside(tmp_path) == sorted(damaged)


def test_a_good_results_file_is_merged_not_moved(tmp_path):
    (tmp_path / "results.json").write_text(json.dumps({"old": {"S": {"checks": []}}}), encoding="utf-8")
    assert _run_all(tmp_path, "e1").returncode == 0
    assert set(json.loads((tmp_path / "results.json").read_text())) == {"old", "e1"}
    assert _aside(tmp_path) == []


def test_a_run_after_damage_still_records_its_entries(tmp_path):
    (tmp_path / "results.json").write_bytes(b'{"a"')
    assert _run_all(tmp_path, "e1").returncode == 0
    assert "e1" in json.loads((tmp_path / "results.json").read_text())


def test_an_undecodable_calc_test_is_a_failing_row(tmp_path, monkeypatch):
    mod = _two_legs_module()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_bytes(b"\xff\xfe\x00")
    monkeypatch.setattr(mod, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    assert any(not ok and "test_calc" in msg for ok, msg in mod.checks(tmp_path))
