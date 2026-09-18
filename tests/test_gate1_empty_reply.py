"""tests/test_gate1_empty_reply.py — RUN-9: an empty presence reply is not a
garbled verdict.

Story background
----------------
Live Gate 1 runs against ``sensenova-6.7-flash-lite`` (``../testtext``,
``../testtext6``, 2026-09-16; ``unparseable_retry_mode = fast``,
``presence_unknown = keep``): 408 and 274 presence replies came back as
``raw=''`` and 93 of 143 "accepted" candidates never got a verdict. The
replies arrived in 20–60 s next to ``HTTP 429 Server is busy`` — a degraded
gateway, not a model that burned a 32k budget thinking. The ladder could not
tell the two apart: it re-asked with the nudge *"your previous reply was not
valid JSON"* a model that had said nothing, GATE1-LEARN-2 (fast) pinned the
tier on the emptiness alone, one re-ask was made out of six, and the
candidate ended ``UNKNOWN → KEPT``.

What this file pins down
------------------------
1. Classification from the stream metadata (``CompletionMeta``) before the
   ladder runs: ``exhausted`` (``finish_reason = length``, ``completion_tokens
   >= 0.9 * max_tokens`` or reasoning streamed) vs ``transport`` (none of
   that), logged once per empty reply with the numbers it was made from.
2. A transport-empty is re-issued unchanged — same ``max_tokens``, same
   temperature, no nudge — up to ``[gate1] presence_empty_retries`` (default
   2), outside the ladder, sleeping ``llm_call_retry_wait_sec`` between calls;
   only a reply that is still empty afterwards enters the ladder (with the
   pin). ``0`` restores today's behaviour exactly.
3. An exhausted-empty keeps today's ladder (pin, temperature sweep) plus one
   new rung — a ``think=False`` re-ask when the call went out with
   ``think=True`` — and one diagnostic: reasoning streamed on a
   ``think=False`` call is logged once per (url, model) per run and counted
   in ``presence_nothink_ignored``.
4. Counters ``presence_empty_transport`` / ``presence_empty_exhausted`` /
   ``presence_nothink_ignored`` in the Gate 1 split; the ``ending unknown``
   WARNING and the ``Gate1[presence] UNKNOWN`` line say ``empty
   (transport)`` / ``empty (exhausted)`` / ``garbled: …`` instead of the
   decoder's "Expecting value: line 1 column 1".
5. The ``capped at unparseable_max_tokens_cap`` line names the budget that is
   actually sent.

Beyond the ticket's letter (found by running 18 contest entries on shared
data — ``contest-bench/run9/``): a technical failure *inside* a transport
retry takes the outer ``llm_call_retry`` road and is never a verdict and
never an exception out of ``filter()``; the transport retry reuses the
learned start budget (GATE1-LEARN-1); every HTTP call, retries included,
leaves its ``llm_request`` / ``llm_response`` trace events.

The provider is faked at ``tools.llm_stream.request_completion`` with a stub
that reports ``CompletionMeta`` through ``on_meta`` exactly as the real one
does; the ``stream_options`` tests go one layer lower and fake
``urllib.request.urlopen`` so the real ``request_completion`` handles the
HTTP 400.
"""

from __future__ import annotations

import configparser
import io
import json
import logging
import re
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.llm_stream as _llm_stream  # noqa: E402
from tools.auto.architect import CandidateTask, CitedLocation  # noqa: E402
from tools.auto.gate1_filter import (  # noqa: E402
    UNKNOWN_PRESENCE_REASON,
    Gate1Filter,
    _classify_empty_reply,
    _is_technical_failure,
    format_gate1_split,
    split_gate1_results,
)
from tools.llm_stream import CompletionMeta  # noqa: E402

LOGGER = "tools.auto.gate1_filter"
NUDGE = "IMPORTANT: your previous reply was not valid JSON"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _config(gate1_over: dict | None = None, *, api_format: str = "openai",
            extra: dict | None = None) -> configparser.ConfigParser:
    """The field shape: fast ladder, no learned budget (deterministic
    max_tokens), no real waiting, one worker."""
    gate1 = {
        "temperature": "0.0", "max_tokens": "4096", "skip_llm": "false",
        "llm_call_retry_wait_sec": "0", "llm_call_retry_max": "1",
        "unparseable_retry_mode": "fast", "unparseable_learn": "false",
        "presence_workers": "1",
    }
    gate1.update({k: str(v) for k, v in (gate1_over or {}).items()})
    base_url = "http://stub.local:11434" if api_format == "ollama" else "http://stub.local:1337/v1"
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": base_url, "api_key": "test", "model": "test-model",
                      "api_format": api_format, "num_ctx": "65536"},
        "gate1": gate1,
        "loop": {"timeout_seconds": "10"},
        **(extra or {}),
    })
    return cfg


def _filter(cfg: configparser.ConfigParser) -> Gate1Filter:
    return Gate1Filter(
        config=cfg, base_url=cfg["api_local"]["base_url"], api_key="test",
        model="test-model", api_format=cfg["api_local"]["api_format"], verify_ssl=False,
    )


