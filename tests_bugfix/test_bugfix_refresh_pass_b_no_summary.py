"""tests_bug/test_bug_refresh_pass_b_no_summary.py

Bug (V7 LOW, tools/collect/cli.py:845/926 in `action_refresh`):
`pass_b_skipped` is only `True` when Pass B was skipped entirely by
config/no `llm_call` (`llm_call is None or not settings.llm_summaries`).
It is `False` — meaning `suffix` stays `""` — in the separate case where
Pass B *did* run but every changed module was filtered out of
`sources_for_summary` before the summarizer was ever called (e.g. every
changed module has a `parse_error`, so `to_summarize` is non-empty but
`sources_for_summary`/`batch` end up empty), leaving
`summarized_by_path = {}`.

In that case the message reads "incrementally refreshed N changed
module(s); wrote M file(s) in ..." with no indication that zero summaries
were produced — exactly the asymmetry `_full_build_message`'s `else`
branch already names for the sibling `--rebuild`/full-build path ("N
module(s) re-scanned, none re-summarized (Pass B produced no summary)").

Fix: `action_refresh`'s suffix now also covers "Pass B ran but produced
nothing" (`to_summarize` non-empty, `summarized_by_path` empty) with the
same "(Pass B produced no summary)" wording, distinct from "(Pass B
skipped)".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import action_collect, action_refresh


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """Same neutralization as tests/test_collect_incremental.py: COLLECT-10/15
    seed data cites real jan-auto-agent symbols that don't exist in these
    synthetic mini repos."""
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    _init_repo(tmp_path)
    return tmp_path


def _counting_llm_call():
    """A fake `LlmCall` that records every `(system, user)` pair it was
    called with — used to prove Pass B was never actually invoked in the
    parse-error case below."""
    calls = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps({"purpose": "stub purpose", "notes": ""})

    _call.calls = calls
    return _call


# ── the bug: Pass B ran, filtered everything out, message stayed silent ────


def test_refresh_message_names_a_parse_error_only_batch(mini_repo):
    """The only changed module becomes unparsable Python. `to_summarize`
    is non-empty (one changed file), but it never reaches the summarizer
    (filtered out for its parse_error before `sources_for_summary` is
    built), so zero LLM calls happen and `summarized_by_path` is empty.
    `pass_b_skipped` is False here (an `llm_call` was given and
    `llm_summaries` is on) — the message must still say Pass B produced
    nothing, distinct from "(Pass B skipped)".
    """
    llm = _counting_llm_call()
    action_collect(mini_repo, llm_call=llm)
    llm.calls.clear()

    (mini_repo / "pkg" / "a.py").write_text("def a(:\n    this is not python\n")

    result = action_refresh(mini_repo, llm_call=llm)

    assert llm.calls == [], "a parse-error module must never reach the summarizer"
    assert "(Pass B produced no summary)" in result.message
    assert "(Pass B skipped)" not in result.message
    assert result.message.startswith("incrementally refreshed 1 changed module(s)")


def test_refresh_message_names_it_even_when_every_changed_module_has_a_parse_error(mini_repo):
    """Both tracked-and-changed files become unparsable in the same
    refresh — still one non-empty `to_summarize` batch that produces
    nothing, still the "produced no summary" wording."""
    llm = _counting_llm_call()
    action_collect(mini_repo, llm_call=llm)
    llm.calls.clear()

    (mini_repo / "pkg" / "a.py").write_text("def a(:\n    broken\n")
    (mini_repo / "pkg" / "b.py").write_text("def b(:\n    also broken\n")

    result = action_refresh(mini_repo, llm_call=llm)

    assert llm.calls == []
    assert "(Pass B produced no summary)" in result.message


# ── regression guards: the other suffix branches are unaffected ────────────


def test_refresh_message_has_no_suffix_when_summarization_succeeds(mini_repo):
    """The ordinary path — Pass B runs and actually summarizes the changed
    module — must not gain a spurious suffix."""
    llm = _counting_llm_call()
    action_collect(mini_repo, llm_call=llm)
    llm.calls.clear()

    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")

    result = action_refresh(mini_repo, llm_call=llm)

    assert len(llm.calls) == 1
    assert "(Pass B" not in result.message
    assert result.message.startswith("incrementally refreshed 1 changed module(s)")


def test_refresh_message_says_skipped_when_no_llm_call_given(mini_repo):
    """Pass B genuinely skipped (no `llm_call` at all) keeps its own,
    distinct wording — must not be conflated with "produced no summary"."""
    action_collect(mini_repo)  # no llm_call: builds structural-only artifact

    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")

    result = action_refresh(mini_repo)  # still no llm_call

    assert "(Pass B skipped)" in result.message
    assert "(Pass B produced no summary)" not in result.message


def test_refresh_message_has_no_suffix_on_deletion_only_change(mini_repo):
    """A deletion-only refresh has no changed module at all, so
    `to_summarize` is empty and Pass B was never even a candidate to run —
    "skipped" would be dishonest (nothing to skip) and so would "produced
    no summary" (nothing to summarize)."""
    llm = _counting_llm_call()
    action_collect(mini_repo, llm_call=llm)
    llm.calls.clear()

    (mini_repo / "pkg" / "b.py").unlink()

    result = action_refresh(mini_repo, llm_call=llm)

    assert llm.calls == []
    assert "(Pass B" not in result.message
    assert result.message.startswith("incrementally refreshed 1 removed module(s)")
