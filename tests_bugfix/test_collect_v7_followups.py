"""tests_bugfix/test_collect_v7_followups.py — V7 follow-ups: the three ways
`--collect`'s freshness gate and the collect flags lied after V7.

V7 (`25d5de8`) made `action_collect` delegate a stale tree to
`action_refresh`, so `--collect` is now the path users are actually on by
default. Three holes were left in the code it now trusts:

  1. `action_check` decided freshness from the manifest's content hashes
     only and never looked at `artifact.json`. A manifest whose artifact
     had been deleted (hand-cleaned `.collect/`, a failed disk write, a
     partial run) reported "up to date" against an unchanged tree — and
     because `action_collect` short-circuits on `fresh=True`, `--collect`
     printed "already up to date — nothing to do" and wrote nothing,
     leaving the consumer with no artifact at all. The loader already
     gated on both files existing; the producer side did not.

  2. `action_module` threaded the raw `--module` argument straight through
     as both the module's `path` and its manifest key. `--module
     ./pkg/a.py` left an artifact with two records for the same file and a
     manifest carrying a key the scanner never produces. Since the
     manifest's keys are exactly what `is_fresh` compares against
     `scan_repo`'s own path list, that phantom key stayed in the manifest
     forever: `is_fresh` returned False forever, and V7's no-op fast path
     was permanently unreachable after one mistyped flag. The same
     happened for any path whose extension no language in the collector
     recognizes at all (`README.md`, a config file).

  3. The two entry points had their own flag-precedence ordering and
     disagreed. `/collect --module pkg/a.py --rebuild` ran a full rebuild
     while `--collect --module pkg/a.py --rebuild` ran the single-file
     patch, and `--collect --check --module pkg/a.py` *wrote* while
     `/collect --check --module pkg/a.py` honoured `--check` and wrote
     nothing — breaking `--check`'s "writes nothing, anywhere" promise on
     one of the two paths.

Also fixed while in this code:

  4. `_full_build_message` said "N module(s) re-summarized" when only some
     were — a module with a parse error is never summarized, so a 4-module
     tree with one broken file reported 4 re-summarized after 3 LLM calls.
     A partial batch now says "4 module(s) re-scanned, 3 re-summarized".
  5. `action_refresh` said "incrementally refreshed 1 changed module(s)"
     even when Pass B never ran (`--no-llm`, `[collect] llm_summaries =
     false`, a summarizer that could not be built) — the same overclaim
     V7 already fixed for the full-build line. It now says "(Pass B
     skipped)", mirroring the full-build message.
"""

from __future__ import annotations

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
    action_from_flags,
    action_module,
    action_refresh,
    parse_collect_args,
    run,
)
from tools.collect.java_parser import is_available as java_available


# ── helpers (same shape as test_collect_stale_is_incremental.py) ─────────────

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


def _manifest_file_hashes(collect_dir: Path) -> dict:
    return json.loads((collect_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))["file_hashes"]


def _counting_llm_call() -> object:
    calls: list = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        return json.dumps({"purpose": "stub purpose", "notes": ""})

    _call.calls = calls
    return _call


def _tree_hash(root: Path) -> str:
    parts = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if ".collect" in path.parts or ".git" in path.parts:
            continue
        parts.append(str(path.relative_to(root)))
        parts.append(path.read_bytes())
    return str(parts)


def _main_dispatches(tmp_path: Path, *flags: str) -> str:
    """The action `main.py`'s own one-shot if-chain chose for `--collect
    <flags>` — the branch `parse_collect_args` does not cover."""
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


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """Same neutralisation as the other collect CLI tests: COLLECT-10/15
    seed data cites real jan-auto-agent symbols absent from these
    synthetic mini repos. This file tests orchestration, not seed content."""
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


# ── 1. a manifest without its artifact is stale ──────────────────────────────


