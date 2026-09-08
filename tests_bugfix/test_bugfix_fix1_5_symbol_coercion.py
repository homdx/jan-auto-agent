"""FIX-1 bug #5 -- ``CitedLocation.symbol`` was not coerced to ``str``.

(Not to be confused with the pre-existing ``B5`` ticket -- ``TicketStore
.delete`` race -- pinned by ``test_bugfix_b5_ticket_delete_race.py``. This
is bug #5 of the separate FIX-1 batch: architect.py's grounding model.)

Before the fix, ``tools/auto/architect.py``'s ``_parse_candidates`` built
``CitedLocation`` with:

    symbol = (loc_raw.get("symbol") or None)

A bare ``or None`` only strips *falsy* values. A truthy value that isn't a
string -- an LLM emitting ``"symbol": 123``, ``["Foo"]``, or ``true`` --
passed straight through. ``CitedLocation.is_valid()`` only checks
``bool(self.symbol)`` (never its type), so the candidate was accepted as
"grounded". It then crashed downstream wherever a string is assumed:
``re.escape()`` in ``backlog_prioritiser.py`` and ``extract_block()`` in
``gate1_filter.py`` both raise ``TypeError`` on a non-string.

Two things about the *shape* of the fix, pinned by the tests below rather
than just the crash itself:

1. **Two construction sites, one invariant.** ``_parse_candidates`` (raw
   LLM JSON) is the reported path, but ``_deserialise_candidates`` module
   function (checkpoint-resume, loading a JSON file back off disk) builds
   a ``CitedLocation`` from equally untrusted data via a *separate*,
   completely unguarded ``symbol = loc.get("symbol")``. A fix that only
   patches ``_parse_candidates`` leaves checkpoint-resume crashing on the
   exact same input. The fix belongs in ``CitedLocation.__post_init__``
   so every construction path -- present and future -- gets it for free.
   ``test_deserialise_candidates_survives_non_string_symbol`` pins this;
   without a ``__post_init__``-level fix it fails with the same
   ``TypeError`` as the unfixed ``_parse_candidates`` path.

2. **Discard, don't launder.** A non-string ``symbol`` carries no real
   grounding -- an LLM's ``123`` doesn't name any symbol in the file --
   so stringifying it into ``"123"`` manufactures a fake anchor that
   will simply fail to be found later. The fix instead treats it as "no
   symbol was given" (``None``), so ``is_valid()`` correctly requires a
   *different* real anchor (``line_start`` or ``new_file``) instead of
   nodding through on garbage.
   ``test_ungrounded_candidate_with_non_string_symbol_still_rejected``
   pins this specifically because a naive ``str(x or None)``-style fix
   turns an *absent* symbol into the four-character string ``"None"``,
   which is truthy -- silently defeating the grounding gate for the very
   common case of an LLM omitting ``cited_location.symbol`` entirely.
   (This exact regression broke three pre-existing tests in
   ``tests/test_architect_domain.py`` in one of the compared drafts.)

Without the fix: ``test_non_string_symbol_coerced_via_parse_candidates``
and ``test_deserialise_candidates_survives_non_string_symbol`` both fail
with ``TypeError`` (or, for the direct-construction tests, the dataclass
simply has no ``__post_init__`` to coerce anything).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import (  # noqa: E402
    CitedLocation,
    ClusterReviewer,
    _deserialise_candidates,
)


def _cfg():
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1", "api_key": "x",
            "model": "m", "api_format": "openai",
        },
        "architect": {"temperature": "0.2", "max_tokens": "512"},
        "loop": {"timeout_seconds": "10"},
    })
    return cfg


def _reviewer(task_mode="code"):
    return ClusterReviewer(
        config=_cfg(), base_url="http://localhost:1337/v1", api_key="x",
        model="m", api_format="openai", verify_ssl=False, task_mode=task_mode,
    )


# ── The reported path: raw LLM JSON via _parse_candidates ──────────────────

class TestParseCandidatesSymbolCoercion:
    @pytest.mark.parametrize("raw_symbol", [123, 3.14, True, ["Foo"]])
    def test_non_string_symbol_coerced_via_parse_candidates(self, raw_symbol):
        r = _reviewer("code")
        item = {
            "title": "t", "instruction": "i", "acceptance_check": "true",
            "target_files": ["src/foo.py"],
            "cited_location": {
                "file": "src/foo.py", "symbol": raw_symbol,
                "line_start": 10, "line_end": 20,
            },
        }
        tasks = r._parse_candidates(json.dumps([item]), "cluster")
        assert len(tasks) == 1
        symbol = tasks[0].cited_location.symbol
        assert symbol is None or isinstance(symbol, str)
        if symbol:
            re.escape(symbol)  # must not raise TypeError

    def test_string_symbol_preserved(self):
        r = _reviewer("code")
        item = {
            "title": "t", "instruction": "i", "acceptance_check": "true",
            "target_files": ["src/foo.py"],
            "cited_location": {
                "file": "src/foo.py", "symbol": "my_function",
                "line_start": None, "line_end": None,
            },
        }
        tasks = r._parse_candidates(json.dumps([item]), "cluster")
        assert len(tasks) == 1
        assert tasks[0].cited_location.symbol == "my_function"


# ── The unreported path: checkpoint-resume via _deserialise_candidates ─────

class TestDeserialiseCandidatesSymbolCoercion:
    def test_deserialise_candidates_survives_non_string_symbol(self):
        """A JSON checkpoint with a non-string symbol must not crash the
        --resume path the same way the LLM-JSON path used to crash."""
        ckpt = [{
            "title": "t", "instruction": "i",
            "target_files": ["src/foo.py"], "acceptance_check": "true",
            "cluster": "c",
            "cited_location": {
                "file": "src/foo.py", "symbol": 123,
                "line_start": 10, "line_end": 20, "new_file": False,
            },
            "raw": {},
        }]
        tasks = _deserialise_candidates(ckpt)  # must not raise TypeError
        symbol = tasks[0].cited_location.symbol
        assert symbol is None or isinstance(symbol, str)
        if symbol:
            re.escape(symbol)


# ── Dataclass invariant, exercised directly ─────────────────────────────────

class TestCitedLocationInvariant:
    @pytest.mark.parametrize("raw", [123, 1.5, ["a"], {"a": 1}, True, False, b"bytes"])
    def test_symbol_is_always_str_or_none(self, raw):
        loc = CitedLocation(file="a.py", symbol=raw)
        assert loc.symbol is None or isinstance(loc.symbol, str)

    def test_whitespace_only_symbol_becomes_none(self):
        assert CitedLocation(file="a.py", symbol="   ").symbol is None

    def test_symbol_is_stripped(self):
        assert CitedLocation(file="a.py", symbol="  Foo.bar  ").symbol == "Foo.bar"

    def test_real_string_symbol_survives_re_escape(self):
        loc = CitedLocation(file="a.py", symbol="Foo.bar")
        assert re.escape(loc.symbol) == r"Foo\.bar"


# ── Grounding-gate integrity: guards against the "None" -> "'None'" trap ───

class TestGroundingGateIntegrity:
    def test_ungrounded_candidate_with_non_string_symbol_still_rejected(self):
        """A candidate with a garbage symbol and no other anchor has no
        real grounding and must still be rejected in code mode -- not
        nodded through because the garbage got stringified into
        something truthy."""
        r = _reviewer("code")
        item = {
            "title": "t", "instruction": "i", "acceptance_check": "true",
            "target_files": ["src/foo.py"],
            "cited_location": {
                "file": "src/foo.py", "symbol": 123,
                "line_start": None, "line_end": None,
            },
        }
        tasks = r._parse_candidates(json.dumps([item]), "cluster")
        assert tasks == []

    def test_absent_symbol_still_rejected_in_code_mode(self):
        """Baseline non-regression: symbol omitted entirely (None) and no
        line_start was already correctly rejected before this fix and
        must remain so."""
        r = _reviewer("code")
        item = {
            "title": "t", "instruction": "i", "acceptance_check": "true",
            "target_files": ["src/foo.py"],
            "cited_location": {
                "file": "src/foo.py", "symbol": None,
                "line_start": None, "line_end": None,
            },
        }
        tasks = r._parse_candidates(json.dumps([item]), "cluster")
        assert tasks == []

    def test_valid_line_anchor_still_accepted_with_no_symbol(self):
        """Non-regression: a real line-range anchor with no symbol at
        all is still valid grounding in code mode."""
        r = _reviewer("code")
        item = {
            "title": "t", "instruction": "i", "acceptance_check": "true",
            "target_files": ["src/foo.py"],
            "cited_location": {
                "file": "src/foo.py", "symbol": None,
                "line_start": 5, "line_end": 9,
            },
        }
        tasks = r._parse_candidates(json.dumps([item]), "cluster")
        assert len(tasks) == 1
        assert tasks[0].cited_location.symbol is None
