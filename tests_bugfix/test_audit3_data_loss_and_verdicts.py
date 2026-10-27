"""tests_bugfix/test_audit3_data_loss_and_verdicts.py — AUDIT3 group A.

Silent data loss and wrong verdicts:

* AUDIT3-1  the Coder wrote a complete-file reply over a file it had shown only in part.
* AUDIT3-2  the test cache served one configuration's verdict to another (-c/-o/-W error/-p…).
* AUDIT3-3  the collect verifier dropped a located crash claim because the same text
            was GUARDED on another line.
* AUDIT3-4  the dataflow pass read `match` and `except*` as plain statements.
* AUDIT3-5  the policy read only the first `{` of a gate reply.
* AUDIT3-6  `main.py --once` exited 0 when the query could not run.
* AUDIT3-8  `collect --module` erased `unsummarized` / `profile_hashes` / `skipped_files`.
* AUDIT3-9  synopsis.md and the story bible were re-encoded as UTF-8 with U+FFFD, and CRLF lost.
* AUDIT3-10 story-bible dedup matched across word boundaries and deleted facts.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from types import SimpleNamespace

from tools.auto.coder import Coder
from tools.auto.summary_memory import SummaryMemory
from tools.collect import cli as collect_cli
from tools.collect.dataflow import extract_guarded_accesses
from tools.collect.model import GuardedAccess, ModuleRecord
from tools.collect.verifier import (
    REASON_CONTRADICTS_GUARD, Claim, contradiction_check,
)
from tools.contest.policy import _extract_verdict
from tools.contest.testcache import parse_pytest

REPO = Path(__file__).resolve().parents[1]


def _real_story_bible():
    """tools/auto/story_bible.py itself — another test module puts a stub under
    `tools.auto.story_bible` in sys.modules at collection time."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_audit3_story_bible", REPO / "tools" / "auto" / "story_bible.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── AUDIT3-1 ────────────────────────────────────────────────────────────────

def _coder(max_chars: int) -> Coder:
    coder = Coder.__new__(Coder)
    coder._max_file_chars = max_chars
    return coder


def _big_module() -> str:
    funcs = "\n\n".join(f"def f{i}(x):\n    return x + {i}  # {'pad ' * 10}" for i in range(12))
    return "import os\n\nCONST = 1\n\n" + funcs + "\n"


def test_a_reply_that_drops_unseen_symbols_is_refused(tmp_path):
    (tmp_path / "m.py").write_text(_big_module(), encoding="utf-8")
    coder = _coder(300)
    shown = coder._read_file_contents(["m.py"], tmp_path)
    assert "not included" in shown, "the file must have been shown in part"
    reply = "import os\n\ndef f0(x):\n    return x\n"
    err = coder._partial_view_error("m.py", reply, tmp_path / "m.py")
    assert err.startswith("[SAFETY]") and "f11" in err


def test_a_symbol_less_large_file_cannot_be_replaced_by_its_imports(tmp_path):
    body = "import os\n" + "".join(f"print({i})  # {'x' * 40}\n" for i in range(80))
    (tmp_path / "script.py").write_text(body, encoding="utf-8")
    coder = _coder(300)
    coder._read_file_contents(["script.py"], tmp_path)
    assert coder._partial_view_error("script.py", "import os\n", tmp_path / "script.py")


def test_a_placeholder_left_in_the_reply_is_refused(tmp_path):
    src = _big_module()
    (tmp_path / "m.py").write_text(src, encoding="utf-8")
    coder = _coder(300)
    coder._read_file_contents(["m.py"], tmp_path)
    reply = src + "\n# [f3 — 120 chars, not included]\n"
    assert "placeholder" in coder._partial_view_error("m.py", reply, tmp_path / "m.py")


def test_a_complete_edit_of_a_partially_shown_file_passes(tmp_path):
    src = _big_module()
    (tmp_path / "m.py").write_text(src, encoding="utf-8")
    coder = _coder(300)
    coder._read_file_contents(["m.py"], tmp_path)
    assert coder._partial_view_error("m.py", src.replace("x + 3", "x - 3"), tmp_path / "m.py") == ""


