"""tests/test_cr1_coder_prose.py — AUTO-CR-1: Coder text-output protocol (creative).

Tests cover the new _parse_response_prose() path and verify that the existing
JSON path (code / docs modes) is byte-for-byte unaffected.
"""

import configparser
import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.auto.coder import Coder, _SYSTEM_PROMPT_CREATIVE, _strip_outer_fence


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_coder(task_mode: str = "creative") -> Coder:
    """Build a minimal Coder for unit-testing parse helpers."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:11434",
            "api_key":    "test",
            "model":      "llama3.1:8b",
            "api_format": "ollama",
            "num_ctx":    "4096",
        },
        "coder": {
            "temperature": "0.2",
            "max_tokens":  "2048",
        },
        "loop": {"timeout_seconds": "30"},
    })
    return Coder(
        config=cfg,
        base_url="http://localhost:11434",
        api_key="test",
        model="llama3.1:8b",
        api_format="ollama",
        verify_ssl=False,
        task_mode=task_mode,
    )


PLAIN_PROSE = textwrap.dedent("""\
    The fog clung to the harbour like a forgotten promise.
    Elena stepped off the gangway, her boots meeting cobblestone
    for the first time in three years.

    Behind her, the sea.  Ahead, the city she had sworn never to return to.
""")


# ─────────────────────────────────────────────────────────────────────────────
# test_single_target_plain_prose_written
# ─────────────────────────────────────────────────────────────────────────────

def test_single_target_plain_prose_written():
    """Plain prose (no JSON, no fences) written to the single target file."""
    coder = _make_coder("creative")
    files, err = coder._parse_response(
        PLAIN_PROSE, "task-01", ["chapter_01.md"]
    )
    assert err == "", f"unexpected error: {err}"
    assert len(files) == 1
    assert files[0]["path"] == "chapter_01.md"
    # Content should be the prose (stripped + trailing newline)
    assert "Elena stepped off the gangway" in files[0]["content"]
    assert "fog clung" in files[0]["content"]


# ─────────────────────────────────────────────────────────────────────────────
# test_fenced_prose_unwrapped
# ─────────────────────────────────────────────────────────────────────────────

def test_fenced_prose_unwrapped():
    """Prose wrapped in ```markdown fences is correctly unwrapped."""
    fenced = "```markdown\n" + PLAIN_PROSE + "\n```"
    coder = _make_coder("creative")
    files, err = coder._parse_response(fenced, "task-02", ["chapter_01.md"])
    assert err == ""
    assert "fog clung" in files[0]["content"]
    # Fence markers must not appear in the written content.
    assert "```" not in files[0]["content"]


# ─────────────────────────────────────────────────────────────────────────────
# test_multi_file_markers_split
# ─────────────────────────────────────────────────────────────────────────────

def test_multi_file_markers_split():
    """<<<FILE: path>>> … <<<END>>> markers correctly split multi-file output."""
    body = (
        "<<<FILE: chapter_02.md>>>\n"
        "This is chapter two.\n"
        "<<<END>>>\n"
        "<<<FILE: chapter_03.md>>>\n"
        "This is chapter three.\n"
        "<<<END>>>\n"
    )
    coder = _make_coder("creative")
    targets = ["chapter_02.md", "chapter_03.md"]
    files, err = coder._parse_response(body, "task-03", targets)
    assert err == ""
    assert len(files) == 2
    paths = {f["path"] for f in files}
    assert "chapter_02.md" in paths
    assert "chapter_03.md" in paths
    content_by_path = {f["path"]: f["content"] for f in files}
    assert "chapter two" in content_by_path["chapter_02.md"]
    assert "chapter three" in content_by_path["chapter_03.md"]


# ─────────────────────────────────────────────────────────────────────────────
# test_trailing_context_request_extracted_and_stripped
# ─────────────────────────────────────────────────────────────────────────────

def test_trailing_context_request_extracted_and_stripped():
    """CONTEXT_REQUEST line is stripped from content and NOT written to the file."""
    body = PLAIN_PROSE.rstrip() + "\nCONTEXT_REQUEST: chapter_01.md, chapter_02.md\n"
    coder = _make_coder("creative")
    files, err = coder._parse_response(body, "task-04", ["chapter_03.md"])
    assert err == ""
    assert len(files) == 1
    written_content = files[0]["content"]
    assert "CONTEXT_REQUEST" not in written_content
    assert "Elena" in written_content


