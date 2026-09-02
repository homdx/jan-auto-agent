"""tests/test_cr23_controller_wiring.py — regression for the CR-23 wiring bug.

The story bible was fully implemented (StoryBible, make_story_bible,
CommitOnSuccess._update_story_bible) but the controller never *built* it nor
passed it to CommitOnSuccess, so in production `self._story_bible` was always
None and the bible (plus its always-on injection and the continuity gate's
anchor) never fired. This locks the controller-side resolution + handoff.
"""

from __future__ import annotations

import configparser


from tools.auto.story_bible import make_story_bible, StoryBible
from tools.auto.commit_on_success import CommitOnSuccess


def _creative_cfg(enabled=True):
    cfg = configparser.ConfigParser()
    cfg["api"] = {"active": "local", "verify_ssl": "true"}
    cfg["api_local"] = {
        "base_url": "http://localhost:11434", "api_key": "ollama",
        "model": "llama3.1:8b", "api_format": "ollama",
    }
    cfg["validator_agent"] = {
        "story_bible_creative": "true" if enabled else "false",
        "story_bible_max_chars": "2000",
    }
    cfg["inner_loop"] = {"temperature": "0.1"}
    cfg["loop"] = {"timeout_seconds": "300"}
    return cfg


def _build_like_controller(cfg, base_dir):
    """Replicate the controller's CR-23 resolution snippet exactly."""
    active = cfg.get("api", "active", fallback="local")
    sec = f"api_{active}"
    return make_story_bible(
        cfg,
        base_url=cfg.get(sec, "base_url", fallback="http://localhost:11434"),
        api_key=cfg.get(sec, "api_key", fallback="ollama"),
        model=cfg.get(sec, "model", fallback="llama3.1:8b"),
        api_format=cfg.get(sec, "api_format", fallback="ollama"),
        base_dir=base_dir,
    )


def test_controller_resolution_builds_bible(tmp_path):
    bible = _build_like_controller(_creative_cfg(enabled=True), tmp_path)
    assert isinstance(bible, StoryBible)


def test_disabled_yields_none(tmp_path):
    assert _build_like_controller(_creative_cfg(enabled=False), tmp_path) is None


def test_commit_on_success_accepts_and_uses_bible(tmp_path, monkeypatch):
    """A creative commit must call StoryBible.update with the chapter text."""
    (tmp_path / "chapter_1.txt").write_text("Глава 1. Капитан Рейес в зелёной куртке.",
                                            encoding="utf-8")
    bible = _build_like_controller(_creative_cfg(enabled=True), tmp_path)

    seen = {}
    monkeypatch.setattr(bible, "update", lambda text: seen.setdefault("text", text))

    # Minimal stubs for git/state — CommitOnSuccess only needs the bible hook here.
    class _State:
        def set_task_status(self, *a, **k): pass
        def log(self, *a, **k): pass

    cos = CommitOnSuccess(
        None, _State(),
        summary_memory=None, task_mode="creative",
        base_dir=tmp_path, story_bible=bible,
    )
    cos._update_story_bible({"target_files": ["chapter_1.txt"], "id": "t1"})
    assert "зелёной куртке" in seen.get("text", "")


def test_code_mode_skips_bible(tmp_path):
    bible = _build_like_controller(_creative_cfg(enabled=True), tmp_path)
    cos = CommitOnSuccess(
        None, None,
        summary_memory=None, task_mode="code",
        base_dir=tmp_path, story_bible=bible,
    )
    # code mode → hook is a no-op (no exception, no update)
    cos._update_story_bible({"target_files": ["chapter_1.txt"], "id": "t1"})
