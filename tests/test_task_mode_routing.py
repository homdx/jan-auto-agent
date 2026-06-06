"""tests/test_task_mode_routing.py — AUTO-DM-1: task_mode routing skeleton.

Verifies that:
  1. AutoController reads task_mode from agents.ini and stores it.
  2. Missing key → "code" fallback.
  3. _run_task_loop accepts task_mode kwarg without error.
  4. make_outer_loop accepts task_mode kwarg without error.
  5. make_inner_loop accepts task_mode kwarg and stores it on the validator.
  6. LLMGate2Validator accepts and stores task_mode.

These are pure unit tests — no LLM calls, no filesystem I/O beyond a temp dir.
"""

from __future__ import annotations

import configparser
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ini(tmp_path: Path, task_mode: str | None) -> Path:
    """Write a minimal agents.ini; omit task_mode key when None."""
    lines = [
        "[api]",
        "active = local",
        "[api_local]",
        "base_url = http://localhost:1337/v1",
        "api_key = jan",
        "model = test-model",
        "api_format = openai",
        "[auto]",
        "git_user = test",
        "git_email = test@test.com",
        "exec_timeout_sec = 60",
    ]
    if task_mode is not None:
        lines.append(f"task_mode = {task_mode}")
    lines += [
        "[validator_agent]",
        "temperature = 0.1",
        "max_hints = 3",
    ]
    ini = tmp_path / "agents.ini"
    ini.write_text("\n".join(lines))
    return ini


# ─────────────────────────────────────────────────────────────────────────────
# 1 & 2 — AutoController.task_mode
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoControllerTaskMode:
    def test_reads_docs_mode(self, tmp_path):
        ini = _make_ini(tmp_path, "docs")
        from tools.auto.controller import AutoController
        c = AutoController(goal="test", base_dir=tmp_path, config_path=str(ini))
        assert c.task_mode == "docs"

    def test_reads_creative_mode(self, tmp_path):
        ini = _make_ini(tmp_path, "creative")
        from tools.auto.controller import AutoController
        c = AutoController(goal="test", base_dir=tmp_path, config_path=str(ini))
        assert c.task_mode == "creative"

    def test_reads_code_mode_explicit(self, tmp_path):
        ini = _make_ini(tmp_path, "code")
        from tools.auto.controller import AutoController
        c = AutoController(goal="test", base_dir=tmp_path, config_path=str(ini))
        assert c.task_mode == "code"

    def test_defaults_to_code_when_key_absent(self, tmp_path):
        ini = _make_ini(tmp_path, None)  # no task_mode key
        from tools.auto.controller import AutoController
        c = AutoController(goal="test", base_dir=tmp_path, config_path=str(ini))
        assert c.task_mode == "code"

    def test_defaults_to_code_when_no_ini(self, tmp_path):
        from tools.auto.controller import AutoController
        c = AutoController(goal="test", base_dir=tmp_path,
                           config_path=str(tmp_path / "nonexistent.ini"))
        assert c.task_mode == "code"


# ─────────────────────────────────────────────────────────────────────────────
# 3 — _run_task_loop accepts task_mode kwarg
# ─────────────────────────────────────────────────────────────────────────────

