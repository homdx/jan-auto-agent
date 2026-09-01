"""tests/test_collect_scan_file_java_extensions.py — regression test.

BUGFIX: `scan_file` (the single-file dispatcher `cli.action_module`'s
`--module <path>` routes through) used to check only the fixed `.java`
suffix via `detect_language`, ignoring `[collect] java_extensions`
entirely — even though `scan_repo`'s own internal `_language()` helper
already honored that config key (COLLECT-28). A repo configured with a
non-default Java extension (e.g. `.jav`) would get a *correct*
`language="java"` record from a full `--collect`/`--refresh` scan, and
then have that record silently overwritten with a bogus
`language="python"`/`parse_error` one the moment `--module <path>` was
used to patch just that file — the exact "valid Java recorded as broken
Python" misclassification `scan_file`'s own docstring says it exists to
prevent, just missed for the custom-extension case.
"""

import configparser

import pytest

from tools.collect.java_parser import is_available
from tools.collect.lang import Language
from tools.collect.scanner import scan_file, scan_repo


def _config(java_extensions: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"languages": "python,java", "java_extensions": java_extensions}
    return cfg


VALID_JAVA_SRC = "public class Weird {\n    void bar() {}\n}\n"


def test_scan_file_honors_custom_java_extension_when_config_passed():
    if not is_available():
        pytest.skip("tree-sitter-java not installed")
    cfg = _config(".jav")
    record = scan_file(VALID_JAVA_SRC, "Weird.jav", config=cfg)
    assert record.language == Language.JAVA
    assert record.parse_error is None


def test_scan_file_without_config_falls_back_to_default_java_only():
    # No config at all: only the built-in `.java` extension is Java-eligible,
    # so `.jav` still falls through to the Python path — same "last resort
    # default" contract the function has always documented for an
    # unrecognized extension.
    record = scan_file(VALID_JAVA_SRC, "Weird.jav")
    assert record.language == Language.PYTHON
    assert record.parse_error is not None


def test_scan_file_agrees_with_scan_repo_for_custom_extension(tmp_path):
    """The bug, stated as an invariant: scan_file(config=...) must never
    disagree with what a full scan_repo(config=...) pass over the same
    tree/config would have recorded for the same file.
    """
    if not is_available():
        pytest.skip("tree-sitter-java not installed")
    (tmp_path / "Weird.jav").write_text(VALID_JAVA_SRC, encoding="utf-8")
    cfg = _config(".jav")

    via_scan_repo = {m.path: m for m in scan_repo(tmp_path, config=cfg)}["Weird.jav"]
    via_scan_file = scan_file(VALID_JAVA_SRC, "Weird.jav", config=cfg)

    assert via_scan_file.language == via_scan_repo.language == Language.JAVA
    assert via_scan_file.parse_error == via_scan_repo.parse_error is None
