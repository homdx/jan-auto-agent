"""tests/test_auto_c2.py — Tests for AUTO-C2: Coder (code-change generator).

Covers all ACs from the story:

  AC: output is a clean patch/file
      - <think> blocks stripped before JSON parsing
      - outer code fences (```json ... ```) stripped before parsing
      - inner code fences inside "content" values stripped
      - no commentary leaks into written file content

  AC: /edit write path reused
      - files written to base_dir at the paths specified in "path" fields
      - original file backed up as <file>.coder.bak before overwriting
      - new (not-yet-existing) files created

  AC: grounded prompt
      - instruction present in prompt sent to LLM
      - cited_location (file / symbol / lines) present in prompt
      - target file contents included in prompt
      - prior_feedback included in prompt when supplied

Additional coverage:

  CoderResult:
      - succeeded == True iff files_written non-empty and error empty
      - succeeded == False when error set
      - succeeded == False when files_written empty
      - summary() format strings for OK / FAIL

  Parsing:
      - valid single-file JSON response → one file written
      - valid multi-file JSON response → multiple files written
      - JSON object with no "files" key → error, no files written (fail-closed)
      - "files" list with item missing "path" → that item skipped
      - "files" list with item missing "content" → that item skipped
      - non-JSON response → error, no files written
      - LLM returns array (not object) → error, no files written

  File writing:
      - backup created for existing file before overwrite
      - no backup created for new (non-existing) file
      - subdirectory created when target_file is in a subdir
      - write error captured in result.error; other files still written

  LLM failure:
      - request_completion raises → CoderResult with error, no files written

  Helpers:
      - _strip_outer_fence: no fence → unchanged
      - _strip_outer_fence: ```json … ``` → inner content
      - _strip_outer_fence: ``` … ``` → inner content
      - _strip_code_fence: no fence → content + trailing newline
      - _strip_code_fence: ```python … ``` → inner + trailing newline
      - _strip_code_fence: already ends with newline → preserved

  make_coder:
      - factory returns a Coder instance with settings from config
      - temperature / max_tokens read from [coder] section
      - missing [coder] section uses defaults

  Integration:
      - end-to-end with mocked LLM: file written, result.succeeded True
      - <think> block in LLM response discarded, JSON still parsed
      - prior_feedback appended to prompt on second round
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto.coder import (
    Coder,
    CoderResult,
    _strip_code_fence,
    _strip_outer_fence,
    make_coder,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

def _minimal_config(
    temperature: float = 0.2,
    max_tokens: int = 4096,
    extra_coder: dict[str, str] | None = None,
) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {
                "base_url": "http://localhost:1337/v1",
                "api_key": "test",
                "model": "test-model",
                "api_format": "openai",
            },
            "loop": {"timeout_seconds": "300"},
            "coder": {
                "temperature": str(temperature),
                "max_tokens": str(max_tokens),
                **(extra_coder or {}),
            },
        }
    )
    return cfg


def _make_coder(cfg: configparser.ConfigParser | None = None) -> Coder:
    cfg = cfg or _minimal_config()
    return Coder(
        config=cfg,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
    )


def _task(
    *,
    task_id: str = "AUTO-T1",
    title: str = "Fix the bug",
    instruction: str = "Add a null check to prevent crash.",
    target_files: list[str] | None = None,
    cited_locations: list[dict] | None = None,
    cited_location: dict | None = None,
) -> dict:
    t: dict = {
        "id":               task_id,
        "title":            title,
        "instruction":      instruction,
        "target_files":     target_files or ["tools/module.py"],
        "acceptance_check": "python -m pytest tests/",
        "status":           "in_progress",
        "round":            0,
        "attempt":          0,
        "dependencies":     [],
    }
    if cited_locations is not None:
        t["cited_locations"] = cited_locations
    elif cited_location is not None:
        t["cited_location"] = cited_location
    else:
        t["cited_locations"] = [
            {"file": "tools/module.py", "symbol": "parse", "line_start": None, "line_end": None}
        ]
    return t


def _valid_llm_response(
    files: list[tuple[str, str]] | None = None,
) -> str:
    """Return a well-formed JSON response string as the LLM would emit."""
    files = files or [("tools/module.py", "def parse(x):\n    return x\n")]
    payload = {"files": [{"path": p, "content": c} for p, c in files]}
    return json.dumps(payload)


def _coder_with_mock_llm(
    tmp_path: Path,
    llm_response: str,
    cfg: configparser.ConfigParser | None = None,
) -> tuple[Coder, Path]:
    """Return (coder, base_dir) with request_completion mocked to return llm_response."""
    base_dir = tmp_path / "repo"
    base_dir.mkdir(parents=True, exist_ok=True)
    return _make_coder(cfg), base_dir


# ─────────────────────────────────────────────────────────────────────────────
# CoderResult
# ─────────────────────────────────────────────────────────────────────────────

class TestCoderResult:
    def test_succeeded_true(self) -> None:
        r = CoderResult(task_id="T1", files_written=["a.py"], error="")
        assert r.succeeded is True

    def test_succeeded_false_no_files(self) -> None:
        r = CoderResult(task_id="T1", files_written=[], error="")
        assert r.succeeded is False

    def test_succeeded_false_error(self) -> None:
        r = CoderResult(task_id="T1", files_written=["a.py"], error="write failed")
        assert r.succeeded is False

    def test_summary_ok(self) -> None:
        r = CoderResult(task_id="T1", files_written=["a.py"], error="")
        assert "OK" in r.summary()
        assert "T1" in r.summary()

    def test_summary_fail(self) -> None:
        r = CoderResult(task_id="T1", files_written=[], error="LLM call failed")
        assert "FAIL" in r.summary()
        assert "T1" in r.summary()

    def test_default_fields(self) -> None:
        r = CoderResult()
        assert r.task_id == ""
        assert r.files_written == []
        assert r.error == ""
        assert r.raw_response == ""


# ─────────────────────────────────────────────────────────────────────────────
# _strip_outer_fence
# ─────────────────────────────────────────────────────────────────────────────

class TestStripOuterFence:
    def test_no_fence_unchanged(self) -> None:
        text = '{"files": []}'
        assert _strip_outer_fence(text) == text

    def test_json_fence_stripped(self) -> None:
        text = '```json\n{"files": []}\n```'
        result = _strip_outer_fence(text)
        assert result == '{"files": []}'

    def test_plain_fence_stripped(self) -> None:
        text = '```\n{"files": []}\n```'
        result = _strip_outer_fence(text)
        assert result == '{"files": []}'

    def test_whitespace_trimmed(self) -> None:
        text = '  ```json\n  {"a": 1}\n  ```  '
        result = _strip_outer_fence(text.strip())
        assert '"a"' in result


# ─────────────────────────────────────────────────────────────────────────────
# _strip_code_fence
# ─────────────────────────────────────────────────────────────────────────────

class TestStripCodeFence:
    def test_no_fence_adds_trailing_newline(self) -> None:
        result = _strip_code_fence("x = 1")
        assert result == "x = 1\n"

    def test_python_fence_stripped(self) -> None:
        text = "```python\nx = 1\n```"
        result = _strip_code_fence(text)
        assert result == "x = 1\n"

    def test_plain_fence_stripped(self) -> None:
        text = "```\ndef f(): pass\n```"
        result = _strip_code_fence(text)
        assert result == "def f(): pass\n"

    def test_existing_trailing_newline_preserved(self) -> None:
        result = _strip_code_fence("x = 1\n")
        assert result == "x = 1\n"


# ─────────────────────────────────────────────────────────────────────────────
# Response parsing
# ─────────────────────────────────────────────────────────────────────────────

class TestResponseParsing:
    """Tests for Coder._parse_response() via the full generate() path."""

    def _run(self, tmp_path: Path, llm_text: str) -> CoderResult:
        base_dir = tmp_path / "repo"
        base_dir.mkdir(parents=True, exist_ok=True)
        coder = _make_coder()
        task = _task()
        with patch("tools.llm_stream.request_completion", return_value=llm_text):
            return coder.generate(task, base_dir)

    def test_valid_single_file(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, _valid_llm_response())
        assert r.succeeded
        assert "tools/module.py" in r.files_written

    def test_valid_multi_file(self, tmp_path: Path) -> None:
        response = _valid_llm_response(
            [("tools/a.py", "a = 1\n"), ("tools/b.py", "b = 2\n")]
        )
        base_dir = tmp_path / "repo"
        base_dir.mkdir(parents=True, exist_ok=True)
        coder = _make_coder()
        task = _task(target_files=["tools/a.py", "tools/b.py"])
        with patch("tools.llm_stream.request_completion", return_value=response):
            r = coder.generate(task, base_dir)
        assert r.succeeded
        assert "tools/a.py" in r.files_written
        assert "tools/b.py" in r.files_written

    def test_missing_files_key_is_error(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, '{"something": []}')
        assert not r.succeeded
        assert r.error

    def test_files_is_not_list_is_error(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, '{"files": "wrong"}')
        assert not r.succeeded
        assert r.error

    def test_non_json_is_error(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, "Here is your improved code: just kidding")
        assert not r.succeeded
        assert r.error

    def test_json_array_at_root_is_error(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, '[{"path": "x.py", "content": "pass"}]')
        assert not r.succeeded

    def test_item_missing_path_skipped(self, tmp_path: Path) -> None:
        bad = json.dumps({"files": [{"content": "x = 1"}]})
        r = self._run(tmp_path, bad)
        # All items skipped → fail-closed
        assert not r.succeeded

    def test_item_missing_content_skipped(self, tmp_path: Path) -> None:
        bad = json.dumps({"files": [{"path": "tools/x.py"}]})
        r = self._run(tmp_path, bad)
        assert not r.succeeded

    def test_raw_response_populated_on_success(self, tmp_path: Path) -> None:
        raw = _valid_llm_response()
        r = self._run(tmp_path, raw)
        assert r.raw_response  # non-empty

    def test_raw_response_populated_on_parse_error(self, tmp_path: Path) -> None:
        r = self._run(tmp_path, "not json at all")
        assert r.raw_response


# ─────────────────────────────────────────────────────────────────────────────
# Output cleanliness (AC: no commentary, fences stripped, think stripped)
# ─────────────────────────────────────────────────────────────────────────────

class TestOutputCleanliness:
    def _written_content(self, tmp_path: Path, llm_text: str) -> str:
        base_dir = tmp_path / "repo"
        base_dir.mkdir(parents=True, exist_ok=True)
        coder = _make_coder()
        task = _task()
        with patch("tools.llm_stream.request_completion", return_value=llm_text):
            coder.generate(task, base_dir)
        return (base_dir / "tools" / "module.py").read_text()

    def test_think_block_stripped(self, tmp_path: Path) -> None:
        think_response = (
            "<think>Let me think about this carefully...</think>\n"
            + _valid_llm_response([("tools/module.py", "x = 42\n")])
        )
        content = self._written_content(tmp_path, think_response)
        assert "<think>" not in content
        assert "x = 42" in content

    def test_outer_json_fence_stripped(self, tmp_path: Path) -> None:
        fenced = "```json\n" + _valid_llm_response([("tools/module.py", "y = 7\n")]) + "\n```"
        content = self._written_content(tmp_path, fenced)
        assert "```" not in content
        assert "y = 7" in content

    def test_inner_code_fence_stripped(self, tmp_path: Path) -> None:
        # Model wraps the file content in inner fences.
        inner_content = "```python\ndef fixed():\n    pass\n```"
        response = json.dumps({"files": [{"path": "tools/module.py", "content": inner_content}]})
        content = self._written_content(tmp_path, response)
        assert "```" not in content
        assert "def fixed" in content

    def test_no_commentary_in_file(self, tmp_path: Path) -> None:
        """The written file must contain exactly what 'content' says, nothing more."""
        expected = "def f():\n    return 1\n"
        response = json.dumps({"files": [{"path": "tools/module.py", "content": expected}]})
        content = self._written_content(tmp_path, response)
        assert content == expected


# ─────────────────────────────────────────────────────────────────────────────
# File writing (AC: /edit write path)
# ─────────────────────────────────────────────────────────────────────────────

class TestFileWriting:
    def test_file_written_to_base_dir(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = _make_coder()
        task = _task(target_files=["module.py"])
        response = json.dumps({"files": [{"path": "module.py", "content": "pass\n"}]})
        with patch("tools.llm_stream.request_completion", return_value=response):
            r = coder.generate(task, base_dir)
        assert r.succeeded
        assert (base_dir / "module.py").read_text() == "pass\n"

    def test_backup_created_for_existing_file(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        (base_dir / "module.py").write_text("original\n")
        coder = _make_coder()
        task = _task(target_files=["module.py"])
        response = json.dumps({"files": [{"path": "module.py", "content": "revised\n"}]})
        with patch("tools.llm_stream.request_completion", return_value=response):
            coder.generate(task, base_dir)
        backup = base_dir / "module.py.coder.bak"
        assert backup.exists()
        assert backup.read_text() == "original\n"

    def test_no_backup_for_new_file(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = _make_coder()
        task = _task(target_files=["new_file.py"])
        response = json.dumps({"files": [{"path": "new_file.py", "content": "# new\n"}]})
        with patch("tools.llm_stream.request_completion", return_value=response):
            coder.generate(task, base_dir)
        assert not (base_dir / "new_file.py.coder.bak").exists()
        assert (base_dir / "new_file.py").read_text() == "# new\n"

    def test_subdirectory_created(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = _make_coder()
        task = _task(target_files=["tools/sub/module.py"])
        response = json.dumps({"files": [{"path": "tools/sub/module.py", "content": "x=1\n"}]})
        with patch("tools.llm_stream.request_completion", return_value=response):
            r = coder.generate(task, base_dir)
        assert (base_dir / "tools" / "sub" / "module.py").exists()

    def test_other_files_written_despite_one_write_error(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = _make_coder()
        task = _task(target_files=["a.py", "b.py"])
        response = json.dumps(
            {
                "files": [
                    {"path": "a.py", "content": "a=1\n"},
                    {"path": "b.py", "content": "b=2\n"},
                ]
            }
        )
        # Fail the write for a.py only.
        original_write_text = Path.write_text

        def patched_write_text(self, content, **kwargs):
            if self.name == "a.py":
                raise OSError("disk full")
            return original_write_text(self, content, **kwargs)

        with (
            patch("tools.llm_stream.request_completion", return_value=response),
            patch.object(Path, "write_text", patched_write_text),
        ):
            r = coder.generate(task, base_dir)

        assert "b.py" in r.files_written
        assert r.error  # first_error captured

    def test_new_file_not_existing_in_prompt(self, tmp_path: Path) -> None:
        """A target file that doesn't exist yet must appear in the prompt as [new file]."""
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = _make_coder()
        task = _task(target_files=["brand_new.py"])

        captured_prompts: list[str] = []

        def fake_completion(url, headers, payload, *args, **kwargs):
            captured_prompts.append(payload["messages"][1]["content"])
            return json.dumps({"files": [{"path": "brand_new.py", "content": "pass\n"}]})

        with patch("tools.llm_stream.request_completion", side_effect=fake_completion):
            coder.generate(task, base_dir)

        assert "[new file" in captured_prompts[0]