def _repo(tmp_path: Path, n: int = 1) -> list[CandidateTask]:
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        (tmp_path / "tools" / f"c{i}.py").write_text(textwrap.dedent(f"""\
            def parse_config_{i}(raw):
                # TODO: validate input
                return raw
        """), encoding="utf-8")
        out.append(CandidateTask(
            title=f"candidate {i}", instruction="fix the problem described in the claim",
            target_files=[f"tools/c{i}.py"], acceptance_check="python -m pytest tests -q",
            cited_location=CitedLocation(file=f"tools/c{i}.py", symbol=f"parse_config_{i}"),
            cluster="agents"))
    return out


# ── replies: (text, CompletionMeta) ──────────────────────────────────────────
REJECTED = json.dumps({"verdict": "rejected", "reason": "already handled"})
GARBLED = "Sure! The problem is present, I think. verdict: maybe"


def transport(**over):
    """Degraded gateway: role-only chunk, finish_reason=stop, 0 tokens."""
    m = dict(finish_reason="stop", completion_tokens=0, reasoning_chars=0, content_chunks=0, elapsed=31.0)
    m.update(over)
    return ("", CompletionMeta(**m))


def exhausted(**over):
    """The budget went into thinking: finish_reason=length, tokens == max_tokens."""
    m = dict(finish_reason="length", completion_tokens=4096, reasoning_chars=0, content_chunks=0, elapsed=290.0)
    m.update(over)
    return ("", CompletionMeta(**m))


def reply(text: str, **over):
    m = dict(finish_reason="stop", completion_tokens=24, reasoning_chars=0, content_chunks=3, elapsed=2.0)
    m.update(over)
    return (text, CompletionMeta(**m))


class Provider:
    """Per-candidate script of replies (the last one repeats). A reply may be
    a callable(payload) → reply, or an Exception instance to raise. Records
    every payload, so the number and shape of the attempts is observable."""

    def __init__(self, plans: dict[str, list], default: list | None = None):
        self.plans = plans
        self.default = default
        self.calls: dict[str, list[dict]] = {}

    def __call__(self, url, headers, payload, timeout=None, stream=False, on_token=None,
                 api_format="openai", ssl_context=None, on_meta=None, **kw):
        user_msg = payload["messages"][-1]["content"]
        loc = re.search(r"Location:\s*(.+)$", user_msg, re.M)
        key = loc.group(1).split(",")[0].strip() if loc else "?"
        seq = self.calls.setdefault(key, [])
        plan = self.plans.get(key, self.default)
        step = plan[min(len(seq), len(plan) - 1)]
        seq.append(payload)
        if callable(step) and not isinstance(step, Exception):
            step = step(payload)
        if isinstance(step, Exception):
            raise step
        text, meta = step
        if on_meta is not None:
            on_meta(meta)
        return text


def _run(tmp_path, provider, cfg=None, n=1, *, sleeps=None):
    """filter() one or more candidates against `provider`; returns
    (filter, accepted titles, rejected {title: reason}, counters, caplog-free log records)."""
    cfg = cfg or _config()
    flt = _filter(cfg)
    counters: dict = {}
    records: list[logging.LogRecord] = []

    class _H(logging.Handler):
        def emit(self, r):
            records.append(r)

    h = _H(level=logging.DEBUG)
    lg = logging.getLogger(LOGGER)
    lg.addHandler(h)
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    try:
        with patch("tools.llm_stream.request_completion", side_effect=provider), \
             patch("tools.auto.gate1_filter.time.sleep", side_effect=(sleeps.append if sleeps is not None else lambda s: None)):
            accepted, rejected = flt.filter(_repo(tmp_path, n), base_dir=tmp_path, counters=counters)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)
    return flt, [c.title for c in accepted], {r.candidate.title: r.reason for r in rejected}, counters, records


def _msgs(records, level=None, needle=None):
    return [r.getMessage() for r in records
            if (level is None or r.levelname == level) and (needle is None or needle in r.getMessage())]


def _params(payloads):
    return [(p["max_tokens"], p["temperature"]) for p in payloads]


def _has_nudge(p):
    return NUDGE in p["messages"][-1]["content"]


def _think_off(p):
    return (p.get("reasoning") or {}).get("exclude") is True


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

class TestConfig:

    def test_default_is_two(self):
        assert _filter(_config())._presence_empty_retries == 2

    def test_explicit_value(self):
        assert _filter(_config({"presence_empty_retries": "5"}))._presence_empty_retries == 5

    def test_negative_clamps_to_zero(self):
        assert _filter(_config({"presence_empty_retries": "-3"}))._presence_empty_retries == 0

    def test_malformed_warns_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert _filter(_config({"presence_empty_retries": "many"}))._presence_empty_retries == 2
        assert any("presence_empty_retries is malformed" in r.message for r in caplog.records)

    def test_documented_in_agents_ini(self):
        text = (PROJECT_ROOT / "agents.ini").read_text(encoding="utf-8")
        gate1 = re.search(r"^\[gate1\](.*?)^\[", text, re.S | re.M).group(1)
        assert re.search(r"^presence_empty_retries\s*=\s*2\b", gate1, re.M)