# ─────────────────────────────────────────────────────────────────────────────
# test_empty_body_is_only_failure
# ─────────────────────────────────────────────────────────────────────────────

def test_empty_body_is_only_failure():
    """An empty / whitespace-only body is the single legitimate failure."""
    coder = _make_coder("creative")

    # Completely empty
    files, err = coder._parse_response("", "task-05", ["chapter_01.md"])
    assert files == []
    assert err != ""

    # Only whitespace
    files2, err2 = coder._parse_response("   \n\n  ", "task-05b", ["chapter_01.md"])
    assert files2 == []
    assert err2 != ""

    # Non-JSON but non-empty → must NOT fail
    non_json = "Once upon a time there was a dragon."
    files3, err3 = coder._parse_response(non_json, "task-05c", ["chapter_01.md"])
    assert err3 == "", f"non-JSON prose should not fail: {err3}"
    assert len(files3) == 1


# ─────────────────────────────────────────────────────────────────────────────
# test_code_mode_json_path_unchanged  (regression)
# ─────────────────────────────────────────────────────────────────────────────

def test_code_mode_json_path_unchanged():
    """Code mode still uses the strict JSON path; prose is rejected."""
    coder = _make_coder("code")

    valid_json = json.dumps({
        "files": [{"path": "src/foo.py", "content": "x = 1\n"}]
    })
    files, err = coder._parse_response(valid_json, "task-06", ["src/foo.py"])
    assert err == ""
    assert files[0]["path"] == "src/foo.py"

    # Plain prose in code mode should fail (invalid JSON)
    files2, err2 = coder._parse_response("Just some prose here.", "task-06b", ["src/foo.py"])
    assert files2 == []
    assert err2 != ""


# ─────────────────────────────────────────────────────────────────────────────
# test_truncation_guard_actionable_error
# ─────────────────────────────────────────────────────────────────────────────

def test_truncation_guard_actionable_error():
    """A body that ends mid-sentence at ≥95 % of the token budget yields an
    actionable error message, not a silent empty-file failure."""
    coder = _make_coder("creative")
    # max_tokens = 2048, char budget = 2048*4 = 8192
    char_budget = coder._max_tokens * 4
    # Build a body that hits the budget and ends mid-word (no sentence terminator)
    truncated_body = "A" * int(char_budget * 0.97) + " the story continu"
    files, err = coder._parse_response(truncated_body, "task-07", ["chapter_01.md"])
    assert files == []
    assert "token budget" in err.lower() or "max_tokens" in err.lower()


# ─────────────────────────────────────────────────────────────────────────────
# test_creative_system_prompt_no_json
# ─────────────────────────────────────────────────────────────────────────────

def test_creative_system_prompt_no_json():
    """The creative system prompt must NOT require JSON output.
    It may say 'No JSON' to prohibit it, but must not instruct the model
    to return a JSON schema/object."""
    # Must not ask for JSON output (these are the code-mode JSON contract phrases)
    assert '"files"' not in _SYSTEM_PROMPT_CREATIVE
    assert "JSON schema" not in _SYSTEM_PROMPT_CREATIVE
    assert "JSON object" not in _SYSTEM_PROMPT_CREATIVE
    # Must use the prose-native instruction
    assert "Return ONLY the chapter prose" in _SYSTEM_PROMPT_CREATIVE
    assert "CONTEXT_REQUEST" in _SYSTEM_PROMPT_CREATIVE


# ─────────────────────────────────────────────────────────────────────────────
# test_multi_file_no_markers_falls_back_to_single
# ─────────────────────────────────────────────────────────────────────────────

def test_multi_file_no_markers_falls_back_to_single():
    """When multiple targets are declared but no FILE markers exist and body is
    non-empty, fall back to single-file behaviour using the first target."""
    coder = _make_coder("creative")
    body = "Prose without any markers.\nJust a plain chapter."
    targets = ["chapter_02.md", "chapter_03.md"]
    files, err = coder._parse_response(body, "task-08", targets)
    # Fail-open: single target gets the whole body
    assert err == ""
    assert len(files) == 1
    assert files[0]["path"] == "chapter_02.md"
    assert "plain chapter" in files[0]["content"]
