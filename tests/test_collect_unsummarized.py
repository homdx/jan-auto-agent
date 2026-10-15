"""tests/test_collect_unsummarized.py — CC-15.

A module whose Pass B gave up after max_retries used to be written without a
summary but with the same content hash as a successfully summarized one, so the
manifest declared the whole artifact "up to date" and the module was never
retried. This module pins the fix:

* the manifest records which modules have no summary (``Manifest.unsummarized``);
* an older manifest without the key reads as ``[]``;
* ``--check`` / ``--collect`` / ``--refresh`` treat a non-empty
  ``unsummarized`` as stale when ``llm_summaries = true``;
* the next ``--collect`` sends exactly those modules to Pass B again, alongside
  any changed ones;
* a ``--no-llm`` build records ``[]`` and is not stale for missing summaries;
* ``load_readonly`` names the count in its stale reason;
* the progress line says ``no summary`` for the failed module, and the build's
  last line counts them.
"""

from __future__ import annotations

import configparser
import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect import loader as loader_mod
from tools.collect import manifest as manifest_mod
from tools.collect.cli import action_check, action_collect, action_rebuild, action_refresh, resolve_collect_dir


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """The refused module's retries do not wait out the real backoff (7 s per failing refresh)."""
    summarize_repo = cli_mod.summarize_repo
    monkeypatch.setattr(cli_mod, "summarize_repo",
                        lambda *a, **k: summarize_repo(*a, **{"sleep_fn": lambda _s: None, **k}))


