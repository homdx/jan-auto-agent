"""tests/test_collect_java_imports.py — COLLECT-26.

Plain and wildcard imports extracted; static imports don't crash the
extractor (present in the output, not silently dropped).

The two real-fixture tests pass an explicit Java-enabling config, since
Java scanning is opt-in as of COLLECT-28.
"""

import configparser
from pathlib import Path

import pytest

from tools.collect.java_facts import extract_java_imports
from tools.collect.java_parser import is_available, parse_java
from tools.collect.scanner import scan_repo

pytestmark = pytest.mark.skipif(not is_available(), reason="tree-sitter-java not installed")

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo_java"


def _java_enabled_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"languages": "python,java"}
    return cfg


def _imports(source: str) -> list:
    result = parse_java(source, "Test.java")
    assert result.error is None and not result.has_error
    return extract_java_imports(result.tree)


# ── the three import forms, individually ────────────────────────────────


def test_plain_import_extracted_as_dotted_name():
    assert _imports("import java.util.List;\nclass X {}\n") == ["java.util.List"]


def test_wildcard_import_extracted_with_trailing_asterisk():
    assert _imports("import java.util.*;\nclass X {}\n") == ["java.util.*"]


def test_static_import_extracted_not_dropped():
    src = "import static java.lang.Math.max;\nclass X {}\n"
    imports = _imports(src)
    assert imports == ["java.lang.Math.max"]


def test_static_wildcard_import_extracted():
    src = "import static java.util.Map.*;\nclass X {}\n"
    assert _imports(src) == ["java.util.Map.*"]


def test_single_segment_import_from_default_package():
    # Unusual but legal: importing a class with no package at all.
    assert _imports("import Foo;\nclass X {}\n") == ["Foo"]


# ── multiple imports: sorted, deduplicated ──────────────────────────────


def test_multiple_imports_sorted_and_deduplicated():
    src = (
        "import java.util.List;\n"
        "import java.io.File;\n"
        "import java.util.List;\n"  # duplicate on purpose
        "class X {}\n"
    )
    assert _imports(src) == ["java.io.File", "java.util.List"]


def test_no_imports_yields_empty_list():
    assert _imports("class X {}\n") == []


# ── real fixture: all three forms together ──────────────────────────────


def test_real_greeter_fixture_has_all_three_import_forms():
    modules = {m.path: m for m in scan_repo(FIXTURE_ROOT, config=_java_enabled_config())}
    imports = modules["com/example/Greeter.java"].imports
    assert imports == ("java.lang.Math.max", "java.util.*", "java.util.List")


def test_files_with_no_imports_have_empty_imports_tuple():
    modules = {m.path: m for m in scan_repo(FIXTURE_ROOT, config=_java_enabled_config())}
    assert modules["com/example/Point.java"].imports == ()
    assert modules["com/example/Color.java"].imports == ()
