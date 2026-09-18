"""tests_bugfix/test_bugfix_module_pass_b_no_summary.py

Bug (tools/collect/cli.py, `action_module`): `action_refresh` and
`_full_build_message` both name Pass B's outcome in their result message —
"(Pass B skipped)" when it was never attempted (`--no-llm` / `[collect]
llm_summaries = false`), "(Pass B produced no summary)" when it ran (or
would have) but produced nothing (a parse error, or a summarizer that
exhausted its retries). `action_module`'s own message never did either:
it always read "patched <path> and refreshed N file(s) in <dir>", even
when the patched module ended up with `summary is None` — silently
overclaiming that the module was re-summarized when it wasn't.

Found independently by two later fix proposals (`Dots-3-note.patch`,
`Ling30-var2.patch`) after the sibling bugs in `action_refresh` and
`parse_collect_args` were already fixed — both proposals collapsed the two
distinct reasons `patched.summary` can be `None` (config/no-llm vs. a
parse error) into a single "(Pass B produced no summary)" suffix, which
would have misreported a `--no-llm` module patch as "produced no summary"
instead of "skipped". This fix keeps the same two-way split
`action_refresh`'s own `pass_b_skipped` already makes.

Fix: `action_module` now tracks `pass_b_skipped` (`llm_call is None or not
settings.llm_summaries` — Pass B never attempted) separately from
`patched.summary is None` after a genuine attempt (parse error, or a
summarizer that gave up) and appends the matching suffix.
"""

from __future__ import annotations

import configparser
import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import ARTIFACT_FILENAME, action_collect, action_module


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


def _artifact_modules(collect_dir: Path) -> dict:
    payload = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    return {m["path"]: m for m in payload["modules"]}


def _counting_llm_call():
    calls = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps({"purpose": "stub purpose", "notes": ""})

    _call.calls = calls
    return _call


# ── the bug: a summary-less patched module said nothing about Pass B ───────


def test_module_with_parse_error_says_no_summary(mini_repo):
    """The patched module has a parse error — Pass B would have run but
    there is nothing to summarize. Must be worded as "produced no
    summary", not "skipped" (an `llm_call` WAS given)."""
    action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def broken(:\n")
    llm = _counting_llm_call()

    result = action_module(mini_repo, "pkg/a.py", llm_call=llm)

    assert "patched pkg/a.py" in result.message
    assert "(Pass B produced no summary)" in result.message
    assert "(Pass B skipped)" not in result.message
    assert llm.calls == []
    modules = _artifact_modules(result.collect_dir)
    assert modules["pkg/a.py"]["summary"] is None


def test_module_with_no_llm_call_says_skipped_not_no_summary(mini_repo):
    """No `llm_call` at all: this is the "(Pass B skipped)" case — the
    same wording `action_refresh` gives `--no-llm`, not "produced no
    summary" (which the two independent later proposals both used here,
    conflating the two situations)."""
    action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 99\n")

    result = action_module(mini_repo, "pkg/a.py", llm_call=None)

    assert "(Pass B skipped)" in result.message
    assert "(Pass B produced no summary)" not in result.message
    modules = _artifact_modules(result.collect_dir)
    assert modules["pkg/a.py"]["summary"] is None


def test_module_with_llm_summaries_disabled_says_skipped(mini_repo):
    """`[collect] llm_summaries = false` is also a "skipped" reason, even
    with an `llm_call` given — mirrors `action_refresh`'s own
    `pass_b_skipped` condition exactly."""
    action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 99\n")
    config = configparser.ConfigParser()
    config.read_dict({"collect": {"llm_summaries": "false"}})

    result = action_module(mini_repo, "pkg/a.py", config=config, llm_call=_counting_llm_call())

    assert "(Pass B skipped)" in result.message
    assert "(Pass B produced no summary)" not in result.message


# ── regression guard: a genuinely re-summarized module keeps its plain message ──


def test_module_successfully_summarized_has_no_suffix(mini_repo):
    action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 99\n")

    result = action_module(mini_repo, "pkg/a.py", llm_call=_counting_llm_call())

    assert result.message == (
        f"patched pkg/a.py and refreshed {len(result.written_files)} "
        f"file(s) in {result.collect_dir}"
    )
    modules = _artifact_modules(result.collect_dir)
    assert modules["pkg/a.py"]["summary"]["purpose"] == "stub purpose"
