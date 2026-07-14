"""tests/test_collect_ast_symbols.py — COLLECT-4.

* All public symbols and imports are extracted on `collect_mini_repo`.
* Private symbols (`_x`) are marked `is_private=True`.
* A syntactically broken file doesn't crash the scan — it's recorded as
  `parse_error`, not raised as an exception out of the scanner.
* AC: scanning this repo covers every discovered `*.py` module with zero
  parse failures.
"""

from pathlib import Path

from tools.collect.scanner import scan_module, scan_repo

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"
REPO_ROOT = Path(__file__).parent.parent


def test_scan_repo_covers_every_fixture_module():
    modules = scan_repo(FIXTURE_ROOT)
    paths = {m.path for m in modules}
    assert paths == {
        "pkg/__init__.py",
        "pkg/config_reader.py",
        "pkg/error_handling.py",
        "pkg/prompt_store.py",
        "pkg/unguarded.py",
        "pkg/view_trace.py",
    }


def test_scan_repo_extracts_known_symbols_in_stable_order():
    modules = {m.path: m for m in scan_repo(FIXTURE_ROOT)}
    error_module = modules["pkg/error_handling.py"]
    names = [s.qualname.split(":")[-1] for s in error_module.public_symbols]
    assert names == ["read_optional", "read_with_log", "read_strict", "scan_all"]


def test_scan_repo_extracts_imports():
    modules = {m.path: m for m in scan_repo(FIXTURE_ROOT)}
    assert modules["pkg/error_handling.py"].imports == ("logging",)
    assert modules["pkg/unguarded.py"].imports == ()


def test_scan_module_marks_private_symbols():
    source = "def public_fn():\n    pass\n\n\ndef _private_fn():\n    pass\n"
    record = scan_module(source, "pkg/mixed.py")
    by_name = {s.qualname.split(":")[-1]: s for s in record.public_symbols}
    assert by_name["public_fn"].is_private is False
    assert by_name["_private_fn"].is_private is True


def test_scan_module_records_classes_too():
    source = "class Foo:\n    pass\n"
    record = scan_module(source, "pkg/cls.py")
    assert [s.qualname for s in record.public_symbols] == ["pkg/cls.py:Foo"]


def test_scan_module_on_broken_syntax_records_parse_error_not_exception():
    broken_source = "def broken(:\n    pass\n"
    record = scan_module(broken_source, "pkg/broken.py")
    assert record.parse_error is not None
    assert record.public_symbols == ()
    assert record.imports == ()


def test_scan_repo_skips_no_module_on_one_broken_file(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "good.py").write_text("def ok():\n    pass\n", encoding="utf-8")
    (pkg / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    modules = scan_repo(tmp_path)
    by_path = {m.path: m for m in modules}
    assert set(by_path) == {"pkg/__init__.py", "pkg/good.py", "pkg/bad.py"}
    assert by_path["pkg/bad.py"].parse_error is not None
    assert by_path["pkg/good.py"].parse_error is None
    assert [s.qualname for s in by_path["pkg/good.py"].public_symbols] == ["pkg/good.py:ok"]


def test_scan_repo_skips_no_module_on_one_unreadable_file(tmp_path):
    """BUGFIX regression: a file that raises on *read* (not on parse) —
    here, one that isn't valid UTF-8 — used to propagate a
    `UnicodeDecodeError` straight out of `scan_repo`, aborting the whole
    scan and losing every already-collected module with it. It must
    instead degrade to a recorded `parse_error`, the same "one broken
    file can't take down the pass" contract a `SyntaxError` already gets.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "good.py").write_text("def ok():\n    pass\n", encoding="utf-8")
    (pkg / "bad_encoding.py").write_bytes(b"x = 1\n# not valid utf-8: \xff\xfe\n")

    modules = scan_repo(tmp_path)
    by_path = {m.path: m for m in modules}
    assert set(by_path) == {"pkg/good.py", "pkg/bad_encoding.py"}
    assert by_path["pkg/bad_encoding.py"].parse_error is not None
    assert by_path["pkg/good.py"].parse_error is None
    assert [s.qualname for s in by_path["pkg/good.py"].public_symbols] == ["pkg/good.py:ok"]


def test_scan_repo_covers_this_repo_without_crashing():
    modules = scan_repo(REPO_ROOT)
    assert len(modules) >= 51
    errors = [m.path for m in modules if m.parse_error]
    assert errors == [], f"unexpected parse errors: {errors}"
    assert any(m.path == "main.py" for m in modules)