class TestCheckSeesAMissingArtifact:
    def test_missing_artifact_is_not_fresh(self, mini_repo):
        """Before the fix this reported "up to date" with nothing on disk."""
        action_collect(mini_repo)
        assert action_check(mini_repo).fresh is True

        (mini_repo / ".collect" / ARTIFACT_FILENAME).unlink()

        result = action_check(mini_repo)

        assert result.wrote is False
        assert result.fresh is False
        assert "artifact" in result.message
        assert "missing" in result.message

    def test_collect_rebuilds_a_deleted_artifact(self, mini_repo):
        """The V7 regression: `--collect` trusted `action_check` and
        short-circuited, printing "nothing to do" over a tree with no
        artifact at all."""
        action_collect(mini_repo)
        (mini_repo / ".collect" / ARTIFACT_FILENAME).unlink()
        llm = _counting_llm_call()

        result = action_collect(mini_repo, llm_call=llm)

        assert result.action == "collect"
        assert result.wrote is True
        assert "nothing to do" not in result.message
        assert "full build" in result.message
        assert (result.collect_dir / ARTIFACT_FILENAME).exists()
        assert len(llm.calls) == 3  # every module, since there was nothing to diff against

    def test_manifest_present_with_artifact_is_still_fresh(self, mini_repo):
        """The guard must not turn an ordinary unchanged tree into a
        rebuild every time."""
        action_collect(mini_repo)
        assert action_check(mini_repo).fresh is True

        result = action_collect(mini_repo, llm_call=_counting_llm_call())

        assert result.wrote is False
        assert "nothing to do" in result.message

    def test_missing_artifact_still_writes_nothing_on_check(self, mini_repo):
        """`--check` promises "writes nothing, anywhere" — including when
        it finds a broken pair."""
        action_collect(mini_repo)
        collect_dir = mini_repo / ".collect"
        (collect_dir / ARTIFACT_FILENAME).unlink()
        before = sorted(p.name for p in collect_dir.iterdir())

        result = action_check(mini_repo)

        assert result.wrote is False
        assert sorted(p.name for p in collect_dir.iterdir()) == before


# ── 2. --module paths are normalized, never phantom ──────────────────────────


