"""tests_bugfix/test_arena_cache_future_199.py — Bugs 4 and 7: future records must not stay in the cache."""
import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.arena import models as arena_models
from tools.contest import context_memory


def test_model_cache_drops_future_record(tmp_path):
    cache = arena_models.ModelCache(tmp_path / "cache.json", days=1.0)
    future_at = 100.0  # 100 seconds in the future
    cache.path.write_text(
        json.dumps([{"provider": "p", "model": "m", "free": "yes", "ctx": 1000, "at": future_at}]),
        encoding="utf-8",
    )
    assert cache.load(now=0.0) == []


def test_score_store_drops_future_record(tmp_path):
    store = arena_models.ScoreStore(tmp_path / "scores.json", days=1.0)
    future_at = 100.0  # 100 seconds in the future
    store.path.write_text(
        json.dumps([{"provider": "p", "model": "m", "score": 1, "max": 1, "error": "", "at": future_at}]),
        encoding="utf-8",
    )
    assert store.load(now=0.0) == []


def test_context_memory_drops_future_record(tmp_path):
    future_at = 100.0  # 100 seconds in the future
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps([{"at": future_at, "round": "r", "agent": "a", "provider": "p", "model": "m"}]),
        encoding="utf-8",
    )
    assert context_memory.load(path, now=0.0) == []


def test_model_cache_keeps_recent_record(tmp_path):
    cache = arena_models.ModelCache(tmp_path / "cache.json", days=1.0)
    cache.path.write_text(
        json.dumps([{"provider": "p", "model": "m", "free": "yes", "ctx": 1000, "at": 0.0}]),
        encoding="utf-8",
    )
    assert len(cache.load(now=1.0)) == 1


def test_score_store_keeps_recent_record(tmp_path):
    store = arena_models.ScoreStore(tmp_path / "scores.json", days=1.0)
    store.path.write_text(
        json.dumps([{"provider": "p", "model": "m", "score": 1, "max": 1, "error": "", "at": 0.0}]),
        encoding="utf-8",
    )
    assert len(store.load(now=1.0)) == 1


def test_context_memory_keeps_recent_record(tmp_path):
    path = tmp_path / "mem.json"
    path.write_text(
        json.dumps([{"at": 0.0, "round": "r", "agent": "a", "provider": "p", "model": "m"}]),
        encoding="utf-8",
    )
    assert len(context_memory.load(path, now=1.0)) == 1
