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


# ── AGNES-10: malformed / empty markers are refused, never leaked or written ──


def test_corrupt_opener_empty_path_refused() -> None:
    """<<<FILE:>>> (empty path) is refused — nothing is written to a chapter."""
    response = f"<<<FILE:>>>\n\n{PROSE}\n\n<<<END>>>"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "A10-1", ["chapter_2.txt"])

    assert parsed == []
    assert bool(err) is True


def test_corrupt_opener_blank_path_refused() -> None:
    """<<<FILE: >>> (whitespace-only path) is refused — nothing is written."""
    response = f"<<<FILE: >>>\n\n{PROSE}\n\n<<<END>>>"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "A10-2", ["chapter_2.txt"])

    assert parsed == []
    assert bool(err) is True


def test_empty_pair_refused() -> None:
    """An opener closed immediately with no body is refused, not overwriting."""
    response = f"<<<FILE: chapter_2.txt>>>\n<<<END>>>"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "A10-3", ["chapter_2.txt"])

    assert parsed == []
    assert bool(err) is True


def test_no_colon_opener_refused() -> None:
    """An opener without a colon (<<<FILE x>>>) is refused, not leaked."""
    response = f"<<<FILE chapter_2.txt>>>\n\n{PROSE}\n\n<<<END>>>"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "A10-4", ["chapter_2.txt"])

    assert parsed == []
    assert bool(err) is True


def test_lone_end_refused() -> None:
    """A lone <<<END>>> on its own line with no opener is refused, not leaked."""
    response = f"{PROSE}\n\n<<<END>>>"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "A10-5", ["chapter_2.txt"])

    assert parsed == []
    assert bool(err) is True


def test_crlf_malformed_marker_refused() -> None:
    """A CRLF marker line with an empty path is refused, not leaked."""
    response = f"<<<FILE:>>>\r\n\r\n{PROSE}\r\n\r\n<<<END>>>\r\n"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "A10-6", ["chapter_2.txt"])

    assert parsed == []
    assert bool(err) is True


def test_crlf_wellformed_marker_saved() -> None:
    """A well-formed CRLF marker line is handled; delimiters never reach content."""
    response = f"<<<FILE: chapter_2.txt>>>\r\n{PROSE}\r\n<<<END>>>\r\n"
    c = _coder()
    parsed, err = c._parse_response_prose(response, "A10-7", ["chapter_2.txt"])

    assert not err
    assert len(parsed) == 1
    content = parsed[0]["content"]
    assert "<<<FILE:" not in content
    assert "<<<END>>>" not in content
    assert PROSE in content


def test_multi_file_crlf_markers_saved() -> None:
    """Multi-file replies using CRLF still parse and save cleanly."""
    response = (
        "<<<FILE: chapter_1.txt>>>\r\nFirst chapter prose.\r\n<<<END>>>\r\n"
        "<<<FILE: chapter_2.txt>>>\r\nSecond chapter prose.\r\n<<<END>>>\r\n"
    )
    c = _coder()
    parsed, err = c._parse_response_prose(
        response, "A10-8", ["chapter_1.txt", "chapter_2.txt"],
    )

    assert not err
    assert len(parsed) == 2
    for p in parsed:
        assert "<<<FILE:" not in p["content"]
        assert "<<<END>>>" not in p["content"]


def test_lookalike_inside_line_accepted() -> None:
    """Prose containing <<<FILE: ...>>> / <<<END>>> inside a line is not a marker."""
    response = (
        'The character said "<<<FILE: chapter_2.txt>>>" and then '
        '"<<<END>>>" and carried on.\n\n' + PROSE
    )
    c = _coder()
    parsed, err = c._parse_response_prose(response, "A10-9", ["chapter_2.txt"])

    assert not err
    assert len(parsed) == 1
    assert PROSE in parsed[0]["content"]


def test_refused_marker_leaves_existing_chapter_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """A malformed marker reply must not overwrite an existing chapter."""
    chapter = tmp_path / "chapter_2.txt"
    original = "The original chapter text must survive a bad reply."
    chapter.write_text(original, encoding="utf-8")

    malformed = f"<<<FILE chapter_2.txt>>>\n\n{PROSE}\n\n<<<END>>>"

    def _fake_completion(**_kwargs):
        return malformed

    monkeypatch.setattr(
        "tools.auto.coder._llm_stream.request_completion", _fake_completion
    )

    task = {
        "id": "T11",
        "title": "improve chapter 2",
        "instruction": "improve the prose",
        "target_files": ["chapter_2.txt"],
        "cited_locations": [],
    }
    c = _coder()
    result = c.generate(task, tmp_path)

    assert result.error, "a refused marker reply must surface a retry error"
    assert result.files_written == []
    assert chapter.read_text(encoding="utf-8") == original



def test_a_mangled_closer_is_refused_not_saved() -> None:
    """`<<<END >>>` and `<<<END>>` are marker attempts: a retry, never a delimiter in a chapter."""
    c = _coder()
    for closer in ("<<<END >>>", "<<<END>>"):
        parsed, err = c._parse_response_prose(
            f"<<<FILE: chapter_2.txt>>>\n{PROSE}\n{closer}", "A10-m", ["chapter_2.txt"],
        )
        assert parsed == [] and bool(err), closer


def test_a_plain_crlf_chapter_keeps_its_line_endings() -> None:
    """Only a reply that has marker lines is normalised; ordinary CRLF prose is written as sent."""
    c = _coder()
    body = f"{PROSE}\r\n\r\n{PROSE}\r\n"
    parsed, err = c._parse_response_prose(body, "A10-c", ["chapter_2.txt"])
    assert not err and "\r\n" in parsed[0]["content"]


def test_a_wellformed_crlf_reply_leaves_no_carriage_return_in_the_path() -> None:
    c = _coder()
    parsed, err = c._parse_response_prose(
        f"<<<FILE: chapter_2.txt>>>\r\n{PROSE}\r\n<<<END>>>\r\n", "A10-p", ["chapter_2.txt"],
    )
    assert not err and parsed[0]["path"] == "chapter_2.txt" and "\r" not in parsed[0]["path"]


def test_indented_markers_still_parse() -> None:
    """The base strips the reply, so a first marker line with leading spaces was a marker: keep it."""
    c = _coder()
    parsed, err = c._parse_response_prose(
        f"  <<<FILE: chapter_2.txt>>>\n{PROSE}\n  <<<END>>>", "A10-i", ["chapter_2.txt"],
    )
    assert not err and "<<<" not in parsed[0]["content"]


def test_a_second_closer_after_a_complete_block_is_refused() -> None:
    """A stray `<<<END>>>` after a good block closes nothing: the reply is ambiguous, so it is a retry."""
    c = _coder()
    parsed, err = c._parse_response_prose(
        f"<<<FILE: chapter_2.txt>>>\n{PROSE}\n<<<END>>>\n<<<END>>>", "A10-s", ["chapter_2.txt"],
    )
    assert parsed == [] and bool(err)
