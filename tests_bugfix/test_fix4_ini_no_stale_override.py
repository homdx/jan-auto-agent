"""tests/test_fix4_ini_no_stale_override.py — AUTO-FIX-4.

Bug (report finding #4 — the most severe of the batch): every shipped .ini
(``agents.ini``, ``agents_4k.ini``, ``agents_32k*.ini``, ``agents_128k.ini``)
set ``[validator_agent] system_creative`` to a stale one-line verdict prompt
("Reply with ONE line: APPROVED, or REVISE: <one concrete reason>"). Because
``_resolve_validator_system()`` gives an explicit mode-specific override
priority over the built-in constant for ANY task_mode, this silently
overrode the richer, actively-maintained ``_GATE2_SYSTEM_CREATIVE`` (numbered
multi-point critique, anti-paste-loop rule — CR-29/CR-32) in every real
creative run, regardless of how much that built-in improved.

Fix: remove the override from all shipped configs so the built-in wins,
per ``_resolve_validator_system``'s own documented priority. This test pins
that state so a future edit can't silently reintroduce it.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.inner_loop import _GATE2_SYSTEM_CREATIVE, _resolve_validator_system

REPO_ROOT = Path(__file__).resolve().parent.parent

ALL_INIS = [
    "agents.ini",
    "agents_4k.ini",
    "agents_32k.ini",
    "agents_32k_slow_cpu.ini",
    "agents_32k_fast_cpu.ini",
    "agents_128k.ini",
]


@pytest.mark.parametrize("ini_name", ALL_INIS)
def test_no_shipped_ini_overrides_system_creative(ini_name):
    path = REPO_ROOT / ini_name
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(path, encoding="utf-8")
    assert not cfg.has_option("validator_agent", "system_creative"), (
        f"{ini_name} re-introduces a system_creative override, which silently "
        "shadows the built-in _GATE2_SYSTEM_CREATIVE for every creative run"
    )


@pytest.mark.parametrize("ini_name", ALL_INIS)
def test_creative_mode_resolves_to_builtin_numbered_prompt(ini_name):
    path = REPO_ROOT / ini_name
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(path, encoding="utf-8")
    resolved = _resolve_validator_system(cfg, "creative")
    assert resolved == _GATE2_SYSTEM_CREATIVE
    assert "NUMBERED LIST" in resolved and "EVERY problem" in resolved


@pytest.mark.parametrize("ini_name", ALL_INIS)
def test_validator_max_tokens_has_headroom_for_numbered_list(ini_name):
    """AUTO-FIX-6: max_tokens must be comfortably above the old 200/350/500
    code-mode sizing now that creative mode uses a multi-point Cyrillic
    critique instead of a one-line verdict."""
    path = REPO_ROOT / ini_name
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(path, encoding="utf-8")
    max_tokens = cfg.getint("validator_agent", "max_tokens")
    assert max_tokens >= 500, (
        f"{ini_name}: validator max_tokens={max_tokens} is too low for a "
        "numbered multi-point Cyrillic critique"
    )
