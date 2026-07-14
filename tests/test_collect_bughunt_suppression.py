"""tests/test_collect_bughunt_suppression.py — COLLECT-22.

* A candidate citing a guarded access ("stack[-1] crashes") is
  auto-suppressed, with the reason/detail sourced from the static guard.
* A candidate whose only backing is an LLM's `notes` (prose) is NOT
  auto-suppressed — `is_safe()` never reads `ModuleRecord.summary` at all.
* A real, unguarded bug is never suppressed (fails closed).
* Every verdict (suppressed or not) is logged with its reason.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.collect import bughunt_filter as bf
from tools.collect import cli as cli_mod
from tools.collect import loader as loader_mod


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "def guarded():\n"
        "    stack = []\n"
        "    if not stack:\n"
        "        return None\n"
        "    return stack[-1]\n"       # line 5 — GUARDED
        "\n"
        "def unguarded():\n"
        "    items = []\n"
        "    return items[-1]\n"       # line 9 — UNGUARDED (real bug)
        "\n"
        "def swallow():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"               # fail-open site
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _load(mini_repo: Path) -> loader_mod.CollectModel:
    cli_mod.action_collect(mini_repo)
    return loader_mod.load(mini_repo)


def test_guarded_candidate_is_suppressed(mini_repo):
    model = _load(mini_repo)
    candidate = bf.BughuntCandidate(location="pkg/a.py:5", access="stack[-1]", claim="stack[-1] crashes")

    verdicts = bf.suppress([candidate], model)
    assert len(verdicts) == 1
    assert verdicts[0].suppressed is True
    assert verdicts[0].reason == "guarded"
    assert verdicts[0].detail  # the guard description


def test_real_unguarded_bug_is_not_suppressed(mini_repo):
    model = _load(mini_repo)
    candidate = bf.BughuntCandidate(location="pkg/a.py:9", access="items[-1]", claim="items[-1] crashes on empty list")

    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is False
    assert verdicts[0].reason == "unguarded"


def test_fail_open_site_is_suppressed(mini_repo):
    model = _load(mini_repo)
    record = model.module("pkg/a.py")
    site = model.fail_open_registry
    assert site, "expected at least one fail-open entry"
    location = site[0].location

    candidate = bf.BughuntCandidate(location=location, claim="silent except swallows something important")
    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is True
    assert verdicts[0].reason == "fail_open"


def test_candidate_resting_only_on_llm_prose_is_not_suppressed(mini_repo):
    """A candidate whose location isn't a recorded guarded_access,
    fail-open site, or contract at all -- i.e. all we have is an LLM's
    say-so -- must not be auto-suppressed, because `is_safe()` never
    looks at LLM notes in the first place."""
    model = _load(mini_repo)
    # pkg/a.py:2 is inside `guarded()` but isn't the indexed-access line
    # itself, so no static source vouches for it -- mirrors a candidate
    # that only an LLM's prose (never consulted here) would speak to.
    candidate = bf.BughuntCandidate(location="pkg/a.py:2", claim="LLM notes claim this line is fine")

    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is False
    assert verdicts[0].reason == "unknown"


def test_surviving_candidates_filters_out_suppressed(mini_repo):
    model = _load(mini_repo)
    guarded = bf.BughuntCandidate(location="pkg/a.py:5", access="stack[-1]", claim="stack[-1] crashes")
    real_bug = bf.BughuntCandidate(location="pkg/a.py:9", access="items[-1]", claim="items[-1] crashes")

    verdicts = bf.suppress([guarded, real_bug], model)
    survivors = bf.surviving_candidates(verdicts)
    assert survivors == [real_bug]


def test_absent_model_suppresses_nothing(mini_repo):
    # collect never ran -- "no model" -- so nothing gets suppressed.
    model = loader_mod.load(mini_repo)
    assert model.status == loader_mod.STATUS_ABSENT

    candidate = bf.BughuntCandidate(location="pkg/a.py:5", access="stack[-1]", claim="stack[-1] crashes")
    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is False
    assert verdicts[0].reason == "unknown"


def test_every_verdict_is_logged_with_its_reason(mini_repo, caplog):
    import logging

    model = _load(mini_repo)
    guarded = bf.BughuntCandidate(location="pkg/a.py:5", access="stack[-1]", claim="stack[-1] crashes")
    real_bug = bf.BughuntCandidate(location="pkg/a.py:9", access="items[-1]", claim="items[-1] crashes")

    with caplog.at_level(logging.INFO, logger="tools.collect.bughunt_filter"):
        bf.suppress([guarded, real_bug], model)

    messages = [r.message for r in caplog.records]
    assert any("suppressed" in m and "reason=guarded" in m for m in messages)
    assert any("NOT suppressed" in m and "reason=unguarded" in m for m in messages)


def test_suppress_for_root_convenience_wrapper(mini_repo):
    _load(mini_repo)  # ensure fresh artifact exists on disk
    candidate = bf.BughuntCandidate(location="pkg/a.py:5", access="stack[-1]", claim="stack[-1] crashes")

    verdicts = bf.suppress_for_root(mini_repo, [candidate])
    assert verdicts[0].suppressed is True
