"""Ticket 212: the cache gate — a recorded run is served, a changed tree or an effectful command is not, and every failure runs the command."""

import subprocess
import time

import pytest

from tools.contest import roster, testcache
from tools.contest.testcache_glue import TestCacheGate, only_pytest_and_harmless, run_key

OUT = "........ [100%]\n7262 passed, 52 skipped in 94.31s (0:01:34)\n"


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def _ask(cmd, call="call_1"):
    return {"permission": "bash", "metadata": {"command": cmd}, "tool": {"callID": call}}


def _part(call, output=OUT, status="completed"):
    return {"tool": "bash", "callID": call,
            "state": {"status": status, "output": output, "time": {"start": 1000, "end": 95000}}}


def test_a_recorded_run_is_served_to_the_same_and_to_another_spelling(tmp_path):
    wt = _repo(tmp_path / "wt")
    gate = TestCacheGate(tmp_path / "c.jsonl", agent="a")
    assert gate.ask(_ask("python3 -m pytest .smoke_tests/ .regression_tests/ -q -n 4 2>&1 | tail -20"), wt) is None
    assert gate.harvest([_part("call_1")]) == 1
    hit = gate.ask(_ask("time python3 -m pytest tests -q -n 1 | tail -5", "call_2"), wt)
    assert hit.startswith("cached: ") and "7262 passed" in hit and "\n" not in hit


def test_a_changed_tree_misses(tmp_path):
    wt = _repo(tmp_path / "wt")
    gate = TestCacheGate(tmp_path / "c.jsonl", agent="a")
    gate.ask(_ask("pytest tests -q"), wt)
    gate.harvest([_part("call_1")])
    (wt / "a.py").write_text("x = 2\n")
    assert gate.ask(_ask("pytest tests -q", "call_2"), wt) is None


def test_other_flags_that_change_what_runs_miss(tmp_path):
    wt = _repo(tmp_path / "wt")
    gate = TestCacheGate(tmp_path / "c.jsonl", agent="a")
    gate.ask(_ask("pytest tests -q"), wt)
    gate.harvest([_part("call_1")])
    assert gate.ask(_ask("pytest tests -q -x", "call_2"), wt) is None


def test_share_round_serves_another_agent_share_agent_does_not(tmp_path):
    wt = _repo(tmp_path / "wt")
    first = TestCacheGate(tmp_path / "c.jsonl", agent="a")
    first.ask(_ask("pytest tests -q"), wt)
    first.harvest([_part("call_1")])
    assert TestCacheGate(tmp_path / "c.jsonl", agent="b").ask(_ask("pytest tests -q"), wt)
    assert TestCacheGate(tmp_path / "c.jsonl", agent="b", share="agent").ask(_ask("pytest tests -q"), wt) is None


def test_a_killed_or_summaryless_run_is_not_recorded(tmp_path):
    wt = _repo(tmp_path / "wt")
    gate = TestCacheGate(tmp_path / "c.jsonl", agent="a")
    gate.ask(_ask("pytest tests -q"), wt)
    killed = "....\n<shell_metadata>terminated command after exceeding timeout 120000 ms</shell_metadata>"
    assert gate.harvest([_part("call_1", killed)]) == 0
    assert gate.ask(_ask("pytest tests -q", "call_2"), wt) is None


@pytest.mark.parametrize("cmd", ["pytest tests && rm -rf build", "make test", "git push && pytest tests",
                                 "pytest tests; python3 other.py"])
def test_a_command_with_another_effect_is_never_answered_from_the_cache(tmp_path, cmd):
    wt = _repo(tmp_path / "wt")
    gate = TestCacheGate(tmp_path / "c.jsonl", agent="a")
    gate.cache.record(run_key(testcache.PytestRun(frozenset({"tests"}), frozenset())),
                      __import__("tools.contest.testcache_store", fromlist=["x"]).tree_fingerprint(wt),
                      {"passed": 1, "failed": 0, "errors": 0, "skipped": 0, "duration_s": 1.0,
                       "failed_names": [], "raw_line": "1 passed"}, None, 1.0, "a")
    assert gate.ask(_ask(cmd), wt) is None


@pytest.mark.parametrize("cmd,ok", [
    ("cd /w && { time (python3 -m pytest tests -q -n 4 && python3 -m pytest tests_bugfix -q); } 2>&1 | tail -30", True),
    ("T0=$(date +%s); python3 -m pytest tests; echo wall=$(date +%s)", True),
    ("/usr/bin/time -v python3 -m pytest tests 2>&1 | tail -30", True),
    ("ls; rm -rf x", False)])
def test_only_pytest_and_harmless(cmd, ok):
    assert only_pytest_and_harmless(cmd) is ok


def test_the_llm_fallback_only_parses_and_a_failure_runs_the_command(tmp_path):
    wt = _repo(tmp_path / "wt")
    def broken(_cmd):
        raise RuntimeError("overloaded")
    gate = TestCacheGate(tmp_path / "c.jsonl", agent="a", classify=broken)
    assert gate.ask(_ask("bash run_tests.sh"), wt) is None
    asked = []
    def good(cmd):
        asked.append(cmd)
        return testcache.PytestRun(frozenset({"tests"}), frozenset())
    gate2 = TestCacheGate(tmp_path / "c2.jsonl", agent="a", classify=good)
    gate2.ask(_ask("bash run_tests.sh"), wt)
    gate2.ask(_ask("bash run_tests.sh", "call_2"), wt)
    assert asked == ["bash run_tests.sh"]   # remembered: asked once


def test_the_config_keys_default_off_and_refuse_a_bad_share(tmp_path):
    ini = tmp_path / "contest.ini"
    ini.write_text("[contest]\ntest_cache = on\ntest_cache_share = agent\ntest_cache_llm = off\n[contest.agent.a]\nmodel = kenary/agnes-2-5-flash:free\n")
    cfg = roster.load_roster(ini)
    assert cfg.test_cache is True and cfg.test_cache_share == "agent" and cfg.test_cache_llm is False
    ini.write_text("[contest]\n[contest.agent.a]\nmodel = kenary/agnes-2-5-flash:free\n")
    cfg = roster.load_roster(ini)
    assert cfg.test_cache is False and cfg.test_cache_share == "round" and cfg.test_cache_llm is True
    ini.write_text("[contest]\ntest_cache_share = everyone\n[contest.agent.a]\nmodel = kenary/agnes-2-5-flash:free\n")
    with pytest.raises(roster.RosterError):
        roster.load_roster(ini)