def test_a_file_shown_whole_is_not_guarded(tmp_path):
    (tmp_path / "s.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    coder = _coder(10_000)
    coder._read_file_contents(["s.py"], tmp_path)
    assert coder._partial_view_error("s.py", "x = 1\n", tmp_path / "s.py") == ""


# ── AUDIT3-2 ────────────────────────────────────────────────────────────────

def test_verdict_changing_options_are_part_of_the_cache_key():
    plain = parse_pytest("pytest tests")
    for cmd in ("pytest tests -c other.ini", "pytest tests --config-file other.ini",
                "pytest tests -o addopts=-x", "pytest tests -W error",
                "pytest tests -W error::DeprecationWarning", "pytest tests -p myplugin",
                "pytest tests --ignore tests/a.py", "pytest tests --deselect tests/a.py::t",
                "pytest tests --runxfail", "pytest tests --strict-markers"):
        assert parse_pytest(cmd) != plain, cmd
    assert parse_pytest("pytest tests -c a.ini") == parse_pytest("pytest tests --config-file a.ini")


def test_speed_only_options_still_share_the_entry():
    plain = parse_pytest("pytest tests")
    for cmd in ("pytest tests -n 4", "pytest tests -p no:cacheprovider",
                "pytest tests -W ignore", "pytest tests --color=no"):
        assert parse_pytest(cmd) == plain, cmd


# ── AUDIT3-3 ────────────────────────────────────────────────────────────────

def _module_with(*sites) -> ModuleRecord:
    return ModuleRecord(path="pkg/m.py", guarded_accesses=tuple(sites))


def test_a_located_crash_claim_is_judged_by_its_own_line():
    module = _module_with(
        GuardedAccess(location="pkg/m.py:6", access="data[0]", status="GUARDED", guard="g"),
        GuardedAccess(location="pkg/m.py:7", access="data[0]", status="UNGUARDED"),
    )
    claim = Claim(text="data[0] crashes", module="pkg/m.py", kind="access_crash",
                  access="data[0]", location="pkg/m.py:7")
    assert contradiction_check(claim, module, frozenset()) is None
    guarded = Claim(text="data[0] crashes", module="pkg/m.py", kind="access_crash",
                    access="data[0]", location="pkg/m.py:6")
    assert contradiction_check(guarded, module, frozenset())[0] == REASON_CONTRADICTS_GUARD


def test_an_unlocated_claim_keeps_the_collect17_rule():
    module = _module_with(
        GuardedAccess(location="pkg/m.py:6", access="data[0]", status="GUARDED", guard="g"),
        GuardedAccess(location="pkg/m.py:7", access="data[0]", status="UNGUARDED"),
    )
    claim = Claim(text="data[0] crashes", module="pkg/m.py", kind="access_crash",
                  access="data[0]")
    assert contradiction_check(claim, module, frozenset())[0] == REASON_CONTRADICTS_GUARD


# ── AUDIT3-4 ────────────────────────────────────────────────────────────────

def _statuses(src: str) -> dict:
    return {(a.location, a.access): a.status
            for a in extract_guarded_accesses(ast.parse(src), "m.py")}


def test_a_rebind_inside_a_case_drops_the_guard():
    src = ("def f(x, y):\n"
           "    if not x:\n"
           "        return\n"
           "    match y:\n"
           "        case 1:\n"
           "            x = []\n"
           "        case _:\n"
           "            pass\n"
           "    return x[-1]\n")
    assert _statuses(src)[("m.py:9", "x[-1]")] == "UNGUARDED"


def test_a_pattern_capture_rebinds_its_name():
    src = ("def f(x, y):\n"
           "    if not x:\n"
           "        return\n"
           "    match y:\n"
           "        case [*x]:\n"
           "            return x[0]\n"
           "    return x[0]\n")
    got = _statuses(src)
    assert got[("m.py:6", "x[0]")] == "UNGUARDED"
    assert got[("m.py:7", "x[0]")] == "UNGUARDED"


def test_a_guard_still_covers_an_untouched_case():
    src = ("def f(x, y):\n"
           "    if not x:\n"
           "        return\n"
           "    match y:\n"
           "        case 1:\n"
           "            return x[0]\n"
           "    return x[-1]\n")
    got = _statuses(src)
    assert got[("m.py:6", "x[0]")] == "GUARDED"
    assert got[("m.py:7", "x[-1]")] == "GUARDED"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="`except*` is a SyntaxError before Python 3.11")
def test_except_star_body_is_walked_like_try():
    src = ("def f(x):\n"
           "    if not x:\n"
           "        return\n"
           "    try:\n"
           "        pass\n"
           "    except* ValueError:\n"
           "        x = []\n"
           "    return x[0]\n")
    assert _statuses(src)[("m.py:8", "x[0]")] == "UNGUARDED"


