"""CC-0: the claim-check bench — fixture shas, the key, the scorer and the real checks."""

import importlib.util
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BENCH = REPO / "contest-bench" / "cc"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"cc_bench_{name}", BENCH / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mf = _load("make_fixture")
sc = _load("score_cc")
rc = _load("real_checks")

FIXTURE = json.loads((BENCH / "claims_fixture.json").read_text(encoding="utf-8"))
FACTS = json.loads((BENCH / "fixture_facts.json").read_text(encoding="utf-8"))
REAL = json.loads((BENCH / "claims_real.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    return mf.build_fixture(tmp_path_factory.mktemp("cc") / "fx")


def _show(fx, sha, path):
    return subprocess.run(["git", "show", f"{sha}:{path}"], cwd=fx.root,
                          capture_output=True, text=True).stdout


# ── the fixture ────────────────────────────────────────────────────────────

def test_fixture_shas_are_stable(tmp_path, fx):
    """Two builds in two directories give the same shas, and the files record them."""
    again = mf.build_fixture(tmp_path / "elsewhere" / "fx")
    assert (again.base_sha, again.head_sha) == (fx.base_sha, fx.head_sha)
    assert (FIXTURE["base_sha"], FIXTURE["head_sha"]) == (fx.base_sha, fx.head_sha)
    assert (FACTS["base_sha"], FACTS["head_sha"]) == (fx.base_sha, fx.head_sha)


def test_committed_json_is_what_the_generator_writes(fx):
    """claims_fixture.json and fixture_facts.json are generated, never hand-edited."""
    facts_text, claims_text = mf.render(fx)
    assert (BENCH / "fixture_facts.json").read_text(encoding="utf-8") == facts_text
    assert (BENCH / "claims_fixture.json").read_text(encoding="utf-8") == claims_text


def test_fixture_facts_match_the_files(fx):
    """Every seeded behaviour names lines that exist and hold the seeded text."""
    kinds = Counter(f["kind"] for f in FACTS["facts"])
    assert kinds == {"fixed": 20, "still": 10, "gone": 1, "new": 2}
    for fact in FACTS["facts"]:
        sha = fx.head_sha if fact["at"] == "head" else fx.base_sha
        lines = _show(fx, sha, fact["file"]).splitlines()
        start, end = fact["lines"]
        assert 1 <= start <= end <= len(lines), fact["id"]
        span = "\n".join(lines[start - 1:end])
        assert fact["text"].rstrip("\n") in span, fact["id"]


def test_head_commit_names_the_fixes(fx):
    message = subprocess.run(["git", "log", "-1", "--format=%B", fx.head_sha], cwd=fx.root,
                             capture_output=True, text=True).stdout
    fixed = [c for c in FIXTURE["claims"] if c["expect"] == "fixed"]
    for c in fixed:
        assert f"{c['anchors'][0]}::{c['anchors'][1]}" in message
    assert "def norm_path(" in _show(fx, fx.base_sha, "paths.py")
    assert "def norm_path(" not in _show(fx, fx.head_sha, "paths.py")


def test_fixture_truth_is_stated(fx):
    """Every claim says how it is settled; the key agrees with the probes at both shas."""
    by_id = {row["id"]: row for row in mf.SPEC}
    for c in FIXTURE["claims"]:
        assert c["how"], c["id"]
        assert "truth_base" in c and "truth_head" in c, c["id"]
        assert c["truth"] == c["truth_base"]
        row = by_id[c["id"]]
        assert mf._truth_at(row, fx, fx.base_sha) == c["truth_base"], c["id"]
        assert mf._truth_at(row, fx, fx.head_sha) == c["truth_head"], c["id"]
    want = {"fixed": (True, False), "still": (True, True), "gone": (True, None), "new": (False, True)}
    for c in FIXTURE["claims"]:
        if c["expect"]:
            assert (c["truth_base"], c["truth_head"]) == want[c["expect"]], c["id"]


def test_how_names_the_deciding_lines(fx):
    """A code claim's `how` cites lines at base that hold the probe text or the symbol."""
    for c in FIXTURE["claims"]:
        if c["kind"] != "code":
            continue
        assert c["how"].startswith("base: read ") or c["how"].startswith("base: no "), c["id"]


def test_claim_files_are_well_formed():
    """Ids unique, kinds allowed, the composition of the ticket (as amended)."""
    for doc, total in ((FIXTURE, 80), (REAL, 30)):
        claims = doc["claims"]
        assert len(claims) == total
        assert len({c["id"] for c in claims}) == total
        assert {c["kind"] for c in claims} <= set(mf.KINDS)
        assert all(c["claim"].strip() and isinstance(c["truth"], bool) for c in claims)
    kinds = Counter(c["kind"] for c in FIXTURE["claims"])
    assert kinds == {"code": 50, "world": 10, "mixed": 8, "dangling": 6, "commit": 3, "ticket": 3}
    code = [c for c in FIXTURE["claims"] if c["kind"] == "code"]
    assert sum(c["truth_base"] for c in code) == 32
    assert sum(not c["truth_base"] for c in code) == 18
    world = [c for c in FIXTURE["claims"] if c["kind"] == "world"]
    assert sum(c["truth"] for c in world) == 5
    assert Counter(c["expect"] for c in code) == {"fixed": 20, "still": 10, "gone": 2, "new": 2, None: 16}
    assert all(c["expect"] is None for c in FIXTURE["claims"] if c["kind"] != "code")
    assert all(c["truth_base"] is False and c["truth_head"] is False
               for c in FIXTURE["claims"] if c["kind"] == "dangling")
    assert len(REAL["real_sha"]) == 40
    assert all(c["how"] and ("grep" in c["check"] or "python" in c["check"]) for c in REAL["claims"])


# ── the scorer ─────────────────────────────────────────────────────────────

KEY = {"set": "toy", "claims": [
    {"id": "a", "claim": "A", "kind": "code", "truth_base": True, "truth_head": False},
    {"id": "b", "claim": "B", "kind": "code", "truth_base": False, "truth_head": False},
    {"id": "c", "claim": "C", "kind": "code", "truth_base": True, "truth_head": None},
    {"id": "d", "claim": "D", "kind": "world", "truth": True},
    {"id": "e", "claim": "E", "kind": "world", "truth": False},
]}


def _votes():
    models = ("m1", "m2", "m3")
    results = [{"model": m, "run": 0, "votes": {"0": "TRUE"}} for m in models]
    every = lambda v: {m: v for m in models}  # noqa: E731
    return {"results": results, "claims": [
        # matched by text: right, decided
        {"claim": "A", "verdict": "TRUE", "unanimous": True, "by_model": every("TRUE")},
        # matched by id: wrong, decided, two fabricated quotes downgraded
        {"id": "b", "claim": "B (reworded)", "verdict": "TRUE", "unanimous": True,
         "by_model": every("TRUE"), "downgrades": 2},
        # CODE-CHECK: the tool's veto; raw says FALSE, which is wrong at base
        {"claim": "C", "verdict": "CODE-CHECK", "unanimous": False, "by_model": every("FALSE")},
        # split: undecided, not raw either
        {"claim": "D", "verdict": "SPLIT", "unanimous": False,
         "by_model": {"m1": "TRUE", "m2": "FALSE", "m3": "TRUE"}},
        # a voter missing: undecided
        {"claim": "E", "verdict": "FALSE", "unanimous": False, "by_model": {"m1": "FALSE", "m2": "FALSE"}},
    ]}


def test_scorer_counts():
    t = sc.score(_votes(), KEY, "base")
    o = t["overall"]
    assert (o["claims"], o["decided"], o["right"], o["wrong"], o["undecided"], o["no_truth"]) == (5, 2, 1, 1, 3, 0)
    assert o["coverage"] == 0.4 and o["precision"] == 0.5 and o["wrong_rate"] == 0.5
    assert (o["raw_decided"], o["raw_right"], o["raw_wrong"]) == (3, 1, 2)
    assert o["downgrades"] == 2
    assert t["kinds"]["code"]["decided"] == 2 and t["kinds"]["world"]["decided"] == 0
    assert t["matched"] == 5
    head = sc.score(_votes(), KEY, "head")
    assert head["overall"]["no_truth"] == 1          # C is gone at head
    assert head["kinds"]["code"]["right"] == 0 and head["kinds"]["code"]["wrong"] == 2  # A was fixed
    assert head["kinds"]["code"]["coverage"] == 1.0  # 2 decided of 2 with a truth


def test_scorer_on_the_fixture_with_a_perfect_voter():
    """Unanimous right answers everywhere: coverage 1, wrong 0, gone claims excluded at head."""
    models = ("m1", "m2", "m3")
    claims = [{"id": c["id"], "claim": c["claim"], "unanimous": True,
               "verdict": "TRUE" if c["truth_base"] else "FALSE",
               "by_model": {m: "TRUE" if c["truth_base"] else "FALSE" for m in models}}
              for c in FIXTURE["claims"]]
    votes = {"results": [{"model": m, "run": 0, "votes": {"0": "TRUE"}} for m in models], "claims": claims}
    t = sc.score(votes, FIXTURE)
    assert t["overall"]["coverage"] == 1.0 and t["overall"]["wrong"] == 0
    assert t["set"] == "fixture"


def test_scorer_deltas():
    deltas = [{"id": c["id"], "change": c["expect"].upper()}
              for c in FIXTURE["claims"] if c["expect"]]
    deltas[0]["change"] = "STILL"                    # one fixed claim missed
    t = sc.score_deltas({"deltas": deltas}, FIXTURE)
    assert t["expected"] == {"fixed": 20, "still": 10, "gone": 2, "new": 2}
    assert t["confusion"]["fixed"] == {"FIXED": 19, "STILL": 1}
    assert t["delta"]["fix_right"] == round(29 / 30, 4)
    assert t["delta"]["fixed_as_still"] == 0


def _cli(tmp_path, table_votes, key, thresholds):
    v, k, th = tmp_path / "votes.json", tmp_path / "key.json", tmp_path / "th.json"
    v.write_text(json.dumps(table_votes)), k.write_text(json.dumps(key))
    th.write_text(json.dumps({"targets": thresholds}))
    return subprocess.run([sys.executable, str(BENCH / "score_cc.py"), str(v), str(k),
                           "--thresholds", str(th)], capture_output=True, text=True)


def test_scorer_exit_code_on_threshold(tmp_path):
    """Coverage is 0.4 on the toy key: a 0.4 target passes, 0.41 fails, n/a never fails."""
    at = [{"id": "cov", "set": "toy", "metric": "overall.coverage", "op": ">=", "value": 0.4}]
    above = [{**at[0], "value": 0.41}]
    na = [{"id": "na", "set": None, "metric": None, "op": ">=", "value": 1.0},
          {"id": "other", "set": "real", "metric": "overall.coverage", "op": ">=", "value": 0.9}]
    ok = _cli(tmp_path, _votes(), KEY, at)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "ok   cov" in ok.stdout
    miss = _cli(tmp_path, _votes(), KEY, above)
    assert miss.returncode == 1 and "MISS cov" in miss.stdout
    skipped = _cli(tmp_path, _votes(), KEY, na)
    assert skipped.returncode == 0 and "n/a  na" in skipped.stdout and "other" not in skipped.stdout


def test_thresholds_file_carries_the_epic_targets():
    targets = json.loads((BENCH / "thresholds.json").read_text(encoding="utf-8"))["targets"]
    assert {t["id"] for t in targets} == {"fixture-code-coverage", "fixture-wrong-rate",
                                          "real-wrong-rate", "fabricated-quotes-caught",
                                          "fixture-fix-right"}
    assert all(t.get("note") for t in targets)
    assert all(t["op"] in sc._OPS for t in targets)


# ── the real claims ────────────────────────────────────────────────────────

def test_real_checks_agree_with_recorded_truth():
    """Every runnable check, at real_sha, observes the recorded truth (≈2 s)."""
    if not (REPO / ".git").exists() or not rc.has_sha(REPO, REAL["real_sha"]):
        pytest.skip(f"no checkout holding {REAL['real_sha'][:12]}")
    rows = rc.run_all(BENCH / "claims_real.json", REPO)
    assert len(rows) == 30
    assert [r["id"] for r in rows if not r["ok"]] == []
