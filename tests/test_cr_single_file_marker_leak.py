"""tests/test_cr_single_file_marker_leak.py — single-file marker leak bug.

Regression test for the bug where <<<FILE: path>>> / <<<END>>> delimiters
were written verbatim into chapter files when the task had only one target file.

Root cause: `_parse_response_prose` only activated the marker-stripping path
when `len(target_files) > 1`.  With a single target, the model still emits
the delimiters (it learned the format), but the parser wrote `body` as-is.

Fix: strip markers regardless of target_files count.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.coder import Coder


def _coder(task_mode: str = "creative") -> Coder:
    cfg = configparser.ConfigParser()
    for sec in ("coder", "api", "api_local"):
        cfg.add_section(sec)
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "32768")
    cfg.set("coder", "max_tokens_creative", "4096")
    return Coder(
        config=cfg,
        base_url="http://x",
        api_key="x",
        model="m",
        api_format="ollama",
        task_mode=task_mode,
    )


PROSE = "Jane watched as Suse took a step forward, but felt a little more hopeful than usual."


def test_single_file_markers_stripped(tmp_path: Path) -> None:
    """<<<FILE:>>> / <<<END>>> must not appear in the written file content."""
    response = f"<<<FILE: chapter_2.txt>>>\n\n{PROSE}\n\n<<<END>>>"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "T1", ["chapter_2.txt"])

    assert not err
    assert len(parsed) == 1
    content = parsed[0]["content"]
    assert "<<<FILE:" not in content, "opening delimiter leaked into file content"
    assert "<<<END>>>" not in content, "closing delimiter leaked into file content"
    assert PROSE in content


def test_single_file_markers_correct_path(tmp_path: Path) -> None:
    """Path extracted from the marker is used, not a synthesised default."""
    response = f"<<<FILE: books/chapter_3.txt>>>\n\n{PROSE}\n\n<<<END>>>"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "T2", ["books/chapter_3.txt"])

    assert not err
    assert parsed[0]["path"] == "books/chapter_3.txt"


def test_multi_file_markers_still_work(tmp_path: Path) -> None:
    """Multi-file path (the original case) still parses correctly after the fix."""
    response = (
        "<<<FILE: chapter_1.txt>>>\nFirst chapter prose.\n<<<END>>>\n"
        "<<<FILE: chapter_2.txt>>>\nSecond chapter prose.\n<<<END>>>"
    )
    c = _coder()
    parsed, err = c._parse_response_prose(
        response, "T3", ["chapter_1.txt", "chapter_2.txt"], 
    )

    assert not err
    assert len(parsed) == 2
    paths = [p["path"] for p in parsed]
    assert "chapter_1.txt" in paths
    assert "chapter_2.txt" in paths
    for p in parsed:
        assert "<<<FILE:" not in p["content"]
        assert "<<<END>>>" not in p["content"]


def test_plain_prose_no_markers_unchanged(tmp_path: Path) -> None:
    """Plain prose with no markers is still written as-is (single-file fallback)."""
    c = _coder()
    parsed, err = c._parse_response_prose(PROSE, "T4", ["chapter_4.txt"])

    assert not err
    assert len(parsed) == 1
    assert PROSE in parsed[0]["content"]
    assert parsed[0]["path"] == "chapter_4.txt"
