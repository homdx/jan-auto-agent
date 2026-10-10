"""claim_extract / claim_vote: sectioning, the no-repeat-credit cache, prompt variants, vote tally (offline)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402
import lenz_claim_filter as lf  # noqa: E402

REPORT = ("# Title\n\nintro line that is long enough to matter " + "x" * 120 + "\n\n"
          "## One\n\n" + "a " * 80 + "\n\n### Two\n\n" + "b " * 80 + "\n\n### tiny\n\nshort\n")


def test_sections_split_at_headings_and_drop_the_tiny_ones():
    secs = lf.split_sections(REPORT)
    assert [s.splitlines()[0] for s in secs] == ["# Title", "## One", "### Two"]


def test_extract_cache_hit_sends_nothing(tmp_path, monkeypatch):
    cache = lf.Cache(tmp_path / "c.json")
    calls = []
    monkeypatch.setattr(lf, "call", lambda *a, **k: calls.append(a) or {"claim": "A fact.", "identified_claims": ["B fact."]})
    assert lf.extract_claims("sec", "k", cache) == ["A fact.", "B fact."]
    assert len(calls) == 1
    monkeypatch.setattr(lf, "call", lambda *a, **k: pytest.fail("a cached section went to the network"))
    assert lf.extract_claims("sec", "k", lf.Cache(tmp_path / "c.json")) == ["A fact.", "B fact."]


def test_assess_never_pays_twice_and_never_caches_errors(tmp_path, monkeypatch):
    cache = lf.Cache(tmp_path / "c.json")
    rows = {"claims": [{"claim": "ok", "verdict": "True"}, {"claim": "bad", "verdict": "Error"}]}
    monkeypatch.setattr(lf, "call", lambda *a, **k: rows)
    first = lf.assess(["ok", "bad"], "k", cache)
    assert [r["cached"] for r in first] == [False, False]
    sent = []
    monkeypatch.setattr(lf, "call", lambda p, path, key, body=None, **k: sent.append(body) or rows)
    lf.assess(["ok", "bad"], "k", lf.Cache(tmp_path / "c.json"))
    assert sent == [{"claims": ["bad"]}]          # the paid one stays cached, the error is retried


def test_prompt_variants_differ_at_the_first_sentence_and_in_order():
    claims = [f"claim number {i}" for i in range(8)]
    texts = [cv.build_prompt(claims, run, 1)[0] for run in range(3)]
    assert len({t.split(".")[0] for t in texts}) == 3
    assert len({t.split("\n\n", 1)[1] for t in texts}) > 1


def test_parse_votes_maps_shuffled_ids_back_and_survives_garbage():
    order = [2, 0, 1]
    assert cv.parse_votes('x [{"id":1,"verdict":"true"},{"id":3,"verdict":"UNSURE"}] y', order) == {2: "TRUE", 1: "UNSURE"}
    assert cv.parse_votes("no json", order) == {}
    assert cv.parse_votes('[{"id":9,"verdict":"TRUE"}]', order) == {}


def _res(model, votes):
    return {"model": model, "run": 0, "votes": votes}


def test_tally_unsure_abstains_quorum_and_code_claims():
    claims = [{"claim": "Pytest exits with code 4 on a usage error."},
              {"claim": "One brave vote."},
              {"claim": "`_pytest()` in `gates.py` passes the flag."}]
    results = [_res("a/m1", {"0": "TRUE", "1": "TRUE", "2": "TRUE"}),
               _res("b/m2", {"0": "TRUE", "1": "UNSURE", "2": "TRUE"}),
               _res("c/m3", {"0": "TRUE", "1": "UNSURE", "2": "TRUE"}),
               _res("d/m4", {"0": "FALSE", "1": "UNSURE", "2": "TRUE"})]
    out = cv.tally(claims, results, set())          # string keys, as read back from a file
    assert [t["verdict"] for t in out] == ["TRUE", "UNSURE", "CODE-CHECK"]
    assert out[2]["needs_code"] is True


def test_unanimous_needs_every_voter_to_commit_to_one_verdict():
    claims = [{"claim": "A plain world fact one."}, {"claim": "A plain world fact two."},
              {"claim": "A plain world fact three."}]
    results = [_res("a/m1", {0: "TRUE", 1: "TRUE", 2: "TRUE"}),
               _res("b/m2", {0: "TRUE", 1: "TRUE", 2: "TRUE"}),
               _res("c/m3", {0: "TRUE", 1: "UNSURE", 2: "FALSE"})]
    out = cv.tally(claims, results, set())
    assert [t["unanimous"] for t in out] == [True, False, False]   # an abstainer or a dissenter spoils it


def test_tally_tie_is_split():
    results = [_res(f"p/m{i}", {0: v}) for i, v in enumerate(["TRUE", "TRUE", "FALSE", "FALSE"])]
    assert cv.tally([{"claim": "A plain world fact here."}], results, set())[0]["verdict"] == "SPLIT"


# ── CC-6 (280): --target, the packs in the prompt, the new votes.json fields ──

import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
from collections import Counter  # noqa: E402

from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.claimcheck import judge  # noqa: E402

_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Ann", "GIT_AUTHOR_EMAIL": "ann@example.com",
            "GIT_COMMITTER_NAME": "Ann", "GIT_COMMITTER_EMAIL": "ann@example.com"}
APP = ("import subprocess\n\n\ndef run_step(cmd):\n    return subprocess.run(cmd, check=False)\n\n\n"
       "def run_safe(cmd):\n    return subprocess.run(cmd, check=True)\n")
CODE = ["`run_step` in `app.py` calls subprocess.run with check=False.",
        "`run_safe` in `app.py` calls subprocess.run with check=False."]
WORLD = ["Python's subprocess module ships with the standard library.",
         "The Earth goes around the Sun once a year."]


def _repo(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    (root / "app.py").write_text(APP, encoding="utf-8")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "app")):
        subprocess.run(["git", "-C", str(root), *args], env=_GIT_ENV, check=True, capture_output=True)
    return root


def _claims(tmp_path, texts):
    path = tmp_path / "claims.json"
    path.write_text(json.dumps([{"id": f"c{i}", "claim": t, "truth": None}
                                for i, t in enumerate(texts)]), encoding="utf-8")
    return path


_BLOCK = re.compile(r"=== CLAIM (\d+) ===\n(.*?)(?=\n\n=== CLAIM \d+ ===|\Z)", re.S)
_LABEL = re.compile(r"^\[\[(.+?)\]\]  \(\w+\)$", re.M)


def _answer(prompt: str) -> str:
    """A voter that reads the pack: the line of the function the claim names, quoted."""
    rows = []
    for m in _BLOCK.finditer(prompt):
        n, (claim, evidence) = int(m.group(1)), m.group(2).split("\n", 1)
        if judge.WORLD_BLOCK in evidence:
            rows.append({"id": n, "verdict": "TRUE"})
            continue
        name = claim.split("`")[1]
        label = _LABEL.search(evidence)
        lines = evidence.splitlines()
        at = next(k for k, ln in enumerate(lines) if f"def {name}(" in ln)
        line = lines[at + 1].split("| ", 1)[1].strip()
        rows.append({"id": n, "verdict": "TRUE" if "check=False" in line else "FALSE",
                     "chunk": label.group(1), "quote": line})
    return json.dumps(rows)


@pytest.fixture
def voters(monkeypatch):
    """Three fake voters behind the real ask/ask_v2: the HTTP call is the only thing faked."""
    calls = []
    monkeypatch.setattr(cv, "voter_settings", lambda ref, parser: LlmSettings(
        base_url=f"http://{ref.split('/')[0]}.invalid/v1", api_key="k", model=ref,
        temperature=0.3, max_tokens=100))

    def completion(url, headers, payload, timeout, **_k):
        prompt = payload["messages"][-1]["content"]
        calls.append(prompt)
        if "=== CLAIM" in prompt:
            return _answer(prompt)
        n = len(re.findall(r"^\d+\. ", prompt, re.M))
        return json.dumps([{"id": i, "verdict": "TRUE"} for i in range(1, n + 1)])
    monkeypatch.setattr(cv, "request_completion", completion)
    return calls


def _main(tmp_path, *extra):
    return cv.main([*extra, "--profiles", "a/x", "b/y", "c/z", "--runs", "1", "--interval", "0.001",
                    "--repo-root", str(tmp_path), "--out", str(tmp_path / "votes.json")])


def test_claim_vote_without_target_is_unchanged(tmp_path, voters, capsys):
    path = _claims(tmp_path, CODE[:1] + WORLD[:1])
    assert _main(tmp_path, str(path), "--symbols-root", str(_repo(tmp_path))) == 0
    rows = json.loads((tmp_path / "votes.json").read_text())["claims"]
    assert [r["verdict"] for r in rows] == ["CODE-CHECK", "TRUE"]          # CLAIM-1's veto stands
    assert [r["unanimous"] for r in rows] == [False, True]
    assert set(rows[0]) == {"claim", "truth", "verdict", "needs_code", "kind", "dangling",
                            "unanimous", "by_model", "all_votes"}         # no CC-6 field
    assert all("=== CLAIM" not in p and "EVIDENCE" not in p for p in voters)
    assert voters[0] == cv.build_prompt([CODE[0], WORLD[0]], 0, 1)[0]     # CLAIM-1's prompt, byte for byte
    out = capsys.readouterr().out
    assert "rejected quotes" not in out and "warnings:" not in out


def test_target_options_parse(tmp_path, monkeypatch, voters):
    seen = {}
    monkeypatch.setattr(cv, "main_target", lambda args, parser, claims, v, batch: seen.update(
        vars(args), voters=v, batch=batch) or 0)
    path = _claims(tmp_path, WORLD)
    assert _main(tmp_path, str(path), "--target", "/some/repo", "--ref", "origin/kc", "--base", "main",
                 "--fetch", "--pack-chars", "3000", "--code-batch", "3", "--expect-sha", "afa53f1") == 0
    assert (seen["target"], seen["ref"], seen["base"], seen["fetch"]) == ("/some/repo", "origin/kc", "main", True)
    assert (seen["pack_chars"], seen["code_batch"], seen["expect_sha"]) == (3000, 3, "afa53f1")
    assert seen["voters"] == ["a/x", "b/y", "c/z"] and voters == []
    # a target that is not a repository is one refusal line and exit 2, never a traceback
    monkeypatch.undo()
    rc = cv.main([str(path), "--profiles", "a/x", "--target", str(tmp_path / "nowhere"),
                  "--repo-root", str(tmp_path)])
    assert rc == 2


def test_votes_json_has_the_new_fields(tmp_path, voters, capsys):
    path = _claims(tmp_path, CODE + WORLD[:1])
    assert _main(tmp_path, str(path), "--target", str(_repo(tmp_path)),
                 "--scratch", str(tmp_path / "scratch")) == 0
    report = json.loads((tmp_path / "votes.json").read_text())
    rows = report["claims"]
    assert [r["verdict"] for r in rows] == ["TRUE", "FALSE", "TRUE"]       # code claims decided, no CODE-CHECK
    assert [r["unanimous"] for r in rows] == [True, True, True]
    assert [r["kind"] for r in rows] == ["code", "code", "world"]
    sha = report["target"]["sha"]
    for r in rows:
        assert {"id", "kind", "dangling", "sha", "evidence", "quotes", "rejected",
                "downgrades", "needs_code"} <= set(r)
        assert r["sha"] == sha and r["rejected"] == {} and r["downgrades"] == 0
    assert rows[0]["id"] == "c0" and rows[0]["needs_code"] is True and rows[2]["needs_code"] is False
    assert rows[0]["evidence"] and rows[0]["evidence"][0].startswith("src:app.py:")
    assert rows[0]["quotes"] == ["return subprocess.run(cmd, check=False)"]
    assert rows[2]["evidence"] == [] and rows[2]["quotes"] == []
    assert all(r["rejected"] == [] for r in report["results"])
    out = capsys.readouterr().out
    assert "rejected quotes: 0" in out and "warnings: none" in out


def test_code_batch_splits_code_claims(tmp_path, voters):
    texts = [CODE[i % 2] for i in range(4)] + [f"World fact number {i} is plain." for i in range(12)]
    path = _claims(tmp_path, texts)
    assert _main(tmp_path, str(path), "--target", str(_repo(tmp_path)),
                 "--scratch", str(tmp_path / "scratch")) == 0
    # three voters run at once, so their calls interleave: count the shapes, three of each
    shapes = Counter()
    for prompt in voters:
        blocks = [m.group(2) for m in _BLOCK.finditer(prompt)]
        shapes[(len(blocks), sum(judge.WORLD_BLOCK in b for b in blocks))] += 1
    assert shapes == {(10, 10): 3, (2, 2): 3, (2, 0): 6}     # world 10 + 2, code 2 + 2, per voter


def test_report_sha_mismatch_is_printed(tmp_path, voters, capsys):
    report = tmp_path / "report.md"
    report.write_text("# Review\n\nCode: origin/kc @ deadbee1\n", encoding="utf-8")
    path = _claims(tmp_path, CODE[:1])
    assert _main(tmp_path, str(path), "--target", str(_repo(tmp_path)), "--report", str(report),
                 "--scratch", str(tmp_path / "scratch")) == 0
    captured = capsys.readouterr()
    assert "warning: the report says `deadbee1`" in captured.err
    assert re.search(r"warnings: the report says `deadbee1`, `HEAD` is `[0-9a-f]{7}`", captured.out)
    assert json.loads((tmp_path / "votes.json").read_text())["target"]["warnings"]