# ─────────────────────────────────────────────────────────────────────────────
# Grounded prompt (AC: instruction + cited_location + file contents in prompt)
# ─────────────────────────────────────────────────────────────────────────────

class TestGroundedPrompt:
    def _capture_prompt(
        self,
        tmp_path: Path,
        task: dict,
        existing_files: dict[str, str] | None = None,
        prior_feedback: list[str] | None = None,
    ) -> str:
        base_dir = tmp_path / "repo"
        base_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in (existing_files or {}).items():
            dest = base_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)

        captured: list[str] = []

        def fake_completion(url, headers, payload, *args, **kwargs):
            captured.append(payload["messages"][1]["content"])
            return _valid_llm_response()

        coder = _make_coder()
        with patch("tools.llm_stream.request_completion", side_effect=fake_completion):
            coder.generate(task, base_dir, prior_feedback=prior_feedback)

        return captured[0] if captured else ""

    def test_instruction_in_prompt(self, tmp_path: Path) -> None:
        task = _task(instruction="Replace the hash with a more secure algorithm.")
        prompt = self._capture_prompt(tmp_path, task)
        assert "Replace the hash with a more secure algorithm." in prompt

    def test_title_in_prompt(self, tmp_path: Path) -> None:
        task = _task(title="Harden the hasher")
        prompt = self._capture_prompt(tmp_path, task)
        assert "Harden the hasher" in prompt

    def test_task_id_in_prompt(self, tmp_path: Path) -> None:
        task = _task(task_id="AUTO-C2-TEST")
        prompt = self._capture_prompt(tmp_path, task)
        assert "AUTO-C2-TEST" in prompt

    def test_cited_symbol_in_prompt(self, tmp_path: Path) -> None:
        task = _task(
            cited_locations=[
                {"file": "tools/module.py", "symbol": "do_something_important",
                 "line_start": None, "line_end": None}
            ]
        )
        prompt = self._capture_prompt(tmp_path, task)
        assert "do_something_important" in prompt

    def test_cited_file_in_prompt(self, tmp_path: Path) -> None:
        task = _task(
            cited_locations=[
                {"file": "tools/core.py", "symbol": None,
                 "line_start": 42, "line_end": 55}
            ]
        )
        prompt = self._capture_prompt(tmp_path, task)
        assert "tools/core.py" in prompt

    def test_cited_line_range_in_prompt(self, tmp_path: Path) -> None:
        task = _task(
            cited_locations=[
                {"file": "tools/module.py", "symbol": None,
                 "line_start": 10, "line_end": 20}
            ]
        )
        prompt = self._capture_prompt(tmp_path, task)
        assert "10" in prompt
        assert "20" in prompt

    def test_file_contents_in_prompt(self, tmp_path: Path) -> None:
        task = _task(target_files=["tools/module.py"])
        existing = {"tools/module.py": "UNIQUE_SENTINEL_XYZ = True\n"}
        prompt = self._capture_prompt(tmp_path, task, existing_files=existing)
        assert "UNIQUE_SENTINEL_XYZ" in prompt

    def test_prior_feedback_in_prompt(self, tmp_path: Path) -> None:
        task = _task()
        prompt = self._capture_prompt(
            tmp_path, task, prior_feedback=["The test failed because of a NameError."]
        )
        assert "The test failed because of a NameError." in prompt

    def test_no_prior_feedback_omitted_from_prompt(self, tmp_path: Path) -> None:
        task = _task()
        prompt = self._capture_prompt(tmp_path, task, prior_feedback=None)
        assert "PRIOR ROUND FEEDBACK" not in prompt

    def test_multiple_feedback_rounds_all_in_prompt(self, tmp_path: Path) -> None:
        task = _task()
        feedback = ["Round 1: AttributeError crash.", "Round 2: Wrong output format."]
        prompt = self._capture_prompt(tmp_path, task, prior_feedback=feedback)
        assert "Round 1: AttributeError crash." in prompt
        assert "Round 2: Wrong output format." in prompt

    def test_flat_cited_location_key_accepted(self, tmp_path: Path) -> None:
        """Task dicts using 'cited_location' (singular) should also work."""
        task = _task(
            cited_location={"file": "flat.py", "symbol": "flat_sym",
                            "line_start": None, "line_end": None}
        )
        prompt = self._capture_prompt(tmp_path, task)
        assert "flat_sym" in prompt


