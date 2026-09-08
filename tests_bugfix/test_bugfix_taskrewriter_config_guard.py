"""FIX-1 #7 -- one malformed value killed TaskRewriter construction.

``TaskRewriter.__init__`` performed five bare type conversions on agents.ini
values — ``int(rewrite_max_tokens)``, ``float(rewrite_temperature)``,
``getboolean(think)``, ``float(timeout_seconds)``,
``getint(api_<active>.num_ctx)`` — with no ``try``/``except`` anywhere on
the instantiation path.

configparser only falls back for a *missing* key. A key that is present but
malformed (``rewrite_max_tokens = many``, ``think = maybe``,
``num_ctx = 8k``) raises ``ValueError`` out of the constructor.
``make_outer_loop`` builds the rewriter unconditionally, so that one bad
value broke the rewrite phase outside of any error handling: the run died at
construction time, before the failure it was hired to fix could be
rewritten.

Every value has a documented default that is strictly safe: the rewrite
phase simply runs with the conservative settings it would have run with had
the key been omitted.

Two properties matter beyond "it doesn't crash":

  * isolation -- a malformed key must not silently discard a *different*,
    well-formed key's real value. Each of the five reads is guarded on its
    own try/except, so a bad ``rewrite_max_tokens`` cannot fall through and
    blank out an otherwise-valid ``think`` or ``num_ctx``.
  * diagnostics -- falling back is silent data loss unless it is logged.
    Each guard names the exact section/key and the parser's own exception
    text so a malformed agents.ini is debuggable from the log instead of a
    silent behaviour change.
"""

from __future__ import annotations

import configparser
import logging

import pytest

from tools.auto.architect import TaskRewriter

BASE = "http://localhost:11434"
MODEL = "llama3.1:8b"
KEY = "x"

DEFAULTS = (512, 0.4, False, 300.0, 0)


def _cfg(extra: dict[tuple[str, str], str]) -> configparser.ConfigParser:
    """A minimal agents.ini plus optional (section, option) overrides."""
    cfg = configparser.ConfigParser()
    cfg.read_string(
        "[api]\nactive = local\n\n"
        "[api_local]\nbase_url = http://localhost:11434\nmodel = llama3.1:8b\n\n"
        "[architect]\n\n[loop]\n"
    )
    for (section, option), value in extra.items():
        cfg.set(section, option, value)
    return cfg


def _rewrite(extra: dict[tuple[str, str], str]) -> TaskRewriter:
    return TaskRewriter(
        _cfg(extra), base_url=BASE, api_key=KEY, model=MODEL, api_format="ollama",
    )


def _vals(rew: TaskRewriter) -> tuple:
    return (
        rew._max_tokens, rew._temperature, rew._think, rew._timeout, rew._num_ctx,
    )


MAX_TOKENS = ("architect", "rewrite_max_tokens")
TEMPERATURE = ("architect", "rewrite_temperature")
THINK = ("architect", "think")
TIMEOUT = ("loop", "timeout_seconds")
NUM_CTX = ("api_local", "num_ctx")


class TestMalformedValueUsesDefault:
    def test_rewrite_max_tokens(self):
        assert _rewrite({MAX_TOKENS: "many"})._max_tokens == 512

    def test_rewrite_temperature(self):
        assert _rewrite({TEMPERATURE: "hot"})._temperature == 0.4

    def test_think(self):
        assert _rewrite({THINK: "maybe"})._think is False

    def test_timeout_seconds(self):
        assert _rewrite({TIMEOUT: "soon"})._timeout == 300.0

    def test_num_ctx(self):
        assert _rewrite({NUM_CTX: "8k"})._num_ctx == 0

    def test_empty_value(self):
        assert _rewrite({MAX_TOKENS: ""})._max_tokens == 512

    def test_garbage_value(self):
        assert _rewrite({MAX_TOKENS: "\x00!!!"})._max_tokens == 512


class TestMalformedValueDoesNotBreakOthers:
    def test_one_bad_key_does_not_stop_the_rest(self):
        """All five are read independently; a bad one must not mask the rest."""
        assert _vals(_rewrite({
            MAX_TOKENS: "many",
            TEMPERATURE: "cold",
            THINK: "maybe",
            TIMEOUT: "soon",
            NUM_CTX: "8k",
        })) == DEFAULTS

    def test_early_malformed_key_does_not_clobber_a_later_valid_key(self):
        """A malformed EARLY field (rewrite_max_tokens) must not blank out
        a well-formed LATER field (think, num_ctx). Each try/except covers
        exactly one field, so a failure on the first read must not skip the
        assignments for the reads that come after it."""
        rew = _rewrite({
            MAX_TOKENS: "many",   # malformed, read first
            THINK: "true",        # valid, read after the malformed one
            NUM_CTX: "8192",      # valid, read after the malformed one
        })
        assert rew._max_tokens == 512       # fell back, as expected
        assert rew._think is True           # NOT clobbered by the earlier failure
        assert rew._num_ctx == 8192         # NOT clobbered by the earlier failure


class TestWellFormedValuesStillApply:
    def test_every_value_is_honoured(self):
        assert _vals(_rewrite({
            MAX_TOKENS: "1024",
            TEMPERATURE: "0.2",
            THINK: "true",
            TIMEOUT: "60",
            NUM_CTX: "4096",
        })) == (1024, 0.2, True, 60.0, 4096)

    def test_missing_keys_use_defaults(self):
        assert _vals(_rewrite({})) == DEFAULTS

    def test_custom_system_prompt_still_wins(self):
        assert _rewrite({("architect", "rewrite_system"): "be terse"})._system == "be terse"


class TestMalformedValueIsLogged:
    def test_malformed_value_logs_the_key_and_reason(self, caplog):
        """Falling back is silent data loss unless it's diagnosable from the
        log: the warning must name the offending key and configparser's own
        reason, not just say 'something was wrong'."""
        with caplog.at_level(logging.WARNING, logger="tools.auto.architect"):
            _rewrite({MAX_TOKENS: "many"})
        assert any(
            "rewrite_max_tokens" in rec.getMessage() and "512" in rec.getMessage()
            for rec in caplog.records
        )
