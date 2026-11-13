"""AUDIT-9 follow-up (359): project search and the Context Broker find .cc .cxx .hh .hxx .mts .cts, and .mts/.cts imports are read."""

import pytest

from tools.auto import context_broker
from tools.auto.context_broker import ContextBroker
from tools.block_extractor import extract_block, extract_imports
from tools import file_reader
from tools.file_reader import list_source_files
from tools.search_agent import SearchAgent

SIX = (".cc", ".cxx", ".hh", ".hxx", ".mts", ".cts")
#: Suffixes that worked before, kept as controls so a regression is not hidden behind the new ones.
CONTROLS = (".cpp", ".hpp", ".ts", ".py", ".java")

_BODIES = {
    ".mts": "export function {name}(): number {{\n  return 41;\n}}\n",
    ".cts": "export function {name}(): number {{\n  return 41;\n}}\n",
    ".ts": "export function {name}(): number {{\n  return 41;\n}}\n",
    ".py": "def {name}():\n    return 41\n",
    ".java": "class C {{\n  int {name}() {{\n    return 41;\n  }}\n}}\n",
}
_CPP_BODY = "int {name}()\n{{\n    return 41;\n}}\n"


def _symbol(ext: str) -> str:
    return "lookup_" + ext.lstrip(".")


def _source(ext: str, name: str) -> str:
    return _BODIES.get(ext, _CPP_BODY).format(name=name)


def _write(root, ext: str, *, directory: str = "src"):
    """One file with the extension's unique symbol and a neighbour that must not be returned with it."""
    (root / directory).mkdir(exist_ok=True)
    path = root / directory / f"sample{ext}"
    path.write_text(_source(ext, _symbol(ext)) + "\n" + _source(ext, "neighbour_" + _symbol(ext)), encoding="utf-8")
    return path


def _block(ext: str) -> str:
    """The block the explicit extraction gives for the extension's symbol: what the other paths must return."""
    text = _source(ext, _symbol(ext)) + "\n" + _source(ext, "neighbour_" + _symbol(ext))
    block = extract_block(text, _symbol(ext), ext)
    assert block.strip() and "neighbour_" not in block, (ext, block)
    return block.strip()


@pytest.mark.parametrize("ext", SIX + CONTROLS)
def test_source_enumeration_lists_the_file(tmp_path, ext):
    path = _write(tmp_path, ext)
    assert str(path) in list_source_files(str(tmp_path), [])


@pytest.mark.parametrize("ext", SIX + CONTROLS)
def test_search_agent_returns_the_file_and_the_whole_block(tmp_path, ext):
    path = _write(tmp_path, ext)
    found = SearchAgent().run([_symbol(ext)], str(tmp_path))["found"]
    assert set(found) == {_symbol(ext)}
    assert found[_symbol(ext)]["file"] == str(path)
    assert found[_symbol(ext)]["code"].strip() == _block(ext)


@pytest.mark.parametrize("ext", SIX + CONTROLS)
def test_broker_fallback_scan_returns_the_whole_block(tmp_path, ext):
    _write(tmp_path, ext)
    resolved = ContextBroker().resolve([_symbol(ext)], [], tmp_path)
    assert set(resolved) == {_symbol(ext)}
    assert resolved[_symbol(ext)].strip() == _block(ext)


@pytest.mark.parametrize("ext", SIX + CONTROLS)
def test_broker_explicit_target_returns_the_whole_block(tmp_path, ext):
    path = _write(tmp_path, ext)
    resolved = ContextBroker().resolve([_symbol(ext)], [str(path.relative_to(tmp_path))], tmp_path)
    assert resolved[_symbol(ext)].strip() == _block(ext)


@pytest.mark.parametrize("ext", SIX)
def test_an_uppercase_suffix_is_enumerated_searched_and_resolved(tmp_path, ext):
    path = tmp_path / ("Sample" + ext.upper())
    path.write_text(_source(ext, _symbol(ext)), encoding="utf-8")
    assert str(path) in list_source_files(str(tmp_path), [])
    assert _symbol(ext) in SearchAgent().run([_symbol(ext)], str(tmp_path))["found"]
    assert _symbol(ext) in ContextBroker().resolve([_symbol(ext)], [], tmp_path)


def test_one_project_holding_every_suffix_is_searched_and_resolved_by_each_symbol(tmp_path):
    paths = {ext: _write(tmp_path, ext) for ext in SIX + CONTROLS}
    listed = set(list_source_files(str(tmp_path), []))
    assert {str(p) for p in paths.values()} <= listed
    symbols = [_symbol(ext) for ext in paths]
    found = SearchAgent().run(symbols, str(tmp_path))["found"]
    assert {s: found[s]["file"] for s in symbols} == {_symbol(ext): str(p) for ext, p in paths.items()}
    for ext in paths:  # one call resolves at most the broker's symbol cap, so one symbol per call
        assert ContextBroker().resolve([_symbol(ext)], [], tmp_path)[_symbol(ext)].strip() == _block(ext)