class TestModulePathNormalization:
    def test_dot_slash_prefix_is_normalized_not_duplicated(self, mini_repo):
        """Before the fix: two records for one file, plus a manifest key
        the scanner never produces."""
        action_collect(mini_repo)

        result = action_module(mini_repo, "./pkg/a.py")

        modules = _artifact_modules(result.collect_dir)
        assert "pkg/a.py" in modules
        assert "./pkg/a.py" not in modules
        assert len(modules) == 3, f"expected the 3 real modules, got {sorted(modules)}"
        assert _manifest_file_hashes(result.collect_dir).keys() == set(modules.keys())

    def test_dot_slash_module_patch_keeps_the_tree_fresh(self, mini_repo):
        """The permanent-staleness regression: a `./`-prefixed manifest key
        is never in `scan_repo`'s path list, so `is_fresh` compared unequal
        maps forever and V7's no-op path never came back."""
        action_collect(mini_repo)

        action_module(mini_repo, "./pkg/a.py")

        assert action_check(mini_repo).fresh is True
        result = action_collect(mini_repo, llm_call=_counting_llm_call())
        assert result.wrote is False

    def test_parent_traversal_is_normalized(self, mini_repo):
        action_collect(mini_repo)

        result = action_module(mini_repo, "pkg/../pkg/b.py")

        modules = _artifact_modules(result.collect_dir)
        assert "pkg/b.py" in modules
        assert ".." not in "".join(modules.keys())
        assert action_check(mini_repo).fresh is True

    def test_absolute_path_is_rejected(self, mini_repo):
        action_collect(mini_repo)

        with pytest.raises(CollectCliError, match="absolute"):
            action_module(mini_repo, str(mini_repo / "pkg" / "a.py"))

    def test_path_outside_the_root_is_rejected(self, mini_repo):
        action_collect(mini_repo)

        with pytest.raises(CollectCliError, match="escapes"):
            action_module(mini_repo, "../outside.py")

    def test_non_scannable_path_is_rejected(self, mini_repo):
        """A path whose extension no language recognizes could never be
        picked up by `is_fresh` either, so it would have made the tree
        stale forever."""
        (mini_repo / "README.md").write_text("# not a module\n")
        action_collect(mini_repo)

        with pytest.raises(CollectCliError, match="extension this collector does not recognize"):
            action_module(mini_repo, "README.md")

        assert action_check(mini_repo).fresh is True

    def test_extension_with_no_suffix_at_all_is_rejected(self, mini_repo):
        (mini_repo / "Makefile").write_text("all:\n\techo hi\n")
        action_collect(mini_repo)

        with pytest.raises(CollectCliError, match="extension this collector does not recognize"):
            action_module(mini_repo, "Makefile")

    @pytest.mark.skipif(not java_available(), reason="tree-sitter-java not installed")
    def test_java_module_still_works_without_enabling_java(self, mini_repo):
        """The guard rejects unrecognized extensions, not languages that
        just aren't enabled: `--module Foo.java` on a Python-only repo is
        the documented COLLECT-28 escape hatch for patching one Java file
        in by hand."""
        (mini_repo / "src").mkdir()
        (mini_repo / "src" / "Foo.java").write_text(
            "public class Foo {\n    public int bar() {\n        return 1;\n    }\n}\n"
        )
        action_collect(mini_repo)

        result = action_module(mini_repo, "src/Foo.java")

        modules = _artifact_modules(result.collect_dir)
        assert modules["src/Foo.java"]["language"] == "java"
        assert modules["src/Foo.java"]["parse_error"] is None

    def test_normalization_is_used_in_the_result_message(self, mini_repo):
        action_collect(mini_repo)

        result = action_module(mini_repo, "./pkg/a.py")

        assert "patched pkg/a.py" in result.message

    def test_rejection_writes_nothing(self, mini_repo):
        """Fail-open: a refused `--module` leaves the previous artifact and
        manifest exactly as they were."""
        action_collect(mini_repo)
        collect_dir = mini_repo / ".collect"
        before = {p.name: p.read_bytes() for p in collect_dir.iterdir()}
        tree_before = _tree_hash(mini_repo)

        with pytest.raises(CollectCliError):
            action_module(mini_repo, "../outside.py")

        assert {p.name: p.read_bytes() for p in collect_dir.iterdir()} == before
        assert _tree_hash(mini_repo) == tree_before


# ── 3. one precedence table, both entry points ───────────────────────────────


class TestFlagPrecedenceAgreesAcrossEntryPoints:
    @pytest.mark.parametrize(
        "flags,expected",
        [
            ([], "collect"),
            (["--check"], "check"),
            (["--refresh"], "refresh"),
            (["--rebuild"], "rebuild"),
            (["--module", "pkg/a.py"], "module"),
            (["--check", "--rebuild"], "check"),
            (["--refresh", "--rebuild"], "rebuild"),
            (["--rebuild", "--refresh"], "rebuild"),
            (["--module", "pkg/a.py", "--rebuild"], "module"),
            (["--rebuild", "--module", "pkg/a.py"], "module"),
            (["--check", "--module", "pkg/a.py"], "check"),
            (["--check", "--refresh", "--rebuild", "--module", "pkg/a.py"], "check"),
        ],
    )
    def test_parse_and_main_dispatch_the_same_action(self, tmp_path, flags, expected):
        """`/collect` (parse_collect_args) and `--collect` (main.py's own
        branch) must agree on every flag combination, not just the ones
        V7 happened to check."""
        assert parse_collect_args(flags)["action"] == expected
        assert _main_dispatches(tmp_path, *flags) == expected

    def test_check_wins_in_the_shared_table(self):
        assert action_from_flags(check=True, module_path="pkg/a.py", rebuild=True) == (
            "check",
            None,
        )

    def test_rebuild_beats_refresh_in_the_shared_table(self):
        """Asking for both must never silently downgrade to the cheap path."""
        assert action_from_flags(refresh=True, rebuild=True) == ("rebuild", None)

    def test_no_flags_is_the_collect_default(self):
        assert action_from_flags() == ("collect", None)

    def test_check_wins_over_module_end_to_end_and_writes_nothing(self, mini_repo):
        """Before the fix `--collect --check --module pkg/a.py` patched a
        file — `--check` promised "writes nothing, anywhere"."""
        action_collect(mini_repo)
        collect_dir = mini_repo / ".collect"
        tree_before = _tree_hash(mini_repo)
        before = {p.name: p.read_bytes() for p in collect_dir.iterdir()}

        kwargs = parse_collect_args(["--check", "--module", "pkg/a.py"])
        result = run(mini_repo, **kwargs)

        assert kwargs["action"] == "check"
        assert result.wrote is False
        assert _tree_hash(mini_repo) == tree_before
        assert {p.name: p.read_bytes() for p in collect_dir.iterdir()} == before


