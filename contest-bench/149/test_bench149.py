"""Round 149 bench: a loose remembered window must not override Kilo's declared one."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_contest_context_memory as ctm  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from tools.contest import cli as contest_cli  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402
from tools.contest.runner import _context_budget  # noqa: E402


def _rec(last_ok, grew):
    return ctm._record(limit=None, last_ok=last_ok, prompt=None, grew=grew)


def _spec(declared):
    return replace(tr.make_config(["agent-a"]).agents[0], context_limit=declared)


def _cfg(tmp_path, records=(), declared=None, **over):
    path = tmp_path / "context-memory.json"
    for r in records:
        assert cm.add(path, r) is True
    config = ctm._config(tmp_path, memory=path, **over)
    if declared is not None:
        config = replace(config, agents=tuple(replace(a, context_limit=declared)
                                              for a in config.agents))
    return config


def _budget(declared, records, config):
    return _context_budget(_spec(declared), [r.to_dict() for r in records], config)


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)


# 1
def test_loose_33k_under_kilo_262k_is_kilo(tmp_path):
    records = [_rec(33_000, 200_000)]
    config = _cfg(tmp_path, records, declared=262_144)
    assert _budget(262_144, records, config) == (262_144, "kilo")


def test_loose_33k_overlay_carries_nothing(tmp_path):
    records = [_rec(33_000, 200_000)]
    config = _cfg(tmp_path, records, declared=262_144)
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out", None)
    assert out.agents[0].context_limit == 262_144
    assert content is None or "agent-a" not in json.dumps(json.loads(content))


def test_loose_33k_overlay_carries_nothing_without_kilo(tmp_path):
    records = [_rec(33_000, 200_000)]
    config = _cfg(tmp_path, records)
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out", None)
    assert not out.agents[0].context_limit
    assert content is None


# 2
def test_loose_no_kilo_with_fallback_sizes_nothing(tmp_path):
    records = [_rec(33_000, 200_000)]
    config = _cfg(tmp_path, records, context_limit_fallback=128_000)
    assert _budget(None, records, config) == (128_000, "fallback")


def test_loose_no_kilo_no_fallback_sizes_nothing(tmp_path):
    records = [_rec(33_000, 200_000)]
    config = _cfg(tmp_path, records, context_limit_fallback=0)
    assert _budget(None, records, config) == (None, "none")


def test_loose_near_fallback_wall_counts(tmp_path):
    records = [_rec(90_000, 200_000)]
    config = _cfg(tmp_path, records, context_limit_fallback=128_000)
    assert _budget(None, records, config) == (90_000, "remembered")


# 3
def test_glm_loose_81k_under_131k_counts(tmp_path):
    records = [_rec(81_311, 42_000)]
    config = _cfg(tmp_path, records, declared=131_072)
    assert _budget(131_072, records, config) == (81_311, "remembered")


def test_glm_overlay_hands_81k(tmp_path):
    records = [_rec(81_311, 42_000)]
    config = _cfg(tmp_path, records, declared=131_072)
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out", None)
    assert out.agents[0].context_limit == 81_311
    assert content is not None


def test_loose_just_below_share_does_not_count(tmp_path):
    records = [_rec(78_000, 42_000)]  # 59.5 % of 131 072
    config = _cfg(tmp_path, records, declared=131_072)
    assert _budget(131_072, records, config) == (131_072, "kilo")


def test_share_follows_full_refusal_percent(tmp_path):
    records = [_rec(78_000, 42_000)]
    config = _cfg(tmp_path, records, declared=131_072, context_full_refusal_percent=50)
    assert _budget(131_072, records, config) == (78_000, "remembered")


# 4
def test_tight_40k_under_131k_sizes(tmp_path):
    records = [_rec(40_000, 1_000)]
    config = _cfg(tmp_path, records, declared=131_072)
    assert _budget(131_072, records, config) == (40_000, "remembered")


def test_tight_no_grew_sizes(tmp_path):
    records = [_rec(40_000, None)]
    config = _cfg(tmp_path, records, declared=131_072)
    assert _budget(131_072, records, config) == (40_000, "remembered")


def test_tight_below_floor_with_big_kilo_dropped(tmp_path):
    records = [_rec(20_000, 1_000)]
    config = _cfg(tmp_path, records, declared=131_072)
    assert _budget(131_072, records, config) == (131_072, "kilo")


# 5
def test_small_window_tight_28k_sizes(tmp_path):
    records = [_rec(28_000, 1_000)]
    config = _cfg(tmp_path, records, declared=32_768)
    assert _budget(32_768, records, config) == (28_000, "remembered")


def test_small_window_overlay_hands_28k(tmp_path):
    records = [_rec(28_000, 1_000)]
    config = _cfg(tmp_path, records, declared=32_768)
    out, _content = contest_cli._with_remembered_limits(config, tmp_path / "out", None)
    assert out.agents[0].context_limit == 28_000


def test_no_kilo_tight_below_floor_still_dropped(tmp_path):
    records = [_rec(28_000, 1_000)]
    config = _cfg(tmp_path, records, context_limit_fallback=0)
    assert _budget(None, records, config) == (None, "none")


# 7
def test_equal_to_kilo_is_kilo(tmp_path):
    records = [_rec(131_072, 1_000)]
    config = _cfg(tmp_path, records, declared=131_072)
    assert _budget(131_072, records, config) == (131_072, "kilo")


# KC-73 + named limits untouched
def test_named_limit_untouched(tmp_path):
    records = [ctm._record(limit=40_000, last_ok=None, prompt=None, grew=200_000)]
    config = _cfg(tmp_path, records, declared=262_144)
    assert _budget(262_144, records, config) == (40_000, "remembered")


def test_largest_loose_does_not_shadow_tight(tmp_path):
    records = [_rec(40_000, 1_000), _rec(50_000, 200_000)]
    config = _cfg(tmp_path, records, declared=262_144)
    assert _budget(262_144, records, config)[0] == 40_000


# cleanup
def test_ini_comment_mentions_loose_records():
    text = (ROOT / "contest.ini").read_text()
    i = text.find("context_full_refusal_percent")
    assert i >= 0
    block = text[max(0, i - 2000):i + 200].lower()
    assert "loose" in block or "record" in block


def test_overlay_docstring_line_width():
    import inspect
    src = inspect.getsource(contest_cli._with_remembered_limits)
    assert max(len(l) for l in src.splitlines()) <= 120
    assert "A model intake knows the size of keeps" not in src
