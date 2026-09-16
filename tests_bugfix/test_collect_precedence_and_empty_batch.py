"""tests_bugfix/test_collect_precedence_and_empty_batch.py — two
collect defects:

Bug 1 — flag precedence in parse_collect_args:
`parse_collect_args(['--check', '--module'])` raised CollectCliError
because `--module` was validated before `--check` was resolved. Per the
documented precedence table, `--check` (read-only) must win over
`--module` (write request): a check must never write, whatever else
was asked. The fix resolves `--check` before touching `--module`'s
value.

Bug 2 — action_refresh honesty when Pass B ran on an empty batch:
When every changed module had a parse error, Pass B ran on zero
modules (0 LLM calls) but the message said "incrementally refreshed
N changed module(s)" with no indication nothing was summarized. The
fix adds "(Pass B produced no summary)" mirroring _full_build_message.
"""

from __future__ import annotations

import configparser
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    CollectCliError,
    action_collect,
    action_refresh,
    parse_collect_args,
    run,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


def _artifact_modules(collect_dir: Path) -> dict:
    payload = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    return {m["path"]: m for m in payload["modules"]}


def _counting_llm_call() -> object:
    calls: list = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps({"purpose": "stub purpose", "notes": ""})

    _call.calls = calls
    return _call


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root=None: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    _init_repo(tmp_path)
    return tmp_path


@pytest.fixture
def built_repo(mini_repo: Path) -> Path:
    action_collect(mini_repo)
    return mini_repo


# ── Bug 1: --check must short-circuit before --module validation ────


class TestCheckShortCircuitsModule:
    def test_check_with_malformed_module_returns_check(self):
        """Bug: `--module` with no value was validated before `--check`
        was resolved, so `parse_collect_args(['--check', '--module'])`
        raised CollectCliError instead of returning check."""
        result = parse_collect_args(["--check", "--module"])
        assert result == {"action": "check", "module_path": None}

    def test_check_with_empty_module_returns_check(self):
        """Same precedence violation with an explicit empty value."""
        result = parse_collect_args(["--check", "--module", ""])
        assert result == {"action": "check", "module_path": None}

    def test_check_with_equals_form_malformed_returns_check(self):
        """`--module=` with no value, guarded by `--check`."""
        result = parse_collect_args(["--check", "--module="])
        assert result == {"action": "check", "module_path": None}

    def test_check_with_flag_value_malformed_returns_check(self):
        """`--module` followed by another flag — still caught by
        `--check` short-circuit, never reaches _module_path_from."""
        result = parse_collect_args(["--check", "--module", "--no-llm"])
        assert result == {"action": "check", "module_path": None}

    def test_malformed_module_without_check_still_raises(self):
        """Without `--check`, the existing guard must still fire —
        `--module` with no value is always an error."""
        with pytest.raises(CollectCliError):
            parse_collect_args(["--module"])

    def test_check_still_writes_nothing_end_to_end(self, built_repo):
        """`--check --module` (malformed or not) must not write on
        either entry point — --check's promise."""
        collect_dir = built_repo / ".collect"
        before = sorted(p.name for p in collect_dir.iterdir())

        kwargs = parse_collect_args(["--check", "--module"])
        result = run(built_repo, **kwargs)

        assert kwargs["action"] == "check"
        assert result.wrote is False
        assert sorted(p.name for p in collect_dir.iterdir()) == before


# ── Bug 2: action_refresh honesty when Pass B ran on empty batch ────


class TestRefreshEmptyBatchHonesty:
    def test_all_changed_modules_with_parse_error_says_no_summary(
        self, built_repo
    ):
        """Bug: when every changed module had a parse error, Pass B
        ran on an empty batch (0 LLM calls) but the message said
        'incrementally refreshed N changed module(s)...' with no
        indication nothing was summarized."""
        (built_repo / "pkg" / "broken.py").write_text("def broken(:\n")
        llm = _counting_llm_call()

        result = action_refresh(built_repo, llm_call=llm)

        assert "Pass B produced no summary" in result.message
        assert "Pass B skipped" not in result.message
        assert llm.calls == [], "no module had a source — no LLM call should have been made"

    def test_some_valid_changed_modules_does_not_claim_no_summary(
        self, built_repo
    ):
        """Regression guard: if at least one changed module has a valid
        source, Pass B ran normally and must NOT say 'no summary'."""
        action_collect(built_repo)
        (built_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")
        llm = _counting_llm_call()

        result = action_refresh(built_repo, llm_call=llm)

        assert "Pass B produced no summary" not in result.message
        assert "incrementally refreshed" in result.message

    def test_no_summary_message_does_not_claim_skipped(self, built_repo):
        """'(Pass B skipped)' is for --no-llm / config-disabled.
        A refresh that ran Pass B on an empty batch is a different
        situation and must not say 'skipped'."""
        (built_repo / "pkg" / "broken.py").write_text("def broken(:\n")

        result = action_refresh(built_repo, llm_call=_counting_llm_call())

        assert "Pass B produced no summary" in result.message
        assert "Pass B skipped" not in result.message

    def test_no_summary_uses_honest_verb(self, built_repo):
        """The message must not imply a summary was derived when none
        was."""
        (built_repo / "pkg" / "broken.py").write_text("def broken(:\n")

        result = action_refresh(built_repo, llm_call=_counting_llm_call())

        assert "re-summarized" not in result.message
        assert "re-scanned" not in result.message
        assert "Pass B produced no summary" in result.message