# ─────────────────────────────────────────────────────────────────────────────
# LLM failure
# ─────────────────────────────────────────────────────────────────────────────

class TestLLMFailure:
    def test_network_error_returns_result_no_raise(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = _make_coder()
        task = _task()
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=RuntimeError("connection refused"),
        ):
            r = coder.generate(task, base_dir)
        assert not r.succeeded
        assert "connection refused" in r.error
        assert r.files_written == []

    def test_http_error_captured_in_result(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        coder = _make_coder()
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=RuntimeError("HTTP 500"),
        ):
            r = coder.generate(_task(), base_dir)
        assert r.error
        assert not r.files_written


# ─────────────────────────────────────────────────────────────────────────────
# make_coder factory
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeCoder:
    def test_returns_coder_instance(self) -> None:
        cfg = _minimal_config()
        c = make_coder(cfg)
        assert isinstance(c, Coder)

    def test_temperature_from_config(self) -> None:
        cfg = _minimal_config(temperature=0.7)
        c = make_coder(cfg)
        assert c._temperature == pytest.approx(0.7)

    def test_max_tokens_from_config(self) -> None:
        cfg = _minimal_config(max_tokens=2048)
        c = make_coder(cfg)
        assert c._max_tokens == 2048

    def test_defaults_when_section_absent(self) -> None:
        cfg = configparser.ConfigParser()
        cfg.read_dict(
            {
                "api": {"active": "local", "verify_ssl": "false"},
                "api_local": {
                    "base_url": "http://localhost:1337/v1",
                    "api_key": "k",
                    "model": "m",
                    "api_format": "openai",
                },
                "loop": {"timeout_seconds": "300"},
            }
        )
        c = make_coder(cfg)
        assert c._temperature == pytest.approx(0.2)
        assert c._max_tokens == 16384

    def test_custom_system_prompt_from_config(self) -> None:
        cfg = _minimal_config(extra_coder={"system": "You are a robot."})
        c = make_coder(cfg)
        assert "You are a robot." in c._system


