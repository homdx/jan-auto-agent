"""tests/test_cr12_generation_fixes.py — AUTO-CR-12.

Three field bugs from the first working generation run:
  1. ContextAssembler found no prior chapters because the coder globbed
     chapter_*.md only — a .txt project had no "story so far", so the model
     drifted to English and had nothing to continue.
  2. acceptance_check "true" is a Unix builtin; on Windows it returns rc=1, so
     EVERY creative task failed the executor gate.
  3. The fail-open prose parser wrote model refusals ("I cannot fulfill…") into
     the chapter file as if they were prose.
"""

from __future__ import annotations

import configparser

import pytest

from tools.auto.coder import _looks_like_refusal, Coder
from tools.auto.executor import Executor


# ── Bug 3: refusal guard ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "{}", "[]",
    "I cannot fulfill your request. Please provide more context.",
    "I don't see any information about Chapter 1.",
    "It seems there's a misunderstanding, provide more context or clarify what...",
])
def test_refusal_detected(text):
    assert _looks_like_refusal(text) is True


@pytest.mark.parametrize("text", [
    "Капитан Рейес стояла на мостике. Ветер рвал паруса, и Мира дрожала от холода.",
    "Chapter 2: The storm broke at dawn and the crew scrambled across the deck.",
])
def test_real_prose_not_flagged(text):
    assert _looks_like_refusal(text) is False


def test_long_prose_with_quote_not_flagged():
    body = ("Chapter 2. " + "The sea churned beneath them. " * 30 +
            '"I cannot fulfill that promise," she whispered to the captain.')
    assert len(body) >= 600
    assert _looks_like_refusal(body) is False


# ── Bug 2: cross-platform no-op acceptance ───────────────────────────────────

def _executor():
    cfg = configparser.ConfigParser()
    cfg.add_section("executor")
    return Executor(timeout_sec=10, base_dir=".")


@pytest.mark.parametrize("cmd,expected_pass", [
    ("true", True), ("True", True), (":", True),
    ("false", False),
])
def test_noop_acceptance_cross_platform(tmp_path, cmd, expected_pass):
    ex = Executor(timeout_sec=10, base_dir=str(tmp_path))
    res = ex.run({"id": "t1", "acceptance_check": cmd, "target_files": []})
    assert res.passed is expected_pass


# ── Bug 1: chapter discovery includes .txt ───────────────────────────────────

def test_creative_context_finds_txt_predecessor(tmp_path):
    (tmp_path / "chapter_1.txt").write_text(
        "Капитан Рейес стояла на мостике «Альбатроса».", encoding="utf-8"
    )
    cfg = configparser.ConfigParser()
    for sec in ("coder", "api", "api_local"):
        cfg.add_section(sec)
    cfg.set("api", "active", "local")
    cfg.set("api_local", "num_ctx", "8192")
    c = Coder(config=cfg, base_url="http://x", api_key="x", model="m",
              api_format="ollama", task_mode="creative")

    ctx = c._build_creative_file_contents(["chapter_2.txt"], tmp_path)
    # The Russian predecessor must now be in the assembled context.
    assert "Рейес" in ctx
    assert "no prior chapters" not in ctx
