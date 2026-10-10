"""tests_bugfix/test_claimcheck_anchors_judged_255.py — ticket 255 (CC-1), found when the round was judged.

Four defects of the winning entry, each pinned here:

* ``HEAD~1`` / ``HEAD^`` was cut to ``HEAD`` by the ref finder, so a claim "landed at HEAD~1"
  resolved to the wrong commit;
* the ticket id ``CC-1`` is written ``cc-1`` in its file name (``255-cc-1-…``): the slug lookup
  wanted ``cc1`` and fell through to the number rule, which answered ``01-…`` — another ticket;
* a commit hash made only of digits ("Commit 480164324 ...") was not an anchor, so one claim in
  forty about a real commit read as ``world`` — and the CC-1 tests failed on whichever run the
  repository's HEAD happened to be such a hash;
* ``claim_vote.main`` built a ``PathRepoView`` even without ``--symbols-root``, so every plain
  run switched from ``lenz_claim_filter.is_internal`` to the anchor classifier; the ticket says
  the view exists only when ``--symbols-root`` names a repository.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote  # noqa: E402
from tools.claimcheck.anchors import PathRepoView, classify, extract_anchors, resolve_anchors  # noqa: E402


def _git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "epic-tasks").mkdir()
    (tmp_path / "epic-tasks" / "01-another-ticket.md").write_text("a\n")
    (tmp_path / "epic-tasks" / "255-cc-1-anchors.md").write_text("b\n")
    (tmp_path / "run.py").write_text("def run():\n    return 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "one")
    (tmp_path / "second.py").write_text("x = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "two")
    return tmp_path


def test_head_with_ancestor_suffix_is_one_ref_and_resolves_to_the_ancestor(repo):
    anchors = extract_anchors("it landed at HEAD~1 and not at HEAD^")
    assert [(a.kind, a.text) for a in anchors] == [("ref", "HEAD~1"), ("ref", "HEAD^")]
    head, parent = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD~1")
    got = resolve_anchors(anchors, PathRepoView(repo))
    assert [r.sha for r in got] == [parent, parent] and parent != head


def test_all_digit_commit_hash_is_an_anchor_when_the_claim_says_commit():
    assert [(a.kind, a.text) for a in extract_anchors("Commit 480164324 and @1234567 and sha 7654321")] == [
        ("commit", "480164324"), ("commit", "1234567"), ("commit", "7654321")]
    assert extract_anchors("it waited at 1234567 requests and `1234567` times") == []


def test_prefixed_ticket_is_found_by_its_hyphenated_slug(repo):
    [r] = resolve_anchors(extract_anchors("CC-1 depends on it"), PathRepoView(repo))
    assert (r.found, r.path) == (True, "epic-tasks/255-cc-1-anchors.md")


@pytest.mark.parametrize("claim", [
    "The branch `main` is ahead of `kc` by two commits.",
    "pytest `-x` stops at the first failure and `run` reports it.",
    "With `chmod` the `read` bit on a directory lets `ls` list `files`.",
    "`main` returns exit code 1 from the shell when `root` is set.",
])
def test_world_claim_with_common_backticked_words_stays_world(repo, claim):
    assert classify(claim, resolve_anchors(extract_anchors(claim), PathRepoView(repo))) == "world"


def test_extraction_of_long_hostile_text_is_linear():
    import time
    for text in ("0123abc" * 2000, "KC-" * 3000, "a.b.c" * 3000, "commit " + "a1" * 3000):
        start = time.perf_counter()
        extract_anchors(text)
        assert time.perf_counter() - start < 1.0


def _run_main(monkeypatch, tmp_path, extra):
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps(["`Policy.decide` never reads the second argument."]), encoding="utf-8")
    seen = {}
    real_tally = claim_vote.tally

    def spy(claims_, results, symbols=None, view=None):
        seen["view"] = view
        return real_tally(claims_, results, symbols, view=view)

    monkeypatch.setattr(claim_vote, "tally", spy)
    monkeypatch.setattr(claim_vote, "ask", lambda *a, **k: {"model": a[0], "run": a[2], "votes": {}, "error": None})
    assert claim_vote.main([str(claims), "--profiles", "a", "b", "c", "--repo-root", str(tmp_path), *extra]) == 0
    return seen["view"]


def test_no_view_without_symbols_root(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, tmp_path, []) is None


def test_symbols_root_builds_a_path_repo_view(monkeypatch, tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    view = _run_main(monkeypatch, tmp_path, ["--symbols-root", str(root)])
    assert isinstance(view, PathRepoView) and view.root == root
