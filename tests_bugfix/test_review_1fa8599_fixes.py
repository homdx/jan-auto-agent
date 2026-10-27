"""Outside review of 1fa8599: Kotlin heads, every section copy, mirrors, pytest 9 configs, claim continuation, non-UTF-8 writes."""
import os
import sys
from pathlib import Path

import pytest

from tools.auto import plan_validator as pv
from tools.auto.coder import Coder
from tools.auto.executor import pytest_config_requests_pool
from tools.block_extractor import extract_block
from tools.claimcheck.anchors import PathRepoView, resolve_claim

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import judge_epic_round as jer  # noqa: E402


@pytest.mark.parametrize("src,name", [
    ("fun <T : Comparable<T>> max(xs: List<T>): T {\n  return xs[0]\n}\n", "max"),
    ("internal suspend inline fun <reified T : Any> load(f: (Int) -> T): T {\n  return f(1)\n}\n", "load"),
    ("fun String.shout(): String {\n  return uppercase()\n}\n", "shout"),
    ("fun <T> List<T>.second(): T = this[1]\n", "second"),
])
def test_kotlin_fun_heads_are_found(src, name):
    assert name in extract_block(src, name, ".kt")


def test_a_kotlin_call_is_not_a_definition():
    assert extract_block("val y = max(xs)\n", "max", ".kt") == ""


def test_every_copy_of_a_section_is_removed():
    text = "# I\n\n### T1: a\nbody\n\n---\n\n### T2: b\nx\n\n---\n\n### T1: a\nbody\n\n---\n\n"
    out = pv._without_section(text, "T1")
    assert "### T1" not in out and "### T2: b" in out


def test_a_path_cited_through_a_mirror_symlink_resolves(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.py").write_text("def f():\n    pass\n")
    (tmp_path / ".smoke").mkdir()
    os.symlink("../tests/x.py", tmp_path / ".smoke" / "x.py")
    r = resolve_claim("`.smoke/x.py` defines f", PathRepoView(tmp_path))
    assert [(a.found, a.path) for a in r if a.path.endswith("x.py")] == [(True, "tests/x.py")]


@pytest.mark.parametrize("name,body", [
    (".pytest.ini", "[pytest]\naddopts = -n 2\n"),
    ("pytest.toml", "[pytest]\naddopts = [\"-n\", \"2\"]\n"),
    (".pytest.toml", "[pytest]\naddopts = \"-n 2\"\n"),
])
def test_pytest_9_config_files_are_read(tmp_path, monkeypatch, name, body):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    (tmp_path / name).write_text(body)
    assert pytest_config_requests_pool(tmp_path)


def test_pytest_toml_wins_over_pytest_ini(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    (tmp_path / "pytest.toml").write_text("[pytest]\naddopts = []\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -n 2\n")
    assert not pytest_config_requests_pool(tmp_path)


def test_only_an_indented_line_continues_a_claim():
    claims = jer.ticket_claims("## Claims\n\n- [still] parse_row splits\n  on commas\nNote: checked at 1a2b3c4.\n")
    assert claims == [{"claim": "parse_row splits on commas", "expect": "still"}]


def test_a_file_that_is_not_utf8_is_never_rewritten(tmp_path):
    f = tmp_path / "m.py"
    original = b"# -*- coding: latin-1 -*-\ns = 'caf\xe9'\nx = 1\n"
    f.write_bytes(original)
    c = Coder.__new__(Coder)
    c._ascii_identifiers_only, c._task_mode, c._allow_preexisting_patterns = False, "code", True
    shown = f.read_text(encoding="utf-8", errors="replace")
    written, err = c._write_files([{"path": "m.py", "content": shown.replace("x = 1", "x = 2")}],
                                  tmp_path, "T", None)
    assert written == [] and "not UTF-8" in err
    assert f.read_bytes() == original
