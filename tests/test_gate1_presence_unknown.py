"""tests/test_gate1_presence_unknown.py — RUN-5: a technical failure in the
presence check is not a rejection.

Story background
----------------
Live plans on ``../testtext`` (run ``c506283620d3``) and ``../testtext6``
(run ``4a93f240ff48``) with ``sensenova-6.7-flash-lite``: 406 of 667 gate-1
replies were the empty string. The re-ask ladder handles a *garbled* reply
well, and GATE1-LEARN-2 correctly stops raising tokens for an *empty* one,
but the ladder's exit was the same for both — ``return last_confirmed,
last_reason``, i.e. ``False, "JSON decode failed …"`` — and the candidate
was then treated exactly like one the model examined and rejected: 142 of
403 candidates (35 %) in testtext and 95 of 375 (25 %) in testtext6 were
dropped from the plan without any model ever saying no, and
``scripts/trace_round_snapshot.py`` reported ``unparsed 511 / 883`` where
the baseline had 0–1. Nothing downstream could be measured while the
plan's membership was decided by provider silence.

Fix: ``_check_presence`` returns a third outcome, ``unknown``, produced only
when the ladder ends with ``_is_technical_failure(reason)`` true (empty
reply, JSON decode failure, transport error after every retry). A model that
answered ``rejected`` is still ``rejected``. ``[gate1] presence_unknown``
(default ``keep``) decides what to do with it, the kept candidate carries a
``presence unknown — …`` reason, the stage logs ``UNKNOWN`` at WARNING
instead of ``REJECTED``, and the M4 split counts it in its own
``presence_unknown`` column.
"""

from __future__ import annotations

import configparser
import importlib.util
import io
import json
import logging
import re
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import (
    UNKNOWN_PRESENCE_REASON,
    FilterResult,
    Gate1Filter,
    _is_technical_failure,
    format_gate1_split,
    gate1_outcome,
    split_gate1_results,
)
from tools.auto.run_trace import RunTrace


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures and helpers
# ─────────────────────────────────────────────────────────────────────────────

def _config(*, presence_unknown: str | None = None,
            unparseable_max_retries: str = "2") -> configparser.ConfigParser:
    """2 re-asks, not 6: enough to prove a verdict reached from the ladder,
    and this suite is not the place to pay for the whole ladder."""
    gate1: dict[str, str] = {
        "temperature": "0.0", "max_tokens": "64", "skip_llm": "false",
        "unparseable_max_retries": unparseable_max_retries,
        # No wait: these tests are about the verdict, not the retry timing
        # (see tests_bugfix/test_gate1_log_levels.py for the same split).
        "llm_call_retry_wait_sec": "0",
    }
    if presence_unknown is not None:
        gate1["presence_unknown"] = presence_unknown
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "gate1": gate1,
        "loop":  {"timeout_seconds": "10"},
    })
    return cfg


def _filter(cfg: configparser.ConfigParser) -> Gate1Filter:
    return Gate1Filter(
        config=cfg, base_url="http://localhost:1337/v1", api_key="test",
        model="test-model", api_format="openai", verify_ssl=False,
    )


def _candidate(title: str, file: str, symbol: str) -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction="fix the problem described in the claim",
        target_files=[file],
        acceptance_check="python -m pytest tests -q",
        cited_location=CitedLocation(file=file, symbol=symbol),
        cluster="agents",
    )


def _repo(tmp_path: Path, n: int = 3) -> list[CandidateTask]:
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    out: list[CandidateTask] = []
    for i in range(n):
        (tmp_path / "tools" / f"c{i}.py").write_text(
            textwrap.dedent(f"""\
                def parse_config_{i}(raw):
                    # TODO: validate input
                    return raw
            """),
            encoding="utf-8",
        )
        out.append(_candidate(f"candidate {i}", f"tools/c{i}.py", f"parse_config_{i}"))
    return out


def _code_block_from(user_msg: str) -> str:
    m = re.search(r"Code at that location:\n```\n(.*?)\n```", user_msg, re.S)
    return m.group(1) if m else ""


