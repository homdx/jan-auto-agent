"""tests/test_gate1_vote_layer2_baselines_review.py — CC-10 layer 2 after the review of 2026-10-12 (n*.txt): a variant
is compared with every baseline run (the verdict flipped with their order), a veto-only variant must have vetoed,
a set's rule is in its name when it is not the size's default, and --detach keeps the caller's directory.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gate1_vote_layer2_baselines", ROOT / "scripts" / "gate1_vote_layer2.py")
l2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(l2)

IDS = [f"AUTO-T{i}" for i in range(1, 21)]
TRUTH = {"AUTO-T1": "REAL", **{f"AUTO-T{i}": "FALSE" for i in range(2, 21)}}


def _res(name, removed, decided=7, opts=()):
    r = {"variant": name, "rc": 0, "seconds": 5, "voters": [] if name.startswith("off") else list("abc"),
         "accept": "all", "removed": {i: "x" for i in removed}, "inconclusive": [], "ok": True,
         "checked": len(IDS), "removed_n": len(removed), "opts": list(opts)}
    if not name.startswith("off"):
        r.update(votes_asked=len(IDS), votes_decided=decided, votes_overridden=0, votes_requests=3 * len(IDS))
    return r


def _removed(n):
    """The first *n* false positives removed (T2…), so `19 - n` are let through."""
    return [f"AUTO-T{i}" for i in range(2, 2 + n)]


@pytest.mark.parametrize("order", ["better-first", "worse-first"])
def test_the_verdict_does_not_depend_on_the_baselines_order(tmp_path, order):
    """Baselines letting through 14 and 10, the vote 12: worse than one of them, so FAIL in either order
    (it was PASS when the 14 came first)."""
    a, b = _res("off", _removed(5)), _res("off-2", _removed(9))     # 14 and 10 let through
    bases = [a, b] if order == "better-first" else [{**b, "variant": "off"}, {**a, "variant": "off-2"}]
    v = l2.summary(tmp_path, [*bases, _res("on-3", _removed(7))], IDS, TRUTH, {})
    assert v["on-3"].startswith("FAIL") and "each of 2 off runs" in v["on-3"]


def test_a_variant_no_worse_than_every_baseline_passes(tmp_path):
    v = l2.summary(tmp_path, [_res("off", _removed(5)), _res("off-2", _removed(9)), _res("on-3", _removed(9))],
                   IDS, TRUTH, {})
    assert v["on-3"] == "PASS"


def test_a_veto_variant_that_never_vetoed_fails(tmp_path):
    base = _res("off", _removed(5))
    inert = _res("on-3-veto", _removed(5), decided=0, opts=("veto",))
    v = l2.summary(tmp_path, [base, inert], IDS, TRUTH, {})
    assert v["on-3-veto"].startswith("FAIL") and "decided>=1" in v["on-3-veto"]
    vetoed = _res("on-3-veto", _removed(5), decided=1, opts=("veto",))
    assert l2.summary(tmp_path, [base, vetoed], IDS, TRUTH, {})["on-3-veto"].startswith("PASS")


def test_one_size_under_both_rules_is_two_variants():
    sets = l2.parse_sets("5:all,5:committed,5:all+veto,3:committed")
    assert [l2.variant_name(n, o, r) for n, r, o in sets] == ["on-5-all", "on-5", "on-5-all-veto", "on-3-committed"]
    assert l2.variant_name(5, ("notests", "veto")) == "on-5-notests-veto"     # the cloud's two-argument form
    with pytest.raises(SystemExit):
        l2.parse_sets("5:all,5:all")


def test_detach_starts_the_child_in_the_callers_directory(tmp_path, monkeypatch, capsys):
    local = tmp_path / "local.ini"
    local.write_text("".join(f"[{l2.P}{n}]\nbase_url = x\napi_key = k\nmodel = {n}\n"
                             for n in dict.fromkeys(l2.ROSTER + [l2.READER])), encoding="utf-8")
    seen = {}

    class Fake:
        def __init__(self, cmd, **kw):
            seen.update(cmd=cmd, **kw)

    monkeypatch.setattr(l2.subprocess, "Popen", Fake)
    monkeypatch.chdir(tmp_path)
    assert l2.main(["--detach", "--sets", "3", "--local-ini", "local.ini", "--out", "run"]) == 0
    assert seen["cwd"] == os.getcwd() == str(tmp_path)
    assert "local.ini" in seen["cmd"] and (tmp_path / "run" / "nohup.out").exists()
