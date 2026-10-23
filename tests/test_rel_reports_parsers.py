"""REL reports: BOM CSVs, new_file spellings, prose-wrapped candidates, cited-symbol spellings, bible punctuation, synopsis codecs, dataflow rebinds, skipped --module, Java enum constructors, partial-view splice."""
import ast
import configparser
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.auto.architect import CitedLocation, ClusterReviewer, _to_bool_or
from tools.auto.coder import Coder
from tools.auto.context_assembler import _SECTION_RE as ASSEMBLER_SECTION_RE
from tools.auto.story_bible import _dedup_substrings
from tools.auto.summary_memory import SummaryMemory, _SECTION_RE
from tools.collect.cli import CollectCliError, _normalize_module_path
from tools.collect.dataflow import extract_guarded_accesses
from tools.collect.java_parser import is_available, parse_java

REPO = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# REL-2: a spreadsheet's BOM in PROGRESS.csv
def test_bom_progress_csv_is_read_and_deduplicated(tmp_path):
    progress = tmp_path / "PROGRESS.csv"
    progress.write_bytes(b"\xef\xbb\xbfticket,finding,outcome,commit,note\r\n01-a.md,,ALREADY-OK,,\r\n")
    assert _load_script("next_task").recorded(str(progress)) == {"01-a.md": "ALREADY-OK"}
    run = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "append_task.py"), "--progress", str(progress),
         "--ticket", "01-a.md", "--outcome", "ALREADY-OK"],
        capture_output=True, text=True, cwd=str(REPO))
    assert run.returncode == 2, run.stdout + run.stderr
    assert progress.read_bytes().count(b"01-a.md") == 1


# REL-2: new_file is a whitelist
@pytest.mark.parametrize("val,want", [
    ("true", True), ("YES", True), ("1", True), (True, True),
    ("false", False), ("none", False), ("null", False), ("n/a", False), ("off", False),
    ("absent", False), ("disabled", False), (None, False),
])
def test_new_file_spellings(val, want):
    assert _to_bool_or(val) is want


# REL-2: a complete array behind prose is not "truncated"
def _reviewer():
    cfg = configparser.ConfigParser()
    cfg.read_dict({"architect": {"temperature": "0.2", "max_tokens": "512"}, "loop": {"timeout_seconds": "10"}})
    return ClusterReviewer(config=cfg, base_url="http://x/v1", api_key="t", model="m",
                           api_format="openai", verify_ssl=False)


def _item(title):
    return {"title": title, "instruction": "Validate.", "target_files": ["a.py"], "acceptance_check": "pytest",
            "cited_location": {"file": "a.py", "symbol": "f", "line_start": 1, "line_end": 2}}


_ARR = json.dumps([_item("one"), _item("two")])


@pytest.mark.parametrize("reply,n", [
    (_ARR, 2), ("```json " + _ARR + "```", 2), ("Here are the tasks: " + _ARR, 2),
    (_ARR + "\nLet me know if you need more.", 2), ("Sure!\n```json\n" + _ARR + "\n```", 2),
    (json.dumps({"tasks": [_item("one"), _item("two")]}), 2), (json.dumps(_item("one")), 1),
    ("Nothing to do: []", 0),
])
def test_complete_candidates_are_not_truncated(reply, n):
    cands, truncated, unsalvageable = _reviewer()._parse_candidates_ex(reply, "c")
    assert (len(cands), truncated, unsalvageable) == (n, False, False)


def test_a_cut_off_array_is_still_truncated_and_a_non_task_object_still_retried():
    assert _reviewer()._parse_candidates_ex(_ARR[:-40], "c")[1:] == (True, False)
    assert _reviewer()._parse_candidates_ex(json.dumps({"title": "x"}), "c") == ([], False, True)