# ─────────────────────────────────────────────────────────────────────────────
# The classifier
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifier:

    @pytest.mark.parametrize("meta, kind", [
        (CompletionMeta(finish_reason="stop", completion_tokens=0), "transport"),
        (CompletionMeta(finish_reason=None, completion_tokens=None), "transport"),
        (CompletionMeta(finish_reason="stop", completion_tokens=12), "transport"),
        (CompletionMeta(finish_reason="length", completion_tokens=None), "exhausted"),
        (CompletionMeta(finish_reason="stop", completion_tokens=3687), "exhausted"),   # 0.9 * 4096
        (CompletionMeta(finish_reason="stop", completion_tokens=3686), "transport"),
        (CompletionMeta(finish_reason="stop", completion_tokens=5, reasoning_chars=1), "exhausted"),
        (CompletionMeta(finish_reason=None, completion_tokens=4096), "exhausted"),
    ])
    def test_table(self, meta, kind):
        assert _classify_empty_reply(meta, 4096) == kind

    def test_none_max_tokens_never_exhausts_on_tokens_alone(self):
        assert _classify_empty_reply(CompletionMeta(completion_tokens=99999), None) == "transport"

    def test_bool_tokens_are_not_numbers(self):
        assert _classify_empty_reply(CompletionMeta(completion_tokens=True), 1) == "transport"

    def test_garbage_meta_never_raises(self):
        assert _classify_empty_reply(object(), 4096) == "transport"
        assert _classify_empty_reply(None, 4096) == "transport"

    def test_causes_are_technical_failures(self):
        assert _is_technical_failure("empty (transport)")
        assert _is_technical_failure("empty (exhausted)")
        assert _is_technical_failure("garbled: unrecognised verdict 'maybe'")
        assert _is_technical_failure(f"{UNKNOWN_PRESENCE_REASON} after 1 re-ask(s): empty (transport)")
        assert not _is_technical_failure("already handled")


# ─────────────────────────────────────────────────────────────────────────────
# AC-2 / AC-7 / AC-8: transport-empty
# ─────────────────────────────────────────────────────────────────────────────