TS_IMPORTS = (
    'import { parse as parseInput, other } from "./dep";\n'
    'import type { Config } from "./types";\n'
    'import type { Original as Local } from "./renamed";\n'
    'import fallback from "./fallback";\n'
    'import * as everything from "./all";\n'
    'import { other } from "./again";\n'
    "export function lookup(): number { return parseInput(); }\n"
)


@pytest.mark.parametrize("ext", (".mts", ".cts", ".MTS", ".CTS"))
def test_mts_and_cts_read_the_same_ordered_bindings_as_ts(ext):
    expected = extract_imports(TS_IMPORTS, ".ts")
    assert expected == ["parseInput", "other", "Config", "Local", "fallback", "everything"]
    assert extract_imports(TS_IMPORTS, ext) == expected


@pytest.mark.parametrize("ext", (".mts", ".cts"))
def test_an_aliased_import_keeps_its_local_name(ext):
    source = ('import { parse as parseInput } from "./dep";\n'
              "export function lookup(): number { return parseInput(); }\n")
    assert extract_imports(source, ext) == ["parseInput"]


def test_ts_and_tsx_imports_are_unchanged():
    expected = ["parseInput", "other", "Config", "Local", "fallback", "everything"]
    assert extract_imports(TS_IMPORTS, ".ts") == expected
    assert extract_imports(TS_IMPORTS, ".tsx") == expected
    assert extract_imports("import java.util.List;\n", ".java") == ["List"]
    assert extract_imports("import os\nfrom a import b as c\n", ".py") == ["os", "c"]


def test_a_suffix_that_only_looks_like_a_code_suffix_is_not_enumerated(tmp_path):
    for name in ("a.cc.orig", "b.hxxx", "c.mtsx", "d.cmts", "e.cxx~", "f.hhh", "cc", "g.ccx"):
        (tmp_path / name).write_text("int zzz() { return 1; }\n", encoding="utf-8")
    assert list_source_files(str(tmp_path), []) == []


def test_a_skipped_directory_stays_excluded_for_every_suffix(tmp_path):
    for directory in ("node_modules", "gen"):
        for ext in SIX:
            _write(tmp_path, ext, directory=directory)
    keep = _write(tmp_path, ".cc", directory="keep")
    assert list_source_files(str(tmp_path), ["node_modules", "gen"]) == [str(keep)]
    # the default skipped directories of the search and the broker
    (tmp_path / "keep").rename(tmp_path / "moved")
    for directory in ("node_modules", ".git", "build"):
        (tmp_path / directory).mkdir(exist_ok=True)
        (tmp_path / directory / "x.cc").write_text(_source(".cc", "only_in_skipped"), encoding="utf-8")
    assert "only_in_skipped" not in SearchAgent().run(["only_in_skipped"], str(tmp_path))["found"]
    assert "only_in_skipped" not in ContextBroker().resolve(["only_in_skipped"], [], tmp_path)


@pytest.mark.parametrize("ext", (".cc", ".hxx", ".mts"))
def test_an_oversized_file_respects_the_search_agent_limit(tmp_path, ext):
    name = _symbol(ext)
    (tmp_path / f"big{ext}").write_text(_source(ext, name) + "// " + "x" * 3000 + "\n", encoding="utf-8")
    assert name in SearchAgent(max_file_kb=50).run([name], str(tmp_path))["found"]
    assert name not in SearchAgent(max_file_kb=1).run([name], str(tmp_path))["found"]


def test_the_explicit_target_wins_over_the_fallback_scan(tmp_path):
    (tmp_path / "a.cc").write_text("int dup() { return 1; }\n", encoding="utf-8")
    (tmp_path / "b.cc").write_text("int dup() { return 2; }\n", encoding="utf-8")
    assert ContextBroker().resolve(["dup"], ["b.cc"], tmp_path)["dup"].strip() == "int dup() { return 2; }"
    assert ContextBroker().resolve(["dup"], ["a.cc"], tmp_path)["dup"].strip() == "int dup() { return 1; }"


def test_the_broker_keeps_resolving_prose_in_md_and_txt(tmp_path):
    (tmp_path / "notes.md").write_text("# Alpha\n\nfirst\n\n# The Lantern\n\nthe lantern glows\n", encoding="utf-8")
    assert "the lantern glows" in ContextBroker().resolve(["The Lantern"], ["notes.md"], tmp_path)["The Lantern"]
    assert context_broker._PROSE_EXTS == frozenset({".md", ".txt"})


def test_the_broker_and_project_search_share_one_list_of_code_suffixes():
    code = set(context_broker._SEARCHABLE_EXTS) - set(context_broker._PROSE_EXTS)
    assert code == set(file_reader._SEARCHABLE_EXTS)
    assert set(SIX) <= code and set(CONTROLS) <= code
    assert set(context_broker._PROSE_EXTS) <= set(context_broker._SEARCHABLE_EXTS)
    assert not set(context_broker._PROSE_EXTS) & set(file_reader._SEARCHABLE_EXTS)
