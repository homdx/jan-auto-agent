"""tests/test_collect_cli.py — COLLECT-19.

* `--check` never writes anything, anywhere.
* `--collect` creates the full artifact set in `.collect/` when missing;
  is a no-op (no write) once fresh.
* `--module <path>` updates only that module's record + patches the
  manifest, rather than doing a full rescan.
* `collect` can never modify a file outside `[collect] dir` — the whole
  source tree's hash is identical before and after every action.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    SUMMARIZE_CHECKPOINT_FILENAME,
    CollectCliError,
    action_check,
    action_collect,
    action_module,
    action_rebuild,
    action_refresh,
    parse_collect_args,
    resolve_collect_dir,
    run,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    # .collect/ is a build artifact directory (same category as .agent/)
    # and is git-ignored in the real project — mirrored here so these
    # fixtures match real-world dirty-tree behavior rather than treating
    # collect's own untracked output as an uncommitted source change.
    (root / ".gitignore").write_text(".collect/\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """COLLECT-10/COLLECT-15 seed data cites symbols from the *real*
    jan-auto-agent repo (build_chat_request, _parse_verdict_soft, ...),
    which don't exist in these synthetic mini repos. This module tests
    orchestration (COLLECT-19), not seed content (COLLECT-10/15), so the
    seeds are neutralized to empty for every test here.
    """
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


def _tree_hash(root: Path) -> str:
    """Hash of every tracked source file's content + relative path, used
    to prove `collect` never touches anything outside `[collect] dir`."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if ".collect" in path.parts or ".git" in path.parts:
            continue
        h.update(str(path.relative_to(root)).encode("utf-8"))
        h.update(path.read_bytes())
    return h.hexdigest()


# ── --check: never writes ───────────────────────────────────────────────────


def test_check_writes_nothing_when_never_run(mini_repo):
    before = _tree_hash(mini_repo)
    result = action_check(mini_repo)
    assert result.action == "check"
    assert result.wrote is False
    assert result.fresh is False
    assert not (mini_repo / ".collect").exists()
    assert _tree_hash(mini_repo) == before


def test_check_writes_nothing_when_stale_or_fresh(mini_repo):
    action_collect(mini_repo)
    collect_dir = resolve_collect_dir(mini_repo, None)
    manifest_before = (collect_dir / MANIFEST_FILENAME).read_bytes()
    src_before = _tree_hash(mini_repo)

    result = action_check(mini_repo)
    assert result.wrote is False
    assert result.fresh is True
    assert (collect_dir / MANIFEST_FILENAME).read_bytes() == manifest_before
    assert _tree_hash(mini_repo) == src_before

    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 2\n")
    src_before = _tree_hash(mini_repo)
    result = action_check(mini_repo)
    assert result.wrote is False
    assert result.fresh is False
    assert (collect_dir / MANIFEST_FILENAME).read_bytes() == manifest_before
    assert _tree_hash(mini_repo) == src_before


# ── --collect: builds if missing/stale, no-op once fresh ───────────────────


def test_collect_builds_full_artifact_set(mini_repo):
    before = _tree_hash(mini_repo)
    result = action_collect(mini_repo)

    assert result.wrote is True
    collect_dir = result.collect_dir
    assert collect_dir == mini_repo / ".collect"
    assert (collect_dir / ARTIFACT_FILENAME).exists()
    assert (collect_dir / MANIFEST_FILENAME).exists()
    for page in (
        "ARCHITECTURE.md", "MODULE_MAP.md", "CONTRACTS.md",
        "FAIL_OPEN_REGISTRY.md", "GATES.md", "TEST_MAP.md",
        "RISK_INDEX.md", "CONFIG_MAP.md", "GLOSSARY.md",
    ):
        assert (collect_dir / page).exists(), f"missing {page}"
        assert page in result.written_files

    # source tree untouched
    assert _tree_hash(mini_repo) == before