# REL-2: the spellings a model cites a symbol in
@pytest.mark.parametrize("raw,want", [
    ("f()", "f"), ("f(x)", "f"), ("A.m(self)", "A.m"), ("`f`", "f"), ("'f'", "f"), ("def f", "f"),
    ("async def f", "f"), ("def f(x, y)", "f"), ("class A", "A"), ("A::m", "A.m"), ("A#m", "A.m"),
    ("Install (Linux)", "Install (Linux)"), ("Chapter 1", "Chapter 1"), ("f", "f"),
])
def test_cited_symbol_spellings(raw, want):
    assert CitedLocation("a.py", raw).symbol == want


# REL-2: Unicode punctuation is a word boundary in the bible dedupe
@pytest.mark.parametrize("longer", [
    "«Анна» живёт в Москве, у неё есть кот", "Анна живёт в Москве… и у неё есть кот",
    "Говорят: Анна живёт в Москве—и это правда", "“Анна живёт в Москве”, сказал он",
])
def test_bible_sub_phrase_behind_unicode_punctuation_is_dropped(longer):
    assert _dedup_substrings(["Анна живёт в Москве", longer]) == [longer]


def test_bible_word_boundary_still_holds():
    assert len(_dedup_substrings(["Ann is old.", "Hannis Oldman was the mayor"])) == 2


# REL-2 / REL-6 #53: synopsis codecs and spaced section names
def _memory(tmp_path):
    return SummaryMemory(lambda *a, **k: "", base_dir=tmp_path)


def test_unencodable_section_rewrites_a_legacy_synopsis_as_utf8(tmp_path):
    (tmp_path / "synopsis.md").write_bytes("# Synopsis\r\nПривет\r\n".encode("cp1251"))
    _memory(tmp_path)._write_section("ch1.md", "Он пришёл → в лес − 😀")
    raw = (tmp_path / "synopsis.md").read_bytes()
    text = raw.decode("utf-8")
    assert "Привет" in text and "😀" in text and b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_utf16_synopsis_stays_utf16(tmp_path):
    (tmp_path / "synopsis.md").write_bytes("# Synopsis\nПривет\n".encode("utf-16"))
    _memory(tmp_path)._write_section("ch1.md", "Он пришёл.")
    text = (tmp_path / "synopsis.md").read_bytes().decode("utf-16")
    assert "Привет" in text and "Он пришёл." in text


def test_section_name_with_a_space_is_read_back(tmp_path):
    mem = _memory(tmp_path)
    mem._write_section("My Novel/chapter_01.md", "x")
    mem._write_section("My Novel/chapter_02.md", "y")
    text = (tmp_path / "synopsis.md").read_text(encoding="utf-8")
    want = ["My Novel/chapter_01.md", "My Novel/chapter_02.md"]
    assert [m.group("name") for m in _SECTION_RE.finditer(text)] == want
    assert [m.group("name") for m in ASSEMBLER_SECTION_RE.finditer(text)] == want


# REL-2: every rebind or emptying after a guard drops it
def _status(shape: str) -> str:
    src = f"def f(items, p=None):\n    if not items:\n        return 0\n    {shape}\n    return items[0]\n"
    return [a.status for a in extract_guarded_accesses(ast.parse(src), "m.py") if a.access.startswith("items[0]")][-1]


@pytest.mark.parametrize("shape", [
    "items: list = []", "items, other = [], 1", "a, *items = [1]", "items[:] = []", "items *= 0",
    "with open(p) as items:\n        pass", "try:\n        pass\n    except Exception as items:\n        pass",
    "(items := [])", "if (items := []):\n        pass", "for z in (items := []):\n        pass",
    "print((items := []))", "import os as items", "def items():\n        pass", "class items:\n        pass",
    "x = [y for y in (items := [])]",
])
def test_a_rebind_drops_the_guard(shape):
    assert _status(shape) == "UNGUARDED"


@pytest.mark.parametrize("shape", [
    "items[0] = 5", "pass", "x = [items for items in range(3)]", "f = lambda items: items",
])
def test_what_does_not_rebind_keeps_the_guard(shape):
    assert _status(shape) == "GUARDED"


