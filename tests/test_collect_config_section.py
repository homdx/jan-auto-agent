"""tests/test_collect_config_section.py — COLLECT-20.

* Absence of `[collect]` (or of `agents.ini` entirely, i.e. `config=None`)
  never breaks a run — every default equals today's behavior.
* Defaults apply key-by-key when the section exists but a given key is
  missing.
* An invalid `staleness` value falls back to `"warn"` rather than raising.
* An invalid boolean value falls back to its own default rather than
  raising.
* `dir` still round-trips through `resolve_collect_dir` exactly as before
  COLLECT-20 (COLLECT-19 regression guard).
* `enabled = false` turns `collect`/`refresh`/`module` into a documented
  no-op while leaving `check` untouched.
"""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import (
    DEFAULT_COLLECT_DIR,
    DEFAULT_STALENESS,
    VALID_STALENESS,
    CollectSettings,
    action_check,
    read_collect_settings,
    resolve_collect_dir,
    run,
)


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """Same rationale as tests/test_collect_cli.py: COLLECT-10/15 seed data
    cites real repo symbols, which the mini-repo fixtures below don't
    have. This file tests [collect] config plumbing (COLLECT-20), not
    seed content, so the seed builders are neutralized for full-build
    tests here."""
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    _init_repo(tmp_path)
    return tmp_path


def _config(text: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    config.read_string(text)
    return config


# ── absence never breaks anything ───────────────────────────────────────────


def test_no_config_object_returns_all_defaults():
    settings = read_collect_settings(None)
    assert settings == CollectSettings()
    assert settings.enabled is True
    assert settings.dir == DEFAULT_COLLECT_DIR
    assert settings.use_in_auto is False
    assert settings.use_in_doc is False
    assert settings.use_in_bughunt is False
    assert settings.staleness == DEFAULT_STALENESS
    assert settings.llm_summaries is True
    assert settings.think is False


def test_missing_section_returns_all_defaults():
    config = _config("[other]\nkey = value\n")
    assert read_collect_settings(config) == CollectSettings()


def test_empty_config_parser_returns_defaults():
    config = configparser.ConfigParser()
    assert read_collect_settings(config) == CollectSettings()


def test_missing_collect_section_does_not_break_check(mini_repo):
    # No agents.ini / no [collect] at all — /collect --check must behave
    # exactly like it always has: report "never run", write nothing.
    result = action_check(mini_repo, config=None)
    assert result.wrote is False
    assert result.fresh is False


# ── per-key defaults when section exists but keys are partial ──────────────


def test_partial_section_fills_in_remaining_defaults():
    config = _config("[collect]\nuse_in_auto = true\n")
    settings = read_collect_settings(config)
    assert settings.use_in_auto is True
    # everything else still default
    assert settings.enabled is True
    assert settings.dir == DEFAULT_COLLECT_DIR
    assert settings.use_in_doc is False
    assert settings.use_in_bughunt is False
    assert settings.staleness == DEFAULT_STALENESS
    assert settings.llm_summaries is True
    assert settings.think is False


def test_all_keys_explicit():
    config = _config(
        "[collect]\n"
        "enabled = false\n"
        "dir = artifacts/model\n"
        "use_in_auto = true\n"
        "use_in_doc = true\n"
        "use_in_bughunt = true\n"
        "staleness = refresh\n"
        "llm_summaries = false\n"
        "think = true\n"
    )
    settings = read_collect_settings(config)
    assert settings == CollectSettings(
        enabled=False,
        dir="artifacts/model",
        use_in_auto=True,
        use_in_doc=True,
        use_in_bughunt=True,
        staleness="refresh",
        llm_summaries=False,
        think=True,
    )


# ── invalid staleness falls back to warn ────────────────────────────────────


@pytest.mark.parametrize("bad_value", ["bogus", "Refresh-ish", "", "  ", "warnn"])
def test_invalid_staleness_falls_back_to_warn(bad_value):
    config = _config(f"[collect]\nstaleness = {bad_value}\n")
    assert read_collect_settings(config).staleness == "warn"


@pytest.mark.parametrize("value", sorted(VALID_STALENESS))
def test_valid_staleness_values_pass_through(value):
    config = _config(f"[collect]\nstaleness = {value}\n")
    assert read_collect_settings(config).staleness == value


def test_staleness_case_insensitive():
    config = _config("[collect]\nstaleness = REFRESH\n")
    assert read_collect_settings(config).staleness == "refresh"


# ── invalid booleans fall back to their own default ─────────────────────────


def test_invalid_boolean_falls_back_to_default():
    config = _config("[collect]\nenabled = maybe\nuse_in_auto = sort-of\n")
    settings = read_collect_settings(config)
    assert settings.enabled is True  # default, not a crash
    assert settings.use_in_auto is False  # default, not a crash


# ── dir / resolve_collect_dir regression guard (COLLECT-19) ────────────────


def test_resolve_collect_dir_default(tmp_path):
    assert resolve_collect_dir(tmp_path, None) == tmp_path / DEFAULT_COLLECT_DIR


def test_resolve_collect_dir_custom_relative(tmp_path):
    config = _config("[collect]\ndir = artifacts/model\n")
    assert resolve_collect_dir(tmp_path, config) == tmp_path / "artifacts" / "model"


def test_resolve_collect_dir_custom_absolute(tmp_path):
    abs_dir = tmp_path / "somewhere-else"
    config = _config(f"[collect]\ndir = {abs_dir}\n")
    assert resolve_collect_dir(tmp_path, config) == abs_dir


# ── enabled=false gates collect/refresh/module, not check ──────────────────


def test_disabled_collect_is_a_documented_noop(mini_repo):
    config = _config("[collect]\nenabled = false\n")

    for action, kwargs in (
        ("collect", {}),
        ("refresh", {}),
        ("module", {"module_path": "pkg/a.py"}),
    ):
        result = run(mini_repo, action, config=config, **kwargs)
        assert result.wrote is False
        assert "disabled" in result.message
        assert not (mini_repo / DEFAULT_COLLECT_DIR).exists()


def test_disabled_check_still_reports_freshness(mini_repo):
    config = _config("[collect]\nenabled = false\n")
    result = run(mini_repo, "check", config=config)
    assert result.action == "check"
    assert result.wrote is False
    assert result.fresh is False  # still a real freshness verdict, not a stub


def test_enabled_true_behaves_like_default(mini_repo):
    config = _config("[collect]\nenabled = true\n")
    result = run(mini_repo, "collect", config=config)
    assert result.wrote is True
    assert (mini_repo / DEFAULT_COLLECT_DIR).exists()


# ── the shipped agents.ini's [collect] section is itself valid ─────────────


def test_shipped_agents_ini_collect_section_is_valid():
    repo_root = Path(__file__).parent.parent
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    config.read(repo_root / "agents.ini", encoding="utf-8")
    assert config.has_section("collect")
    settings = read_collect_settings(config)
    assert settings.staleness in VALID_STALENESS
    assert isinstance(settings.enabled, bool)
    assert isinstance(settings.dir, str) and settings.dir
