"""tests/test_gate1_vote_layer2_review.py — CC-10 layer 2 after the review of 2026-10-12: a candidate Gate 1
could not judge is out of the comparison (not a false positive let through), --resume runs a failed variant
again, no verdict is not exit 0, the progress line says the verdict, the variant ini is created 0600, the
sets are checked, a timeout keeps its time, and the baseline's runs come from contest.ini.
No network, no model: run_variant's child is stubbed where a run is needed.
"""

from __future__ import annotations

import builtins
import configparser
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gate1_vote_layer2_review", ROOT / "scripts" / "gate1_vote_layer2.py")
l2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(l2)

IDS = [f"AUTO-T{i}" for i in range(1, 7)]
TRUTH = {"AUTO-T1": "REAL", **{f"AUTO-T{i}": "FALSE" for i in range(2, 7)}}
TITLES = {i: f"fix the thing number {i[-1]}" for i in IDS}

# what plan_validator and Gate 1 print, as they print it
LOG = """2026-10-12 10:00:01,000 [WARNING] Gate1[presence] UNKNOWN 'fix the thing number 5' — presence unknown — empty (transport)
2026-10-12 10:00:01,000 [INFO] Gate1[presence] KEPT (presence_unknown = keep) 'fix the thing number 5' — presence unknown — empty (transport)
🗳 Gate 1 vote: asked 6, decided 2, overridden by the reader 0, requests 18, skipped 1
Checked 6 task(s): 3 confirmed still needed, 1 removed as false positive(s), 2 left unresolved (technical failure).
  - AUTO-T2 (presence): the file is closed by the caller
Removed from IMPROVEMENTS.md and plan.json; recorded in IMPROVEMENTS-FALSE.md. Committed.
⚠️  2 task(s) could not be checked due to a technical failure (network/provider error surviving every retry) and were left unchanged in plan.json:
  - AUTO-T3: LLM call failed: HTTP 503 (after 2 retries)
  - AUTO-T4: LLM call failed: TimeoutError (after 2 retries)
Re-run --validate-plan to retry just these.
"""


# ── №27: what Gate 1 could not judge ─────────────────────────────────────────────────────────

def test_the_report_names_the_unresolved_candidates():
    res = l2.read_report(LOG, TITLES)
    assert (res["checked"], res["kept_n"], res["removed_n"], res["unresolved_n"]) == (6, 3, 1, 2)
    assert list(res["removed"]) == ["AUTO-T2"]
    assert res["inconclusive"] == ["AUTO-T3", "AUTO-T4", "AUTO-T5"]      # T5: kept by presence_unknown = keep
    assert (res["votes_asked"], res["votes_decided"], res["votes_skipped"]) == (6, 2, 1)


def _res(name, removed, inconclusive=(), decided=3):
    r = {"variant": name, "rc": 0, "seconds": 5, "voters": [] if name.startswith("off") else list("abc"),
         "accept": "all", "removed": {i: "x" for i in removed}, "inconclusive": list(inconclusive), "ok": True,
         "checked": 6, "removed_n": len(removed)}
    if not name.startswith("off"):
        r.update(votes_asked=6, votes_decided=decided, votes_overridden=0, votes_requests=18)
    return r


