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
  6. Flag precedence lives in one shared table (`cli.action_from_flags`),
     used by both entry points (`parse_collect_args` for `/collect`,
     `main.py`'s one-shot branch for `--collect`), so asking for both
     `--rebuild` and `--refresh` never silently downgrades to the cheap
     path — and `--check` + `--module` no longer writes on one entry
     point and stays read-only on the other.

Follow-up to the same ticket (V7-2) — the stale-tree path still cost a
second Pass A scan and a third tree hash, and four things about it were
wrong:

  7. `action_collect` decided staleness via `action_check`, which scanned
     the whole tree (Pass A) just to hash it, and the `action_refresh` it
     then delegated to scanned it a second time and hashed it a third
     (again inside `manifest.build_manifest`). One `--collect` on a stale
     tree: 2 `scan_repo` calls, 3 full hash passes. Now: 1 and 1.
  8. A refresh whose only change was a deletion reported
     "incrementally refreshed 0 changed and 1 removed module(s)".
  9. A changed module that could not be re-read for the summarizer prompt
     was still summarized — from an empty source. `summarize_repo` falls
     back to `sources.get(path, "")`, so the reply parsed into a
     plausible-looking `purpose` for a file the summarizer never read.
 10. `verification_report.json` is the one derived file whose write is
     conditional, so a build where Pass B/C skipped left the previous
     build's report next to an `artifact.json` that no longer had any
     summary for it to describe.
 11. `--check` said "stale — a tracked file changed" for every non-fresh
     verdict, including a `collector_version` mismatch and an unreadable
     manifest, where no file changed at all.
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
from tools.collect.manifest import hash_tree as _hash_tree_unpatched
from tools.collect.cli import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    action_check,
    action_collect,
    action_module,
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

    def test_parse_collect_args_module_beats_rebuild(self):
        """`--module` names one specific file, so it wins over the
        whole-tree `--rebuild` — the order `main.py`'s one-shot dispatch
        already used. `/collect` used to disagree here (`--rebuild` won),
        so the same flag combination ran a full rebuild interactively and
        a single-file patch one-shot; both now use `action_from_flags`.
        See `test_collect_v7_followups.py` for the full agreement matrix."""
        assert parse_collect_args(["--rebuild", "--module", "pkg/a.py"]) == {
            "action": "module",
            "module_path": "pkg/a.py",
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


# ── 6. the stale-tree path costs one Pass A scan and one hash pass ───────────


@pytest.fixture
def op_counts(monkeypatch):
    """Count the two tree-wide operations `--collect` performs.

    `scan_repo` is imported into `tools.collect.cli`'s own namespace, so it
    has to be patched there; `hash_tree` is always reached through the
    manifest module (`build_manifest`, `is_fresh` and the actions all call
    it that way), so patching that one attribute covers every caller.
    """
    from tools.collect import scanner as scanner_mod

    counts = {"scan_repo": 0, "hash_tree": 0}
    real_scan = scanner_mod.scan_repo
    real_hash = cli_mod.manifest_mod.hash_tree

    def _scan(*args, **kwargs):
        counts["scan_repo"] += 1
        return real_scan(*args, **kwargs)

    def _hash(*args, **kwargs):
        counts["hash_tree"] += 1
        return real_hash(*args, **kwargs)

    monkeypatch.setattr(cli_mod, "scan_repo", _scan)
    monkeypatch.setattr(cli_mod.manifest_mod, "hash_tree", _hash)
    return counts


class TestStaleTreeCostsOneScanAndOneHashPass:
    def test_one_llm_call_one_scan_one_hash_pass(self, mini_repo, op_counts):
        """The whole point of the ticket, measured on all three axes.

        Before the follow-up: 1 changed file cost 1 LLM call (the fix), but
        `action_check`'s freshness gate and the delegated `action_refresh`
        each ran their own Pass A over the same tree, and the tree was
        hashed a third time inside `manifest.build_manifest`.
        """
        action_collect(mini_repo, llm_call=_counting_llm_call())
        _change_a(mini_repo)
        # the bootstrap build above is not the call under test
        op_counts.update(scan_repo=0, hash_tree=0)

        llm = _counting_llm_call()
        result = action_collect(mini_repo, llm_call=llm)

        assert result.wrote is True
        assert len(llm.calls) == 1
        assert op_counts["scan_repo"] == 1, "Pass A ran more than once over the same tree"
        assert op_counts["hash_tree"] == 1, "the tree was hashed more than once"

    def test_a_fresh_tree_still_scans_once_and_hashes_once(self, mini_repo, op_counts):
        """The no-op path must not get worse while the slow path gets
        better — `--check`-equivalent work is still one scan and one hash."""
        action_collect(mini_repo)
        # the bootstrap build above is not the call under test
        op_counts.update(scan_repo=0, hash_tree=0)

        result = action_collect(mini_repo)

        assert result.wrote is False
        assert op_counts["scan_repo"] == 1
        assert op_counts["hash_tree"] == 1

    def test_a_full_rebuild_scans_and_hashes_once_too(self, mini_repo, op_counts):
        """The `--rebuild` path got the same treatment: the manifest is
        built from the hashes `_full_build` already computed for the scan,
        not from a second pass after `_write_artifact` wrote to disk."""
        action_collect(mini_repo)
        # the bootstrap build above is not the call under test
        op_counts.update(scan_repo=0, hash_tree=0)

        llm = _counting_llm_call()
        result = action_rebuild(mini_repo, llm_call=llm)

        assert op_counts["scan_repo"] == 1
        assert op_counts["hash_tree"] == 1
        assert len(llm.calls) == _module_count(mini_repo)

    def test_caller_supplied_scan_and_hashes_are_not_recomputed(self, mini_repo, op_counts):
        """`action_refresh(modules=..., hashes=...)` is the seam
        `action_collect` uses: a caller that already has both must not pay
        for either again."""
        from tools.collect import scanner as scanner_mod

        action_collect(mini_repo)
        _change_a(mini_repo)
        op_counts.update(scan_repo=0, hash_tree=0)

        modules = scanner_mod.scan_repo(mini_repo)
        # `_hash_tree_unpatched` is bound at import time, before the
        # fixture's monkeypatch, so this is the real function — calling
        # `cli_mod.manifest_mod.hash_tree` here would hit the counter and
        # spoil the assertion below.
        hashes = _hash_tree_unpatched(mini_repo, [m.path for m in modules])

        result = action_refresh(mini_repo, llm_call=_counting_llm_call(),
                                modules=modules, hashes=hashes)

        assert op_counts["scan_repo"] == 0
        assert op_counts["hash_tree"] == 0
        assert "incrementally refreshed 1 changed" in result.message

    def test_reused_hashes_do_not_change_what_the_manifest_records(
        self, mini_repo, op_counts,
    ):
        """The optimization must be byte-neutral for the manifest: the same
        file set as a plain `--collect` run, and still fresh afterwards."""
        import json

        action_collect(mini_repo)
        _change_a(mini_repo)
        op_counts.update(scan_repo=0, hash_tree=0)

        result = action_collect(mini_repo)

        manifest = json.loads((result.collect_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
        assert set(manifest["file_hashes"]) == {"pkg/__init__.py", "pkg/a.py", "pkg/b.py"}
        assert action_check(mini_repo).fresh is True


# ── 7. a change that is only a deletion does not report "0 changed" ───────────


class TestDeletionOnlyMessage:
    def test_deletion_only_does_not_report_zero_changed(self, mini_repo):
        """Before: 'incrementally refreshed 0 changed and 1 removed
        module(s)'. The zero says nothing; the one does."""
        action_collect(mini_repo)
        (mini_repo / "pkg" / "b.py").unlink()

        result = action_collect(mini_repo)

        assert "0 changed" not in result.message
        assert "1 removed module(s)" in result.message

    def test_deletion_only_does_not_claim_pass_b_was_skipped(self, mini_repo):
        """"(Pass B skipped)" is for a changed module that went unsummarized.
        A deletion leaves nothing to summarize, so Pass B had no work — not
        a reason the line should read like `--no-llm` ran."""
        action_collect(mini_repo, llm_call=_counting_llm_call())
        (mini_repo / "pkg" / "b.py").unlink()

        with_llm = action_collect(mini_repo, llm_call=_counting_llm_call())
        assert "1 removed module(s)" in with_llm.message
        assert "Pass B skipped" not in with_llm.message

        (mini_repo / "pkg" / "__init__.py").unlink()
        without_llm = action_collect(mini_repo, llm_call=None)
        assert "1 removed module(s)" in without_llm.message
        assert "Pass B skipped" not in without_llm.message

        _change_a(mini_repo)
        changed_without_llm = action_collect(mini_repo, llm_call=None)
        assert "1 changed module(s) (Pass B skipped)" in changed_without_llm.message

    def test_change_and_deletion_together_name_both_counts(self, mini_repo):
        action_collect(mini_repo)
        _change_a(mini_repo)
        (mini_repo / "pkg" / "b.py").unlink()
        (mini_repo / "pkg" / "c.py").write_text("def c():\n    return 3\n")

        result = action_collect(mini_repo)

        assert "2 changed and 1 removed module(s)" in result.message

    def test_a_single_edit_still_reports_only_the_changed_count(self, mini_repo):
        """The original V7 wording on the common path is unchanged."""
        action_collect(mini_repo)
        _change_a(mini_repo)

        result = action_collect(mini_repo)

        assert "incrementally refreshed 1 changed module(s)" in result.message


# ── 8. an unreadable changed file is not summarized from an empty source ─────


class TestNoSummaryWithoutASource:
    def test_changed_file_that_becomes_unreadable_is_not_summarized(self, mini_repo, monkeypatch):
        """The race the existing guard names: the file is fine when Pass A
        reads it (a few lines up), unreadable when the summarizer re-reads
        it for its prompt. `summarize_repo` falls back to
        `sources.get(path, "")`, so that module was sent to the LLM with no
        code at all — and the reply still parsed into a `purpose` that
        landed in artifact.json as prose about a file nobody read.

        The guard already kept such a module out of `sources_for_summary`;
        it just never kept it out of the batch.
        """
        action_collect(mini_repo)
        _change_a(mini_repo)

        target = mini_repo / "pkg" / "a.py"
        reads = {"n": 0}
        real_read_text = Path.read_text

        def _second_read_fails(self, *args, **kwargs):
            if self == target:
                reads["n"] += 1
                if reads["n"] > 1:
                    raise OSError("simulated: unreadable on the summarizer's re-read")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _second_read_fails)

        llm = _counting_llm_call()
        result = action_collect(mini_repo, llm_call=llm)

        assert result.wrote is True
        modules = _artifact_modules(result.collect_dir)
        # the file was still re-scanned structurally, so the change is picked
        # up — just without invented prose for it
        assert "pkg/a.py:extra" in {
            s["qualname"] for s in modules["pkg/a.py"]["public_symbols"]
        }
        assert all("pkg/a.py" not in user for _system, user in llm.calls)
        assert modules["pkg/a.py"]["summary"] is None
        assert reads["n"] >= 2, "sanity: the second read was actually attempted"

    def test_every_summarized_module_has_a_source_in_the_batch(self, mini_repo, monkeypatch):
        """The invariant, checked at the seam: whatever reaches
        `summarize_repo` is accompanied by a source for it."""
        action_collect(mini_repo)
        _change_a(mini_repo)

        seen: list = []
        real_summarize_repo = cli_mod.summarize_repo

        def _recording(modules, sources, *args, **kwargs):
            seen.extend((m.path, m.path in sources) for m in modules)
            return real_summarize_repo(modules, sources, *args, **kwargs)

        monkeypatch.setattr(cli_mod, "summarize_repo", _recording)

        action_collect(mini_repo, llm_call=_counting_llm_call())

        assert seen, "sanity: Pass B ran"
        assert all(had_source for _path, had_source in seen), seen


# ── 9. a build without Pass B/C does not leave the last report behind ────────


class TestNoStaleVerificationReport:
    def test_a_build_without_pass_b_removes_the_previous_report(self, mini_repo):
        """`verification_report.json` is the one derived file whose write is
        conditional on `ctx.verification_report is not None`, while every
        rendered page is rewritten unconditionally. A build where Pass B/C
        skipped therefore left the *previous* build's report next to an
        `artifact.json` with no summary left in it — a file whose presence
        looked like a claim about the current build."""
        llm = _counting_llm_call()
        first = action_collect(mini_repo, llm_call=llm)
        report = first.collect_dir / cli_mod.VERIFICATION_REPORT_FILENAME
        assert report.exists()
        assert any(m["summary"] is not None for m in _artifact_modules(first.collect_dir).values())

        result = action_rebuild(mini_repo)  # llm_call=None -> Pass B/C skipped

        assert not report.exists()
        assert cli_mod.VERIFICATION_REPORT_FILENAME not in result.written_files
        assert all(m["summary"] is None for m in _artifact_modules(result.collect_dir).values())
        # the rest of the artifact set is untouched by the removal
        for page in ("MODULE_MAP.md", "TEST_MAP.md", "RISK_INDEX.md"):
            assert (result.collect_dir / page).exists()

    def test_a_build_with_pass_b_writes_the_report_again(self, mini_repo):
        llm = _counting_llm_call()
        action_collect(mini_repo)
        action_rebuild(mini_repo)  # drops the report

        result = action_rebuild(mini_repo, llm_call=llm)

        report = result.collect_dir / cli_mod.VERIFICATION_REPORT_FILENAME
        assert report.exists()
        assert cli_mod.VERIFICATION_REPORT_FILENAME in result.written_files

    def test_an_incremental_run_that_keeps_summaries_keeps_its_report(self, mini_repo):
        """The removal must be scoped to 'no summary anywhere in this build':
        an incremental refresh that reuses unchanged summaries still runs
        Pass C and still owes a report."""
        llm = _counting_llm_call()
        first = action_collect(mini_repo, llm_call=llm)
        report = first.collect_dir / cli_mod.VERIFICATION_REPORT_FILENAME
        assert report.exists()
        _change_a(mini_repo)

        result = action_collect(mini_repo, llm_call=_counting_llm_call())

        assert result.collect_dir == first.collect_dir
        assert report.exists()


# ── 10. --check names the reason a tree is not fresh ────────────────────────


class TestCheckNamesTheReason:
    def _bump_manifest_version(self, mini_repo: Path) -> None:
        manifest_path = mini_repo / ".collect" / MANIFEST_FILENAME
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["collector_version"] = "999.0.0-future-schema"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

    def test_a_real_change_says_a_file_changed(self, mini_repo):
        action_collect(mini_repo)
        _change_a(mini_repo)

        result = action_check(mini_repo)

        assert result.fresh is False
        assert "a tracked file changed" in result.message

    def test_a_version_mismatch_does_not_claim_a_file_changed(self, mini_repo):
        """No file changed at all — the reason is the schema. The old
        blanket message said the opposite of what was true, which matters
        because `--collect` used to route on this verdict."""
        action_collect(mini_repo)
        self._bump_manifest_version(mini_repo)

        result = action_check(mini_repo)

        assert result.fresh is False
        assert "a tracked file changed" not in result.message
        assert "collector_version='999.0.0-future-schema'" in result.message
        assert f"current is {cli_mod.manifest_mod.COLLECTOR_VERSION!r}" in result.message

    def test_an_unreadable_manifest_says_unreadable(self, mini_repo):
        action_collect(mini_repo)
        manifest_path = mini_repo / ".collect" / MANIFEST_FILENAME
        manifest_path.write_text("{ this is not json", encoding="utf-8")

        result = action_check(mini_repo)

        assert result.fresh is False
        assert "unreadable" in result.message
        assert "a tracked file changed" not in result.message

    def test_a_never_run_collect_still_says_so(self, mini_repo):
        result = action_check(mini_repo)

        assert result.fresh is False
        assert "never run" in result.message

    def test_collect_falls_back_honestly_when_the_manifest_is_unreadable(self, mini_repo):
        """Before V7 every full-build fallback said "no prior artifact to
        diff against". A manifest that exists but cannot be read is not that."""
        action_collect(mini_repo)
        manifest_path = mini_repo / ".collect" / MANIFEST_FILENAME
        manifest_path.write_text("{ this is not json", encoding="utf-8")

        result = action_collect(mini_repo)

        assert result.wrote is True
        assert "no prior artifact" not in result.message
        assert "unreadable" in result.message
        assert "full build" in result.message

    def test_a_missing_artifact_rebuilds_and_says_why(self, mini_repo):
        """A tree can match its manifest while the artifact describing it is
        gone — hand-deleted, or a build that died mid-`_write_artifact`.
        `--collect` answering "already up to date" there would leave
        `[collect] dir` with no artifact at all, and the tree would stay in
        exactly that state forever: `--check` reports fresh, so nothing
        else would ever try to fix it.
        """
        action_collect(mini_repo)
        artifact = mini_repo / ".collect" / ARTIFACT_FILENAME
        assert artifact.exists()
        artifact.unlink()

        result = action_collect(mini_repo)

        assert result.wrote is True
        assert result.fresh is True
        assert "already up to date" not in result.message
        assert "no prior artifact" not in result.message
        assert "artifact.json" in result.message
        assert "full build" in result.message
        assert artifact.exists()
        assert _artifact_modules(result.collect_dir)

    def test_check_reports_the_missing_artifact_as_stale_too(self, mini_repo):
        """`--check` and `--collect` share one verdict (`_freshness`): a
        manifest whose artifact is gone is *absent*, not fresh, on both. A
        `--check` that said "up to date" here would have told the user
        there was nothing to do while the consumer had nothing to load —
        and `--check` still writes nothing, anywhere."""
        action_collect(mini_repo)
        (mini_repo / ".collect" / ARTIFACT_FILENAME).unlink()

        check = action_check(mini_repo)

        assert check.fresh is False
        assert check.wrote is False
        assert "artifact.json" in check.message
        assert "missing" in check.message
        assert not (mini_repo / ".collect" / ARTIFACT_FILENAME).exists()

        collect = action_collect(mini_repo)
        assert collect.wrote is True


# ── 11. --check and --collect keep agreeing about freshness ──────────────────


class TestCheckAndCollectAgree:
    """`action_collect` and `action_check` take their verdict from the one
    `_freshness` gate — one scan, one hash pass, the artifact test
    included. Whatever it decides for one must hold for the other, or
    `--collect` would start doing work a `--check` told the user there
    was none (or the reverse)."""

    def test_check_fresh_iff_collect_is_a_noop(self, mini_repo):
        """One case per way the tree can be not-fresh, plus the fresh case.
        Each pairs the `--check` verdict with whether `--collect` actually
        wrote anything."""
        pairs = []

        # no manifest yet
        pairs.append((
            "no manifest",
            action_check(mini_repo).fresh,
            action_collect(mini_repo).wrote is False,
        ))

        # fresh (the previous call just built it)
        pairs.append((
            "fresh",
            action_check(mini_repo).fresh,
            action_collect(mini_repo).wrote is False,
        ))

        # one file changed
        _change_a(mini_repo)
        pairs.append((
            "one file changed",
            action_check(mini_repo).fresh,
            action_collect(mini_repo).wrote is False,
        ))

        # deletion only
        (mini_repo / "pkg" / "b.py").unlink()
        pairs.append((
            "deletion only",
            action_check(mini_repo).fresh,
            action_collect(mini_repo).wrote is False,
        ))

        # collector_version mismatch
        manifest_path = mini_repo / ".collect" / MANIFEST_FILENAME
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["collector_version"] = "999.0.0-future-schema"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        pairs.append((
            "version mismatch",
            action_check(mini_repo).fresh,
            action_collect(mini_repo).wrote is False,
        ))

        # unreadable manifest
        manifest_path.write_text("{ this is not json", encoding="utf-8")
        pairs.append((
            "unreadable manifest",
            action_check(mini_repo).fresh,
            action_collect(mini_repo).wrote is False,
        ))

        for name, check_fresh, collect_noop in pairs:
            assert check_fresh == collect_noop, (
                f"{name}: --check says fresh={check_fresh} but --collect "
                f"{'was a no-op' if collect_noop else 'did work'}"
            )

class TestModulePatchLeavesTheManifestWhole:
    """`--module` is incremental by one file, but its manifest patch was not
    incremental in the safe direction: with no previous manifest to patch
    into it wrote a `file_hashes` map holding ONLY the patched file. The
    next `--collect` then diffed the whole tree against that one entry,
    saw every other module as newly "added", and re-summarized all of them.

    That is V7's exact cost bug — one changed file costs the same as a
    from-scratch build — reached through the *incremental* `--module` path
    rather than `--collect`, so V7's fix did not cover it.
    """

    def _module_paths(self, mini_repo: Path) -> set:
        return set(_artifact_modules(mini_repo / ".collect"))

    def test_missing_manifest_records_every_module(self, mini_repo):
        llm = _counting_llm_call()
        action_rebuild(mini_repo, llm_call=llm)
        n = len(self._module_paths(mini_repo))
        assert n > 1, "sanity: multi-module repo"

        (mini_repo / ".collect" / MANIFEST_FILENAME).unlink()
        assert not (mini_repo / ".collect" / MANIFEST_FILENAME).exists()

        action_module(mini_repo, "pkg/a.py", llm_call=llm)

        hashes = json.loads(
            (mini_repo / ".collect" / MANIFEST_FILENAME).read_text(encoding="utf-8")
        )["file_hashes"]
        assert set(hashes) == self._module_paths(mini_repo), (
            f"the manifest recorded {sorted(hashes)} after patching one module — it "
            f"must track every module in the tree, or the next --collect treats the "
            f"rest as newly added and re-summarizes them all"
        )

    def test_next_collect_is_a_noop_after_a_manifestless_module_patch(self, mini_repo):
        """The cost assertion: the patch must leave the tree fresh."""
        llm = _counting_llm_call()
        action_rebuild(mini_repo, llm_call=llm)
        (mini_repo / ".collect" / MANIFEST_FILENAME).unlink()

        action_module(mini_repo, "pkg/a.py", llm_call=llm)

        assert action_check(mini_repo).fresh is True
        llm.calls.clear()

        result = action_collect(mini_repo, llm_call=llm)

        assert result.wrote is False
        assert llm.calls == [], (
            f"patching one module with no prior manifest made the next --collect "
            f"spend {len(llm.calls)} LLM call(s) — before the fix it re-summarized "
            f"every module the truncated manifest forgot"
        )

    def test_unreadable_manifest_records_every_module(self, mini_repo):
        """A manifest that would not parse is the same situation as an absent
        one — the code already read it as an empty hash map."""
        llm = _counting_llm_call()
        action_rebuild(mini_repo, llm_call=llm)
        (mini_repo / ".collect" / MANIFEST_FILENAME).write_text(
            "{not json", encoding="utf-8"
        )

        action_module(mini_repo, "pkg/b.py", llm_call=llm)

        hashes = json.loads(
            (mini_repo / ".collect" / MANIFEST_FILENAME).read_text(encoding="utf-8")
        )["file_hashes"]
        assert len(hashes) == len(self._module_paths(mini_repo))
        assert "pkg/b.py" in hashes
        assert action_check(mini_repo).fresh is True

    def test_usable_manifest_still_patches_one_entry_only(self, mini_repo):
        """Non-regression: with a previous manifest there is no reason to
        rehash the tree — only the patched file's entry changes."""
        llm = _counting_llm_call()
        action_rebuild(mini_repo, llm_call=llm)
        manifest_path = mini_repo / ".collect" / MANIFEST_FILENAME
        before = json.loads(manifest_path.read_text(encoding="utf-8"))
        other_hashes = {
            p: h for p, h in before["file_hashes"].items() if p != "pkg/a.py"
        }

        (mini_repo / "pkg" / "a.py").write_text(
            "def a():\n    return 42\n\ndef extra():\n    pass\n"
        )
        action_module(mini_repo, "pkg/a.py", llm_call=llm)

        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(after["file_hashes"]) == len(before["file_hashes"])
        assert after["file_hashes"]["pkg/a.py"] != before["file_hashes"]["pkg/a.py"]
        assert after["file_hashes"] == {
            **other_hashes,
            "pkg/a.py": after["file_hashes"]["pkg/a.py"],
        }
        assert action_check(mini_repo).fresh is True


class TestSummarizerFactoryFailsOpen:
    """`make_summarizer_call` reads many `[collect]`/`[api]` keys and can
    raise on any of them. `main.py` has two call sites: the `--collect`
    one-shot branch guarded it inline, the interactive `/collect` command
    did not — and the only enclosing handlers there are `CollectCliError`
    and `KeyboardInterrupt`/`EOFError`, so one bad key killed the whole
    interactive shell instead of degrading one optional stage to
    structural-only output.

    The guard now lives in `make_summarizer_call_or_none`, which both entry
    points call, so they cannot drift apart again.
    """

    def test_a_raising_factory_degrades_to_none(self, monkeypatch, caplog):
        import tools.collect.summarizer as summarizer_mod

        def _boom(config, task_mode="code"):
            raise ValueError("config [collect] temperature is malformed")

        monkeypatch.setattr(summarizer_mod, "_make_llm_call", _boom)
        import configparser

        cfg = configparser.ConfigParser()

        with caplog.at_level("WARNING"):
            assert summarizer_mod.make_summarizer_call_or_none(cfg) is None

        assert any("could not build the summarizer LLM call" in r.message for r in caplog.records)

    def test_a_working_factory_is_returned_untouched(self, monkeypatch):
        import tools.collect.summarizer as summarizer_mod

        fake = lambda system, user: "{}"  # noqa: E731
        monkeypatch.setattr(summarizer_mod, "_make_llm_call", lambda config, task_mode: fake)
        import configparser

        cfg = configparser.ConfigParser()

        assert summarizer_mod.make_summarizer_call_or_none(cfg) is fake

    def test_mode_is_forwarded_to_the_factory(self, monkeypatch):
        import tools.collect.summarizer as summarizer_mod

        seen: dict = {}

        def _spy(config, task_mode="code"):
            seen["task_mode"] = task_mode
            return lambda system, user: "{}"

        monkeypatch.setattr(summarizer_mod, "_make_llm_call", _spy)
        import configparser

        cfg = configparser.ConfigParser()

        summarizer_mod.make_summarizer_call_or_none(cfg, task_mode="docs")

        assert seen["task_mode"] == "docs"


class TestRebuildEndToEndViaMain:
    def _make_mini_repo(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").write_text("")
        (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
        (tmp_path / "pkg" / "b.py").write_text("def b():\n    return 2\n")
        _init_repo(tmp_path)

    def test_collect_rebuild_via_main_writes_full_artifact(self, tmp_path, monkeypatch):
        """`main.py --collect --rebuild` must perform a full rebuild
        end-to-end: every module re-summarized, artifact.json written.
        Not just dispatch to the right action name — actual behavior."""
        import main as main_mod

        calls: list = []

        def _stub_llm(system: str, user: str) -> str:
            calls.append((system, user))
            return json.dumps({"purpose": "stub purpose", "notes": ""})

        monkeypatch.setattr("tools.collect.summarizer.make_summarizer_call", lambda cfg, task_mode=None: _stub_llm)
        monkeypatch.setattr("tools.collect.summarizer.should_run_pass_b", lambda cfg: True)

        self._make_mini_repo(tmp_path)

        argv = ["main.py", "--collect", "--rebuild", "--base", str(tmp_path)]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                main_mod.main()

        assert exc_info.value.code == 0
        n_modules = len(_artifact_modules(tmp_path / ".collect"))
        assert n_modules >= 2, "sanity: multi-module repo"
        assert len(calls) == n_modules, (
            f"--collect --rebuild made {len(calls)} LLM calls via main.py; "
            f"expected {n_modules} (one per module)"
        )
        assert (tmp_path / ".collect" / ARTIFACT_FILENAME).exists()
        assert (tmp_path / ".collect" / MANIFEST_FILENAME).exists()

    def test_collect_rebuild_via_main_prints_rebuild_path(self, tmp_path, monkeypatch, capsys):
        """The printed line must say 'collect rebuild' — proving the
        result.action was 'rebuild' and the message names the full
        rebuild path, not the incremental one."""
        import main as main_mod

        def _stub_llm(system: str, user: str) -> str:
            return json.dumps({"purpose": "stub purpose", "notes": ""})

        monkeypatch.setattr("tools.collect.summarizer.make_summarizer_call", lambda cfg, task_mode=None: _stub_llm)
        monkeypatch.setattr("tools.collect.summarizer.should_run_pass_b", lambda cfg: True)

        self._make_mini_repo(tmp_path)

        argv = ["main.py", "--collect", "--rebuild", "--base", str(tmp_path)]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                main_mod.main()

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "collect rebuild:" in captured.out, (
            f"expected 'collect rebuild:' in output, got: {captured.out!r}"
        )
        assert "re-summarized" in captured.out

    def test_collect_rebuild_no_llm_via_main_skips_pass_b(self, tmp_path, monkeypatch):
        """`main.py --collect --rebuild --no-llm` must pass
        llm_call=None to action_rebuild, skipping Pass B entirely.
        The artifact is structural only — no summaries."""
        import main as main_mod

        called = False

        def _should_never_be_called(system: str, user: str) -> str:
            nonlocal called
            called = True
            return json.dumps({"purpose": "stub purpose"})

        monkeypatch.setattr("tools.collect.summarizer.make_summarizer_call", _should_never_be_called)

        self._make_mini_repo(tmp_path)

        argv = ["main.py", "--collect", "--rebuild", "--no-llm", "--base", str(tmp_path)]
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                main_mod.main()

        assert exc_info.value.code == 0
        assert not called, "make_summarizer_call must not be called with --no-llm"
        assert (tmp_path / ".collect" / ARTIFACT_FILENAME).exists()
        payload = json.loads(
            (tmp_path / ".collect" / ARTIFACT_FILENAME).read_text(encoding="utf-8")
        )
        assert all(m["summary"] is None for m in payload["modules"])
