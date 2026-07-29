"""tests/test_collect_module_incremental.py — COLLECT-module-LLM bug.

`--module <path>` is documented as an incremental action: it re-scans and
re-summarises exactly ONE file, reusing every other module's record verbatim
from the existing artifact.

Before the fix, `action_module` forwarded the whole `modules` list (all N
files loaded from artifact.json, plus the freshly-scanned patched record) to
`build_context(..., llm_call=llm_call)`, which in turn handed it unchanged to
`summarize_repo`.  `summarize_repo` has no guard for modules that already
carry a `summary` — it only skips parse-error modules and checkpoint hits —
so it called the LLM once per module, burning O(N) API calls where exactly
1 was needed.

This file verifies:
  1. The LLM is called exactly once (for the patched module) during
     `action_module`, regardless of how many other modules already have
     summaries in the artifact.
  2. Unchanged modules keep their previous summary byte-for-byte.
  3. The patched module gets a fresh summary from this run.
  4. `action_module` on a module with a parse_error makes zero LLM calls
     (same fail-open posture as `action_refresh`).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect.cli import (
    ARTIFACT_FILENAME,
    action_collect,
    action_module,
    action_refresh,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


def _artifact_modules(collect_dir: Path) -> dict:
    payload = json.loads((collect_dir / ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    return {m["path"]: m for m in payload["modules"]}


def _counting_llm_call(purpose_prefix: str = "stub purpose") -> object:
    """Fake `LlmCall` that records every call and returns a minimal JSON summary."""
    calls: list = []

    def _call(system: str, user: str) -> str:
        calls.append((system, user))
        # Return a valid summarizer JSON payload so Pass B doesn't retry.
        return json.dumps({"purpose": f"{purpose_prefix} for {user[:30]}", "notes": ""})

    _call.calls = calls
    return _call


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """Neutralise seed contracts/gates that reference real jan-auto-agent
    symbols not present in these synthetic mini repos."""
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def multi_module_repo(tmp_path: Path) -> Path:
    """Three-file repo so we can assert the LLM is NOT called for the two
    untouched files when --module patches just one."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    (tmp_path / "pkg" / "c.py").write_text("def c():\n    return 3\n")
    _init_repo(tmp_path)
    return tmp_path


# ── core LLM-call-count tests ─────────────────────────────────────────────────

class TestModuleIncrementalLlmCalls:
    """The key invariant: --module calls the LLM exactly once (for the patched
    file), never once-per-file-in-the-repo."""

    def test_module_calls_llm_exactly_once_not_n_times(self, multi_module_repo):
        """Bug reproduction: before the fix this would call the LLM 4 times
        (once per module including __init__.py), not once."""
        llm = _counting_llm_call()
        first = action_collect(multi_module_repo, llm_call=llm)
        n_files = len(_artifact_modules(first.collect_dir))
        assert n_files >= 3, "sanity: repo has at least 3 modules"

        llm.calls.clear()  # reset counter — only action_module calls matter

        (multi_module_repo / "pkg" / "a.py").write_text(
            "def a():\n    return 42\n\ndef extra():\n    pass\n"
        )
        action_module(multi_module_repo, "pkg/a.py", llm_call=llm)

        assert len(llm.calls) == 1, (
            f"Expected exactly 1 LLM call (for pkg/a.py), got {len(llm.calls)}. "
            f"Before the fix this was {n_files} — one per file in the repo."
        )

    def test_module_llm_call_is_for_the_patched_file(self, multi_module_repo):
        """The single LLM call must contain the patched file's path/source,
        not some other module's content."""
        llm = _counting_llm_call()
        action_collect(multi_module_repo, llm_call=llm)
        llm.calls.clear()

        (multi_module_repo / "pkg" / "b.py").write_text(
            "def b():\n    return 99\n\ndef helper():\n    pass\n"
        )
        action_module(multi_module_repo, "pkg/b.py", llm_call=llm)

        assert len(llm.calls) == 1
        _system, user_msg = llm.calls[0]
        assert "pkg/b.py" in user_msg, (
            f"Expected the LLM user message to mention pkg/b.py, got: {user_msg[:120]!r}"
        )

    def test_module_with_parse_error_makes_no_llm_calls(self, multi_module_repo):
        """A file that fails to parse must not trigger an LLM call —
        same fail-open posture as action_refresh already has."""
        llm = _counting_llm_call()
        action_collect(multi_module_repo, llm_call=llm)
        llm.calls.clear()

        (multi_module_repo / "pkg" / "a.py").write_text("def (\n")  # syntax error
        action_module(multi_module_repo, "pkg/a.py", llm_call=llm)

        assert llm.calls == [], (
            f"Expected zero LLM calls for a parse-error module, got {len(llm.calls)}"
        )


