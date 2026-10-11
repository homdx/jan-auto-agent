"""Review of cc-epic-docs: the collect facts the epic describes reach the voter's pack, and the CC-8 rebuild shows what the vote had."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claim_vote as cv  # noqa: E402
from tools.claimcheck import truth as cc_truth  # noqa: E402
from tools.claimcheck.pack import PackBudget  # noqa: E402
from tools.claimcheck.target import Target  # noqa: E402

ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
CLAIM = "`load_rows` in store.py reads the file once."


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    (r / "pkg").mkdir(parents=True)
    (r / "store.py").write_text("def load_rows(path):\n    with open(path) as fh:\n        return fh.read()\n")
    (r / "pkg" / "__init__.py").write_text("")
    (r / "pkg" / "use.py").write_text("from store import load_rows\n\n\ndef main():\n    return load_rows('x')\n")
    env = {**os.environ, **ENV}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True, env=env)
    return r


def _kinds(pack):
    return sorted({c.kind for c in pack.chunks})


def test_the_pack_carries_collect_facts(repo, tmp_path):
    with Target.open(str(repo), "HEAD", scratch=tmp_path / "s") as t:
        [(_, pack)], _ = cv.prepare_target([{"claim": CLAIM}], t, PackBudget())
        assert "collect" in _kinds(pack), [c.id for c in pack.chunks]
        assert any(c.id.startswith("collect:") and "use.py" in c.text for c in pack.chunks)
        [(_, bare)], _ = cv.prepare_target([{"claim": CLAIM}], t, PackBudget(), collect=False)
        assert "collect" not in _kinds(bare)


def test_votes_json_records_it_and_the_rebuild_follows(repo, tmp_path, monkeypatch):
    seen = []

    def fake_ask_v2(ref, items, run, seed, parser, timeout, batch, code_batch, fixed):
        seen.append([c.id for c in items[0][1].chunks])
        return {"model": ref, "run": run, "votes": {0: "TRUE"}, "accepted": {}, "rejected": []}

    monkeypatch.setattr(cv, "ask_v2", fake_ask_v2)
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps([CLAIM]))
    for extra, collected in (((), True), (("--no-collect",), False)):
        out = tmp_path / f"votes-{collected}.json"
        assert cv.main([str(claims), "--target", str(repo), "--runs", "1", "--models", "a/x", "b/y", "c/z",
                        "--out", str(out), "--repo-root", str(tmp_path), "--scratch", str(tmp_path / "s"),
                        *extra]) == 0
        votes = json.loads(out.read_text())
        assert votes["target"]["collect"] is collected
        _r, packs, _b = cc_truth.rebuild(votes, str(repo), scratch=tmp_path / "s2")
        assert [c.id for c in packs[0].chunks] == seen[-1]
    # a votes.json written before the field had no collect facts: the rebuild has none either
    del votes["target"]["collect"]
    _r, packs, _b = cc_truth.rebuild(votes, str(repo), scratch=tmp_path / "s3")
    assert "collect" not in _kinds(packs[0])
