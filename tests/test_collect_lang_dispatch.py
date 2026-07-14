"""tests/test_collect_lang_dispatch.py — COLLECT-25.

`.py` -> python backend; `.java` -> java backend; unknown extension is
skipped (not fatal) by the scan_repo walk filter.

Both `scan_repo` calls below that exercise a `.java` file now pass an
explicit Java-enabling config — COLLECT-28 made Java scanning opt-in via
`[collect] languages`, so `scan_repo(tmp_path)` with no config at all is
Python-only by design (see `tests/test_collect_config_languages.py` for
that toggle's own dedicated tests); these two are about the *dispatch*
mechanics once Java scanning is turned on, not about the toggle itself.
"""

import configparser
from pathlib import Path

from tools.collect.lang import Language, detect_language, supported_extensions
from tools.collect.scanner import scan_repo


def _java_enabled_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"languages": "python,java"}
    return cfg


def test_detect_language_python():
    assert detect_language("tools/collect/model.py") == Language.PYTHON


def test_detect_language_java():
    assert detect_language("src/main/java/com/foo/Bar.java") == Language.JAVA


def test_detect_language_unknown_extension_returns_none():
    assert detect_language("README.md") is None
    assert detect_language("agents.ini") is None
    assert detect_language("no_extension") is None


def test_supported_extensions_contains_py_and_java():
    exts = supported_extensions()
    assert ".py" in exts
    assert ".java" in exts


def test_scan_repo_mixed_tree_skips_unknown_extensions(tmp_path: Path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "B.java").write_text(
        "public class B { void f() {} }\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# not code\n", encoding="utf-8")

    modules = scan_repo(tmp_path, config=_java_enabled_config())
    paths = {m.path for m in modules}

    assert "a.py" in paths
    assert "B.java" in paths
    assert not any(p.endswith(".md") for p in paths)


def test_scan_repo_records_language_field(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "B.java").write_text(
        "public class B {}\n", encoding="utf-8"
    )

    modules = {m.path: m for m in scan_repo(tmp_path, config=_java_enabled_config())}

    assert modules["a.py"].language == Language.PYTHON
    assert modules["B.java"].language == Language.JAVA