class TestTransportEmpty:

    def test_two_empties_then_verdict_is_rejected_without_a_nudge(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport(), transport(), reply(REJECTED)]})
        flt, accepted, rejected, counters, records = _run(tmp_path, prov)
        calls = prov.calls["tools/c0.py"]
        assert accepted == [] and rejected["candidate 0"] == "already handled"
        assert len(calls) == 3
        assert _params(calls) == [(4096, 0.0)] * 3                     # same request, three times
        assert not any(_has_nudge(p) for p in calls)
        assert calls[1]["messages"] == calls[0]["messages"] == calls[2]["messages"]
        assert counters["presence_empty_transport"] == 1
        assert counters["presence_reask"] == 0
        assert counters["presence_unknown"] == 0
        assert counters["presence_empty_exhausted"] == 0

    def test_classification_line_once_per_empty_reply_with_the_numbers(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport(elapsed=31.4), transport(elapsed=44.0), reply(REJECTED)]})
        *_, records = _run(tmp_path, prov)
        lines = _msgs(records, needle="empty reply — kind=")
        assert len(lines) == 2
        assert lines[0] == ("Gate1._check_presence [candidate 0]: empty reply — kind=transport "
                            "finish_reason=stop completion_tokens=0 reasoning_chars=0 elapsed=31.4s")
        assert "elapsed=44.0s" in lines[1]
        # the empty string is never run through the JSON decoder
        assert not _msgs(records, needle="Expecting value")

    def test_bare_stream_no_finish_no_usage_is_transport(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport(finish_reason=None, completion_tokens=None),
                                         transport(finish_reason=None, completion_tokens=None), reply(REJECTED)]})
        _, accepted, rejected, counters, _ = _run(tmp_path, prov)
        assert rejected["candidate 0"] == "already handled" and counters["presence_empty_transport"] == 1

    def test_transport_forever_ends_unknown_kept_after_retries_then_ladder(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport()]})
        flt, accepted, rejected, counters, records = _run(tmp_path, prov)
        calls = prov.calls["tools/c0.py"]
        # 1 call + 2 retries at the same params, then the pinned ladder re-ask
        assert _params(calls) == [(4096, 0.0), (4096, 0.0), (4096, 0.0), (4096, 0.1)]
        assert not any(_has_nudge(p) for p in calls)                  # a model that said nothing gets no nudge
        assert accepted == ["candidate 0"]                             # presence_unknown = keep
        assert counters == {**counters, "presence_unknown": 1, "presence_empty_transport": 1,
                            "presence_empty_exhausted": 0, "presence_reask": 0}
        (ending,) = _msgs(records, "WARNING", "ending unknown")
        assert "after 1 re-ask(s)" in ending and "empty (transport)" in ending
        (unknown_line,) = _msgs(records, "WARNING", "Gate1[presence] UNKNOWN")
        assert unknown_line.endswith("after 1 re-ask(s): empty (transport)")
        assert "Expecting value" not in unknown_line

    def test_transport_forever_with_reject_policy_is_dropped(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport()]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov, _config({"presence_unknown": "reject"}))
        assert accepted == [] and rejected["candidate 0"].endswith("empty (transport)")
        assert counters["presence_unknown"] == 1

    def test_retries_zero_restores_todays_behaviour(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport()]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov, _config({"presence_empty_retries": "0"}))
        assert _params(prov.calls["tools/c0.py"]) == [(4096, 0.0), (4096, 0.1)]   # straight into the pinned ladder
        assert accepted == ["candidate 0"]
        assert counters["presence_empty_transport"] == 1               # still classified and counted
        assert _msgs(records, needle="empty reply — kind=transport")

    def test_retries_zero_then_verdict_on_the_reask(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport(), reply(REJECTED)]})
        _, accepted, rejected, counters, _ = _run(tmp_path, prov, _config({"presence_empty_retries": "0"}))
        assert rejected["candidate 0"] == "already handled"
        assert counters["presence_reask"] == 1 and counters["presence_empty_transport"] == 1

    def test_retry_count_is_configurable(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport()]})
        _run(tmp_path, prov, _config({"presence_empty_retries": "1"}))
        assert len(prov.calls["tools/c0.py"]) == 3                     # 1 + 1 retry + 1 ladder

    def test_sleeps_llm_call_retry_wait_between_retries(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport(), transport(), reply(REJECTED)]})
        sleeps: list[float] = []
        _run(tmp_path, prov, _config({"llm_call_retry_wait_sec": "7"}), sleeps=sleeps)
        assert sleeps == [7.0, 7.0]

    def test_ladder_after_retries_is_todays_ladder(self, tmp_path):
        """Still empty after the retries → the ladder with the pin, as today;
        a verdict from it counts as a re-ask."""
        prov = Provider({"tools/c0.py": [transport(), transport(), transport(), reply(REJECTED)]})
        _, accepted, rejected, counters, _ = _run(tmp_path, prov)
        assert _params(prov.calls["tools/c0.py"]) == [(4096, 0.0)] * 3 + [(4096, 0.1)]
        assert rejected["candidate 0"] == "already handled"
        assert counters["presence_reask"] == 1 and counters["presence_empty_transport"] == 1

    def test_retry_that_returns_garbled_takes_the_garbled_road(self, tmp_path):
        """A non-empty reply from a transport retry is parsed exactly as the
        first call's would have been — garbled → the full grid, no pin."""
        prov = Provider({"tools/c0.py": [transport(), reply(GARBLED)]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov)
        calls = prov.calls["tools/c0.py"]
        assert _params(calls) == [(4096, 0.0), (4096, 0.0), (4096, 0.1), (8192, 0.0), (8192, 0.1),
                                  (16384, 0.0), (16384, 0.1)]
        assert not _has_nudge(calls[1]) and all(_has_nudge(p) for p in calls[2:])
        assert counters["presence_empty_transport"] == 1
        (unknown_line,) = _msgs(records, "WARNING", "Gate1[presence] UNKNOWN")
        assert "after 5 re-ask(s): garbled: JSON decode failed" in unknown_line

    def test_transport_then_exhausted_enters_the_ladder_with_the_pin(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport(), exhausted(), reply(REJECTED)]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov)
        assert _params(prov.calls["tools/c0.py"]) == [(4096, 0.0), (4096, 0.0), (4096, 0.1)]
        assert rejected["candidate 0"] == "already handled"
        assert counters["presence_empty_transport"] == 1 and counters["presence_empty_exhausted"] == 0
        kinds = [m.split("kind=")[1].split()[0] for m in _msgs(records, needle="empty reply — kind=")]
        assert kinds == ["transport", "exhausted"]

    def test_learned_start_budget_is_reused_by_the_retry(self, tmp_path):
        """GATE1-LEARN-1: candidate 0 teaches 8192; candidate 1's transport
        retries re-issue the same request — at 8192, not the configured 4096."""
        prov = Provider({"tools/c0.py": [reply(GARBLED), reply(GARBLED), reply(GARBLED), reply(REJECTED)],
                         "tools/c1.py": [transport(), transport(), reply(REJECTED)]})
        _, accepted, rejected, counters, _ = _run(tmp_path, prov, _config({"unparseable_learn": "true"}), n=2)
        assert prov.calls["tools/c0.py"][-1]["max_tokens"] == 8192
        assert _params(prov.calls["tools/c1.py"]) == [(8192, 0.0)] * 3
        assert rejected["candidate 1"] == "already handled"

    def test_workers_count_every_candidate(self, tmp_path):
        prov = Provider({}, default=[transport(), transport(), reply(REJECTED)])
        _, accepted, rejected, counters, _ = _run(tmp_path, prov, _config({"presence_workers": "4"}), n=12)
        assert counters["presence_empty_transport"] == 12 and counters["presence_unknown"] == 0
        assert len(rejected) == 12 and all(len(v) == 3 for v in prov.calls.values())


