"""REP reports: prosody keywords, chapter subfolders, Gate 1 JSON, existence refs, FAQ, clusters, SUMMARY cells, limits, Retry-After, prompt paths."""
import configparser
import email.message
import logging
import urllib.error
from pathlib import Path

import pytest

from tools.auto.canon_validator import CanonValidator
from tools.auto.continuity_validator import find_previous_chapter_text
from tools.auto.existence_validator import ExistenceValidator
from tools.auto.gate1_filter import Gate1Filter
from tools.auto.prosody import is_verse_task, verse_requirements
from tools.auto.repo_ingest import RepoIngestor
from tools.auto.summary_memory import _chunk_paragraphs
from tools.collect.scanner import collect_walk_limits
from tools.contest.export import _cell
from tools.faq_agent import FaqAgent
from tools.llm_stream import _parse_retry_after
from tools.prompt_parser import parse_prompt


# REP-PROSODY (rep2 #57)
@pytest.mark.parametrize("task", [
    "Виктор объясняет Даниле алгоритм сортировки", "урок арифметики", "логарифм", "аритмия сердца",
])
def test_words_that_merely_contain_ritm_or_rifm_are_prose(task):
    assert not is_verse_task(task)
    assert verse_requirements(task) == (False, False)


@pytest.mark.parametrize("task,req", [
    ("Ритмичные стихи", (False, True)), ("Зарифмуй это", (True, False)), ("срифмуй", (True, False)),
    ("с рифмой", (True, False)), ("стихи с ритмом и рифмой", (True, True)), ("стихи с ритмом, без рифмы", (False, True)),
])
def test_verse_keywords_still_open_the_gate(task, req):
    assert is_verse_task(task)
    assert verse_requirements(task) == req


# REP-CHAPTER-DIR (rep2 #58)
def test_previous_and_earlier_chapters_are_read_from_the_chapters_folder(tmp_path):
    (tmp_path / "chapters").mkdir()
    for i in (1, 2, 3):
        (tmp_path / "chapters" / f"chapter_0{i}.md").write_text(f"text of ch{i}", encoding="utf-8")
    assert find_previous_chapter_text("chapters/chapter_03.md", tmp_path) == "text of ch2"
    assert CanonValidator._earlier_chapters("chapters/chapter_03.md", tmp_path) == [
        "chapters/chapter_01.md", "chapters/chapter_02.md"]
    (tmp_path / "chapter_02.md").write_text("root ch2", encoding="utf-8")
    assert find_previous_chapter_text("chapter_03.md", tmp_path) == "root ch2"
    assert CanonValidator._earlier_chapters("chapter_03.md", tmp_path) == ["chapter_02.md"]


# REP-GATE1-JSON (rep4)
_JS = '{"verdict":"confirmed","reason":"div by zero","evidence":"return x / 0"}'


@pytest.mark.parametrize("reply", [
    "```json " + _JS + "```", "```" + _JS + "```", "Here is my verdict:\n```json\n" + _JS + "\n```",
    "Verdict: " + _JS, _JS + "\nExplanation: it divides.", _JS.replace('"confirmed"', '"Confirmed."'),
])
def test_presence_verdict_with_prose_or_inline_fence_is_read(reply):
    g = Gate1Filter.__new__(Gate1Filter)
    assert g._parse_presence_response(reply, "cand", "def f(x):\n    return x / 0\n") == (True, "div by zero", False)


def test_presence_reply_with_no_object_stays_unparseable():
    g = Gate1Filter.__new__(Gate1Filter)
    ok, _reason, unparseable = g._parse_presence_response("I think {it is} confirmed", "cand", "x")
    assert (ok, unparseable) == (False, True)


# REP-EXISTENCE (rep2 #56)
@pytest.mark.parametrize("text", [
    "Run `pytest tests/test_*.py`", "```\ngit add *.md\n```", "`python -m unittest discover -p test_*.py`",
    "Edit `/etc/myapp/config.yaml`", "Edit `~/.myapp/settings.json`", "Edit `$HOME/.myapp/config.ini`",
    "Edit `C:\\Users\\me\\myapp.ini`",
])
def test_wildcards_and_system_paths_are_not_missing_files(tmp_path, text):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_x(): pass\n")
    assert ExistenceValidator().check(text, tmp_path).approved