# ── AUDIT3-5 ────────────────────────────────────────────────────────────────

def test_the_verdict_object_need_not_be_the_first_brace():
    assert _extract_verdict('I checked {the file} first.\n{"verdict":"allow","reason":"ok"}') \
        == ("allow", "ok")
    assert _extract_verdict('see {x}. {"verdict": "reject", "reason": "cut her') \
        == ("reject", "cut her … (cut)")
    assert _extract_verdict('{"verdict":"allow","reason":"a"}') == ("allow", "a")


# ── AUDIT3-6 ────────────────────────────────────────────────────────────────

def test_once_exits_1_when_the_query_cannot_run(tmp_path):
    r = subprocess.run([sys.executable, str(REPO / "main.py"), "--once",
                        "show nonexistent_file_xyz.py"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr


# ── AUDIT3-8 ────────────────────────────────────────────────────────────────

def test_module_patch_keeps_the_previous_manifest_fields():
    previous = SimpleNamespace(unsummarized=("a.py", "b.py"),
                               profile_hashes={"x.ini": "h"},
                               skipped_files=(("deep/z.py", "too deep"),))
    kept = collect_cli._carried_manifest_fields(previous, "a.py", SimpleNamespace(summary=None))
    assert kept == {"unsummarized": ("a.py", "b.py"), "profile_hashes": {"x.ini": "h"},
                    "skipped_files": (("deep/z.py", "too deep"),)}
    done = collect_cli._carried_manifest_fields(previous, "a.py", SimpleNamespace(summary="s"))
    assert done["unsummarized"] == ("b.py",)
    assert collect_cli._carried_manifest_fields(None, "a.py", SimpleNamespace(summary="s")) == {}


# ── AUDIT3-9 ────────────────────────────────────────────────────────────────

def _memory(path: Path) -> SummaryMemory:
    sm = SummaryMemory.__new__(SummaryMemory)
    sm._synopsis_path = path
    return sm


def test_a_cp1251_crlf_synopsis_keeps_its_codec_and_line_ends(tmp_path):
    path = tmp_path / "synopsis.md"
    old = ("<!-- BEGIN chapter_1.md -->\r\n## chapter_1.md\r\n• Борис нашёл письмо\r\n"
           "<!-- END chapter_1.md -->\r\n")
    path.write_bytes(old.encode("cp1251"))
    _memory(path)._write_section("chapter_2.md", "• Анна ушла")
    raw = path.read_bytes()
    assert b"\xef\xbf\xbd" not in raw
    text = raw.decode("cp1251")
    assert "Борис нашёл письмо" in text and "Анна ушла" in text
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_a_utf8_synopsis_with_bom_is_unchanged_in_kind(tmp_path):
    path = tmp_path / "synopsis.md"
    path.write_text("\ufeffintro\n", encoding="utf-8")
    _memory(path)._write_section("c.md", "• fact")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    assert "fact" in raw.decode("utf-8-sig")


def test_a_story_bible_in_cp1251_is_written_back_in_cp1251(tmp_path):
    story_bible = _real_story_bible()
    bible = story_bible.StoryBible.__new__(story_bible.StoryBible)
    bible._path = tmp_path / "story_bible.md"
    bible._path.write_bytes("• Борис — кузнец\r\n".encode("cp1251"))
    text = bible.load()
    bible._write(text + "\n• Анна — дочь Бориса")
    raw = bible._path.read_bytes()
    assert b"\xef\xbf\xbd" not in raw
    assert "Анна — дочь Бориса" in raw.decode("cp1251") and b"\r\n" in raw


# ── AUDIT3-10 ───────────────────────────────────────────────────────────────

def test_dedup_does_not_match_across_word_boundaries():
    story_bible = _real_story_bible()
    facts = ["Ann is old.", "Hannis Oldman was the mayor of the village."]
    assert story_bible._dedup_substrings(facts) == facts


def test_dedup_still_drops_a_real_contained_fact_and_spacing_variants():
    story_bible = _real_story_bible()
    assert story_bible._dedup_substrings(["Ann is old.", "Ann is old and tired."]) \
        == ["Ann is old and tired."]
    assert story_bible._dedup_substrings(["Ann, is old", "Ann is old"]) == ["Ann, is old"]