class TestRunTaskLoopSignature:
    def test_accepts_task_mode_kwarg(self, tmp_path):
        """_run_task_loop(task_mode="docs") must not raise TypeError."""
        ini = _make_ini(tmp_path, "docs")
        from tools.auto.controller import AutoController
        c = AutoController(goal="test", base_dir=tmp_path, config_path=str(ini))

        # Stub out every side-effect so only the signature is exercised.
        c.state = MagicMock()
        c.state.resume_info.return_value = {"pending": []}
        c.limits = MagicMock(exec_timeout_sec=60)
        c.git = None
        c.run_trace = None
        c.progress_display = None
        c.metrics_stream = None
        c.auto_tuner = None

        with patch("tools.auto.controller.make_outer_loop") as mock_ol:
            mock_ol.return_value = MagicMock()
            stop_reason, tasks_done = c._run_task_loop(task_mode="docs")

        assert stop_reason is None
        assert tasks_done == 0

    def test_default_task_mode_is_code(self, tmp_path):
        """Calling _run_task_loop() with no args still works (backward compat)."""
        ini = _make_ini(tmp_path, None)
        from tools.auto.controller import AutoController
        c = AutoController(goal="test", base_dir=tmp_path, config_path=str(ini))
        c.state = MagicMock()
        c.state.resume_info.return_value = {"pending": []}
        c.limits = MagicMock(exec_timeout_sec=60)
        c.git = None
        c.run_trace = None
        c.progress_display = None
        c.metrics_stream = None
        c.auto_tuner = None

        with patch("tools.auto.controller.make_outer_loop") as mock_ol:
            mock_ol.return_value = MagicMock()
            stop_reason, tasks_done = c._run_task_loop()  # no task_mode

        assert stop_reason is None
        assert tasks_done == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4 — make_outer_loop accepts task_mode kwarg
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeOuterLoopTaskMode:
    def _base_config(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": {"active": "local"},
            "api_local": {
                "base_url": "http://localhost:1337/v1",
                "api_key":  "jan",
                "model":    "test-model",
                "api_format": "openai",
            },
            "auto": {"max_rounds_per_task": "2", "max_rewrites": "0"},
        })
        return cfg

    def test_accepts_docs_mode(self, tmp_path):
        from tools.auto.outer_loop import make_outer_loop
        cfg = self._base_config()
        state = MagicMock()
        inner = MagicMock()
        # Injecting inner so no real make_inner_loop is called
        ol = make_outer_loop(cfg, tmp_path, state, inner_loop=inner, task_mode="docs")
        assert ol is not None

    def test_accepts_creative_mode(self, tmp_path):
        from tools.auto.outer_loop import make_outer_loop
        cfg = self._base_config()
        state = MagicMock()
        inner = MagicMock()
        ol = make_outer_loop(cfg, tmp_path, state, inner_loop=inner, task_mode="creative")
        assert ol is not None

    def test_no_task_mode_kwarg_works(self, tmp_path):
        """Backward compat: existing call sites pass no task_mode."""
        from tools.auto.outer_loop import make_outer_loop
        cfg = self._base_config()
        state = MagicMock()
        inner = MagicMock()
        ol = make_outer_loop(cfg, tmp_path, state, inner_loop=inner)
        assert ol is not None

    def test_forwards_task_mode_to_make_inner_loop(self, tmp_path):
        """When no inner_loop injected, make_outer_loop calls make_inner_loop(task_mode=...)."""
        from tools.auto.outer_loop import make_outer_loop
        cfg = self._base_config()
        cfg.set("auto", "max_rewrites", "0")
        state = MagicMock()

        with patch("tools.auto.outer_loop.make_inner_loop") as mock_mil:
            mock_mil.return_value = MagicMock()
            make_outer_loop(cfg, tmp_path, state, task_mode="docs")

        _, kwargs = mock_mil.call_args
        assert kwargs.get("task_mode") == "docs"