def test_collect_is_noop_once_fresh(mini_repo):
    first = action_collect(mini_repo)
    assert first.wrote is True
    artifact_path = first.collect_dir / ARTIFACT_FILENAME
    mtime_before = artifact_path.stat().st_mtime_ns

    second = action_collect(mini_repo)
    assert second.wrote is False
    assert second.fresh is True
    assert artifact_path.stat().st_mtime_ns == mtime_before


def test_collect_rebuilds_when_stale(mini_repo):
    action_collect(mini_repo)
    (mini_repo / "pkg" / "b.py").write_text("def b():\n    return 99\n")

    result = action_collect(mini_repo)
    assert result.wrote is True
    assert result.fresh is True


# ── manifest dirty flag must reflect the tracked tree, not collect's own
#    output (`.collect/` isn't git-ignored, so a check running *after* the
#    write would see the write's own untracked files and misreport dirty)
#    ─────────────────────────────────────────────────────────────────────


def _manifest_dirty(collect_dir: Path) -> bool:
    import json as _json

    payload = _json.loads((collect_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    return bool(payload["dirty"])


def test_collect_full_build_reports_clean_on_clean_tree(mini_repo):
    """Regression: a full `--collect` build on a freshly committed, clean
    tree must record `dirty=False`. This used to be unconditionally True
    because provenance was captured after `_write_artifact` had already
    dropped untracked files into `.collect/`."""
    result = action_collect(mini_repo)
    assert _manifest_dirty(result.collect_dir) is False


def test_refresh_incremental_reports_clean_on_clean_tree(mini_repo):
    """Same regression, exercised via the incremental branch of
    `action_refresh` (a second `--refresh` against an existing artifact),
    which captures provenance independently of the full-build path."""
    action_collect(mini_repo)
    result = action_refresh(mini_repo)
    assert _manifest_dirty(result.collect_dir) is False


def test_module_patch_reports_clean_on_clean_tree(mini_repo):
    """Same regression again, exercised via `action_module`'s hand-built
    patched manifest, which used to compute provenance after its own
    `_write_artifact` call too."""
    action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")
    _git(mini_repo, "add", "-A")
    _git(mini_repo, "commit", "-q", "-m", "update a.py")
    result = action_module(mini_repo, "pkg/a.py")
    assert _manifest_dirty(result.collect_dir) is False


# ── --refresh: diff-driven incremental, regardless of freshness ─────────────


def test_refresh_rebuilds_even_when_fresh(mini_repo):
    first = action_collect(mini_repo)
    assert first.wrote is True

    result = action_refresh(mini_repo)
    assert result.wrote is True
    assert result.action == "refresh"


# ── --module: incremental ───────────────────────────────────────────────────


def test_module_patches_single_record_and_manifest(mini_repo):
    action_collect(mini_repo)

    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n\ndef extra():\n    pass\n")
    before = _tree_hash(mini_repo)
    result = action_module(mini_repo, "pkg/a.py")

    assert result.action == "module"
    assert result.wrote is True
    assert _tree_hash(mini_repo) == before  # module read, never written to

    import json
    artifact = json.loads((result.collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    by_path = {m["path"]: m for m in artifact["modules"]}
    assert "pkg/a.py" in by_path
    qualnames = {s["qualname"] for s in by_path["pkg/a.py"]["public_symbols"]}
    assert "pkg/a.py:extra" in qualnames
    # untouched module b.py still present, not re-parsed away
    assert "pkg/b.py" in by_path

    # manifest patched — now fresh again
    check = action_check(mini_repo)
    assert check.fresh is True


def test_module_falls_back_to_full_refresh_without_existing_artifact(mini_repo):
    result = action_module(mini_repo, "pkg/a.py")
    assert result.action == "module"
    assert result.wrote is True
    assert "no existing artifact" in result.message
    assert (result.collect_dir / ARTIFACT_FILENAME).exists()


def test_module_rejects_nonexistent_path(mini_repo):
    action_collect(mini_repo)
    with pytest.raises(CollectCliError):
        action_module(mini_repo, "pkg/does_not_exist.py")


def test_module_raises_clean_error_on_undecodable_file(mini_repo):
    """BUGFIX regression: `action_module` used to `read_text()` the
    target file with no guard at all — a present-but-undecodable file
    (not valid UTF-8) raised a bare `UnicodeDecodeError` straight out of
    `action_module` instead of the clean `CollectCliError` every other
    user-facing failure in this function already gets (a missing path,
    an unreadable existing artifact)."""
    action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_bytes(b"x = 1\n# not valid utf-8: \xff\xfe\n")
    with pytest.raises(CollectCliError):
        action_module(mini_repo, "pkg/a.py")


# ── COLLECT-28: --module dispatches by language, not just Python ───────────
#
# action_module used to call scan_module (the Python-only, ast.parse-based
# path) directly, so `--module Foo.java` would `ast.parse` valid Java
# source, get a SyntaxError, and record a perfectly valid Java file as
# broken *Python*. Fixed by routing through scan_file (the same
# language-dispatching entry point scan_repo's own loop uses).


def test_module_on_java_file_is_parsed_as_java_not_broken_python(mini_repo):
    from tools.collect.java_parser import is_available

    if not is_available():
        pytest.skip("tree-sitter-java not installed")

    action_collect(mini_repo)  # existing Python-only artifact
    (mini_repo / "Greeting.java").write_text(
        "public class Greeting {\n    public String hello() { return \"hi\"; }\n}\n",
        encoding="utf-8",
    )
    result = action_module(mini_repo, "Greeting.java")
    assert result.action == "module"

    import json

    artifact = json.loads((result.collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    by_path = {m["path"]: m for m in artifact["modules"]}
    assert "Greeting.java" in by_path
    record = by_path["Greeting.java"]
    assert record["language"] == "java"
    assert record["parse_error"] is None
    qualnames = {s["qualname"] for s in record["public_symbols"]}
    assert "Greeting.java:Greeting" in qualnames
    assert "Greeting.java:Greeting.hello" in qualnames


# ── [collect] dir config plumbing ───────────────────────────────────────────


def test_custom_collect_dir_from_config(mini_repo):
    config = configparser.ConfigParser()
    config.read_dict({"collect": {"dir": "artifacts/model"}})
    result = action_collect(mini_repo, config=config)
    assert result.collect_dir == mini_repo / "artifacts" / "model"
    assert (mini_repo / "artifacts" / "model" / ARTIFACT_FILENAME).exists()
    assert not (mini_repo / ".collect").exists()


# ── run() dispatch + parse_collect_args ─────────────────────────────────────


def test_run_dispatches_all_actions(mini_repo):
    assert run(mini_repo, "check").action == "check"
    assert run(mini_repo, "collect").action == "collect"
    assert run(mini_repo, "refresh").action == "refresh"
    assert run(mini_repo, "module", module_path="pkg/a.py").action == "module"


def test_run_rejects_unknown_action(mini_repo):
    with pytest.raises(CollectCliError):
        run(mini_repo, "bogus")


def test_run_module_requires_module_path(mini_repo):
    with pytest.raises(CollectCliError):
        run(mini_repo, "module")


def test_parse_collect_args_defaults_to_collect():
    assert parse_collect_args([]) == {"action": "collect", "module_path": None, "drop_summaries": False}


def test_parse_collect_args_check():
    assert parse_collect_args(["--check"]) == {"action": "check", "module_path": None, "drop_summaries": False}


def test_parse_collect_args_refresh():
    assert parse_collect_args(["--refresh"]) == {"action": "refresh", "module_path": None, "drop_summaries": False}


def test_parse_collect_args_drop_summaries():
    # V8: `--drop-summaries` rides along with whichever action was asked for.
    assert parse_collect_args(["--rebuild", "--no-llm", "--drop-summaries"]) == {
        "action": "rebuild", "module_path": None, "drop_summaries": True,
    }


def test_parse_collect_args_module():
    assert parse_collect_args(["--module", "pkg/a.py"]) == {
        "action": "module", "module_path": "pkg/a.py", "drop_summaries": False,
    }


def test_parse_collect_args_module_missing_path_raises():
    with pytest.raises(CollectCliError):
        parse_collect_args(["--module"])


# ── RUN-11: a Ctrl-C mid-Pass B costs one module ─────────────────────────────
#
# Nothing touches `.collect/` until Pass C, the registries, the test map and
# the risk index have run, so an interrupt anywhere in a six-hour batch used
# to leave `.collect/` exactly as it was — the next `--collect` or `--auto`
# start took the same decision and paid for the same summaries again. The
# two batch calls now checkpoint every landed summary into
# `<collect dir>/collect_summarize_state.json`.


def _interrupting_llm(after: int):
    """An LLM that lands `after` modules and then behaves like a Ctrl-C."""
    calls = {"n": 0}

    def _call(system, user):
        calls["n"] += 1
        if calls["n"] > after:
            raise KeyboardInterrupt("operator gave up on the 429 storm")
        return json.dumps({"purpose": "landed purpose", "notes": ""})

    return _call, calls


def _counting_llm():
    calls = {"n": 0}

    def _call(system, user):
        calls["n"] += 1
        return json.dumps({"purpose": "counted purpose", "notes": ""})

    return _call, calls


def test_collect_keyboard_interrupt_keeps_the_landed_summaries(mini_repo):
    llm, calls = _interrupting_llm(after=2)

    with pytest.raises(KeyboardInterrupt):
        action_collect(mini_repo, llm_call=llm)

    collect_dir = resolve_collect_dir(mini_repo, None)
    checkpoint = collect_dir / SUMMARIZE_CHECKPOINT_FILENAME
    assert checkpoint.exists()
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert state["loop"] == "collect_summarize"
    # Three modules in this repo, interrupted on the third call: two landed.
    assert calls["n"] == 3
    assert len(state["modules"]) == 2
    for entry in state["modules"].values():
        # Each entry is tied to the source it was summarized from.
        assert set(entry) == {"purpose", "notes", "sha"}
        assert entry["sha"]
        assert entry["purpose"] == "landed purpose"
    # Nothing else was written: the interrupted run left no artifact.
    assert not (collect_dir / ARTIFACT_FILENAME).exists()
    assert not (collect_dir / MANIFEST_FILENAME).exists()


def test_collect_resumes_the_interrupted_batch_and_writes_every_summary(mini_repo, capsys):
    with pytest.raises(KeyboardInterrupt):
        action_collect(mini_repo, llm_call=_interrupting_llm(after=2)[0])

    collect_dir = resolve_collect_dir(mini_repo, None)
    checkpoint = collect_dir / SUMMARIZE_CHECKPOINT_FILENAME
    assert checkpoint.exists()

    llm, calls = _counting_llm()
    result = action_collect(mini_repo, llm_call=llm)

    assert result.wrote is True
    # Only what was still owed was asked for: 3 modules, 2 already paid for.
    assert calls["n"] == 3 - 2
    artifact = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    assert len(artifact["modules"]) == 3
    assert all(m["summary"] is not None for m in artifact["modules"])
    assert {m["summary"]["purpose"] for m in artifact["modules"]} == {
        "landed purpose", "counted purpose",
    }
    # The batch completed, so the checkpoint is gone.
    assert not checkpoint.exists()

    resume_lines = [ln for ln in capsys.readouterr().out.splitlines() if "Pass B resumes" in ln]
    assert len(resume_lines) == 1
    assert resume_lines[0] == (
        "[collect] Pass B resumes: 2 module(s) kept from the interrupted run, "
        "0 re-summarised (source changed)"
    )


@pytest.fixture
def mini_repo_six(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for i in range(1, 6):
        (pkg / f"m{i}.py").write_text(f"def m{i}():\n    return {i}\n")
    (tmp_path / ".gitignore").write_text(".collect/\n")
    _init_repo(tmp_path)
    return tmp_path


def test_refresh_resumes_the_interrupted_incremental_batch(mini_repo_six, capsys):
    """Same round-trip through `action_refresh`'s incremental batch — the
    path a `collector_version`-bump fallback takes, and the one a 429 storm
    kills: 5 files changed, interrupted after 2 landed."""
    action_collect(mini_repo_six)  # artifact + manifest, Pass B off
    for i in range(1, 6):
        (mini_repo_six / "pkg" / f"m{i}.py").write_text(f"def m{i}():\n    return {i * 100}\n")

    llm1, calls1 = _interrupting_llm(after=2)
    with pytest.raises(KeyboardInterrupt):
        action_refresh(mini_repo_six, llm_call=llm1)

    collect_dir = resolve_collect_dir(mini_repo_six, None)
    checkpoint = collect_dir / SUMMARIZE_CHECKPOINT_FILENAME
    assert calls1["n"] == 3
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert state["loop"] == "collect_summarize"
    assert set(state["modules"]) == {"pkg/m1.py", "pkg/m2.py"}
    for entry in state["modules"].values():
        assert set(entry) == {"purpose", "notes", "sha"}
        assert entry["sha"]
    # The interrupted run wrote nothing else: the previous artifact stands,
    # with every summary still null (Pass B never ran in run 1).
    before = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    assert len(before["modules"]) == 6
    assert all(m["summary"] is None for m in before["modules"])

    llm2, calls2 = _counting_llm()
    result = action_refresh(mini_repo_six, llm_call=llm2)

    assert result.wrote is True
    assert calls2["n"] == 5 - 2
    artifact = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    assert len(artifact["modules"]) == 6
    summarized = [m for m in artifact["modules"] if m["summary"] is not None]
    assert len(summarized) == 5
    assert {m["path"] for m in summarized} == {f"pkg/m{i}.py" for i in range(1, 6)}
    assert not checkpoint.exists()
    assert capsys.readouterr().out.count("Pass B resumes: 2 module(s) kept") == 1


def test_module_creates_no_checkpoint(mini_repo):
    """A one-module batch has nothing to resume, so it must not create the
    file — and must not touch the one an interrupted full build left."""
    action_collect(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")

    result = action_module(mini_repo, "pkg/a.py", llm_call=_counting_llm()[0])

    assert result.wrote is True
    assert not (resolve_collect_dir(mini_repo, None) / SUMMARIZE_CHECKPOINT_FILENAME).exists()


def test_module_leaves_an_interrupted_full_builds_checkpoint_untouched(mini_repo):
    """`summarize_repo` clears its checkpoint when a batch completes; a
    `--module` run without a checkpoint path must not delete the file an
    interrupted full build is waiting on."""
    action_collect(mini_repo)

    with pytest.raises(KeyboardInterrupt):
        action_rebuild(mini_repo, llm_call=_interrupting_llm(after=1)[0])

    checkpoint = resolve_collect_dir(mini_repo, None) / SUMMARIZE_CHECKPOINT_FILENAME
    assert checkpoint.exists()
    before = checkpoint.read_bytes()

    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 42\n")
    result = action_module(mini_repo, "pkg/a.py", llm_call=_counting_llm()[0])

    assert result.wrote is True
    assert checkpoint.read_bytes() == before


def test_checkpointed_entries_are_not_reused_after_the_source_moves(mini_repo, capsys):
    """The entry's `sha` is what ties it to its source: a file that changed
    between the interrupted run and this one is re-summarized, not reused,
    and the resume line says how many."""
    with pytest.raises(KeyboardInterrupt):
        action_collect(mini_repo, llm_call=_interrupting_llm(after=1)[0])

    collect_dir = resolve_collect_dir(mini_repo, None)
    checkpoint = collect_dir / SUMMARIZE_CHECKPOINT_FILENAME

    # Move the one file the interrupted run had already paid for.
    (mini_repo / "pkg" / "__init__.py").write_text("# regenerated\n")

    llm, calls = _counting_llm()
    result = action_collect(mini_repo, llm_call=llm)

    assert result.wrote is True
    assert calls["n"] == 3  # nothing from the checkpoint was reusable
    artifact = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    assert all(m["summary"] is not None for m in artifact["modules"])
    assert not checkpoint.exists()
    out = capsys.readouterr().out
    resume_lines = [ln for ln in out.splitlines() if "Pass B resumes" in ln]
    assert len(resume_lines) == 1
    assert "1 re-summarised (source changed)" in resume_lines[0]
