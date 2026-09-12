"""tests_bugfix/test_collect_check_dispatch_followups.py — V7 follow-ups.

Three defects in the code V7 put in charge of deciding *whether* to rebuild
and *how to name the flag*:

1. `action_check` read only `collect_manifest.json`. A manifest with no
   `artifact.json` beside it — someone deleted a suspect artifact, or a
   partial/failed write left the manifest behind — reported `fresh=True` and
   `up to date`, so `action_collect` took its no-op branch forever while the
   consumer was handed nothing. `loader.load()` and `action_refresh()` both
   treat the manifest+artifact pair as a unit; the check did not.

2. `parse_collect_args` recognised only the space form of `--module`.
   argparse accepts both `--module <path>` and `--module=<path>`, so
   `main.py --collect --module=pkg/a.py` patched one module while
   `/collect --module=pkg/a.py` silently ran a full-tree collect — the same
   "the two entry points disagree" defect V7 closed for
   `--refresh`/`--rebuild`, one flag later.

3. `_full_build_message` counted `len(ctx.modules)`, not the modules Pass B
   actually summarised. A tree with one module that produced no prose (a
   syntax error, invalid UTF-8, an LLM call that exhausted its retries)
   still read "N module(s) re-summarized" for every module — overcounted on
   the single number V7 made the thing a full build reports.
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
    action_check,
    action_collect,
    action_rebuild,
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


def _counting_llm_call() -> object:
    calls: list = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps({"purpose": "stub purpose", "notes": ""})

    _call.calls = calls
    return _call


def _raising_llm_call() -> object:
    """An LLM call that never succeeds — the second way a module ends up
    with `summary=None` after Pass B."""
    calls: list = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        raise RuntimeError("stub summarizer is down")

    _call.calls = calls
    return _call


def _no_retry_config() -> configparser.ConfigParser:
    """`max_retries = 0`: a failing stub would otherwise be retried on the
    real backoff schedule, which both slows the test down and masks a
    deterministic failure (a 50 %-flaky stub recovers on retry, so it never
    demonstrates a partial result at all)."""
    config = configparser.ConfigParser()
    config.read_dict({"collect": {"max_retries": "0"}})
    return config


def _main_dispatches(tmp_path: Path, *flags: str) -> str:
    """Run `main.py --collect <flags>` against a stubbed `cli.run` and
    return the action main.py chose — the argparse if-chain in its one-shot
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
    synthetic mini repos. This file tests collect orchestration, not seed
    content."""
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


@pytest.fixture
def built_repo(mini_repo: Path) -> Path:
    """A repo with a complete, fresh artifact — the state every test here
    starts from when it needs something to break."""
    action_collect(mini_repo)
    return mini_repo


# ── 1. a manifest without its artifact is not fresh ──────────────────────────


class TestCheckRequiresArtifact:
    def test_manifest_without_artifact_is_stale(self, built_repo):
        """The bug: this reported `fresh=True` / `up to date`."""
        (built_repo / ".collect" / ARTIFACT_FILENAME).unlink()

        result = action_check(built_repo)

        assert result.fresh is False
        assert result.wrote is False
        assert ARTIFACT_FILENAME in result.message
        assert "up to date" not in result.message

    def test_collect_rebuilds_instead_of_no_oping(self, built_repo):
        """V7's `action_collect` is freshness-gated, so a false `fresh=True`
        here was a permanent no-op: the manifest said the tree was current
        while the consumer had nothing to load."""
        llm = _counting_llm_call()
        (built_repo / ".collect" / ARTIFACT_FILENAME).unlink()

        result = action_collect(built_repo, llm_call=llm)

        assert result.wrote is True
        assert (result.collect_dir / ARTIFACT_FILENAME).exists()
        assert len(llm.calls) == 3, "a missing artifact means a full build, not a no-op"
        assert "nothing to do" not in result.message

    def test_rebuild_restores_the_pair_and_check_is_fresh_again(self, built_repo):
        (built_repo / ".collect" / ARTIFACT_FILENAME).unlink()
        assert action_check(built_repo).fresh is False

        action_rebuild(built_repo)

        assert action_check(built_repo).fresh is True

    def test_missing_manifest_message_is_unchanged(self, built_repo):
        """A missing manifest is still reported as "never run", not
        conflated with the new artifact-missing verdict."""
        (built_repo / ".collect" / MANIFEST_FILENAME).unlink()

        result = action_check(built_repo)

        assert result.fresh is False
        assert "has never run" in result.message

    def test_complete_pair_is_still_fresh(self, built_repo):
        """The fix must not turn a healthy tree into a perpetual rebuild."""
        assert action_check(built_repo).fresh is True

        result = action_collect(built_repo, llm_call=_counting_llm_call())

        assert result.wrote is False
        assert "nothing to do" in result.message

    def test_check_still_writes_nothing(self, built_repo):
        """`--check` never writes — including on the new verdict."""
        (built_repo / ".collect" / ARTIFACT_FILENAME).unlink()
        before = sorted(p.name for p in (built_repo / ".collect").iterdir())

        action_check(built_repo)

        assert sorted(p.name for p in (built_repo / ".collect").iterdir()) == before
        assert not (built_repo / ".collect" / ARTIFACT_FILENAME).exists()


# ── 2. `--module=<path>` reaches the same action as `--module <path>` ────────


class TestModuleEqualsForm:
    def test_parse_collect_args_accepts_equals_form(self):
        assert parse_collect_args(["--module=pkg/a.py"]) == {
            "action": "module",
            "module_path": "pkg/a.py",
        }

    def test_space_form_still_works(self):
        assert parse_collect_args(["--module", "pkg/a.py"]) == {
            "action": "module",
            "module_path": "pkg/a.py",
        }

    def test_both_entry_points_agree(self, tmp_path):
        """The defect V7 closed for `--refresh`/`--rebuild`: `main.py`
        (argparse) and `/collect` (`parse_collect_args`) must not silently
        run different actions for the same request."""
        assert parse_collect_args(["--module=pkg/a.py"])["action"] == "module"
        assert _main_dispatches(tmp_path, "--module=pkg/a.py") == "module"
        assert _main_dispatches(tmp_path, "--module", "pkg/a.py") == "module"

    def test_equals_form_patches_one_module(self, built_repo):
        """Round trip through the real dispatch, not just the parser."""
        llm = _counting_llm_call()
        kwargs = parse_collect_args(["--module=pkg/a.py"])

        result = run(built_repo, llm_call=llm, **kwargs)

        assert result.action == "module"
        assert len(llm.calls) == 1
        assert "patched pkg/a.py" in result.message

    def test_equals_form_sits_in_the_same_precedence_table(self):
        """Same precedence as the space form (`action_from_flags`): the one
        named file beats the whole-tree flags, and `--check` beats it."""
        assert parse_collect_args(["--rebuild", "--module=pkg/a.py"]) == {
            "action": "module",
            "module_path": "pkg/a.py",
        }
        assert parse_collect_args(["--check", "--module=pkg/a.py"])["action"] == "check"

    @pytest.mark.parametrize(
        "argv",
        [
            ["--module"],            # no value at all
            ["--module", ""],        # empty value
            ["--module="],           # empty equals value
            ["--module", "--no-llm", "pkg/a.py"],   # a flag, not a path
            ["--module=--no-llm"],
        ],
    )
    def test_unusable_module_value_raises(self, argv):
        """Previously `--module=` returned an empty path and
        `--module --no-llm pkg/a.py` handed the flag to `action_module`,
        which reported it as a nonexistent file. argparse rejects
        `--module --no-llm` with "expected one argument"; this mirrors it
        with the same clean error class."""
        with pytest.raises(CollectCliError):
            parse_collect_args(argv)

    def test_flag_value_error_names_what_was_given(self):
        with pytest.raises(CollectCliError) as exc_info:
            parse_collect_args(["--module=--no-llm"])
        assert "--no-llm" in str(exc_info.value)


# ── 3. a full build reports how many modules it actually summarised ──────────


class TestFullBuildMessageCountsTruthfully:
    def test_all_summarized_reports_the_total(self, built_repo):
        """The common case keeps V7's exact phrasing."""
        result = action_rebuild(built_repo, llm_call=_counting_llm_call())

        assert "3 module(s) re-summarized" in result.message
        assert "re-scanned" not in result.message

    def test_unparseable_module_is_counted_separately(self, tmp_path):
        """A syntax error reaches Pass B with `summary=None`, so the total
        overcounted by one."""
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").write_text("")
        (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
        (tmp_path / "pkg" / "broken.py").write_text("def broken(:\n    return\n")
        _init_repo(tmp_path)

        result = action_rebuild(tmp_path, llm_call=_counting_llm_call())

        modules = _artifact_modules(result.collect_dir)
        assert sum(1 for m in modules.values() if m["summary"] is not None) == 2
        assert "3 module(s) re-scanned, 2 re-summarized" in result.message
        # the old overcount, on the number that identifies the path
        assert "full build: 3 module(s) re-summarized" not in result.message

    def test_exhausted_retries_is_counted_the_same_way(self, built_repo):
        """The second cause of a missing summary: Pass B ran but never
        produced prose. Same honest accounting."""
        result = action_rebuild(built_repo, llm_call=_raising_llm_call(), config=_no_retry_config())

        modules = _artifact_modules(result.collect_dir)
        assert sum(1 for m in modules.values() if m["summary"] is not None) == 0
        # Pass B ran and produced nothing — which is not "skipped"
        assert "3 module(s) re-scanned, none re-summarized (Pass B produced no summary)" in result.message
        assert "Pass B skipped" not in result.message

    def test_partial_failure_reports_both_halves(self, built_repo):
        """Some modules summarised, some not — neither total alone is true.

        The failure is pinned to one module's path rather than being
        round-robin: a flaky stub that fails every *other* call recovers on
        `summarize_repo`'s retry and so never demonstrates a partial result.
        """
        def _flaky(system: str, user: str) -> str:
            if "pkg/a.py" in user:
                raise RuntimeError("stub summarizer dropped this module")
            return json.dumps({"purpose": "stub purpose", "notes": ""})

        result = action_rebuild(built_repo, llm_call=_flaky, config=_no_retry_config())

        modules = _artifact_modules(result.collect_dir)
        summarized = sum(1 for m in modules.values() if m["summary"] is not None)
        assert 0 < summarized < 3
        assert f"3 module(s) re-scanned, {summarized} re-summarized" in result.message


# ── 4. the V7 guarantees V7 made still hold, end to end ──────────────────────


class TestV7GuaranteesIntact:
    def test_stale_tree_is_still_incremental(self, built_repo):
        llm = _counting_llm_call()
        (built_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")
        llm.calls.clear()

        result = action_collect(built_repo, llm_call=llm)

        assert len(llm.calls) == 1
        assert "incrementally refreshed 1 changed" in result.message

    def test_rebuild_is_still_one_call_per_module(self, built_repo):
        llm = _counting_llm_call()
        result = action_rebuild(built_repo, llm_call=llm)

        assert len(llm.calls) == 3
        assert "3 module(s) re-summarized" in result.message