def _confirmed(user_msg: str) -> str:
    """A confirmed verdict grounded in the code actually shown to the model."""
    lines = [ln.strip() for ln in _code_block_from(user_msg).splitlines() if ln.strip()]
    evidence = lines[0][:200] if lines else "def "
    return json.dumps({"verdict": "confirmed", "evidence": evidence,
                       "reason": "the claim holds"})


def _rejected(user_msg: str) -> str:
    return json.dumps({"verdict": "rejected", "reason": "already handled"})


def _make_provider(plans: dict[str, list]) -> tuple[object, dict]:
    """Reply per candidate file, consumed in call order.

    `plans` maps a file path to a list of replies; the last reply is held
    until the ladder runs out. Returns (fake, calls) where `calls` maps a
    path to every prompt it received, so the number of attempts per
    candidate is observable.
    """
    calls: dict[str, list] = {}

    def fake_request_completion(url, headers, payload, **kwargs):
        user_msg = payload["messages"][-1]["content"]
        location = re.search(r"Location:\s*(.+)$", user_msg, re.M)
        path = location.group(1).split(",")[0].strip() if location else ""
        plan = plans[path]
        calls.setdefault(path, []).append(user_msg)
        index = min(len(calls[path]) - 1, len(plan) - 1)
        step = plan[index]
        return step(user_msg) if callable(step) else step

    return fake_request_completion, calls


class RecordingTracer:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def event(self, source=None, target=None, kind=None, params=None, **extra) -> None:
        self.events.append({
            "source": source, "target": target, "kind": kind,
            "params": dict(params or {}),
        })

    def kinds(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]


