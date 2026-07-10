"""tests/test_fix13_prompt_store_nested_dir.py — AUTO-FIX-13.

Bug found by comparing PromptStore._save() against the near-identical
atomic-write helper in MetricsCollector.record(): MetricsCollector creates
its parent directory (`dir_.mkdir(parents=True, exist_ok=True)`) before
calling tempfile.mkstemp(); PromptStore._save() called mkstemp directly.

If [prompt_store] store_path is configured to a nested path whose directory
doesn't exist yet (e.g. "state/prompts.json"), mkstemp raised
FileNotFoundError, caught by the generic `except Exception` and only
logged — push() returned normally with no error surfaced to the caller.
Confirmed via direct repro: pushing to a not-yet-existing nested directory
logged "PromptStore failed to write ..." and the file was never created;
the very next get_current() call re-loaded from disk, found nothing, and
silently served the stale/hardcoded prompt as though the push had never
happened.

Fix: mkdir(parents=True, exist_ok=True) on store_path.parent before
mkstemp, matching MetricsCollector's existing pattern.
"""

from __future__ import annotations

from pathlib import Path

from tools.prompt_store import PromptStore


class TestNestedDirectoryIsCreated:
    def test_push_creates_missing_nested_parent(self, tmp_path):
        store_path = tmp_path / "state" / "deeper" / "prompts.json"
        ps = PromptStore(store_path=store_path, max_versions=3)

        ps.push("validator_agent", "a new optimized prompt", 0.9)

        assert store_path.exists()

    def test_pushed_prompt_survives_reload_from_disk(self, tmp_path):
        store_path = tmp_path / "state" / "prompts.json"
        ps = PromptStore(store_path=store_path, max_versions=3)
        ps.push("validator_agent", "a new optimized prompt", 0.9)

        # A second, independent PromptStore pointed at the same path must
        # see the pushed prompt — proves it actually reached disk, not just
        # the in-memory `data` dict of the first instance.
        ps_reloaded = PromptStore(store_path=store_path, max_versions=3)
        assert ps_reloaded.get_current("validator_agent") == "a new optimized prompt"

    def test_get_current_immediately_after_push_is_consistent(self, tmp_path):
        store_path = tmp_path / "a" / "b" / "c" / "prompts.json"
        ps = PromptStore(store_path=store_path, max_versions=3)
        ps.push("validator_agent", "freshly optimized", 0.9)
        assert ps.get_current("validator_agent") == "freshly optimized"


class TestExistingDirectoryStillWorks:
    """The fix must not change behaviour when the directory already exists
    (the common case: default store_path="prompts.json", parent=".")."""

    def test_push_to_already_existing_dir(self, tmp_path):
        store_path = tmp_path / "prompts.json"
        ps = PromptStore(store_path=store_path, max_versions=3)
        ps.push("validator_agent", "v1", 0.5)
        assert ps.get_current("validator_agent") == "v1"

    def test_push_to_bare_filename_parent_dot(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ps = PromptStore(store_path=Path("prompts.json"), max_versions=3)
        ps.push("validator_agent", "v1", 0.5)
        assert (tmp_path / "prompts.json").exists()