@pytest.fixture
def mini_repo_two_modules(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "store.py").write_text("class AlphaStore:\n    pass\n")
    (pkg / "still.py").write_text("class Still:\n    pass\n")
    (tmp_path / ".gitignore").write_text(".collect/\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _read_manifest(collect_dir: Path) -> manifest_mod.Manifest:
    return manifest_mod.read_manifest(collect_dir / cli_mod.MANIFEST_FILENAME)


def _read_artifact(collect_dir: Path) -> dict:
    return json.loads((collect_dir / cli_mod.ARTIFACT_FILENAME).read_text(encoding="utf-8"))


def _stub_llm(refusing_paths=None):
    """Return an LLM call stub and a callable to toggle refusing for specific paths.

    ``refusing_paths`` is a set of module-path substrings; when the user message
    mentions any of them the stub raises RuntimeError, which is the shape
    ``summarize_repo`` treats as a retry-worthy error.
    """
    refusing = set(refusing_paths or [])

    def _call(system, user):
        for path in refusing:
            if path in user:
                raise RuntimeError(f"provider error for {path}")
        # Extract class names from the prompt to use as purpose.
        import re
        classes = re.findall(r"class\s+(\w+)", user)
        purpose = "Defines " + ", ".join(classes) + "." if classes else "Module."
        return json.dumps({"purpose": purpose, "notes": ""})

    return _call


def _tracking_llm(inner, asked):
    """Wrap `_stub_llm`'s stub, recording the module path of every prompt it sees
    (`summarizer._prompt` starts the user message with `path: <module path>`), so a
    test can assert *which* modules a refresh handed to Pass B."""
    import re

    def _call(system, user):
        match = re.search(r"^path:\s*(\S+)", user, re.MULTILINE)
        if match:
            asked.append(match.group(1))
        return inner(system, user)

    return _call


def _fail_store(root: Path) -> None:
    """Drive the repo into the CC-15 state the CC-16 tests start from: a full build
    with a healthy model, then a change of `pkg/store.py` that the model refuses, so
    `pkg/store.py` is the one module listed in the manifest as without a summary."""
    action_collect(root, llm_call=_stub_llm())
    (root / "pkg" / "store.py").write_text("class BetaStore:\n    pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "rename AlphaStore -> BetaStore")
    action_collect(root, llm_call=_stub_llm(refusing_paths=["store.py"]))
    manifest = _read_manifest(resolve_collect_dir(root, None))
    assert manifest.unsummarized == ("pkg/store.py",)


# ── reproduction: step for step ───────────────────────────────────────────────

def test_reproduction_step1_both_modules_summarized(mini_repo_two_modules, capsys):
    """Step 1: full collect on a two-module repo — both get summaries."""
    llm = _stub_llm()
    result = action_collect(mini_repo_two_modules, llm_call=llm)
    assert result.wrote is True
    # Three modules total (including __init__.py), all summarized.
    assert "3 module(s) re-summarized" in result.message
    collect_dir = resolve_collect_dir(mini_repo_two_modules, None)
    manifest = _read_manifest(collect_dir)
    assert manifest.unsummarized == ()
    artifact = _read_artifact(collect_dir)
    by_path = {m["path"]: m for m in artifact["modules"]}
    assert by_path["pkg/store.py"]["summary"] is not None
    assert by_path["pkg/still.py"]["summary"] is not None


def test_reproduction_step2_rename_and_fail(mini_repo_two_modules, capsys):
    """Step 2: rename the class in store.py, commit, make the LLM refuse store.py,
    then collect — store.py ends up without a summary."""
    llm = _stub_llm()
    # First, a clean full build.
    action_collect(mini_repo_two_modules, llm_call=llm)
    # Rename the class.
    (mini_repo_two_modules / "pkg" / "store.py").write_text("class BetaStore:\n    pass\n")
    _git(mini_repo_two_modules, "add", "-A")
    _git(mini_repo_two_modules, "commit", "-q", "-m", "rename AlphaStore -> BetaStore")
    # Now make the LLM refuse store.py.
    llm = _stub_llm(refusing_paths=["store.py"])
    result = action_collect(mini_repo_two_modules, llm_call=llm)
    assert result.wrote is True
    collect_dir = resolve_collect_dir(mini_repo_two_modules, None)
    manifest = _read_manifest(collect_dir)
    assert manifest.unsummarized == ("pkg/store.py",)
    artifact = _read_artifact(collect_dir)
    by_path = {m["path"]: m for m in artifact["modules"]}
    assert by_path["pkg/store.py"]["summary"] is None
    assert by_path["pkg/still.py"]["summary"] is not None
    # The progress line says "no summary" for the failed module.
    out = capsys.readouterr().out
    assert any("no summary pkg/store.py" in ln for ln in out.splitlines())
    # ... with the last error, so the operator sees why (the ticket: `no summary` with the last error)
    assert any("no summary pkg/store.py (" in ln and "provider error" in ln for ln in out.splitlines())


def test_reproduction_step3_healthy_again_reported_stale(mini_repo_two_modules, capsys):
    """Step 3: use a healthy LLM again — --collect must NOT say 'already up to date'.
    --check must report stale, and --collect must retry store.py."""
    # Replay steps 1 and 2 to set up the unsummarized state.
    llm = _stub_llm()
    action_collect(mini_repo_two_modules, llm_call=llm)
    (mini_repo_two_modules / "pkg" / "store.py").write_text("class BetaStore:\n    pass\n")
    _git(mini_repo_two_modules, "add", "-A")
    _git(mini_repo_two_modules, "commit", "-q", "-m", "rename AlphaStore -> BetaStore")
    llm = _stub_llm(refusing_paths=["store.py"])
    action_collect(mini_repo_two_modules, llm_call=llm)
    # Now use a healthy LLM again.
    llm = _stub_llm()
    # --check must be stale with the count.
    check = action_check(mini_repo_two_modules)
    assert check.fresh is False
    assert "1 module without a summary" in check.message
    # --collect must retry store.py and succeed.
    result = action_collect(mini_repo_two_modules, llm_call=llm)
    assert result.wrote is True
    collect_dir = resolve_collect_dir(mini_repo_two_modules, None)
    manifest = _read_manifest(collect_dir)
    assert manifest.unsummarized == ()
    artifact = _read_artifact(collect_dir)
    by_path = {m["path"]: m for m in artifact["modules"]}
    assert by_path["pkg/store.py"]["summary"] is not None


# ── a module that fails twice stays listed and does not block others ─────────

def test_failing_module_does_not_block_other_modules(mini_repo_two_modules):
    """When store.py fails, still.py must still be summarized if it changed."""
    llm = _stub_llm()
    # Set up unsummarized state.
    action_collect(mini_repo_two_modules, llm_call=llm)
    (mini_repo_two_modules / "pkg" / "store.py").write_text("class BetaStore:\n    pass\n")
    _git(mini_repo_two_modules, "add", "-A")
    _git(mini_repo_two_modules, "commit", "-q", "-m", "rename AlphaStore -> BetaStore")
    llm = _stub_llm(refusing_paths=["store.py"])
    action_collect(mini_repo_two_modules, llm_call=llm)
    # Now change still.py but keep store.py failing — store.py stays unsummarized
    # but still.py gets summarized because it changed and the LLM accepts it.
    (mini_repo_two_modules / "pkg" / "still.py").write_text("class StillChanged:\n    pass\n")
    _git(mini_repo_two_modules, "add", "-A")
    _git(mini_repo_two_modules, "commit", "-q", "-m", "change still.py")
    llm = _stub_llm(refusing_paths=["store.py"])
    result = action_collect(mini_repo_two_modules, llm_call=llm)
    assert result.wrote is True
    collect_dir = resolve_collect_dir(mini_repo_two_modules, None)
    manifest = _read_manifest(collect_dir)
    artifact = _read_artifact(collect_dir)
    by_path = {m["path"]: m for m in artifact["modules"]}
    # store.py still has no summary (LLM still refuses it).
    assert by_path["pkg/store.py"]["summary"] is None
    # still.py got a new summary.
    assert by_path["pkg/still.py"]["summary"] is not None
    assert manifest.unsummarized == ("pkg/store.py",)


# ── --no-llm builds record [] and are not stale for missing summaries ─────────

def test_no_llm_build_records_empty_unsummarized(mini_repo_two_modules):
    """A --no-llm build (no llm_call) leaves unsummarized empty, so the
    manifest is fresh as long as hashes match."""
    result = action_collect(mini_repo_two_modules)
    assert result.wrote is True
    collect_dir = resolve_collect_dir(mini_repo_two_modules, None)
    manifest = _read_manifest(collect_dir)
    assert manifest.unsummarized == ()
    # Second collect is a no-op.
    second = action_collect(mini_repo_two_modules)
    assert second.wrote is False
    assert second.fresh is True


def test_no_llm_build_with_config_flag(mini_repo_two_modules):
    """Explicit [collect] llm_summaries = false also records []."""
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"llm_summaries": "false"}
    result = action_collect(mini_repo_two_modules, config=cfg)
    assert result.wrote is True
    collect_dir = resolve_collect_dir(mini_repo_two_modules, cfg)
    manifest = _read_manifest(collect_dir)
    assert manifest.unsummarized == ()
    check = action_check(mini_repo_two_modules, config=cfg)
    assert check.fresh is True


# ── older manifest without the key reads as [] ────────────────────────────────

def test_old_manifest_without_unsummarized_key_is_fresh(mini_repo_two_modules):
    """A manifest written by an older collector (no unsummarized key) must
    still compare fresh when hashes match — backward compatibility."""
    action_collect(mini_repo_two_modules)
    collect_dir = resolve_collect_dir(mini_repo_two_modules, None)
    manifest_path = collect_dir / cli_mod.MANIFEST_FILENAME
    raw = manifest_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    del payload["unsummarized"]
    manifest_path.write_text(json.dumps(payload))
    check = action_check(mini_repo_two_modules)
    assert check.fresh is True


# ── load_readonly reports the stale state and the count ───────────────────────

def test_load_readonly_names_unsummarized_modules(mini_repo_two_modules):
    """CC-13: load_readonly must include the unsummarized count in its
    stale reason, not only file-hash changes."""
    llm = _stub_llm()
    # Set up unsummarized state.
    action_collect(mini_repo_two_modules, llm_call=llm)
    (mini_repo_two_modules / "pkg" / "store.py").write_text("class BetaStore:\n    pass\n")
    _git(mini_repo_two_modules, "add", "-A")
    _git(mini_repo_two_modules, "commit", "-q", "-m", "rename AlphaStore -> BetaStore")
    llm = _stub_llm(refusing_paths=["store.py"])
    action_collect(mini_repo_two_modules, llm_call=llm)
    read = loader_mod.load_readonly(mini_repo_two_modules)
    assert read.status == loader_mod.STATUS_STALE
    assert "1 module without a summary" in read.reason


# ── progress line says no summary ─────────────────────────────────────────────

def test_progress_line_says_no_summary(mini_repo_two_modules, capsys):
    """The per-module progress line for a failed module must say
    `no summary <path>`, not `summarized <path>`."""
    llm = _stub_llm()
    action_collect(mini_repo_two_modules, llm_call=llm)
    (mini_repo_two_modules / "pkg" / "store.py").write_text("class BetaStore:\n    pass\n")
    _git(mini_repo_two_modules, "add", "-A")
    _git(mini_repo_two_modules, "commit", "-q", "-m", "rename AlphaStore -> BetaStore")
    llm = _stub_llm(refusing_paths=["store.py"])
    action_collect(mini_repo_two_modules, llm_call=llm)
    out = capsys.readouterr().out
    assert "no summary pkg/store.py" in out


# ── CC-16 Part A: a refresh without Pass B keeps the list ─────────────────────

def test_refresh_without_pass_b_keeps_the_unsummarized_list(mini_repo_two_modules):
    """CC-16 (Part A), the ticket's reproduction: after a build whose Pass B refused
    `pkg/store.py`, an edit of another module and a refresh that does not run Pass B
    must not erase the list — it is the only record of which modules have no summary,
    and the manifest is the only place it lives."""
    root = mini_repo_two_modules
    _fail_store(root)
    collect_dir = resolve_collect_dir(root, None)

    (root / "pkg" / "still.py").write_text("class StillEdited:\n    pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "edit still.py")

    result = action_refresh(root, llm_call=None)
    assert result.wrote is True
    manifest = _read_manifest(collect_dir)
    assert manifest.unsummarized == ("pkg/store.py",)
    artifact = _read_artifact(collect_dir)
    by_path = {m["path"]: m for m in artifact["modules"]}
    assert by_path["pkg/store.py"]["summary"] is None
    # The refresh attempted nothing, so the module is still owed, and --check says so.
    check = action_check(root)
    assert check.fresh is False
    assert "1 module without a summary" in check.message


def test_refresh_without_pass_b_then_a_refresh_with_the_model_retries_only_the_listed(mini_repo_two_modules):
    """CC-16 (Part A): the module the no-Pass-B refresh could not retry is the one the
    next refresh *with* the model asks about — and only it, so the carried list is what
    cuts the requests, and it is cleared once the summary lands."""
    root = mini_repo_two_modules
    _fail_store(root)

    action_refresh(root, llm_call=None)
    asked: list = []
    llm = _tracking_llm(_stub_llm(), asked)
    result = action_refresh(root, llm_call=llm)
    assert result.wrote is True
    assert asked == ["pkg/store.py"]
    assert "1 without a summary" not in result.message
    manifest = _read_manifest(resolve_collect_dir(root, None))
    assert manifest.unsummarized == ()
    check = action_check(root)
    assert check.fresh is True


def test_unsummarized_list_carried_through_several_refreshes_without_pass_b(mini_repo_two_modules):
    """CC-16 (Part A): the list survives any number of refreshes that cannot retry,
    not just the first one."""
    root = mini_repo_two_modules
    _fail_store(root)
    collect_dir = resolve_collect_dir(root, None)
    for i in range(3):
        (root / "pkg" / "still.py").write_text(f"class Still{i}:\n    pass\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", f"edit still.py {i}")
        result = action_refresh(root, llm_call=None)
        assert result.wrote is True
        assert _read_manifest(collect_dir).unsummarized == ("pkg/store.py",)
    assert action_check(root).fresh is False


def test_listed_module_deleted_between_refreshes_is_dropped(mini_repo_two_modules):
    """CC-16 (Part A): the carried list is restricted to what is still true of the
    tree — a listed module that is gone drops out instead of being owed forever."""
    root = mini_repo_two_modules
    _fail_store(root)
    collect_dir = resolve_collect_dir(root, None)

    (root / "pkg" / "store.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "delete store.py")
    action_refresh(root, llm_call=None)
    assert _read_manifest(collect_dir).unsummarized == ()


def test_listed_module_moved_between_refreshes_is_dropped(mini_repo_two_modules):
    """CC-16 (Part A): the same for a listed module that moved — its old path is not
    in the tree any more, and the new one was never attempted."""
    root = mini_repo_two_modules
    _fail_store(root)
    collect_dir = resolve_collect_dir(root, None)

    (root / "pkg" / "store.py").rename(root / "pkg" / "moved.py")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "move store.py")
    action_refresh(root, llm_call=None)
    assert _read_manifest(collect_dir).unsummarized == ()


def test_refresh_without_pass_b_changing_a_listed_module_keeps_it_listed(mini_repo_two_modules):
    """CC-16 (Part A): a no-Pass-B refresh that changes a *listed* module edits the file,
    still produces no summary for it (nothing ran), and leaves it listed."""
    root = mini_repo_two_modules
    _fail_store(root)
    collect_dir = resolve_collect_dir(root, None)

    (root / "pkg" / "store.py").write_text("class GammaStore:\n    pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "edit store.py again")
    result = action_refresh(root, llm_call=None)
    assert result.wrote is True
    manifest = _read_manifest(collect_dir)
    assert manifest.unsummarized == ("pkg/store.py",)
    artifact = _read_artifact(collect_dir)
    by_path = {m["path"]: m for m in artifact["modules"]}
    assert by_path["pkg/store.py"]["summary"] is None
    assert action_check(root).fresh is False


def test_llm_summaries_false_keeps_the_list_like_no_llm_call(mini_repo_two_modules):
    """CC-16 (Part A): `[collect] llm_summaries = false` is the same path as
    `llm_call = None` — it runs no Pass B, so it keeps the list rather than clearing it."""
    root = mini_repo_two_modules
    _fail_store(root)
    collect_dir = resolve_collect_dir(root, None)

    cfg = configparser.ConfigParser()
    cfg["collect"] = {"llm_summaries": "false"}
    (root / "pkg" / "still.py").write_text("class StillEdited:\n    pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "edit still.py")
    result = action_refresh(root, config=cfg, llm_call=_stub_llm())
    assert result.wrote is True
    assert _read_manifest(collect_dir).unsummarized == ("pkg/store.py",)
    check = action_check(root, config=cfg)
    assert check.fresh is False
    assert "1 module without a summary" in check.message


def test_full_no_llm_build_still_records_empty_unsummarized(mini_repo_two_modules):
    """CC-16 (Part A): a *full* `--no-llm` build is unchanged — it rebuilds the list
    from the modules it handed to Pass B, and it handed none, so it records `[]` even
    when the previous manifest listed one."""
    root = mini_repo_two_modules
    _fail_store(root)
    collect_dir = resolve_collect_dir(root, None)

    result = action_collect(root, drop_summaries=True)  # no llm_call, and a stale tree -> full build? no: refresh
    assert result.wrote is True
    # An incremental refresh without Pass B carries the list (the Part A fix)...
    assert _read_manifest(collect_dir).unsummarized == ("pkg/store.py",)
    # ... while an explicit rebuild, which is a full build, records [] as before.
    result = action_rebuild(root, llm_call=None, drop_summaries=True)
    assert result.wrote is True
    assert _read_manifest(collect_dir).unsummarized == ()