def _load_trace_round_snapshot():
    spec = importlib.util.spec_from_file_location(
        "trace_round_snapshot", PROJECT_ROOT / "scripts" / "trace_round_snapshot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(fn, *args, **kwargs) -> str:
    buf, old = io.StringIO(), sys.stdout
    sys.stdout = buf
    try:
        fn(*args, **kwargs)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _trace_event(kind: str, params: dict, run_id: str = "r1") -> dict:
    return {
        "seq": 1, "ts": "2026-01-01T00:00:00+00:00", "run_id": run_id,
        "source": "controller", "target": "gate1_filter", "kind": kind,
        "params": params, "content": "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Config: the new [gate1] key
# ─────────────────────────────────────────────────────────────────────────────

class TestPresenceUnknownConfig:

    def test_default_is_keep(self):
        cfg = _config()
        assert "presence_unknown" not in cfg["gate1"]
        assert _filter(cfg)._presence_unknown_policy == "keep"

    def test_explicit_keep(self):
        cfg = _config(presence_unknown="keep")
        assert _filter(cfg)._presence_unknown_policy == "keep"

    def test_explicit_reject(self):
        cfg = _config(presence_unknown="reject")
        assert _filter(cfg)._presence_unknown_policy == "reject"

    def test_case_insensitive(self):
        cfg = _config(presence_unknown="  REJECT  ")
        assert _filter(cfg)._presence_unknown_policy == "reject"

    def test_malformed_falls_back_to_keep(self, caplog):
        cfg = _config(presence_unknown="maybe")
        with caplog.at_level(logging.WARNING, logger="tools.auto.gate1_filter"):
            assert _filter(cfg)._presence_unknown_policy == "keep"
        assert any("presence_unknown is malformed" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# The classifier: an unknown reason is a technical failure, not a verdict
# ─────────────────────────────────────────────────────────────────────────────

class TestUnknownReasonClassification:

    def test_unknown_reason_is_a_technical_failure(self):
        reason = (
            f"{UNKNOWN_PRESENCE_REASON} after 2 re-ask(s): "
            "JSON decode failed (Expecting value: line 1 column 1) — failing closed"
        )
        assert _is_technical_failure(reason) is True

    def test_unknown_reason_is_not_a_model_reason(self):
        assert _is_technical_failure("already handled") is False
        assert _is_technical_failure("LLM found problem absent or already fixed") is False

    def test_unknown_acceptance_is_not_a_confirmation(self):
        assert gate1_outcome(
            "presence",
            f"{UNKNOWN_PRESENCE_REASON} after 2 re-ask(s): JSON decode failed (x)",
            True,
        ) == ""

    def test_unknown_rejection_is_not_fail_closed(self):
        assert gate1_outcome(
            "presence",
            f"{UNKNOWN_PRESENCE_REASON} after 0 re-ask(s): LLM call failed: boom",
            False,
        ) == ""


# ─────────────────────────────────────────────────────────────────────────────
# Policy: keep is the default, reject is today's behaviour
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyReplyIsUnknown:
    """Acceptance: a stub that returns "" for every attempt."""

    def test_empty_reply_kept_under_the_default_policy(self, tmp_path: Path, caplog):
        filt = _filter(_config())
        plans = {f"tools/c{i}.py": [lambda user_msg: ""] for i in range(3)}
        fake, calls = _make_provider(plans)
        with caplog.at_level(logging.INFO, logger="tools.auto.gate1_filter"):
            with patch("tools.llm_stream.request_completion", side_effect=fake):
                accepted, rejected = filt.filter(_repo(tmp_path, 3), tmp_path)

        assert len(accepted) == 3
        assert rejected == []
        # The ladder ran out, so the counter says how many candidates never
        # got a verdict.
        assert filt.presence_unknown == 3
        assert all(len(v) == 3 for v in calls.values())
        # The reason travels with the candidate: the KEPT line carries it,
        # and it names both the absence of a verdict and the cause behind it.
        kept = [r for r in caplog.records if "KEPT (presence_unknown = keep)" in r.message]
        assert len(kept) == 3
        for record in kept:
            assert UNKNOWN_PRESENCE_REASON in record.message
            assert "JSON decode failed" in record.message
            assert "after 2 re-ask(s)" in record.message

    def test_empty_reply_dropped_under_reject(self, tmp_path: Path):
        filt = _filter(_config(presence_unknown="reject"))
        plans = {f"tools/c{i}.py": [lambda user_msg: ""] for i in range(3)}
        fake, _ = _make_provider(plans)
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            accepted, rejected = filt.filter(_repo(tmp_path, 3), tmp_path)

        assert accepted == []
        assert len(rejected) == 3
        assert all(r.reason.startswith(UNKNOWN_PRESENCE_REASON) for r in rejected)
        assert all("JSON decode failed" in r.reason for r in rejected)
        assert filt.presence_unknown == 3


class TestUnknownReasonTravelsWithTheTask:
    """The FilterResult never leaves filter(); the instruction is the field
    that reaches plan.json and the coder prompt."""

    def test_kept_unknown_instruction_carries_the_note_once(self, tmp_path: Path):
        filt = _filter(_config())
        plans = {f"tools/c{i}.py": [lambda user_msg: ""] for i in range(2)}
        fake, _ = _make_provider(plans)
        cands = _repo(tmp_path, 2)
        before = [c.instruction for c in cands]
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            accepted, _ = filt.filter(cands, tmp_path)

        assert len(accepted) == 2
        for c, orig in zip(accepted, before):
            assert c.instruction.startswith(orig)
            assert c.instruction.count(UNKNOWN_PRESENCE_REASON) == 1
            assert "JSON decode failed" in c.instruction
            assert "before changing code" in c.instruction
        # A second pass that ends unknown again (a --validate-plan re-check)
        # does not stack a second note.
        fake2, _ = _make_provider(plans)
        with patch("tools.llm_stream.request_completion", side_effect=fake2):
            accepted2, _ = _filter(_config()).filter(accepted, tmp_path)
        assert all(c.instruction.count(UNKNOWN_PRESENCE_REASON) == 1 for c in accepted2)

    def test_confirmed_and_rejected_instructions_are_untouched(self, tmp_path: Path):
        filt = _filter(_config())
        plans = {"tools/c0.py": [_confirmed], "tools/c1.py": [_rejected]}
        fake, _ = _make_provider(plans)
        cands = _repo(tmp_path, 2)
        before = [c.instruction for c in cands]
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            accepted, rejected = filt.filter(cands, tmp_path)
        assert [c.instruction for c in cands] == before
        assert len(accepted) == 1 and len(rejected) == 1

    def test_reject_policy_leaves_the_instruction_alone(self, tmp_path: Path):
        filt = _filter(_config(presence_unknown="reject"))
        plans = {"tools/c0.py": [lambda user_msg: ""]}
        fake, _ = _make_provider(plans)
        cands = _repo(tmp_path, 1)
        before = cands[0].instruction
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            filt.filter(cands, tmp_path)
        assert cands[0].instruction == before


class TestLadderStillGivesModelVerdicts:

    def test_garbage_then_rejected_is_rejected(self, tmp_path: Path):
        """Acceptance: the ladder's success path is unchanged."""
        filt = _filter(_config())
        plans = {
            "tools/c0.py": ["not json at all", _rejected],
            "tools/c1.py": [_confirmed],
            "tools/c2.py": [_rejected],
        }
        fake, calls = _make_provider(plans)
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            accepted, rejected = filt.filter(_repo(tmp_path, 3), tmp_path)

        assert [c.title for c in accepted] == ["candidate 1"]
        assert [r.candidate.title for r in rejected] == ["candidate 0", "candidate 2"]
        assert all("presence unknown" not in r.reason for r in rejected)
        # c0 answered on the re-ask: a real verdict, so not unknown.
        assert filt.presence_unknown == 0
        assert filt.presence_reask == 1
        assert len(calls["tools/c0.py"]) == 2

    def test_rejected_on_the_first_call_increments_nothing(self, tmp_path: Path):
        """Acceptance: no `unknown` counter increment for a clean rejection."""
        filt = _filter(_config())
        fake, calls = _make_provider({"tools/c0.py": [_rejected]})
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            accepted, rejected = filt.filter(_repo(tmp_path, 1), tmp_path)

        assert accepted == []
        assert len(rejected) == 1
        assert filt.presence_unknown == 0
        assert filt.presence_reask == 0
        assert len(calls["tools/c0.py"]) == 1

    def test_transport_error_is_unknown_not_rejected(self, tmp_path: Path):
        filt = _filter(_config())
        with patch("tools.auto.gate1_filter.time.sleep"), \
             patch("tools.llm_stream.request_completion",
                   side_effect=RuntimeError("HTTP 500 from provider")):
            accepted, rejected = filt.filter(_repo(tmp_path, 1), tmp_path)

        assert len(accepted) == 1
        assert rejected == []
        assert filt.presence_unknown == 1
        assert accepted[0].title == "candidate 0"


# ─────────────────────────────────────────────────────────────────────────────
# Logging: UNKNOWN is a warning, and REJECTED is reserved for the model
# ─────────────────────────────────────────────────────────────────────────────

class TestUnknownLogsAsUnknown:

    def test_unknown_logs_at_warning_not_rejected(self, tmp_path: Path, caplog):
        filt = _filter(_config())
        fake, _ = _make_provider({"tools/c0.py": [lambda user_msg: ""]})
        with caplog.at_level(logging.INFO, logger="tools.auto.gate1_filter"):
            with patch("tools.llm_stream.request_completion", side_effect=fake):
                filt.filter(_repo(tmp_path, 1), tmp_path)

        unknown = [r for r in caplog.records if "Gate1[presence] UNKNOWN" in r.message]
        assert unknown, "expected a UNKNOWN log line"
        assert unknown[0].levelno == logging.WARNING
        assert "candidate 0" in unknown[0].message
        assert not any("Gate1[presence] REJECTED" in r.message for r in caplog.records)

    def test_unknown_kept_is_visible_in_the_presence_summary(self, tmp_path: Path, capsys):
        filt = _filter(_config())
        plans = {
            "tools/c0.py": [lambda user_msg: ""],
            "tools/c1.py": [_confirmed],
            "tools/c2.py": [lambda user_msg: ""],
        }
        fake, _ = _make_provider(plans)
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            filt.filter(_repo(tmp_path, 3), tmp_path)
        out = capsys.readouterr().out
        assert "3/3 candidate(s) confirmed" in out
        assert "(2 without a verdict — presence_unknown = keep)" in out

    def test_rejected_still_logs_at_info(self, tmp_path: Path, caplog):
        filt = _filter(_config(presence_unknown="reject"))
        fake, _ = _make_provider({"tools/c0.py": [_rejected]})
        with caplog.at_level(logging.INFO, logger="tools.auto.gate1_filter"):
            with patch("tools.llm_stream.request_completion", side_effect=fake):
                filt.filter(_repo(tmp_path, 1), tmp_path)
        rejected = [r for r in caplog.records if "Gate1[presence] REJECTED" in r.message]
        assert len(rejected) == 1
        assert rejected[0].levelno == logging.INFO


# ─────────────────────────────────────────────────────────────────────────────
# M4: the counter, the split, and the two readers
# ─────────────────────────────────────────────────────────────────────────────

class TestPresenceUnknownCounter:

    def test_counter_appears_in_the_gate1_split(self, tmp_path: Path):
        """Acceptance: `presence_unknown` in the gate-1 summary event."""
        filt = _filter(_config())
        plans = {
            "tools/c0.py": [_confirmed],
            "tools/c1.py": [_rejected],
            "tools/c2.py": [lambda user_msg: ""],
            "tools/c3.py": ["", _confirmed],
            "tools/c4.py": [lambda user_msg: ""],
        }
        fake, _ = _make_provider(plans)
        counts: dict = {}
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            filt.filter(_repo(tmp_path, 5), tmp_path, counters=counts)

        assert counts["presence_unknown"] == 2
        assert counts["presence_confirmed"] == 2   # c0, c3 (on the re-ask)
        assert counts["presence_rejected"] == 1    # c1
        assert counts["presence_fail_closed"] == 0
        assert counts["presence_reask"] == 1
        assert filt.presence_unknown == 2
        assert "presence_unknown=2" in format_gate1_split(counts)

    def test_split_fields_always_print_including_unknown(self):
        assert format_gate1_split({}) == (
            "existence=0 presence_confirmed=0 presence_rejected=0 "
            "presence_fail_closed=0 presence_unknown=0 presence_reask=0 "
            "presence_empty_transport=0 presence_empty_exhausted=0 "
            "presence_nothink_ignored=0 "
            "duplicate=0 non_py=0"
        )

    def test_split_gate1_results_counts_unknown_from_the_counter(self):
        out = split_gate1_results(
            [], reask=1, unknown=3, non_py=0,
        )
        assert out["presence_unknown"] == 3
        assert out["presence_reask"] == 1
        assert out["presence_fail_closed"] == 0

    def test_unknown_does_not_double_count_as_fail_closed(self):
        """A `reject`-policy unknown lands in `presence_unknown` only."""
        results = [
            FilterResult(candidate=None, accepted=False, stage="presence",
                         reason=f"{UNKNOWN_PRESENCE_REASON} after 2 re-ask(s): "
                                "JSON decode failed (x) — failing closed"),
            FilterResult(candidate=None, accepted=False, stage="presence",
                         reason="already handled"),
        ]
        out = split_gate1_results(results, unknown=1)
        assert out["presence_unknown"] == 1
        assert out["presence_fail_closed"] == 0
        assert out["presence_rejected"] == 1

    def test_gate1_summary_event_carries_the_counter(self, monkeypatch):
        """Acceptance: the counter is in the gate-1 summary event."""
        rec = RecordingTracer()
        monkeypatch.setattr("tools.auto.run_trace.tracer", rec)
        run_trace = RunTrace(SimpleNamespace(log=lambda *a: None), "run123", None)

        run_trace.log_gate1_split({
            "accepted": 3, "rejected": 1, "existence": 0,
            "presence_confirmed": 2, "presence_rejected": 1,
            "presence_fail_closed": 0, "presence_unknown": 2,
            "presence_reask": 1, "duplicate": 0, "non_py": 0, "_uncounted": 0,
        })

        (event,) = rec.kinds("gate1_split")
        assert event["params"]["presence_unknown"] == 2
        assert "_uncounted" not in event["params"]


# ─────────────────────────────────────────────────────────────────────────────
# presence_workers > 1: the third outcome travels through the pool in order
# ─────────────────────────────────────────────────────────────────────────────

class TestPresenceWorkersCarryUnknown:

    def test_unknown_outcomes_kept_in_candidate_order(self, tmp_path: Path):
        """Acceptance: `presence_workers > 1` carries the third outcome."""
        cfg = _config()
        cfg.set("gate1", "presence_workers", "3")
        filt = _filter(cfg)
        plans = {
            "tools/c0.py": [lambda user_msg: ""],
            "tools/c1.py": [_rejected],
            "tools/c2.py": [lambda user_msg: ""],
        }
        fake, _ = _make_provider(plans)
        counts: dict = {}
        with patch("tools.llm_stream.request_completion", side_effect=fake):
            accepted, rejected = filt.filter(_repo(tmp_path, 3), tmp_path,
                                             counters=counts)

        assert [c.title for c in accepted] == ["candidate 0", "candidate 2"]
        assert [r.candidate.title for r in rejected] == ["candidate 1"]
        assert filt.presence_unknown == 2
        assert counts["presence_unknown"] == 2
        assert counts["presence_rejected"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# scripts/trace_round_snapshot.py: unknown is its own column
# ─────────────────────────────────────────────────────────────────────────────

class TestTraceRoundSnapshotUnknownColumn:

    def test_read_run_reads_unknown_from_the_gate1_split_event(self, tmp_path):
        module = _load_trace_round_snapshot()
        agent = tmp_path / ".agent"
        agent.mkdir()
        with open(agent / "trace_r1.jsonl", "w", encoding="utf-8") as fh:
            for event in (
                _trace_event("run_start", {"goal": "g"}),
                _trace_event("gate1_split", {
                    "accepted": 2, "rejected": 1, "existence": 0,
                    "presence_confirmed": 1, "presence_rejected": 1,
                    "presence_fail_closed": 0, "presence_unknown": 2,
                    "presence_reask": 0, "duplicate": 0, "non_py": 0,
                }),
            ):
                fh.write(json.dumps(event) + "\n")

        snap = module.read_run(tmp_path)

        assert snap["gate1"]["unknown"] == 2
        # Its own column, not folded into the per-response `unparsed` count.
        assert snap["gate1"]["unparsed"] == 0
        # The per-candidate verdicts are still read from the gate1_split
        # event, not invented from the per-response counts above.
        assert snap["gate1_split"]["presence_rejected"] == 1

    def test_main_prints_the_unknown_column(self, monkeypatch, tmp_path):
        module = _load_trace_round_snapshot()
        # The run name is the base dir's basename, and it is printed in a
        # 12-char column — keep it short or the row shifts under the header.
        base = tmp_path / "r1"
        agent = base / ".agent"
        agent.mkdir(parents=True)
        with open(agent / "trace_r1.jsonl", "w", encoding="utf-8") as fh:
            for event in (
                _trace_event("run_start", {"goal": "g"}),
                _trace_event("gate1_split", {
                    "accepted": 2, "presence_unknown": 2,
                    "presence_rejected": 1, "presence_confirmed": 1,
                    "presence_fail_closed": 0, "presence_reask": 0,
                    "existence": 0, "duplicate": 0, "non_py": 0,
                }),
            ):
                fh.write(json.dumps(event) + "\n")

        monkeypatch.setattr("sys.argv", ["trace_round_snapshot.py", str(base)])
        out = _capture(module.main)

        assert "unk" in out
        rows = [ln for ln in out.splitlines() if not ln.startswith("-")]
        # Columns are fixed-width and the TOTAL row has empty fields, so read
        # the field by character offset rather than by splitting on spaces.
        start = rows[0].index("unk")
        assert [ln[start:start + 4].strip() for ln in rows[1:]] == ["2", "2"]
        assert "TOTAL" in rows[2]

    def test_pre_run5_split_event_prints_zero(self, tmp_path):
        """A gate1_split event that predates the column still renders."""
        module = _load_trace_round_snapshot()
        agent = tmp_path / ".agent"
        agent.mkdir()
        with open(agent / "trace_r1.jsonl", "w", encoding="utf-8") as fh:
            for event in (
                _trace_event("run_start", {"goal": "g"}),
                _trace_event("gate1_split", {
                    "accepted": 1, "presence_rejected": 0,
                    "presence_confirmed": 1, "presence_fail_closed": 0,
                    "presence_reask": 0, "existence": 0, "duplicate": 0,
                    "non_py": 0,
                }),
            ):
                fh.write(json.dumps(event) + "\n")

        snap = module.read_run(tmp_path)
        assert snap["gate1"]["unknown"] == 0