# ── 4. the full-build line counts what Pass B actually did ──────────────────


class TestFullBuildMessageCountsHonestly:
    def test_partial_summary_reports_both_counts(self, mini_repo):
        """4 modules, one of them unparseable and therefore never
        summarized: "4 module(s) re-summarized" after 3 LLM calls was a
        literal overclaim."""
        (mini_repo / "pkg" / "broken.py").write_text("def broken(:\n")
        llm = _counting_llm_call()

        result = action_collect(mini_repo, llm_call=llm)

        assert len(llm.calls) == 3
        assert "4 module(s) re-scanned, 3 re-summarized" in result.message
        modules = _artifact_modules(result.collect_dir)
        assert modules["pkg/broken.py"]["parse_error"] is not None
        assert modules["pkg/broken.py"]["summary"] is None

    def test_all_summarized_says_re_summarized(self, mini_repo):
        llm = _counting_llm_call()

        result = action_collect(mini_repo, llm_call=llm)

        assert "3 module(s) re-summarized" in result.message
        assert "re-scanned" not in result.message

    def test_no_pass_b_says_re_scanned(self, mini_repo):
        result = action_collect(mini_repo)  # llm_call=None

        assert "3 module(s) re-scanned (Pass B skipped)" in result.message
        assert "re-summarized" not in result.message


# ── 5. the refresh line says when Pass B did not run ────────────────────────


class TestRefreshMessageReportsSkippedPassB:
    def test_collect_without_llm_says_pass_b_skipped(self, mini_repo):
        """`--collect` with `--no-llm` takes the incremental path but makes
        zero LLM calls — the line has to say so, the way the full-build
        line already does."""
        action_collect(mini_repo, llm_call=_counting_llm_call())
        (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")

        result = action_collect(mini_repo)  # llm_call=None

        assert "incrementally refreshed 1 changed" in result.message
        assert "(Pass B skipped)" in result.message

    def test_collect_with_llm_does_not_claim_pass_b_skipped(self, mini_repo):
        action_collect(mini_repo, llm_call=_counting_llm_call())
        (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")

        result = action_collect(mini_repo, llm_call=_counting_llm_call())

        assert "incrementally refreshed 1 changed" in result.message
        assert "Pass B skipped" not in result.message

    def test_no_llm_drops_only_the_changed_module_summary(self, mini_repo):
        """The incremental guarantee under `--no-llm`: the changed module
        loses its summary, every unchanged module keeps its own."""
        llm = _counting_llm_call()
        action_collect(mini_repo, llm_call=llm)
        before = _artifact_modules(mini_repo / ".collect")
        (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")

        result = action_collect(mini_repo)  # llm_call=None
        after = _artifact_modules(result.collect_dir)

        assert before["pkg/b.py"]["summary"] == after["pkg/b.py"]["summary"]
        assert after["pkg/a.py"]["summary"] is None

    def test_tree_unchanged_message_is_untouched(self, mini_repo):
        action_collect(mini_repo)
        (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 1\n")
        action_collect(mini_repo)  # re-commit the same bytes: nothing changed
        (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")
        action_collect(mini_repo)

        result = action_refresh(mini_repo)

        assert "tree unchanged" in result.message
        assert "Pass B skipped" not in result.message
