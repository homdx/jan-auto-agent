"""tests/test_collect_config_languages.py — COLLECT-28.

`[collect] languages` (default `python`): Java is not scanned by default
even if `.java` files exist in the tree (backward compat); explicit
`python,java` opt-in scans it. `[collect] java_extensions` (default
`.java`): which extensions count as Java once enabled.
"""

import configparser
from pathlib import Path

import pytest

from tools.collect.java_parser import is_available
from tools.collect.lang import (
    Language,
    enabled_languages,
    java_extensions_from_config,
)
from tools.collect.scanner import scan_repo

REPO_ROOT = Path(__file__).parent.parent


def _config(languages=None, java_extensions=None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    section = {}
    if languages is not None:
        section["languages"] = languages
    if java_extensions is not None:
        section["java_extensions"] = java_extensions
    if section:
        cfg["collect"] = section
    return cfg


def _mixed_tree(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "B.java").write_text("public class B { void f() {} }\n", encoding="utf-8")


# ── enabled_languages: the fallback chain ───────────────────────────────


def test_enabled_languages_defaults_to_python_only_with_no_config():
    assert enabled_languages(None) == frozenset({Language.PYTHON})


def test_enabled_languages_defaults_to_python_only_with_empty_config():
    assert enabled_languages(configparser.ConfigParser()) == frozenset({Language.PYTHON})


def test_enabled_languages_defaults_to_python_only_with_collect_section_but_no_key():
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"enabled": "true"}  # a real key, just not "languages"
    assert enabled_languages(cfg) == frozenset({Language.PYTHON})


def test_enabled_languages_explicit_opt_in_includes_java():
    cfg = _config(languages="python,java")
    assert enabled_languages(cfg) == frozenset({Language.PYTHON, Language.JAVA})


def test_enabled_languages_is_whitespace_and_case_tolerant():
    cfg = _config(languages="  Python , JAVA  ")
    assert enabled_languages(cfg) == frozenset({Language.PYTHON, Language.JAVA})


def test_enabled_languages_java_only_excludes_python():
    cfg = _config(languages="java")
    assert enabled_languages(cfg) == frozenset({Language.JAVA})


def test_enabled_languages_malformed_value_falls_back_to_python_only():
    # A config typo must never silently turn off Python scanning too.
    cfg = _config(languages="  ,  ,")
    assert enabled_languages(cfg) == frozenset({Language.PYTHON})


# ── java_extensions_from_config ─────────────────────────────────────────


def test_java_extensions_defaults_to_dot_java():
    assert java_extensions_from_config(None) == frozenset({".java"})
    assert java_extensions_from_config(configparser.ConfigParser()) == frozenset({".java"})


def test_java_extensions_accepts_extension_without_leading_dot():
    cfg = _config(java_extensions="java")
    assert java_extensions_from_config(cfg) == frozenset({".java"})


def test_java_extensions_supports_multiple_extensions():
    cfg = _config(java_extensions=".java,.jav")
    assert java_extensions_from_config(cfg) == frozenset({".java", ".jav"})


# ── scan_repo: the actual opt-in gate, end to end ───────────────────────


def test_java_not_scanned_by_default_even_though_java_files_exist(tmp_path):
    _mixed_tree(tmp_path)
    modules = scan_repo(tmp_path)  # no config at all
    paths = {m.path for m in modules}
    assert "a.py" in paths
    assert "B.java" not in paths


def test_java_not_scanned_with_empty_config_either(tmp_path):
    _mixed_tree(tmp_path)
    modules = scan_repo(tmp_path, config=configparser.ConfigParser())
    paths = {m.path for m in modules}
    assert "a.py" in paths
    assert "B.java" not in paths


def test_explicit_opt_in_scans_java(tmp_path):
    if not is_available():
        pytest.skip("tree-sitter-java not installed")
    _mixed_tree(tmp_path)
    modules = scan_repo(tmp_path, config=_config(languages="python,java"))
    paths = {m.path for m in modules}
    assert "a.py" in paths
    assert "B.java" in paths


def test_java_only_config_excludes_python_too(tmp_path):
    if not is_available():
        pytest.skip("tree-sitter-java not installed")
    _mixed_tree(tmp_path)
    modules = scan_repo(tmp_path, config=_config(languages="java"))
    paths = {m.path for m in modules}
    assert "a.py" not in paths
    assert "B.java" in paths


def test_custom_java_extension_recognized_when_configured(tmp_path):
    if not is_available():
        pytest.skip("tree-sitter-java not installed")
    (tmp_path / "Weird.jav").write_text("public class Weird {}\n", encoding="utf-8")
    cfg = _config(languages="python,java", java_extensions=".jav")
    modules = scan_repo(tmp_path, config=cfg)
    paths = {m.path for m in modules}
    assert "Weird.jav" in paths
    assert {m.path: m for m in modules}["Weird.jav"].language == Language.JAVA


def test_custom_java_extension_not_recognized_when_not_configured(tmp_path):
    (tmp_path / "Weird.jav").write_text("public class Weird {}\n", encoding="utf-8")
    modules = scan_repo(tmp_path, config=_config(languages="python,java"))
    # default java_extensions is still just .java — .jav is unrecognized
    assert {m.path for m in modules} == set()


# ── real agents.ini: the actual shipped default ─────────────────────────


def test_real_agents_ini_defaults_to_python_only():
    import configparser as cp

    cfg = cp.ConfigParser()
    cfg.read(REPO_ROOT / "agents.ini")
    assert enabled_languages(cfg) == frozenset({Language.PYTHON})
    assert java_extensions_from_config(cfg) == frozenset({".java"})


def test_real_repo_scan_with_default_config_has_no_java_modules():
    # A real end-to-end backward-compat check: this repo *does* now
    # contain real .java files (tests/fixtures/collect_mini_repo_java/,
    # COLLECT-26) — scanning it with the actual shipped agents.ini must
    # still produce zero Java-language modules, since languages=python
    # is the default there. This is the genuine exclusion case, not a
    # vacuous one.
    import configparser as cp

    cfg = cp.ConfigParser()
    cfg.read(REPO_ROOT / "agents.ini")
    modules = scan_repo(REPO_ROOT, config=cfg)
    assert not any(m.language == Language.JAVA for m in modules)
    assert any(m.path.endswith(".py") for m in modules)  # sanity: scan still ran