# ─────────────────────────────────────────────────────────────────────────────
# Beyond the letter: a failure inside the retry is neither a verdict nor a crash
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureInsideTheRetry:

    def test_http_error_during_a_retry_takes_the_outer_retry_road(self, tmp_path):
        err = urllib.error.HTTPError("http://stub.local", 404, "not found", {}, io.BytesIO(b""))
        prov = Provider({"tools/c0.py": [transport(), err, reply(REJECTED)]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov)
        assert len(prov.calls["tools/c0.py"]) == 3
        assert rejected["candidate 0"] == "already handled"             # the model's verdict, not the 404's
        assert counters["presence_unknown"] == 0
        assert _msgs(records, "WARNING", "LLM call failed")            # the outer loop said so

    def test_exhausted_exception_budget_ends_unknown_never_rejected(self, tmp_path):
        err = RuntimeError("HTTP 404 from http://stub.local: not found")
        prov = Provider({"tools/c0.py": [transport(), err, err, err]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov)
        assert accepted == ["candidate 0"]                             # kept as unknown
        assert counters["presence_unknown"] == 1
        (unknown_line,) = _msgs(records, "WARNING", "Gate1[presence] UNKNOWN")
        assert "LLM call failed: HTTP 404" in unknown_line

    def test_transport_retries_do_not_consume_the_exception_budget(self, tmp_path):
        err = RuntimeError("boom")
        prov = Provider({"tools/c0.py": [transport(), transport(), err, reply(REJECTED)]})
        _, accepted, rejected, counters, _ = _run(tmp_path, prov)
        assert rejected["candidate 0"] == "already handled" and len(prov.calls["tools/c0.py"]) == 4


# ─────────────────────────────────────────────────────────────────────────────
# AC-3: exhausted-empty keeps today's path
# ─────────────────────────────────────────────────────────────────────────────

class TestExhaustedEmpty:

    def test_pin_and_one_temperature_reask(self, tmp_path):
        prov = Provider({"tools/c0.py": [exhausted(), reply(REJECTED)]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov)
        calls = prov.calls["tools/c0.py"]
        assert _params(calls) == [(4096, 0.0), (4096, 0.1)]            # no transport retry, pinned tier
        assert rejected["candidate 0"] == "already handled"
        assert counters["presence_empty_exhausted"] == 1 and counters["presence_empty_transport"] == 0
        assert counters["presence_reask"] == 1
        assert _msgs(records, needle="empty reply — kind=exhausted finish_reason=length completion_tokens=4096")

    def test_no_nudge_after_an_empty_reply(self, tmp_path):
        """The nudge says "your previous reply was not valid JSON" — a model
        that said nothing has nothing to correct (ticket §4)."""
        prov = Provider({"tools/c0.py": [exhausted(), reply(REJECTED)]})
        _run(tmp_path, prov)
        assert not _has_nudge(prov.calls["tools/c0.py"][1])

    def test_nudge_still_follows_a_garbled_reply(self, tmp_path):
        prov = Provider({"tools/c0.py": [reply(GARBLED), reply(REJECTED)]})
        _run(tmp_path, prov)
        assert _has_nudge(prov.calls["tools/c0.py"][1])

    def test_exhausted_forever_ends_unknown_exhausted(self, tmp_path):
        prov = Provider({"tools/c0.py": [exhausted()]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov)
        assert len(prov.calls["tools/c0.py"]) == 2                     # think=false: no extra rung
        (unknown_line,) = _msgs(records, "WARNING", "Gate1[presence] UNKNOWN")
        assert unknown_line.endswith("after 1 re-ask(s): empty (exhausted)")
        assert _msgs(records, needle="nothing to switch off")
        assert counters["presence_empty_exhausted"] == 1 and counters["presence_unknown"] == 1

    def test_usage_near_the_cap_without_finish_reason_is_exhausted(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport(completion_tokens=3900), reply(REJECTED)]})
        _, accepted, rejected, counters, _ = _run(tmp_path, prov)
        assert len(prov.calls["tools/c0.py"]) == 2 and counters["presence_empty_exhausted"] == 1

    def test_reasoning_streamed_is_exhausted(self, tmp_path):
        prov = Provider({"tools/c0.py": [transport(reasoning_chars=230, completion_tokens=12), reply(REJECTED)]})
        _, accepted, rejected, counters, _ = _run(tmp_path, prov)
        assert len(prov.calls["tools/c0.py"]) == 2 and counters["presence_empty_exhausted"] == 1

    def test_ladder_empties_get_their_own_classification_line(self, tmp_path):
        prov = Provider({"tools/c0.py": [exhausted(), transport(elapsed=9.0)]})
        *_, records = _run(tmp_path, prov)
        kinds = [m.split("kind=")[1].split()[0] for m in _msgs(records, needle="empty reply — kind=")]
        assert kinds == ["exhausted", "transport"]
        (unknown_line,) = _msgs(records, "WARNING", "Gate1[presence] UNKNOWN")
        assert unknown_line.endswith("empty (transport)")             # the LAST reply's shape

    def test_no_metadata_at_all_is_todays_ladder_verbatim(self, tmp_path):
        """A stub (or transport) that reports no CompletionMeta leaves an
        empty reply to the pre-RUN-9 path: JSON decoder reason, nudge, pin."""
        calls: list[dict] = []

        def legacy(url, headers, payload, **kw):
            calls.append(payload)
            return ""

        flt = _filter(_config())
        counters: dict = {}
        with patch("tools.llm_stream.request_completion", side_effect=legacy):
            accepted, rejected = flt.filter(_repo(tmp_path), base_dir=tmp_path, counters=counters)
        assert _params(calls) == [(4096, 0.0), (4096, 0.1)]            # the pin, no transport retry
        assert not _has_nudge(calls[1])                                # §4: no nudge after an empty reply
        assert accepted and counters["presence_empty_transport"] == 0 == counters["presence_empty_exhausted"]
        assert rejected == []


# ─────────────────────────────────────────────────────────────────────────────
# AC-4: the think=off rung
# ─────────────────────────────────────────────────────────────────────────────

class TestNoThinkRung:

    @staticmethod
    def _verdict_only_without_thinking(payload):
        return reply(REJECTED) if _think_off(payload) else exhausted()

    def test_last_rung_goes_out_with_think_false(self, tmp_path):
        prov = Provider({"tools/c0.py": [self._verdict_only_without_thinking]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov, _config({"think": "true"}))
        calls = prov.calls["tools/c0.py"]
        assert [_think_off(p) for p in calls] == [False, False, True]
        last = calls[-1]
        assert "think" not in last and "reasoning_effort" not in last
        assert last["reasoning"] == {"effort": "low", "exclude": True}
        assert last["thinking"] == {"type": "disabled"}
        assert calls[0]["thinking"] == {"type": "enabled"} and "reasoning" not in calls[0]
        assert (last["max_tokens"], last["temperature"]) == (4096, 0.0)   # pinned budget, initial temperature
        assert rejected["candidate 0"] == "already handled"
        assert counters["presence_reask"] == 1
        assert _msgs(records, needle="think=off")

    def test_rung_is_skipped_when_think_is_already_off(self, tmp_path):
        prov = Provider({"tools/c0.py": [exhausted()]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov, _config({"think": "false"}))
        assert len(prov.calls["tools/c0.py"]) == 2 and accepted == ["candidate 0"]
        assert len(_msgs(records, needle="nothing to switch off")) == 1

    def test_rung_is_made_once_even_when_it_is_empty_too(self, tmp_path):
        prov = Provider({"tools/c0.py": [exhausted()]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov, _config({"think": "true"}))
        calls = prov.calls["tools/c0.py"]
        assert [_think_off(p) for p in calls] == [False, False, True]
        (unknown_line,) = _msgs(records, "WARNING", "Gate1[presence] UNKNOWN")
        assert unknown_line.endswith("after 2 re-ask(s): empty (exhausted)")

    def test_rung_in_strict_mode_comes_after_the_whole_grid(self, tmp_path):
        prov = Provider({"tools/c0.py": [self._verdict_only_without_thinking]})
        _run(tmp_path, prov, _config({"think": "true", "unparseable_retry_mode": "strict"}))
        calls = prov.calls["tools/c0.py"]
        assert len(calls) == 8 and [_think_off(p) for p in calls] == [False] * 7 + [True]

    def test_rung_follows_the_profile_think_not_the_shared_one(self, tmp_path):
        profile = {"gate1_llm": {"base_url": "http://stub.local:1337/v1", "api_key": "k",
                                 "model": "profile-model", "api_format": "openai", "think": "true"}}
        prov = Provider({"tools/c0.py": [self._verdict_only_without_thinking]})
        cfg = _config({"think": "false", "presence_llm_profile": "gate1_llm"}, extra=profile)
        _, accepted, rejected, *_ = _run(tmp_path, prov, cfg)
        calls = prov.calls["tools/c0.py"]
        assert all(p["model"] == "profile-model" for p in calls)
        assert _think_off(calls[-1]) and rejected["candidate 0"] == "already handled"

    def test_no_rung_when_the_ladder_is_disabled(self, tmp_path):
        prov = Provider({"tools/c0.py": [exhausted()]})
        _run(tmp_path, prov, _config({"think": "true", "unparseable_max_retries": "0"}))
        assert len(prov.calls["tools/c0.py"]) == 1


# ─────────────────────────────────────────────────────────────────────────────
# AC-5: the no-think request is not honoured
# ─────────────────────────────────────────────────────────────────────────────

class TestNoThinkIgnored:

    def test_warned_once_per_provider_for_five_candidates(self, tmp_path):
        prov = Provider({}, default=[exhausted(reasoning_chars=1200)])
        _, accepted, rejected, counters, records = _run(tmp_path, prov, n=5)
        warns = _msgs(records, "WARNING", "no-think request is not honoured")
        assert len(warns) == 1
        assert warns[0] == ("provider streams reasoning_content despite think=false — the no-think "
                            "request is not honoured by http://stub.local:1337/v1/test-model; empty "
                            "replies from this model are exhaustion, not transport")
        assert counters["presence_nothink_ignored"] == 1
        assert counters["presence_empty_exhausted"] == 5
        assert len(accepted) == 5

    def test_still_once_with_workers(self, tmp_path):
        prov = Provider({}, default=[exhausted(reasoning_chars=5)])
        _, accepted, rejected, counters, records = _run(tmp_path, prov, _config({"presence_workers": "4"}), n=8)
        assert len(_msgs(records, "WARNING", "no-think request is not honoured")) == 1
        assert counters["presence_nothink_ignored"] == 1 and counters["presence_empty_exhausted"] == 8

    def test_not_counted_when_think_is_true(self, tmp_path):
        prov = Provider({"tools/c0.py": [reply(REJECTED, reasoning_chars=50)]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov, _config({"think": "true"}))
        assert counters["presence_nothink_ignored"] == 0 and not _msgs(records, "WARNING", "not honoured")

    def test_counted_on_a_non_empty_reply_too(self, tmp_path):
        prov = Provider({"tools/c0.py": [reply(REJECTED, reasoning_chars=50)]})
        _, accepted, rejected, counters, _ = _run(tmp_path, prov)
        assert counters["presence_nothink_ignored"] == 1 and rejected["candidate 0"] == "already handled"


# ─────────────────────────────────────────────────────────────────────────────
# AC-4 / point 4: the split line and the wording
# ─────────────────────────────────────────────────────────────────────────────

class TestSplitAndWording:

    def test_split_fields_and_order(self):
        assert format_gate1_split({}) == (
            "existence=0 presence_confirmed=0 presence_rejected=0 "
            "presence_fail_closed=0 presence_unknown=0 presence_reask=0 "
            "presence_empty_transport=0 presence_empty_exhausted=0 "
            "presence_nothink_ignored=0 duplicate=0 non_py=0"
        )
        out = split_gate1_results([], empty_transport=7, empty_exhausted=8, nothink_ignored=9)
        assert (out["presence_empty_transport"], out["presence_empty_exhausted"], out["presence_nothink_ignored"]) == (7, 8, 9)

    def test_garbled_forever_says_garbled_and_keeps_the_parser_reason(self, tmp_path):
        prov = Provider({"tools/c0.py": [reply(GARBLED)]})
        _, accepted, rejected, counters, records = _run(tmp_path, prov, _config({"unparseable_retry_mode": "strict"}))
        assert len(prov.calls["tools/c0.py"]) == 7 and all(_has_nudge(p) for p in prov.calls["tools/c0.py"][1:])
        (unknown_line,) = _msgs(records, "WARNING", "Gate1[presence] UNKNOWN")
        assert "after 6 re-ask(s): garbled: JSON decode failed (Expecting value" in unknown_line
        assert counters["presence_empty_transport"] == 0 == counters["presence_empty_exhausted"]
        assert not _msgs(records, needle="empty reply — kind=")

    def test_trace_has_one_request_and_one_response_per_http_call(self, tmp_path):
        events: list[dict] = []

        class Rec:
            def event(self, source=None, target=None, kind=None, params=None, **extra):
                events.append({"kind": kind, "params": dict(params or {})})

        prov = Provider({"tools/c0.py": [transport()]})
        with patch("tools.auto.gate1_filter.tracer", Rec()):
            _run(tmp_path, prov)
        assert len(prov.calls["tools/c0.py"]) == 4
        assert sum(e["kind"] == "llm_request" for e in events) == 4
        assert sum(e["kind"] == "llm_response" for e in events) == 4
        assert all(e["params"].get("candidate") == "candidate 0" for e in events if e["kind"] == "llm_request")


# ─────────────────────────────────────────────────────────────────────────────
# AC-9: the cap log names the budget that is sent
# ─────────────────────────────────────────────────────────────────────────────

class TestCapLog:

    @staticmethod
    def _cap_then_skip_pairs(records):
        msgs = [r.getMessage() for r in records]
        bad = []
        for i, m in enumerate(msgs):
            if "capped at unparseable_max_tokens_cap" not in m:
                continue
            cap = int(re.search(r"unparseable_max_tokens_cap=(\d+)", m).group(1))
            nxt = msgs[i + 1] if i + 1 < len(msgs) else ""
            if "already tried" in nxt and int(re.search(r"max_tokens=(\d+)", nxt).group(1)) != cap:
                bad.append((m, nxt))
        return bad

    def test_pinned_ladder_never_logs_a_cap_it_does_not_send(self, tmp_path):
        prov = Provider({"tools/c0.py": [exhausted()]})
        *_, records = _run(tmp_path, prov, _config({"unparseable_max_tokens_cap": "8192"}))
        assert self._cap_then_skip_pairs(records) == []
        assert not _msgs(records, needle="capped at")                  # the pin decided, not the cap

    def test_legitimate_cap_is_logged_right_before_the_capped_reask(self, tmp_path):
        prov = Provider({"tools/c0.py": [reply(GARBLED)]})
        *_, records = _run(tmp_path, prov, _config({"unparseable_retry_mode": "strict",
                                                    "unparseable_max_tokens_cap": "8192"}))
        msgs = [r.getMessage() for r in records if r.name == LOGGER]
        cap_idx = [i for i, m in enumerate(msgs) if "capped at unparseable_max_tokens_cap=8192" in m]
        assert cap_idx                                                 # tiers 16384 (×2) hit the cap
        for i in cap_idx:
            assert "re-asking" in msgs[i + 1] and "max_tokens=8192" in msgs[i + 1]
        assert max(p["max_tokens"] for p in prov.calls["tools/c0.py"]) == 8192


# ─────────────────────────────────────────────────────────────────────────────
# AC-6: stream_options end to end (real request_completion, fake urlopen)
# ─────────────────────────────────────────────────────────────────────────────

def _sse(*chunks) -> bytes:
    return b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks) + b"data: [DONE]\n\n"


def _content_chunk(text: str, finish: str | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}]}


class _Resp:
    def __init__(self, body: bytes):
        self._lines = body.splitlines(keepends=True)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


class FakeUrlopen:
    def __init__(self, script: list):
        self.script = script
        self.sent: list[dict] = []

    def __call__(self, req, timeout=None, context=None):
        self.sent.append(json.loads(req.data.decode("utf-8")))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, tuple):
            raise urllib.error.HTTPError(req.full_url, step[0], "bad request", {}, io.BytesIO(step[1].encode()))
        return _Resp(step)


@pytest.fixture(autouse=True)
def _clean_stream_options_memory():
    _llm_stream._STREAM_OPTIONS_UNSUPPORTED_KEYS.clear()
    yield
    _llm_stream._STREAM_OPTIONS_UNSUPPORTED_KEYS.clear()


class TestStreamOptionsEndToEnd:

    def test_presence_call_asks_for_usage(self, tmp_path, monkeypatch):
        fake = FakeUrlopen([_sse(_content_chunk(REJECTED, "stop"))])
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        flt = _filter(_config())
        with patch("tools.auto.gate1_filter.time.sleep", lambda s: None):
            accepted, rejected = flt.filter(_repo(tmp_path), base_dir=tmp_path)
        assert fake.sent[0]["stream_options"] == {"include_usage": True} and fake.sent[0]["stream"] is True
        assert rejected and rejected[0].reason == "already handled"

    def test_http_400_on_the_field_is_paid_once_per_run(self, tmp_path, monkeypatch):
        fake = FakeUrlopen([(400, '{"error": {"message": "Unrecognized request argument supplied: stream_options"}}'),
                            _sse(_content_chunk(REJECTED, "stop"))])
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        flt = _filter(_config())
        counters: dict = {}
        with patch("tools.auto.gate1_filter.time.sleep", lambda s: None):
            accepted, rejected = flt.filter(_repo(tmp_path, 2), base_dir=tmp_path, counters=counters)
        assert [("stream_options" in p) for p in fake.sent] == [True, False, False]   # 400, retry, candidate 1
        assert len(rejected) == 2 and counters["presence_unknown"] == 0

    def test_role_only_stream_is_a_transport_empty_end_to_end(self, tmp_path, monkeypatch):
        role_only = _sse({"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
                         {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                         {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 0}})
        fake = FakeUrlopen([role_only, role_only, _sse(_content_chunk(REJECTED, "stop"))])
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        flt = _filter(_config())
        counters: dict = {}
        with patch("tools.auto.gate1_filter.time.sleep", lambda s: None):
            accepted, rejected = flt.filter(_repo(tmp_path), base_dir=tmp_path, counters=counters)
        assert len(fake.sent) == 3 and rejected[0].reason == "already handled"
        assert counters["presence_empty_transport"] == 1

    def test_ollama_end_to_end(self, tmp_path, monkeypatch):
        def nd(*lines):
            return b"".join(json.dumps(l).encode() + b"\n" for l in lines)
        empty = nd({"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "eval_count": 0})
        verdict = nd({"message": {"role": "assistant", "content": REJECTED}, "done": False},
                     {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "eval_count": 20})
        fake = FakeUrlopen([empty, empty, verdict])
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        flt = _filter(_config(api_format="ollama"))
        counters: dict = {}
        with patch("tools.auto.gate1_filter.time.sleep", lambda s: None):
            accepted, rejected = flt.filter(_repo(tmp_path), base_dir=tmp_path, counters=counters)
        assert len(fake.sent) == 3 and all("stream_options" not in p for p in fake.sent)
        assert rejected[0].reason == "already handled" and counters["presence_empty_transport"] == 1