# ─────────────────────────────────────────────────────────────────────────────
# 5 — make_inner_loop accepts task_mode and stores it on validator
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeInnerLoopTaskMode:
    def _base_config(self) -> configparser.ConfigParser:
        cfg = configparser.ConfigParser()
        cfg.read_dict({
            "api": {"active": "local", "verify_ssl": "true"},
            "api_local": {
                "base_url":   "http://localhost:1337/v1",
                "api_key":    "jan",
                "model":      "test-model",
                "api_format": "openai",
                "num_ctx":    "0",
            },
            "auto":             {"max_attempts_per_task": "3", "exec_timeout_sec": "60"},
            "validator_agent":  {"temperature": "0.1", "max_hints": "3", "max_tokens": "512"},
        })
        return cfg

    def test_accepts_task_mode_kwarg(self, tmp_path):
        from tools.auto.inner_loop import make_inner_loop
        cfg = self._base_config()
        coder    = MagicMock()
        executor = MagicMock()
        il = make_inner_loop(cfg, tmp_path,
                             coder=coder, executor=executor,
                             task_mode="docs")
        assert il is not None

    def test_task_mode_stored_on_validator(self, tmp_path):
        from tools.auto.inner_loop import make_inner_loop
        cfg = self._base_config()
        coder    = MagicMock()
        executor = MagicMock()
        il = make_inner_loop(cfg, tmp_path,
                             coder=coder, executor=executor,
                             task_mode="creative")
        assert il.validator.task_mode == "creative"

    def test_default_task_mode_code_on_validator(self, tmp_path):
        from tools.auto.inner_loop import make_inner_loop
        cfg = self._base_config()
        coder    = MagicMock()
        executor = MagicMock()
        il = make_inner_loop(cfg, tmp_path, coder=coder, executor=executor)
        assert il.validator.task_mode == "code"

    def test_no_task_mode_kwarg_backward_compat(self, tmp_path):
        """Existing call sites that pass no task_mode must still work."""
        from tools.auto.inner_loop import make_inner_loop
        cfg = self._base_config()
        coder    = MagicMock()
        executor = MagicMock()
        il = make_inner_loop(cfg, tmp_path, coder=coder, executor=executor)
        assert il is not None

    def test_task_mode_forwarded_to_coder(self, tmp_path):
        """make_inner_loop must forward task_mode to the Coder, not only the Validator."""
        from tools.auto.inner_loop import make_inner_loop
        cfg = self._base_config()
        executor = MagicMock()
        il = make_inner_loop(cfg, tmp_path, executor=executor, task_mode="docs")
        assert il.coder._task_mode == "docs"


# ─────────────────────────────────────────────────────────────────────────────
# 6 — LLMGate2Validator accepts and stores task_mode
# ─────────────────────────────────────────────────────────────────────────────

class TestLLMGate2ValidatorTaskMode:
    def test_stores_docs_mode(self):
        from tools.auto.inner_loop import LLMGate2Validator
        v = LLMGate2Validator(task_mode="docs")
        assert v.task_mode == "docs"

    def test_stores_creative_mode(self):
        from tools.auto.inner_loop import LLMGate2Validator
        v = LLMGate2Validator(task_mode="creative")
        assert v.task_mode == "creative"

    def test_defaults_to_code(self):
        from tools.auto.inner_loop import LLMGate2Validator
        v = LLMGate2Validator()
        assert v.task_mode == "code"

    def test_backward_compat_no_kwarg(self):
        """Existing instantiation without task_mode must not raise."""
        from tools.auto.inner_loop import LLMGate2Validator
        v = LLMGate2Validator(
            base_url="http://localhost:1337/v1",
            model="test",
            api_key="x",
        )
        assert v.task_mode == "code"


# ─────────────────────────────────────────────────────────────────────────────
# Medium #1 — _StubCoder.generate() accepts prefetched_context kwarg
# ─────────────────────────────────────────────────────────────────────────────

class TestStubCoderSignature:
    """_StubCoder must accept prefetched_context so broken-env failures are clean."""

    def test_generate_accepts_prefetched_context_kwarg(self):
        """Must not raise TypeError when called the same way InnerLoop.run_task() does."""
        from tools.auto.inner_loop import _StubCoder
        stub = _StubCoder()
        result = stub.generate(
            task={},
            base_dir="/tmp",
            prior_feedback=None,
            prefetched_context="some context",
        )
        assert result.succeeded is False

    def test_generate_prefetched_context_defaults_to_empty_string(self):
        """Omitting prefetched_context must still work (backward compat)."""
        from tools.auto.inner_loop import _StubCoder
        stub = _StubCoder()
        result = stub.generate(task={}, base_dir="/tmp")
        assert result.succeeded is False

    def test_generate_returns_error_message(self):
        """Stub must communicate that no real coder is available."""
        from tools.auto.inner_loop import _StubCoder
        stub = _StubCoder()
        result = stub.generate(task={}, base_dir="/tmp", prefetched_context="ctx")
        assert result.error
        assert result.files_written == []