def test_a_missing_repo_file_is_still_missing(tmp_path):
    (tmp_path / "docs").mkdir()
    v = ExistenceValidator().check("See `docs/guide.md` and `/docs/usage.md`.", tmp_path)
    assert not v.approved and v.missing == ["docs/guide.md", "docs/usage.md"]


# REP-FAQ-EXT (rep4)
@pytest.mark.parametrize("exts", [[".txt", ".md"], ["md", "txt"], ["*.md", "*.TXT"]])
def test_faq_knowledge_extensions_are_case_blind(tmp_path, exts):
    for n in ("a.md", "B.MD", "C.TXT", "e.txt", "skip.py"):
        (tmp_path / n).write_text("x", encoding="utf-8")
    agent = FaqAgent.__new__(FaqAgent)
    agent.knowledge_dir = tmp_path
    agent.extensions = exts
    assert sorted(Path(p).name for p, _ in agent._load_knowledge()) == ["B.MD", "C.TXT", "a.md", "e.txt"]


# REP-CLUSTER-GLOB (rep4)
@pytest.mark.parametrize("pat,path,hit", [
    ("src/**/*.py", "src/a.py", True), ("src/**/*.py", "src/x/y/c.py", True), ("tests/", "tests/a.py", True),
    ("/src/*", "src/a.py", True), ("src/**/*.py", "lib/a.py", False), ("tests/", "src/tests.py", False),
])
def test_cluster_patterns(pat, path, hit):
    assert RepoIngestor._matches_any(path, [pat]) is hit


# REP-CELL-CR (rep4)
def test_summary_cell_has_no_carriage_return():
    assert _cell("line1\r\nline2\rline3 | x") == "line1 line2 line3 \\| x"


# REP-CHUNK-BLANK (rep2 #54)
def test_blank_lines_with_spaces_split_paragraphs():
    text = "\n   \n".join(["p" * 300] * 10)
    chunks = _chunk_paragraphs(text, 1000)
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)


# REP-COLLECT-LIMIT (rep4)
def test_non_positive_collect_limit_is_warned(caplog):
    cfg = configparser.ConfigParser()
    cfg.read_string("[collect]\nmax_depth = 0\nmax_file_kb = -1\n")
    with caplog.at_level(logging.WARNING):
        assert collect_walk_limits(cfg) == (6, 4096)
    assert "max_depth = 0" in caplog.text and "max_file_kb = -1" in caplog.text


# REP-RETRY-INF (rep3)
@pytest.mark.parametrize("value,expected", [("inf", None), ("1e400", None), ("nan", None), ("30", 30.0)])
def test_retry_after_must_be_finite(value, expected):
    m = email.message.Message()
    m["Retry-After"] = value
    assert _parse_retry_after(urllib.error.HTTPError("http://x", 429, "x", m, None), "") == expected


# REP-PROMPT-PATH / REP-PROMPT-DOTTED (rep4)
_SRC = "class Loader:\n    def load(self):\n        pass\n\ndef parse_data(x):\n    return x\n"


@pytest.mark.parametrize("prompt,want", [
    ("show Loader.load in a.py", ("Loader.load", "a.py", "show")),
    ("improve Loader.load in a.py", ("Loader.load", "a.py", "improve")),
    ("improve `parse_data` in `a.py`", ("parse_data", "a.py", "improve")),
    ("show parse_data in ~/proj/a.py", ("parse_data", "~/proj/a.py", "show")),
    ("explain parse_data in C:\\proj\\a.py", ("parse_data", "C:\\proj\\a.py", "explain")),
    ("explain parse_data in 'my file.py'", ("parse_data", "my file.py", "explain")),
    ("show parse_data in src/a.py.", ("parse_data", "src/a.py", "show")),
])
def test_prompt_paths_and_dotted_targets(prompt, want):
    r = parse_prompt(prompt, source=_SRC)
    assert (r.target_name, r.file_path, r.intent) == want
