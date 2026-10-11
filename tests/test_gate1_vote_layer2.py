"""tests/test_gate1_vote_layer2.py — CC-10 layer 2: the launch-and-forget script reads a --validate-plan report,
scores it against the known answers and says PASS or FAIL by the ticket's rule (no network, no model)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gate1_vote_layer2", ROOT / "scripts" / "gate1_vote_layer2.py")
l2 = importlib.util.module_from_spec(_spec)
sys.modules["gate1_vote_layer2"] = l2
_spec.loader.exec_module(l2)

REPORT = """Checked 53 task(s): 28 confirmed still needed, 25 removed as false positive(s).
  - AUTO-T1 (presence): The visible code shows `self.config_path = config_path` and a comment
  - AUTO-T2 (existence): the cited symbol is gone
Removed from IMPROVEMENTS.md and plan.json; recorded in IMPROVEMENTS-FALSE.md. Committed.
🗳 Gate 1 vote: asked 20, decided 9, overridden by the reader 2, requests 60
"""


def test_the_report_lines_are_read():
    assert l2.CHECKED.search(REPORT).groups() == ("53", "28", "25")
    removed = [m.groups() for m in map(l2.REMOVED.match, REPORT.splitlines()) if m]
    assert [r[0] for r in removed] == ["AUTO-T1", "AUTO-T2"] and removed[1][1] == "existence"
    assert l2.VOTE.search(REPORT).groups() == ("20", "9", "2", "60")


def test_sets_parse_with_the_default_rule_per_size():
    assert l2.parse_sets("3,5:all,7") == [(3, "all"), (5, "all"), (7, "committed")]
    with pytest.raises(SystemExit):
        l2.parse_sets("2")
    with pytest.raises(SystemExit):
        l2.parse_sets("99")


def _res(name, removed, decided=None, ok=True):
    r = {"variant": name, "rc": 0, "seconds": 5, "voters": [] if name == "off" else list("abc"), "accept": "all",
         "removed": {i: "x" for i in removed}, "ok": ok, "checked": 6, "removed_n": len(removed)}
    if decided is not None:
        r.update(votes_asked=6, votes_decided=decided, votes_overridden=0, votes_requests=18)
    return r


IDS = [f"AUTO-T{i}" for i in range(1, 7)]
TRUTH = {"AUTO-T1": "REAL", "AUTO-T2": "FALSE", "AUTO-T3": "FALSE", "AUTO-T4": "FALSE", "AUTO-T5": "FALSE", "AUTO-T6": "FALSE"}


def _verdicts(tmp_path, *results):
    return l2.summary(tmp_path, list(results), IDS, TRUTH, {})


def test_pass_when_the_vote_lets_through_fewer_false_positives(tmp_path):
    v = _verdicts(tmp_path, _res("off", []), _res("on-3", ["AUTO-T2", "AUTO-T3"], decided=3))
    assert v["on-3"] == "PASS"
    assert "| on-3 |" in (tmp_path / "SUMMARY.md").read_text()


def test_fail_when_the_vote_rejects_a_true_bug_the_baseline_kept(tmp_path):
    v = _verdicts(tmp_path, _res("off", []), _res("on-3", ["AUTO-T1"], decided=4))
    assert v["on-3"].startswith("FAIL")


def test_fail_when_it_lets_more_false_positives_through(tmp_path):
    v = _verdicts(tmp_path, _res("off", ["AUTO-T2"]), _res("on-3", [], decided=4))
    assert v["on-3"].startswith("FAIL")


def test_fail_when_it_decides_less_than_a_third(tmp_path):
    v = _verdicts(tmp_path, _res("off", []), _res("on-3", ["AUTO-T2"], decided=1))
    assert v["on-3"].startswith("FAIL")


def test_a_variant_that_could_not_run_is_an_error_not_a_pass(tmp_path):
    bad = {"variant": "on-5", "rc": 1, "seconds": 1, "voters": list("abcde"), "accept": "committed",
           "removed": {}, "ok": False}
    v = _verdicts(tmp_path, _res("off", []), bad)
    assert v["on-5"] == "ERROR"


def test_no_recorded_answers_means_no_verdict(tmp_path):
    v = l2.summary(tmp_path, [_res("off", []), _res("on-3", ["AUTO-T2"], decided=3)], IDS, {}, {})
    assert v["on-3"] == "no answers recorded"


def test_the_recorded_set_is_consistent():
    known = l2.KNOWN
    plan = json.loads((known / "plan.json").read_text())
    ids = {t["id"] for t in plan["tasks"]}
    truth = l2.load_truth(known)
    assert set(truth) == ids, "every candidate has an answer and every answer a candidate"
    assert set(truth.values()) == {"REAL", "FALSE"}
    assert len((known / "TARGET").read_text().split()[0]) == 40


def test_dry_run_lists_the_variants_and_runs_nothing(capsys):
    rc = l2.main(["--dry-run", "--sets", "3,5", "--local-ini", str(ROOT / "agents.ini"), "--reader", "x"])
    out = capsys.readouterr().out
    assert rc == 2 and "MISSING" in out            # no claim_vote_llm.* sections in agents.ini: refused, not run
    assert "off" in out and "on-5" in out


def test_the_generated_ini_caps_the_readers_request_time(tmp_path):
    import configparser
    local = configparser.ConfigParser(interpolation=None)
    for name in ("rd", "a", "b", "c"):
        local.add_section(l2.P + name)
        for k, v in (("base_url", "http://x/v1"), ("api_key", "k"), ("model", name)):
            local.set(l2.P + name, k, v)
    dest = tmp_path / "agents.ini"
    l2.build_ini(dest, local, ROOT / "agents.ini", "on", ["a", "b", "c"], "all", "rd")
    got = configparser.ConfigParser(interpolation=None)
    got.read(dest)
    assert got["loop"]["timeout_seconds"] == "240"
    assert got["gate1"]["presence_llm_profile"] == "gate1_llm" and got["gate1_llm"]["model"] == "rd"
    assert got["gate1"]["vote_profiles"] == "claim_vote_llm.a, claim_vote_llm.b, claim_vote_llm.c"
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
