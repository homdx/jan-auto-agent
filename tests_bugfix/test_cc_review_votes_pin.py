"""Review of cc-epic-docs: votes.json pins the base sha and the pack budget the voters saw, and the CC-8 rebuild uses both."""

import configparser
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

ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
CLAIM = "`f()` in a.py returns 2"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **ENV}).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.py").write_text("def f():\n    return 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "one")
    git(r, "checkout", "-qb", "feat")
    (r / "a.py").write_text("def f():\n    return 2\n")
    git(r, "commit", "-qam", "two")
    git(r, "checkout", "-q", "main")
    return r


def _vote(tmp_path, repo, monkeypatch, *extra):
    seen = []

    def fake_ask_v2(ref, items, run, seed, parser, timeout, batch, code_batch, fixed):
        seen.append([[c.id for c in p.chunks] if p else None for _t, p in items])
        return {"model": ref, "run": run, "votes": {0: "TRUE"}, "accepted": {}, "rejected": []}

    monkeypatch.setattr(cv, "ask_v2", fake_ask_v2)
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps([CLAIM]))
    out = tmp_path / "votes.json"
    rc = cv.main([str(claims), "--target", str(repo), "--ref", "feat", "--base", "main", "--runs", "1",
                  "--models", "a/x", "b/y", "c/z", "--out", str(out), "--repo-root", str(tmp_path),
                  "--scratch", str(tmp_path / "scratch"), *extra])
    return rc, out, seen


def test_rebuild_after_the_base_moved_shows_what_the_voters_saw(tmp_path, repo, monkeypatch, capsys):
    rc, out, seen = _vote(tmp_path, repo, monkeypatch)
    assert rc == 0
    votes = json.loads(out.read_text())
    assert votes["target"]["base"] == "main"
    assert votes["target"]["base_sha"] == git(repo, "rev-parse", "main")
    (repo / "b.py").write_text("x = 1\n")                   # main moves after the vote
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "main moves")
    _res, packs, base_sha = cc_truth.rebuild(votes, str(repo), scratch=tmp_path / "s2")
    assert [c.id for c in packs[0].chunks] == seen[0][0]
    assert base_sha == votes["target"]["base_sha"]


def test_a_base_that_does_not_resolve_is_refused(tmp_path, repo, monkeypatch, capsys):
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps([CLAIM]))
    rc = cv.main([str(claims), "--target", str(repo), "--ref", "feat", "--base", "nope", "--runs", "1",
                  "--models", "a/x", "b/y", "c/z", "--repo-root", str(tmp_path),
                  "--scratch", str(tmp_path / "scratch")])
    assert rc == 2
    assert "--base nope: does not resolve" in capsys.readouterr().err


def test_the_recorded_budget_is_the_one_the_rebuild_uses(tmp_path, repo, monkeypatch):
    rc, out, _seen = _vote(tmp_path, repo, monkeypatch, "--pack-chars", "900")
    votes = json.loads(out.read_text())
    assert votes["target"]["budget"]["chars"] == 900
    parser = configparser.ConfigParser()
    assert cc_truth._budget_of(votes["target"], parser).chars == 900
    # a report without the field keeps today's behaviour; a damaged one falls back, it does not raise
    assert cc_truth._budget_of({}, parser) == PackBudget.from_config(parser)
    assert cc_truth._budget_of({"budget": {"chars": "x"}}, parser) == PackBudget.from_config(parser)


def test_an_old_votes_file_with_a_base_name_is_warned_about(tmp_path, repo, capsys):
    import claimcheck_truth as ct
    sha = git(repo, "rev-parse", "feat")
    f = tmp_path / "votes.json"
    f.write_text(json.dumps({"target": {"repo": str(repo), "sha": sha, "base": "main"},
                             "results": [], "claims": [{"claim": CLAIM, "verdict": "UNSURE"}]}))
    assert ct.main([str(f), "--out", str(tmp_path / "t.csv"), "--scratch", str(tmp_path / "s"),
                    "--no-lenz-hint"]) == 0
    assert "as a name, not a sha" in capsys.readouterr().err
