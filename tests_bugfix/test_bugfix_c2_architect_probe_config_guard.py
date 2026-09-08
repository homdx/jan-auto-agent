"""C2 — unguarded probe config reads must not abort ClusterReviewer construction.

``ClusterReviewer.__init__`` read nine probe variables with bare
``config.getboolean``/``getint``/``getfloat`` calls and no ``try/except``::

    self._probe_enabled          = config.getboolean(arch, "probe_enabled", ...)
    self._probe_max_rounds       = max(0, config.getint(arch, "probe_max_rounds", ...))
    ...
    self._probe_memo_max_entries = max(1, config.getint(arch, "probe_memo_max_entries", ...))

``fallback=`` only covers a *missing* key. A key that is present but
unparseable (``probe_max_rounds = five``, or a stray leftover from editing
agents.ini) raises ``ValueError`` straight out of the call — and nothing on
the call chain catches it::

    pipeline._run_plan_phase()
      → architect.review_clusters()
        → ClusterReviewer(config, ...)   # constructed with NO guard

The only guarded construction site is ``pipeline._build_plan_validator``,
which is the plan-*validation* path — the main architect call is not. So one
malformed probe key aborted the entire ``--auto`` run at the plan phase,
before any task ran, reported as a bare traceback rather than next to the
offending key.

The fix wraps each probe read in ``try/except ValueError`` with its
documented fallback, matching the guard style already used for
``temperature``/``max_tokens``/``num_ctx``/``think`` in the same block.
"""

from __future__ import annotations

import configparser
import logging

import pytest

from tools.auto.architect import ClusterReviewer

_BASE = {
    "api": {"active": "local", "verify_ssl": "false"},
    "api_local": {
        "base_url": "http://localhost:1337/v1",
        "api_key": "test",
        "model": "test-model",
        "api_format": "openai",
    },
    "architect": {"temperature": "0.2", "max_tokens": "512"},
    "loop": {"timeout_seconds": "10"},
}


def _cfg(**architect_overrides) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({**_BASE, "architect": {**_BASE["architect"], **architect_overrides}})
    return cfg


def _reviewer(cfg: configparser.ConfigParser) -> ClusterReviewer:
    return ClusterReviewer(
        cfg,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
        task_mode="code",
    )


@pytest.mark.parametrize(
    "key,attr,expected",
    [
        ("probe_enabled", "_probe_enabled", False),
        ("probe_max_rounds", "_probe_max_rounds", 1),
        ("probe_max_chars", "_probe_max_chars", 2000),
        ("probe_max_total_chars", "_probe_max_total_chars", 6000),
        ("probe_budget_warmup", "_probe_budget_warmup", 3),
        ("probe_budget_headroom", "_probe_budget_headroom", 2.0),
        ("probe_budget_max_chars", "_probe_budget_max_chars", 0),
        ("probe_budget_escalations", "_probe_budget_escalations", 2),
        ("probe_memo_max_entries", "_probe_memo_max_entries", 200),
        # Already guarded before this fix — pinned here so the whole probe
        # block is covered by one contract.
        ("probe_budget_ladder_scale", "_probe_budget_ladder_scale", 1.0),
    ],
)
def test_malformed_probe_value_falls_back_to_its_default(key, attr, expected):
    """Construction must survive, and the attribute must hold the documented
    default. Before the fix, every case but the last raised ValueError."""
    reviewer = _reviewer(_cfg(**{key: "five"}))
    assert getattr(reviewer, attr) == expected


def test_every_probe_key_malformed_at_once_still_constructs():
    """The realistic failure: a botched [architect] section, not one typo."""
    bad = {
        "probe_enabled": "perhaps",
        "probe_max_rounds": "many",
        "probe_max_chars": "huge",
        "probe_max_total_chars": "lots",
        "probe_budget_warmup": "?",
        "probe_budget_headroom": "wide",
        "probe_budget_max_chars": "x",
        "probe_budget_escalations": "nope",
        "probe_memo_max_entries": "plenty",
    }
    reviewer = _reviewer(_cfg(**bad))
    assert reviewer._probe_enabled is False
    assert reviewer._probe_max_rounds == 1
    assert reviewer._probe_memo_max_entries == 200


def test_malformed_probe_value_warns_and_names_the_key(caplog):
    """Failing open is only safe if the operator can find the bad key."""
    with caplog.at_level(logging.WARNING):
        _reviewer(_cfg(probe_max_rounds="five"))
    assert any("probe_max_rounds" in r.getMessage() for r in caplog.records)


def test_well_formed_probe_values_are_still_honoured():
    """The guards must not swallow valid configuration."""
    reviewer = _reviewer(_cfg(
        probe_enabled="true",
        probe_max_rounds="4",
        probe_budget_headroom="3.5",
        probe_memo_max_entries="50",
    ))
    assert reviewer._probe_enabled is True
    assert reviewer._probe_max_rounds == 4
    assert reviewer._probe_budget_headroom == 3.5
    assert reviewer._probe_memo_max_entries == 50