# REL-2: --module refuses a skipped directory
@pytest.mark.parametrize("path", ["node_modules/n.py", "out/b.py", ".hidden/h.py"])
def test_module_under_a_skipped_directory_is_refused(path):
    cfg = configparser.ConfigParser()
    cfg.read_string("[collect]\nskip_dirs = out\n")
    with pytest.raises(CollectCliError):
        _normalize_module_path(Path("."), path, cfg)
    assert _normalize_module_path(Path("."), "src/out_x/a.py", cfg) == "src/out_x/a.py"


# REL-1 #4: an enum constructor is private
@pytest.mark.skipif(not is_available(), reason="tree-sitter-java not installed")
def test_enum_constructor_is_private():
    from tools.collect.java_facts import extract_java_symbols
    tree = parse_java("enum Color { RED; Color() {} void m() {} class C { C() {} } }", "T.java").tree
    access = {s.qualname: s.access_modifier for s in extract_java_symbols(tree, "T.java")}
    assert access["T.java:Color.Color"] == "private"
    assert access["T.java:Color.m"] == "package-private"
    assert access["T.java:Color.C.C"] == "package-private"


# REL-6 #59/#60: a Python file shown in part is spliced, not replaced
_FUNCS = "".join(f"def f{i}(x):\n    return x + {i}  # {'pad ' * 10}\n\n\n" for i in range(12))
_SRC = ("import os\n\nTIMEOUT = 30\n\n\n" + _FUNCS
        + "@decorator\ndef dead_helper(a, b):\n    return a - b\n\n\nif __name__ == '__main__':\n    f1(2)\n")
_F1 = "def f1(x):\n    return x + 1  # " + "pad " * 10 + "\n"


def _view(tmp_path):
    (tmp_path / "m.py").write_text(_SRC, encoding="utf-8")
    cfg = configparser.ConfigParser()
    cfg.read_dict({"api": {"active": "local", "verify_ssl": "false"},
                   "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test", "model": "m",
                                 "api_format": "openai"},
                   "loop": {"timeout_seconds": "300"}, "coder": {"temperature": "0.2", "max_tokens": "4096"}})
    coder = Coder(config=cfg, base_url="http://localhost:1337/v1", api_key="test", model="m")
    coder._max_file_chars = 300
    view = coder._read_file_contents(["m.py"], tmp_path, cited_symbol="f5")
    assert "not included" in view
    return coder, view.split("### m.py\n", 1)[1]


_EDITED = _SRC.replace("return x + 5", "return x + 500")


@pytest.mark.parametrize("make,want", [
    (lambda body: body.replace("return x + 5", "return x + 500"), _EDITED),
    (lambda body: re.sub(r"(?m)^# \[.*not included\]\n?", "", body).replace("return x + 5", "return x + 500"), _EDITED),
    (lambda body: body.replace(_F1, ""), _SRC.replace(_F1 + "\n\n", "")),
    (lambda body: body + "\n\n\ndef g():\n    return 1\n",
     _SRC.replace("\n\nif __name__", "\n\ndef g():\n    return 1\n\n\nif __name__")),
    (lambda body: "import sys\n" + body, "import sys\n" + _SRC),
    (lambda body: _EDITED, _EDITED),
])
def test_partial_view_reply_is_spliced(tmp_path, make, want):
    coder, body = _view(tmp_path)
    out = coder._splice_partial_reply("m.py", make(body))
    assert out == want
    assert coder._partial_view_error("m.py", out, tmp_path / "m.py") == ""


def test_partial_view_reply_is_written_spliced(tmp_path):
    coder, body = _view(tmp_path)
    written, err = coder._write_files(
        [{"path": "m.py", "content": body.replace("return x + 5", "return x + 500")}],
        tmp_path, "T", allowed_paths=frozenset({"m.py"}))
    assert err == "" and written == ["m.py"]
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == _EDITED
