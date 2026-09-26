"""KC-11 — variant probe cache: `probe_model`, `load_probe_cache`, `save_probe_cache`.

Tests are against `_kilo_fake` using its `reject_variants` scenario key — the
same shape KC-49 uses for the live probe. The cache path is a tmp_path file.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _kilo_fake import FakeKiloServer

from tools.contest.kilo_client import KiloServer
from tools.contest.think_probe import (
    PROBE_CACHE_FILE,
    ProbeResult,
    load_probe_cache,
    probe_model,
    save_probe_cache,
)
from tools.contest.variant import hello_probe

# ── helpers ──────────────────────────────────────────────────────────────────

REJECTED = {"name": "APIError",
            "data": {"message": "the model's provider rejected the request. check the "
                                "model id, request fields, and context length",
                     "statusCode": 400, "isRetryable": False}}


def _attached(scenario, tmp_path):
    fake = FakeKiloServer(scenario, directory=str(tmp_path)).start()
    return fake, KiloServer.attach(fake.url)


def _model(model_id, variants=(), reasoning=None):
    has_reasoning = bool(variants) if reasoning is None else reasoning
    return {"id": model_id, "status": "active",
            "capabilities": {"reasoning": has_reasoning, "toolcall": True},
            "variants": {name: {"reasoningEffort": name} for name in variants}}


def _offer_providers(models, provider_id="kenary"):
    return {"all": [{"id": provider_id, "name": provider_id, "source": "static",
                     "models": {m["id"]: m for m in models}}],
            "default": {}, "connected": [provider_id], "failed": []}


# ── pure cache functions ──────────────────────────────────────────────────────

def test_load_probe_cache_returns_empty_dict_for_missing_file(tmp_path):
    result = load_probe_cache(str(tmp_path / "no-such-file.json"))
    assert result == {}


def test_load_probe_cache_returns_empty_dict_for_malformed_file(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not json {{{{")
    assert load_probe_cache(str(p)) == {}


def test_save_and_reload_roundtrip(tmp_path):
    p = tmp_path / "probe.json"
    cache = {"kenary/hy3:free": {"variant": "high", "probed_at": 1.0,
                                  "kilo_version": "7.6.2", "tried": [], "reasoning_tokens": 0}}
    save_probe_cache(str(p), cache)
    assert p.exists()
    loaded = load_probe_cache(str(p))
    assert loaded == cache


def test_save_probe_cache_is_atomic_tmp_is_not_left_behind(tmp_path):
    p = tmp_path / "probe.json"
    save_probe_cache(str(p), {"k": "v"})
    assert not (tmp_path / "probe.json.tmp").exists()


# ── probe_model without a server (no reasoning) ──────────────────────────────

def test_probe_model_no_reasoning_returns_unusable_without_probing(tmp_path):
    """A model with no listed variants skips the probe entirely."""
    called = []

    def client_factory(variant):
        called.append(variant)
        return None

    result = probe_model(client_factory, "kenary", "plain:free", [],
                         cache={}, cache_path=None)
    assert not result.usable
    assert result.variant is None
    assert result.tried == []
    assert called == []  # no session opened


# ── probe_model cache TTL ─────────────────────────────────────────────────────

def test_probe_model_cache_hit_skips_the_probe(tmp_path):
    """A fresh cache entry means no session is created."""
    p = tmp_path / PROBE_CACHE_FILE
    cache = {
        "kenary/hy3:free": {
            "variant": "high",
            "usable": True,
            "probed_at": time.time() - 86400,  # 1 day ago, within default 7-day TTL
            "kilo_version": "",
            "tried": [["max", "http 400"]],
            "reasoning_tokens": 0,
        }
    }
    called = []

    def client_factory(variant):
        called.append(variant)
        return None

    result = probe_model(client_factory, "kenary", "hy3:free",
                         ["max", "high", "medium"],
                         cache=cache, cache_path=str(p), ttl_days=7)

    assert result.variant == "high"
    assert result.usable
    assert result.tried == [("max", "http 400")]
    assert result.elapsed == 0.0
    assert called == []  # no live probe


def test_probe_model_stale_cache_reprobes(tmp_path):
    """An entry older than the TTL is re-probed."""
    p = tmp_path / PROBE_CACHE_FILE
    cache = {
        "kenary/hy3:free": {
            "variant": "high",
            "usable": True,
            "probed_at": time.time() - 86400 * 10,  # 10 days ago
            "kilo_version": "",
            "tried": [],
            "reasoning_tokens": 0,
        }
    }
    called = []

    def client_factory(variant):
        called.append(variant)
        return None if variant == "high" else "rejected"

    result = probe_model(client_factory, "kenary", "hy3:free",
                         ["max", "high"],
                         cache=cache, cache_path=str(p), ttl_days=7)

    assert result.variant == "high"
    assert called  # the live probe ran


def test_probe_model_reprobe_flag_forces_live_probe_despite_fresh_cache(tmp_path):
    p = tmp_path / PROBE_CACHE_FILE
    cache = {
        "kenary/hy3:free": {
            "variant": "high",
            "usable": True,
            "probed_at": time.time() - 100,  # very fresh
            "kilo_version": "",
            "tried": [],
            "reasoning_tokens": 0,
        }
    }
    called = []

    def client_factory(variant):
        called.append(variant)
        return None if variant == "medium" else "rejected"

    result = probe_model(client_factory, "kenary", "hy3:free",
                         ["max", "high", "medium"],
                         cache=cache, cache_path=str(p), ttl_days=7, reprobe=True)

    assert result.variant == "medium"
    assert called  # a live probe ran


def test_probe_model_kilo_version_mismatch_forces_reprobing(tmp_path):
    """A cache entry with a different `kilo_version` is treated as stale."""
    cache = {
        "kenary/hy3:free": {
            "variant": "high",
            "usable": True,
            "probed_at": time.time() - 1000,  # fresh by days
            "kilo_version": "7.5.0",
            "tried": [],
            "reasoning_tokens": 0,
        }
    }
    called = []

    def client_factory(variant):
        called.append(variant)
        return None

    probe_model(client_factory, "kenary", "hy3:free",
                ["high"],
                cache=cache, cache_path=None, ttl_days=7, kilo_version="7.6.2")
    assert called  # re-probed because version changed


def test_probe_model_kilo_version_match_uses_cache(tmp_path):
    cache = {
        "kenary/hy3:free": {
            "variant": "high",
            "usable": True,
            "probed_at": time.time() - 3600,
            "kilo_version": "7.6.2",
            "tried": [],
            "reasoning_tokens": 0,
        }
    }
    called = []

    def client_factory(variant):
        called.append(variant)
        return None

    result = probe_model(client_factory, "kenary", "hy3:free",
                         ["high"],
                         cache=cache, cache_path=None, ttl_days=7, kilo_version="7.6.2")
    assert called == []
    assert result.variant == "high"


# ── probe_model against the fake server ──────────────────────────────────────

def test_probe_model_max_rejected_high_answers_two_sessions_created_winning_deleted(tmp_path):
    """A model with `variants: {max, high}` whose fake rejects `max` (400) and
    answers `high` with `hello` → `variant == "high"`, two sessions created,
    the winning one deleted."""
    scenario = {
        "reject_variants": {"max": REJECTED},
        "turns": [{"events": ["busy", "idle"], "assistant": "hello"}],
        "providers": {
            "all": [{"id": "kenary", "name": "kenary", "source": "static",
                     "models": {"glm:free": _model("glm:free", ("max", "high"))}}],
            "default": {}, "connected": ["kenary"], "failed": [],
        },
    }
    fake, server = _attached(scenario, tmp_path)
    cache = {}
    p = tmp_path / PROBE_CACHE_FILE
    try:
        client_factory = hello_probe(server, "kenary", "glm:free", timeout=20)
        result = probe_model(client_factory, "kenary", "glm:free",
                             ["max", "high"],
                             cache=cache, cache_path=str(p), ttl_days=7)
    finally:
        server.close()
        fake.stop()

    assert result.variant == "high"
    assert result.usable
    assert result.tried == [("max", result.tried[0][1])]
    assert "rejected" in result.tried[0][1].lower() or "400" in result.tried[0][1]

    # Two sessions created, all deleted
    created = fake.calls(method="POST", path="/session")
    assert len(created) == 2
    assert [c["body"]["model"].get("variant") for c in created] == ["max", "high"]
    assert len(fake.calls(method="DELETE")) == 2
    assert fake.sessions() == []

    # Cache written
    assert p.exists()
    saved = json.loads(p.read_text())
    assert saved["kenary/glm:free"]["variant"] == "high"


def test_probe_model_reasoning_false_returns_unusable_no_session(tmp_path):
    """A model with `reasoning: false` (empty variants) → no session, `variant is None`."""
    scenario = {
        "turns": [{"events": ["busy", "idle"], "assistant": "hi"}],
    }
    fake, server = _attached(scenario, tmp_path)
    cache = {}
    try:
        client_factory = hello_probe(server, "kenary", "plain:free", timeout=20)
        result = probe_model(client_factory, "kenary", "plain:free",
                             [],  # no listed variants
                             cache=cache)
    finally:
        server.close()
        fake.stop()

    assert result.variant is None
    assert not result.usable
    # No session was created
    assert fake.calls(method="POST", path="/session") == []


def test_probe_model_all_rungs_empty_reply_returns_unusable(tmp_path):
    """Every rung returns empty text → `usable is False`."""
    scenario = {
        "turns": [{"events": ["busy", "idle"], "assistant": ""},
                  {"events": ["busy", "idle"], "assistant": ""},
                  {"events": ["busy", "idle"], "assistant": ""}],
    }
    fake, server = _attached(scenario, tmp_path)
    cache = {}
    try:
        client_factory = hello_probe(server, "kenary", "hy3:free", timeout=20)
        result = probe_model(client_factory, "kenary", "hy3:free",
                             ["high", "medium"],
                             cache=cache)
    finally:
        server.close()
        fake.stop()

    assert not result.usable
    assert result.variant is None


def test_probe_model_cache_younger_than_ttl_no_post_session(tmp_path):
    """A cache entry younger than the TTL produces no POST /session at all."""
    scenario = {"turns": [{"events": ["busy", "idle"], "assistant": "hi"}]}
    fake, server = _attached(scenario, tmp_path)
    cache = {
        "kenary/hy3:free": {
            "variant": "high",
            "usable": True,
            "probed_at": time.time() - 3600,  # 1 hour ago, well within 7 days
            "kilo_version": "",
            "tried": [],
            "reasoning_tokens": 0,
        }
    }
    try:
        client_factory = hello_probe(server, "kenary", "hy3:free", timeout=20)
        result = probe_model(client_factory, "kenary", "hy3:free",
                             ["high", "medium"],
                             cache=cache, ttl_days=7)
    finally:
        server.close()
        fake.stop()

    # Cache was hit — no session opened
    assert fake.calls(method="POST", path="/session") == []
    assert result.variant == "high"


def test_probe_model_roster_set_variant_low_is_checked_once_and_kept(tmp_path):
    """A roster-set variant is probed once; on success the try_one returns None."""
    called = []

    def client_factory(variant):
        called.append(variant)
        return None if variant == "low" else "rejected"

    result = probe_model(client_factory, "kenary", "hy3:free",
                         ["high", "medium", "low"],
                         cache={}, ttl_days=7)

    # pick_variant walks the ladder high→medium→low→None; this stops at "low"
    # (the first rung that answers after high and medium fail).
    assert result.variant == "low"
    assert result.usable
    assert ("high", "rejected") in result.tried
    assert ("medium", "rejected") in result.tried