# ─────────────────────────────────────────────────────────────────────────────
# Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_end_to_end_file_written(self, tmp_path: Path) -> None:
        """Full generate() call with mocked LLM writes the correct file."""
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        (base_dir / "tools").mkdir()
        (base_dir / "tools" / "module.py").write_text("def parse(x): return None\n")

        coder = _make_coder()
        task = _task(
            target_files=["tools/module.py"],
            instruction="Make parse return x instead of None.",
            cited_locations=[
                {"file": "tools/module.py", "symbol": "parse",
                 "line_start": 1, "line_end": 1}
            ],
        )

        response = json.dumps(
            {"files": [{"path": "tools/module.py", "content": "def parse(x): return x\n"}]}
        )
        with patch("tools.llm_stream.request_completion", return_value=response):
            r = coder.generate(task, base_dir)

        assert r.succeeded
        assert "tools/module.py" in r.files_written
        content = (base_dir / "tools" / "module.py").read_text()
        assert "return x" in content

    def test_think_block_before_json_handled(self, tmp_path: Path) -> None:
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        new_content = "FIXED = True\n"
        think_wrapped = (
            "<think>Hmm, I need to fix the bug...</think>\n"
            + json.dumps({"files": [{"path": "fix.py", "content": new_content}]})
        )
        task = _task(target_files=["fix.py"])
        with patch("tools.llm_stream.request_completion", return_value=think_wrapped):
            r = _make_coder().generate(task, base_dir)

        assert r.succeeded
        assert (base_dir / "fix.py").read_text() == new_content

    def test_prior_feedback_affects_second_round_prompt(self, tmp_path: Path) -> None:
        """Verifies prior_feedback from round 1 appears in the prompt on round 2."""
        base_dir = tmp_path / "repo"
        base_dir.mkdir()
        task = _task()
        feedback_round1 = "NameError: 'helper' is not defined on line 5."

        prompts_captured: list[str] = []

        def capture(url, headers, payload, *args, **kwargs):
            prompts_captured.append(payload["messages"][1]["content"])
            return _valid_llm_response()

        with patch("tools.llm_stream.request_completion", side_effect=capture):
            _make_coder().generate(task, base_dir, prior_feedback=[feedback_round1])

        assert feedback_round1 in prompts_captured[0]

    def test_system_prompt_in_payload(self, tmp_path: Path) -> None:
        """Verifies the system message is the first message in the payload."""
        base_dir = tmp_path / "repo"
        base_dir.mkdir()

        payloads: list[dict] = []

        def capture(url, headers, payload, *args, **kwargs):
            payloads.append(payload)
            return _valid_llm_response()

        with patch("tools.llm_stream.request_completion", side_effect=capture):
            _make_coder().generate(_task(), base_dir)

        assert payloads[0]["messages"][0]["role"] == "system"
        assert "engineer" in payloads[0]["messages"][0]["content"].lower()