def test_a_candidate_the_variant_could_not_judge_is_not_its_false_positive(tmp_path):
    """The baseline judged T2 and removed it; the vote run's reader never answered on T2. Counted as kept,
    T2 was the vote's "false positive let through" and the variant failed for a provider's outage."""
    v = l2.summary(tmp_path, [_res("off", ["AUTO-T2"]), _res("on-3", [], inconclusive=["AUTO-T2"])], IDS, TRUTH, TITLES)
    assert v["on-3"].startswith("PASS") and "on 5 of 6 candidates, 1 unresolved" in v["on-3"]
    text = (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")
    assert "| AUTO-T2 | FALSE | R | ? |" in text


def test_the_baselines_unresolved_are_out_of_both_sides(tmp_path):
    v = l2.summary(tmp_path, [_res("off", [], inconclusive=["AUTO-T3"]), _res("on-3", ["AUTO-T4"])], IDS, TRUTH, TITLES)
    assert v["on-3"].startswith("PASS") and "1 unresolved" in v["on-3"]


def test_a_variant_that_is_worse_on_what_both_judged_still_fails(tmp_path):
    v = l2.summary(tmp_path, [_res("off", ["AUTO-T2", "AUTO-T3"]), _res("on-3", ["AUTO-T2"], inconclusive=["AUTO-T6"])],
                   IDS, TRUTH, TITLES)
    assert v["on-3"].startswith("FAIL")


# ── №29, №30, №31: --resume, the exit code, the progress line ────────────────────────────────

@pytest.fixture
def local_ini(tmp_path):
    """The voters' and the reader's sections, with a BOM (Notepad)."""
    return _local(tmp_path / "contest.local.ini", bom=True)


@pytest.fixture
def plain_ini(tmp_path):
    """The same sections without a BOM: a test of another fix does not fail on the BOM's."""
    return _local(tmp_path / "plain.local.ini", bom=False)


def _local(path, bom):
    path.write_text(("\ufeff" if bom else "") + "".join(
        f"[{l2.P}{name}]\nbase_url = http://127.0.0.1:9/v1\napi_key = k\nmodel = {name}\n"
        for name in dict.fromkeys(l2.ROSTER + [l2.READER])), encoding="utf-8")
    return path


def _stub(monkeypatch, calls):
    def fake(out, name, known, repo, sha, base_ini, local, voters, accept, reader, timeout, opts=(), titles=None):
        calls.append(name)
        ids = [t["id"] for t in json.loads((known / "plan.json").read_text(encoding="utf-8"))["tasks"]]
        res = {"variant": name, "rc": 0, "opts": list(opts), "seconds": 1, "voters": voters, "accept": accept,
               "removed": {}, "inconclusive": [], "ok": True, "checked": len(ids), "removed_n": 0}
        if voters:
            res.update(votes_asked=len(ids), votes_decided=len(ids), votes_overridden=0, votes_requests=3 * len(ids))
        (out / name).mkdir(parents=True, exist_ok=True)
        (out / name / "result.json").write_text(json.dumps(res), encoding="utf-8")
        return res
    monkeypatch.setattr(l2, "run_variant", fake)


def test_resume_runs_a_failed_variant_again(tmp_path, monkeypatch, plain_ini):
    out = tmp_path / "run"
    for name, ok in (("off", True), ("off-2", True), ("on-3", False)):
        (out / name).mkdir(parents=True)
        (out / name / "result.json").write_text(json.dumps(
            {"variant": name, "rc": 0 if ok else 1, "seconds": 9, "voters": [], "accept": "all", "removed": {},
             "inconclusive": [], "ok": ok, "checked": 56}), encoding="utf-8")
    calls = []
    _stub(monkeypatch, calls)
    rc = l2.main(["--resume", str(out), "--sets", "3", "--local-ini", str(plain_ini), "--off-runs", "2"])
    assert calls == ["on-3"]
    assert "on-3: failed before (rc=1) — running it again" in (out / "progress.log").read_text(encoding="utf-8")
    assert rc in (0, 1)                                    # it ran: a verdict, not "could not run"


def test_no_recorded_answers_is_exit_3_and_the_progress_line_says_why(tmp_path, monkeypatch, plain_ini):
    known = tmp_path / "known"
    known.mkdir()
    for name in ("plan.json", "TARGET"):
        shutil.copy(l2.KNOWN / name, known / name)             # no truth.csv
    calls = []
    _stub(monkeypatch, calls)
    out = tmp_path / "run"
    rc = l2.main(["--known", str(known), "--sets", "3", "--local-ini", str(plain_ini), "--out", str(out),
                  "--off-runs", "1"])
    assert rc == 3 and calls == ["off", "on-3"]
    assert "on-3 = no answers recorded" in (out / "progress.log").read_text(encoding="utf-8")


# ── №32: the variant's ini is created 0600 ───────────────────────────────────────────────────

def test_the_variant_ini_is_never_opened_with_the_default_mode(tmp_path, monkeypatch, local_ini):
    local = configparser.ConfigParser(interpolation=None)
    local.read(local_ini, encoding="utf-8-sig")
    dest = tmp_path / "agents.ini"
    real_open = builtins.open
    seen = []

    def watch(file, mode="r", *a, **k):
        if str(file) == str(dest) and "w" in mode:
            seen.append(mode)
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", watch)
    l2.build_ini(dest, local, ROOT / "agents.ini", "on", l2.ROSTER[:3], "all", l2.READER)
    assert seen == [] and oct(dest.stat().st_mode & 0o777) == "0o600"
    dest.chmod(0o644)
    l2.build_ini(dest, local, ROOT / "agents.ini", "on", l2.ROSTER[:3], "all", l2.READER)
    assert oct(dest.stat().st_mode & 0o777) == "0o600"


# ── №33: the sets, the reader, a timeout ─────────────────────────────────────────────────────

@pytest.mark.parametrize("sets, why", [("x", "the size is a number"), ("5,5", "on-5 is named twice"),
                                       ("3+veto,3:all+veto", "on-3-veto is named twice")])
def test_a_bad_set_list_is_a_clean_refusal(sets, why):
    with pytest.raises(SystemExit) as exc:
        l2.parse_sets(sets)
    assert why in str(exc.value)


def test_the_reader_is_checked_with_no_vote_set(tmp_path, capsys):
    empty = tmp_path / "local.ini"
    empty.write_text("", encoding="utf-8")
    rc = l2.main(["--dry-run", "--sets", "", "--local-ini", str(empty)])
    assert rc == 2 and f"{l2.P}{l2.READER}" in capsys.readouterr().out


def test_a_local_ini_with_a_bom_is_read(capsys, local_ini):
    assert l2.main(["--dry-run", "--sets", "3", "--local-ini", str(local_ini)]) == 0
    assert "MISSING" not in capsys.readouterr().out


def test_a_variant_that_times_out_keeps_its_time(tmp_path, monkeypatch, local_ini):
    local = configparser.ConfigParser(interpolation=None)
    local.read(local_ini, encoding="utf-8-sig")
    monkeypatch.setattr(l2, "prepare_target", lambda *a, **k: None)

    def slow(*a, **k):
        time.sleep(1.2)
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))

    monkeypatch.setattr(l2.subprocess, "run", slow)
    res = l2.run_variant(tmp_path, "on-3", l2.KNOWN, ROOT, "a" * 40, ROOT / "agents.ini", local,
                         l2.ROSTER[:3], "all", l2.READER, 1)
    assert res["ok"] is False and res["seconds"] >= 1 and res["error"] == "timed out after 1 s"
    l2.summary(tmp_path, [res], IDS, TRUTH, TITLES)
    assert "FAILED timed out after 1 s" in (tmp_path / "SUMMARY.md").read_text(encoding="utf-8")


# ── №34: the baseline's runs come from contest.ini ───────────────────────────────────────────

def test_off_runs_is_read_from_contest_ini(tmp_path):
    assert l2.ini_off_runs(ROOT / "contest.ini") == 2
    ini = tmp_path / "contest.ini"
    ini.write_text("[gate1_layer2]\noff_runs = 3   ; three\n", encoding="utf-8")
    assert l2.ini_off_runs(ini) == 3
    for bad in ("0", "many"):
        ini.write_text(f"[gate1_layer2]\noff_runs = {bad}\n", encoding="utf-8")
        assert l2.ini_off_runs(ini) == l2.DEFAULT_OFF_RUNS
    assert l2.ini_off_runs(tmp_path / "missing.ini") == l2.DEFAULT_OFF_RUNS


def test_the_dry_run_lists_two_baselines_by_default(capsys, local_ini):
    assert l2.main(["--dry-run", "--sets", "3", "--local-ini", str(local_ini)]) == 0
    out = capsys.readouterr().out
    assert "  off " in out and "  off-2 " in out
