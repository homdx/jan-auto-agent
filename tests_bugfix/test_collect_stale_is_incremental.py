"""tests_bugfix/test_collect_stale_is_incremental.py — V7: `--collect` on a stale
tree used to be a full rebuild.

`action_collect` called `action_check` and, on a stale verdict, went
straight to `_full_build` — which re-ran Pass B (the only part of the
pipeline that makes a network call) over *every* module in the tree. One
changed file cost the same as a from-scratch build: ~483 LLM calls here
instead of 1, on the default path.

The docs said the opposite of what the code did. `main.py`'s `--refresh`
help read "unconditional full rebuild, ignoring freshness" and README.md
read "`--refresh` forces a full rebuild", while `--refresh` is in fact the
diff-driven incremental path and `_full_build` was only ever reached by
deleting `[collect] dir`.

After the fix:
  1. `action_collect` delegates a stale tree to `action_refresh` — one Pass B
     call per changed module, unchanged modules reuse their previous record
     (summary included) verbatim.
  2. A fresh tree is still a pure no-op: zero writes, zero LLM calls.
  3. `action_rebuild` (`--collect --rebuild` / `/collect --rebuild`) is the
     unconditional full rebuild, wired to today's `_full_build` path: one
     call per module, no freshness gate, no dependence on a prior artifact.
  4. A `collector_version` mismatch still forces a full rebuild — that is
     the schema-mismatch path, not the stale-tree path, and
     `manifest.is_fresh` / `action_refresh` own it.
  5. The message says which path ran: "incrementally refreshed N changed
     module(s)" vs "N module(s) re-summarized" vs "no prior artifact ...
     full build" vs "manifest was built by collector_version=... — full
     build". A full build without Pass B says "re-scanned (Pass B
     skipped)", never "re-summarized".
  6. `--rebuild` beats `--refresh` in both entry points (`parse_collect_args`
     for `/collect`, `main.py`'s if-chain for `--collect`), so asking for
     both never silently downgrades to the cheap path.
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
    action_check,
    action_collect,
    action_rebuild,
    action_refresh,
    parse_collect_args,
    run,
)


# ── helpers ──────────────────────────────────────────────────────────────────

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


def _tree_hash(root: Path) -> str:
    """Every file under root (except .collect/.git) as path+bytes, so a
    "zero writes" claim can be checked for the whole tree at once."""
    parts = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if ".collect" in path.parts or ".git" in path.parts:
            continue
        parts.append(str(path.relative_to(root)))
        parts.append(path.read_bytes())
    return str(parts)


def _counting_llm_call() -> object:
    """Fake `LlmCall` recording every (system, user) pair it was called
    with, so the cost of a given action can be asserted exactly."""
    calls: list = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps({"purpose": "stub purpose", "notes": ""})

    _call.calls = calls
    return _call


def _change_a(root: Path) -> None:
    (root / "pkg" / "a.py").write_text(
        "def a():\n    return 42\n\ndef extra():\n    pass\n"
    )


def _main_dispatches(tmp_path: Path, *flags: str) -> str:
    """Run `main.py --collect <flags>` against a stubbed `cli.run` and
    return the action `main.py` chose — the flag if-chain in its one-shot
    branch is code `parse_collect_args` does not cover."""
    import main as main_mod

    captured: dict = {}

    def _fake_run(root, action, **kwargs):
        captured["action"] = action
        return cli_mod.CollectResult(action=action, wrote=False, fresh=True, message="ok")

    argv = ["main.py", "--collect", *flags, "--base", str(tmp_path)]
    with patch("tools.collect.cli.run", _fake_run), patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
    assert exc_info.value.code == 0
    return captured["action"]


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """Same neutralisation as `test_collect_cli.py`: COLLECT-10/15 seed
    data cites real jan-auto-agent symbols that don't exist in these
    synthetic mini repos. This file tests `collect` orchestration, not
    seed content."""
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


def _module_count(mini_repo: Path) -> int:
    """How many modules the current artifact records — one `--rebuild` must
    cost exactly that many LLM calls (one per module)."""
    return len(_artifact_modules(mini_repo / ".collect"))


# ── 1. stale tree + --collect == one LLM call per changed module ─────────────

class TestCollectStaleIsIncremental:
    def test_one_changed_file_is_one_llm_call(self, mini_repo):
        """The bug: before the fix this was one call per module in the
        repo — a full Pass B over the whole tree for a one-line edit."""
        llm = _counting_llm_call()
        first = action_collect(mini_repo, llm_call=llm)
        assert len(_artifact_modules(first.collect_dir)) > 1, "sanity: multi-module repo"
        llm.calls.clear()

        _change_a(mini_repo)

        result = action_collect(mini_repo, llm_call=llm)

        assert result.action == "collect"
        assert result.wrote is True
        assert result.fresh is True
        assert len(llm.calls) == 1, (
            f"--collect on a stale tree made {len(llm.calls)} LLM calls; the incremental "
            f"path makes exactly one, for the one changed module."
        )
        _system, user_msg = llm.calls[0]
        assert "pkg/a.py" in user_msg

    def test_message_names_the_incremental_path(self, mini_repo):
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        _change_a(mini_repo)

        result = action_collect(mini_repo, llm_call=llm)

        assert "incrementally refreshed 1 changed" in result.message
        assert "re-summarized" not in result.message

    def test_changed_module_is_rescanned(self, mini_repo):
        action_collect(mini_repo)
        _change_a(mini_repo)

        result = action_collect(mini_repo)

        modules = _artifact_modules(result.collect_dir)
        qualnames = {s["qualname"] for s in modules["pkg/a.py"]["public_symbols"]}
        assert "pkg/a.py:extra" in qualnames  # new symbol visible -> really re-scanned
        assert "pkg/b.py" in modules

    def test_unchanged_module_summary_reused_verbatim(self, mini_repo):
        llm = _counting_llm_call()
        first = action_collect(mini_repo, llm_call=llm)
        before = _artifact_modules(first.collect_dir)
        _change_a(mini_repo)

        result = action_collect(mini_repo, llm_call=llm)
        after = _artifact_modules(result.collect_dir)

        assert after["pkg/b.py"]["summary"] == before["pkg/b.py"]["summary"]
        assert after["pkg/a.py"]["summary"]["purpose"] == "stub purpose"

    def test_manifest_is_refreshed_so_check_reports_fresh_again(self, mini_repo):
        action_collect(mini_repo)
        _change_a(mini_repo)
        assert action_check(mini_repo).fresh is False

        action_collect(mini_repo)

        assert action_check(mini_repo).fresh is True

    def test_added_file_is_summarized(self, mini_repo):
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        (mini_repo / "pkg" / "c.py").write_text("def c():\n    return 3\n")
        llm.calls.clear()

        result = action_collect(mini_repo, llm_call=llm)

        assert len(llm.calls) == 1
        assert "pkg/c.py" in _artifact_modules(result.collect_dir)

    def test_deletion_only_change_makes_no_llm_calls(self, mini_repo):
        """Nothing to summarize -> nothing to call, but the artifact is
        still brought up to date and the manifest refreshed."""
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        (mini_repo / "pkg" / "b.py").unlink()
        llm.calls.clear()

        result = action_collect(mini_repo, llm_call=llm)

        assert llm.calls == []
        assert "pkg/b.py" not in _artifact_modules(result.collect_dir)
        assert action_check(mini_repo).fresh is True

    def test_collect_matches_an_explicit_refresh(self, mini_repo):
        """`--collect` on a stale tree and `--refresh` are one code path,
        so they cost the same and report the same message."""
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        _change_a(mini_repo)
        llm.calls.clear()

        result_collect = action_collect(mini_repo, llm_call=llm)
        calls_collect = len(llm.calls)

        llm2 = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm2)
        # a *different* second edit — writing the same bytes again would
        # leave the manifest hash untouched and cost zero calls
        (mini_repo / "pkg" / "a.py").write_text(
            "def a():\n    return 42\n\ndef extra():\n    pass\n\ndef extra2():\n    pass\n"
        )
        llm2.calls.clear()

        result_refresh = action_refresh(mini_repo, llm_call=llm2)

        assert calls_collect == len(llm2.calls) == 1
        assert result_collect.message == result_refresh.message
        assert result_collect.action == "collect"
        assert result_refresh.action == "refresh"


# ── 2. fresh tree stays a pure no-op ─────────────────────────────────────────


class TestCollectFreshIsNoop:
    def test_fresh_tree_writes_nothing_and_calls_nothing(self, mini_repo):
        llm = _counting_llm_call()
        first = action_collect(mini_repo, llm_call=llm)
        collect_dir = first.collect_dir
        tree_before = _tree_hash(mini_repo)
        dir_before = sorted(p.name for p in collect_dir.iterdir())
        contents_before = {p.name: p.read_bytes() for p in collect_dir.iterdir()}
        llm.calls.clear()

        result = action_collect(mini_repo, llm_call=llm)

        assert result.wrote is False
        assert result.fresh is True
        assert "nothing to do" in result.message
        assert llm.calls == []
        assert _tree_hash(mini_repo) == tree_before
        assert sorted(p.name for p in collect_dir.iterdir()) == dir_before
        assert {p.name: p.read_bytes() for p in collect_dir.iterdir()} == contents_before


# ── 3. --rebuild is the unconditional full rebuild ───────────────────────────


class TestRebuildIsUnconditionalFullBuild:
    def test_one_llm_call_per_module(self, mini_repo):
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        n = _module_count(mini_repo)
        assert n > 1, "sanity: multi-module repo"
        llm.calls.clear()

        result = action_rebuild(mini_repo, llm_call=llm)

        assert result.action == "rebuild"
        assert result.wrote is True
        assert result.fresh is True
        assert len(llm.calls) == n, f"expected one call per module ({n}), got {len(llm.calls)}"
        assert f"{n} module(s) re-summarized" in result.message
        assert "incrementally refreshed" not in result.message

    def test_runs_even_when_fresh(self, mini_repo):
        """No freshness gate at all: the point is to re-summarize
        everything, even when there is nothing to catch up on."""
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        assert action_check(mini_repo).fresh is True
        llm.calls.clear()

        result = action_rebuild(mini_repo, llm_call=llm)

        assert result.wrote is True
        assert len(llm.calls) == _module_count(mini_repo)

    def test_runs_with_no_prior_artifact(self, mini_repo):
        """`--rebuild` does not need anything to diff against — unlike
        `refresh`'s fallback, it is the primary path here."""
        assert not (mini_repo / ".collect").exists()

        result = action_rebuild(mini_repo)

        assert result.action == "rebuild"
        assert (result.collect_dir / ARTIFACT_FILENAME).exists()
        assert "module(s) re-scanned" in result.message  # no llm_call -> Pass B skipped
        assert "no prior artifact" not in result.message

    def test_rebuild_without_pass_b_does_not_claim_to_have_summarized(self, mini_repo):
        """`--no-llm` / `[collect] llm_summaries = false` / a summarizer that
        could not be built all reach `action_rebuild` with `llm_call=None`.
        The message must say what ran: every module was re-scanned, none was
        re-summarized."""
        action_collect(mini_repo, llm_call=_counting_llm_call())

        result = action_rebuild(mini_repo)  # llm_call=None

        assert f"{_module_count(mini_repo)} module(s) re-scanned (Pass B skipped)" in result.message
        assert "re-summarized" not in result.message
        assert all(m["summary"] is None for m in _artifact_modules(result.collect_dir).values())

    def test_resummarizes_every_module_regardless_of_which_changed(self, mini_repo):
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        before = _artifact_modules(mini_repo / ".collect")
        n = len(before)
        _change_a(mini_repo)
        llm.calls.clear()

        action_rebuild(mini_repo, llm_call=llm)

        assert len(llm.calls) == n
        user_msgs = [user for _system, user in llm.calls]
        # the untouched module got re-summarized too — that is the whole
        # difference between --rebuild and the incremental path
        for path in before:
            assert any(path in user for user in user_msgs), f"{path} was not re-summarized"

    def test_rebuild_stays_clean_on_a_clean_tree(self, mini_repo):
        """`_full_build` captures provenance before writing under .collect/,
        so a clean tree must still record dirty=False through the new
        entry point."""
        action_rebuild(mini_repo)
        manifest = json.loads(
            (mini_repo / ".collect" / MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        assert manifest["dirty"] is False


# ── 4. schema mismatch still forces a full rebuild ───────────────────────────


class TestVersionMismatchStillForcesFullBuild:
    def _bump_manifest_version(self, mini_repo: Path) -> Path:
        manifest_path = mini_repo / ".collect" / MANIFEST_FILENAME
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["collector_version"] = "999.0.0-future-schema"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        return manifest_path

    def test_collect_forces_a_full_rebuild_on_mismatch(self, mini_repo):
        """Migration note: a collector_version mismatch must still force a
        full rebuild. This ticket changes the *stale-tree* path only, not
        the *schema-mismatch* path."""
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        self._bump_manifest_version(mini_repo)
        llm.calls.clear()

        result = action_collect(mini_repo, llm_call=llm)

        assert "full build" in result.message
        assert "incrementally refreshed" not in result.message
        assert len(llm.calls) == _module_count(mini_repo)

    def test_mismatch_message_names_the_reason_not_a_missing_artifact(self, mini_repo):
        """Before V7 both fallbacks said "no prior artifact to diff against",
        which is false for a version mismatch — the artifact is right there,
        it just cannot be trusted. The line now says which path ran and why,
        and counts what Pass B did."""
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        self._bump_manifest_version(mini_repo)
        llm.calls.clear()

        result = action_collect(mini_repo, llm_call=llm)

        assert "no prior artifact" not in result.message
        assert "collector_version='999.0.0-future-schema'" in result.message
        assert f"current is {cli_mod.manifest_mod.COLLECTOR_VERSION!r}" in result.message
        assert f"{_module_count(mini_repo)} module(s) re-summarized" in result.message

    def test_first_ever_collect_says_no_prior_artifact(self, mini_repo):
        llm = _counting_llm_call()

        result = action_collect(mini_repo, llm_call=llm)

        assert result.message.startswith("no prior artifact to diff against — full build: ")
        assert f"{len(llm.calls)} module(s) re-summarized" in result.message

    def test_matching_version_with_one_change_stays_incremental(self, mini_repo):
        """The guard must not turn every stale run into a full rebuild."""
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        _change_a(mini_repo)
        llm.calls.clear()

        result = action_collect(mini_repo, llm_call=llm)

        assert len(llm.calls) == 1
        assert "full build" not in result.message

    def test_rebuild_bumps_the_manifest_to_the_current_version(self, mini_repo):
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        self._bump_manifest_version(mini_repo)

        action_rebuild(mini_repo, llm_call=llm)

        manifest = json.loads(
            (mini_repo / ".collect" / MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        assert manifest["collector_version"] != "999.0.0-future-schema"
        assert action_check(mini_repo).fresh is True


# ── 5. dispatch / arg parsing for the new flag ───────────────────────────────


class TestRebuildWiring:
    def test_parse_collect_args_rebuild(self):
        assert parse_collect_args(["--rebuild"]) == {"action": "rebuild", "module_path": None}

    def test_parse_collect_args_rebuild_ignores_late_module(self):
        assert parse_collect_args(["--rebuild", "--module", "pkg/a.py"]) == {
            "action": "rebuild",
            "module_path": None,
        }

    def test_parse_collect_args_check_still_wins(self):
        assert parse_collect_args(["--check", "--rebuild"]) == {"action": "check", "module_path": None}

    def test_rebuild_wins_over_refresh_in_both_entry_points(self, tmp_path):
        """`/collect --refresh --rebuild` (parse_collect_args) and
        `--collect --refresh --rebuild` (main.py's own if-chain) must agree,
        and the unconditional flag is the one that wins — asking for both
        and getting the cheap one would be a silent downgrade."""
        assert parse_collect_args(["--refresh", "--rebuild"])["action"] == "rebuild"
        assert _main_dispatches(tmp_path, "--refresh", "--rebuild") == "rebuild"

    def test_main_rebuild_flag_dispatches_the_rebuild_action(self, tmp_path):
        assert _main_dispatches(tmp_path, "--rebuild") == "rebuild"

    def test_main_without_rebuild_still_dispatches_collect(self, tmp_path):
        assert _main_dispatches(tmp_path) == "collect"

    def test_main_module_still_wins_over_rebuild(self, tmp_path):
        assert _main_dispatches(tmp_path, "--rebuild", "--module", "pkg/a.py") == "module"

    def test_run_dispatches_rebuild(self, mini_repo):
        llm = _counting_llm_call()
        result = run(mini_repo, "rebuild", llm_call=llm)

        assert result.action == "rebuild"
        assert len(llm.calls) == _module_count(mini_repo)

    def test_rebuild_honours_the_collect_enabled_switch(self, mini_repo):
        config = configparser.ConfigParser()
        config.read_dict({"collect": {"enabled": "false"}})

        result = run(mini_repo, "rebuild", config=config)

        assert result.wrote is False
        assert "disabled" in result.message

    def test_collect_dispatch_stays_incremental(self, mini_repo):
        """`run('collect')` is the entry point `main.py` uses; it must take
        the incremental path too, not bypass `action_collect`."""
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        _change_a(mini_repo)
        llm.calls.clear()

        result = run(mini_repo, "collect", llm_call=llm)

        assert result.action == "collect"
        assert len(llm.calls) == 1
