r"""tests_bugfix/test_bugfix_target_path_traversal.py

Bug A1 (consolidated bug report):

  main.py:439 used

      target_path = os.path.normpath(os.path.join(base_dir, parsed.file_path))
      if not os.path.exists(target_path) or not os.path.isfile(target_path):
          print(f"Error: Target path is not a valid file: '{parsed.file_path}'")
          return

  `parsed.file_path` is user-controlled text from parse_prompt(). A
  prompt like 'improve foo in ../../etc/passwd' makes normpath
  collapse '..' segments and produce a path outside base_dir.
  Downstream code then reads/writes outside the project root
  (file_reader.read_file, run_edit, _save_history) — the only
  security bug in the file.

Fix:

  Add a static helper resolve_target_path(base_dir, parsed_file_path)
  that
    1. Resolves both paths via os.path.realpath().
    2. Verifies the resolved target is INSIDE the resolved base_dir
       (or equal to it) via os.path.commonpath.
    3. Returns the absolute resolved target string on success.
    4. Returns None on any traversal attempt (different drives on
       Windows raise ValueError from commonpath — also None).

  Update the run_pipeline call site to use the helper. Refuse to
  read the file when the helper returns None.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


# ── Import the helper from main.py ─────────────────────────────────────

def _helper():
    """Lazy import so the test can be collected even if main.py
    fails to import (e.g. missing optional deps in CI)."""
    import main as main_mod
    return main_mod.resolve_target_path


# ── Direct unit tests of resolve_target_path ───────────────────────────

class TestResolveTargetPath:
    """Pure unit tests for the path-resolution helper."""

    def test_relative_path_inside_base(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("# empty\n", encoding="utf-8")
        resolve = _helper()
        result = resolve(str(tmp_path), "app.py")
        assert result == str(f.resolve())

    def test_relative_subdir_inside_base(self, tmp_path):
        sub = tmp_path / "pkg"
        sub.mkdir()
        f = sub / "mod.py"
        f.write_text("# empty\n", encoding="utf-8")
        resolve = _helper()
        result = resolve(str(tmp_path), "pkg/mod.py")
        assert result == str(f.resolve())

    def test_relative_path_with_dotdot_inside_base(self, tmp_path):
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        f = sub / "mod.py"
        f.write_text("# empty\n", encoding="utf-8")
        # a/b/../b/mod.py resolves inside base — must succeed.
        resolve = _helper()
        result = resolve(str(tmp_path), "a/b/../b/mod.py")
        assert result == str(f.resolve())

    def test_absolute_path_inside_base_is_accepted(self, tmp_path):
        f = tmp_path / "app.py"
        f.write_text("# empty\n", encoding="utf-8")
        resolve = _helper()
        result = resolve(str(tmp_path), str(f))
        assert result == str(f.resolve())

    def test_dotdot_traversal_above_base_is_refused(self, tmp_path):
        resolve = _helper()
        # ../<sibling>/file.py must NOT resolve inside base.
        result = resolve(str(tmp_path), "../sibling/file.py")
        assert result is None, (
            f"expected None on path traversal, got {result!r}"
        )

    def test_dotdot_traversal_to_system_path_is_refused(self, tmp_path):
        resolve = _helper()
        # /etc/passwd absolute → must NOT resolve inside base.
        result = resolve(str(tmp_path), "/etc/passwd")
        assert result is None

    def test_dotdot_traversal_via_nested_dotdot_is_refused(self, tmp_path):
        resolve = _helper()
        # pkg/../../escape.py — even with a valid-looking prefix the
        # trailing .. escapes.
        result = resolve(str(tmp_path), "pkg/../../escape.py")
        assert result is None

    def test_dotdot_traversal_via_sibling_dir_is_refused(self, tmp_path):
        """A relative path that goes sideways via '..' to a file
        under a sibling directory of base must also be rejected."""
        # Create base and sibling as actual directories under a parent.
        parent = tmp_path
        base = parent / "base"
        sibling = parent / "sibling"
        base.mkdir()
        sibling.mkdir()
        sibling_file = sibling / "secret.py"
        sibling_file.write_text("# secret\n", encoding="utf-8")

        resolve = _helper()
        result = resolve(str(base), "../sibling/secret.py")
        assert result is None

    def test_absolute_path_to_outside_base_is_refused(self, tmp_path):
        """A bare absolute path that lands outside base must be
        refused even if the file does exist."""
        # Build a sibling tree so we have an absolute path outside base.
        sibling = tmp_path / "sibling"
        sibling.mkdir()
        outside = sibling / "thing.py"
        outside.write_text("# outside\n", encoding="utf-8")

        base = tmp_path / "base"
        base.mkdir()

        resolve = _helper()
        result = resolve(str(base), str(outside))
        assert result is None, (
            f"absolute path outside base must be refused; got {result!r}"
        )

    def test_returns_resolved_path_string(self, tmp_path):
        """When allowed, the helper returns an absolute resolved
        string — never a relative one."""
        (tmp_path / "app.py").write_text("# empty\n", encoding="utf-8")
        resolve = _helper()
        result = resolve(str(tmp_path), "app.py")
        assert os.path.isabs(result)
        assert os.path.realpath(result) == result

    def test_empty_path_returns_none(self, tmp_path):
        resolve = _helper()
        assert resolve(str(tmp_path), "") is None

    def test_dot_returns_base_itself(self, tmp_path):
        """A bare '.' should resolve to base_dir itself (which is
        contained)."""
        resolve = _helper()
        result = resolve(str(tmp_path), ".")
        assert result is not None
        assert os.path.realpath(result) == os.path.realpath(str(tmp_path))


# ── Integration with run_pipeline ──────────────────────────────────────

class TestRunPipelineRefusesTraversal:
    """End-to-end: Orchestrator.run_pipeline('improve foo in
    ../../../etc/passwd') must NOT actually open /etc/passwd. We
    mock file_reader.read_file so any successful resolution +
    invocation would raise (we'd see the patched call)."""

    def test_run_pipeline_refuses_traversal(self, tmp_path, monkeypatch,
                                            capsys):
        # Set up a minimal project under tmp_path.
        f = tmp_path / "inside.py"
        f.write_text("# inside\n", encoding="utf-8")

        # Build a real Orchestrator instance — its __init__ builds
        # agents from config; for this test we only need the
        # run_pipeline path up to file_reader.read_file, so we
        # substitute a tiny stub class that exposes run_pipeline
        # only (avoid loading the full agent stack).
        import main as main_mod

        # Stub file_reader.read_file to raise if called on a path
        # outside tmp_path. If the security check is in place, the
        # helper returns None and read_file is never called.
        from tools import file_reader as real_file_reader

        def boom(path):
            raise AssertionError(
                f"file_reader.read_file was called with {path!r} — "
                "the path-traversal check did not stop it"
            )

        monkeypatch.setattr(real_file_reader, "read_file", boom)

        # Build an Orchestrator-like stub that exposes just
        # run_pipeline, calling the real run_pipeline code path
        # enough to exercise the target_path check.
        from tools.prompt_parser import parse_prompt
        from tools.prompt_parser import ParsedPrompt

        class _StubOrch:
            # Bind the real run_pipeline as a method so its
            # `self`-based access works.
            run_pipeline = main_mod.Orchestrator.run_pipeline
            model = "x"
            base_url = "http://fake"
            api_key = ""
            timeout_seconds = 5
            stream_agents = False
            ssl_context = None
            api_format = "openai"
            config = None
            search_full_file_max_chars = 50000

            def __init__(self):
                self._direct_chat_history = []

        orch = _StubOrch()
        # Attack vector: prompt that parse_prompt parses into a
        # '..'-prefixed file_path. The simplest one that the
        # regex-driven parser accepts is "show in <relative path>"
        # — see test_probe above.
        attack = "show in ../outside/secret.py"
        # run_pipeline prints an error and returns; it must NOT
        # raise (which would happen if read_file got called via the
        # boom() patched function above).
        orch.run_pipeline(attack, str(tmp_path))

        out = capsys.readouterr().out
        # The friendly error from run_pipeline or the helper should
        # appear in stdout; what matters is that no exception
        # escaped (the patched read_file would have raised).
        assert "Traceback" not in out
        # And the friendly rejection message must mention
        # 'outside the project' (from resolve_target_path) or
        # 'not a valid file'.
        assert ("outside the project" in out
            or "not a valid file" in out), (
            f"expected a refusal message in stdout; got:\n{out}"
        )