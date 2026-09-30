"""collect's per-request timeout comes from [collect] timeout_seconds, not the agent's [loop] budget."""

import configparser
from pathlib import Path

import pytest

from tools import llm_stream
from tools.collect import summarizer

ROOT = Path(__file__).resolve().parent.parent


def _config(collect=None, loop=None):
    cfg = configparser.ConfigParser()
    cfg["api"] = {"active": "local"}
    cfg["local"] = {"base_url": "http://localhost:11434", "model": "m", "api_format": "openai"}
    cfg["collect"] = {} if collect is None else {"timeout_seconds": collect}
    if loop is not None:
        cfg["loop"] = {"timeout_seconds": loop}
    return cfg


def _timeout_used(monkeypatch, cfg):
    seen = {}

    def fake_request_completion(**kw):
        seen["timeout"] = kw["timeout"]
        return '{"summary": "x"}'

    monkeypatch.setattr(llm_stream, "request_completion", fake_request_completion)
    summarizer._make_llm_call(cfg)("sys", "user")
    return seen["timeout"]


def test_collect_timeout_wins_over_loop(monkeypatch):
    assert _timeout_used(monkeypatch, _config(collect="300", loop="4800")) == 300


def test_unset_collect_timeout_falls_back_to_loop(monkeypatch):
    assert _timeout_used(monkeypatch, _config(loop="4800")) == 4800


def test_nothing_set_is_300(monkeypatch):
    assert _timeout_used(monkeypatch, _config()) == 300


def test_malformed_collect_timeout_is_300_not_loop(monkeypatch):
    assert _timeout_used(monkeypatch, _config(collect="soon", loop="4800")) == 300


@pytest.mark.parametrize("ini", sorted(p.name for p in ROOT.glob("agents*.ini")))
def test_every_shipped_profile_sets_collect_timeout(ini):
    cfg = configparser.ConfigParser(interpolation=None, strict=False)
    cfg.read(ROOT / ini, encoding="utf-8")
    assert cfg.getint("collect", "timeout_seconds") == 300