# ── summary-content correctness tests ────────────────────────────────────────

class TestModuleIncrementalSummaryContent:
    """Beyond call-count: unchanged modules must keep their old summary;
    the patched module must get a fresh one."""

    def test_unchanged_modules_keep_their_summary(self, multi_module_repo):
        llm = _counting_llm_call()
        first = action_collect(multi_module_repo, llm_call=llm)
        before = _artifact_modules(first.collect_dir)

        (multi_module_repo / "pkg" / "a.py").write_text(
            "def a():\n    return 42\n"
        )
        result = action_module(multi_module_repo, "pkg/a.py", llm_call=llm)
        after = _artifact_modules(result.collect_dir)

        # b.py and c.py untouched — their summary must be byte-identical
        for path in ("pkg/b.py", "pkg/c.py"):
            assert after[path]["summary"] == before[path]["summary"], (
                f"{path} summary changed even though the file was not patched"
            )

    def test_patched_module_gets_fresh_summary(self, multi_module_repo):
        llm = _counting_llm_call(purpose_prefix="INITIAL")
        action_collect(multi_module_repo, llm_call=llm)

        # Switch to a different prefix so we can tell which run produced
        # the summary in the final artifact.
        llm2 = _counting_llm_call(purpose_prefix="PATCHED")
        (multi_module_repo / "pkg" / "a.py").write_text(
            "def a():\n    return 42\n"
        )
        result = action_module(multi_module_repo, "pkg/a.py", llm_call=llm2)
        after = _artifact_modules(result.collect_dir)

        assert after["pkg/a.py"]["summary"] is not None
        assert "PATCHED" in after["pkg/a.py"]["summary"]["purpose"], (
            "Expected the patched module's summary to come from the action_module "
            "LLM call, not the earlier action_collect call."
        )


class TestModuleRunsVerification:
    """Bug reproduction: `action_module` called `build_context(...,
    llm_call=None)` (Pass B already ran separately above it) but, unlike
    `action_refresh`, never re-ran `verifier_mod.verify_repo` afterward —
    so `build_context`'s own `if llm_call is not None` guard meant Pass C
    silently never ran for `--module` at all. A fabricated citation in the
    freshly-summarized module's `purpose`/`notes` would survive straight
    into artifact.json, unlike `--refresh`, which strips it.
    """

    def _fabricating_llm_call(self, system: str, user: str) -> str:
        return json.dumps(
            {"purpose": "Relies on pkg/a.py:totally_invented_helper for its logic.", "notes": ""}
        )

    def test_module_strips_fabricated_citation_like_refresh_does(self, multi_module_repo):
        action_collect(multi_module_repo, llm_call=self._fabricating_llm_call)

        (multi_module_repo / "pkg" / "a.py").write_text(
            "def a():\n    return 42\n"
        )
        result = action_module(
            multi_module_repo, "pkg/a.py", llm_call=self._fabricating_llm_call
        )
        after = _artifact_modules(result.collect_dir)

        purpose = after["pkg/a.py"]["summary"]["purpose"]
        assert "totally_invented_helper" not in purpose, (
            "action_module must run Pass C (verify_repo) the same way "
            "action_refresh does, dropping a fabricated citation instead "
            f"of writing it straight into the artifact. Got: {purpose!r}"
        )
