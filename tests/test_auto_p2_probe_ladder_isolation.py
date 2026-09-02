"""tests/test_auto_p2_probe_ladder_isolation.py — AUTO-P2: an architect
context-probe reply must not be consumed by the AUTO-H4 or AUTO-H5 ladder.

Background
----------
AUTO-P lets the Architect answer with ``ARCH_PROBE: facts <name>`` instead
of the JSON task array when it lacks the facts to plan. That line is
non-JSON prose, so ``_parse_candidates_ex`` classifies it *unsalvageable* —
the exact input AUTO-H5 exists to retry. Left unguarded, a probe reply
would trigger up to ``empty_response_retry_max`` escalating re-asks of the
identical question, ignoring what the model actually requested. The failure
is silent: the logs read as a flaky model, and the feature looks like it
does nothing.

AUTO-P2 is the guard, landed *before* the parser that would trigger it
(``arch_probe.extract_probe_request`` is still a stub returning ``[]``), so
it can be proven correct against a function that returns nothing. These
tests monkeypatch that module attribute to inject ops.

  AC-P2-1  A probe reply consumes NO AUTO-H5 retry — one call, not seven.
  AC-P2-2  A probe reply consumes NO AUTO-H4 shrink — max_tasks is
           unchanged, verified from the prompt text actually sent.
  AC-P2-3  A parser that returns [] for a malformed request means the
           response is NOT a probe and falls through to AUTO-H5 normally.
  AC-P2-4  A genuinely unsalvageable response still drives AUTO-H5 exactly
           as before this epic (guards the 3-tuple → 4-tuple change).
  AC-P2-5  A genuinely truncated response still drives AUTO-H4 exactly as
           before (same guard).
  AC-P2-6  A transient LLM failure still returns None so the batch is NOT
           checkpointed (the fourth early-return path through the widened
           closure).
  AC-P2-7  probe_enabled=false → a response that merely CONTAINS the
           ARCH_PROBE: prefix is treated as unsalvageable, not as a probe;
           the parser is never called at all.
  AC-P2-8  A parser that raises is contained: the response is handled as a
           normal (non-probe) reply, and the batch is not aborted.
  AC-P2-9  A probe reply that also carried salvageable tasks keeps them.

AC-P2-4 and AC-P2-5 duplicate coverage that
``test_auto_h4_shrink_retry.py`` / ``test_auto_h5_empty_response_retry.py``
already own. That is deliberate and they should not be deleted as
redundant: AUTO-P2 changes the arity of the ``_call_and_parse`` closure
both ladders read their verdict from, and these two are the regression net
for *that* change, sitting next to the code that motivated it.

All LLM calls are patched; no network and no real sleep I/O occurs.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto import arch_probe
from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures — mirror test_auto_h5_empty_response_retry.py
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**architect_overrides: str) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    architect = {"temperature": "0.2", "max_tokens": "512"}
    architect.update(architect_overrides)
    c.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url":   "http://localhost:1337/v1",
            "api_key":    "test",
            "model":      "test-model",
            "api_format": "openai",
        },
        "architect": architect,
        "loop":      {"timeout_seconds": "10"},
    })
    return c


@pytest.fixture()
def cfg() -> configparser.ConfigParser:
    """Probe ON — the configuration AUTO-P2's guard is written for."""
    return _cfg(probe_enabled="true")


@pytest.fixture()
def cfg_probe_off() -> configparser.ConfigParser:
    return _cfg(probe_enabled="false")


def _reviewer(cfg: configparser.ConfigParser) -> ClusterReviewer:
    return ClusterReviewer(
        config=cfg,
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
    )


@pytest.fixture()
def cluster_and_base(tmp_path: Path) -> tuple[RepoCluster, Path]:
    """One real file so _build_file_contents doesn't error."""
    src = tmp_path / "tools" / "example.py"
    src.parent.mkdir()
    src.write_text("def fn(): pass\n", encoding="utf-8")
    cl = RepoCluster(name="agents", patterns=["tools/*"], files=["tools/example.py"])
    return cl, tmp_path


def _task(title: str) -> dict:
    return {
        "title": title,
        "instruction": "Do the fix.",
        "target_files": ["tools/example.py"],
        "acceptance_check": "pytest tests/",
        "cited_location": {
            "file": "tools/example.py",
            "symbol": "fn",
            "line_start": 1,
            "line_end": 1,
        },
    }


def _good_payload(*titles: str) -> str:
    return json.dumps([_task(t) for t in titles])


def _truncated_payload(*titles: str) -> str:
    """A JSON array cut off mid-way through the LAST object's string value
    — salvageable, i.e. AUTO-H4's territory."""
    complete = ",\n".join(json.dumps(_task(t)) for t in titles[:-1])
    tail_title = titles[-1]
    truncated_tail = '{"title": "' + tail_title[: max(1, len(tail_title) // 2)]
    prefix = f"[{complete},\n" if complete else "["
    return prefix + truncated_tail


_PROBE_PAYLOAD = "ARCH_PROBE: facts fn, facts Config"
_EMPTY_PAYLOAD = ""
_PROSE_PAYLOAD = "Sure! Here are some ideas for improving your code, let me think..."

_OPS = [arch_probe.ProbeOp("facts", "fn"), arch_probe.ProbeOp("facts", "Config")]


def _user_messages(mock_llm) -> list[str]:
    """User-message content sent on each call, in call order."""
    out = []
    for c in mock_llm.call_args_list:
        payload = c.kwargs.get("payload") or c.args[2]
        messages = payload.get("messages", [])
        out.append(next((m["content"] for m in messages if m.get("role") == "user"), ""))
    return out


def _fake_parser(ops):
    """Build a stand-in for arch_probe.extract_probe_request that returns
    *ops* only for a response actually carrying the protocol prefix."""
    def _parse(text: str, **_kwargs):
        return list(ops) if arch_probe.PROBE_PREFIX in (text or "") else []
    return _parse


# ─────────────────────────────────────────────────────────────────────────────
# AC-P2-1 / AC-P2-2 — a probe consumes neither ladder's budget
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeDoesNotConsumeLadderRetries:

    def test_probe_reply_triggers_no_h5_escalation(
        self, cfg, cluster_and_base, monkeypatch
    ) -> None:
        """AC-P2-1: without the guard this response is 'unsalvageable' and
        AUTO-H5 re-asks it 6 more times (7 calls). With it: two — the probe
        itself, then AUTO-P1's mandatory forced final call. The number that
        matters is that it is nowhere near the H5 budget."""
        cluster, base_dir = cluster_and_base
        monkeypatch.setattr(
            arch_probe, "extract_probe_request", _fake_parser(_OPS)
        )
        reviewer = _reviewer(cfg)

        with patch(
            "tools.llm_stream.request_completion", return_value=_PROBE_PAYLOAD
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2, (
            "expected probe + forced final call, not an AUTO-H5 ladder run"
        )
        assert mock_llm.call_count < 7, (
            "a probe reply must not enter the AUTO-H5 escalation ladder"
        )
        assert results == []

    def test_probe_reply_does_not_shrink_max_tasks(
        self, cfg, cluster_and_base, monkeypatch
    ) -> None:
        """AC-P2-2: no AUTO-H4 shrink either — asserted against the prompt
        text actually sent, not against an internal counter."""
        cluster, base_dir = cluster_and_base
        monkeypatch.setattr(
            arch_probe, "extract_probe_request", _fake_parser(_OPS)
        )
        reviewer = _reviewer(cfg)

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE_PAYLOAD, _good_payload("Later task")],
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        msgs = _user_messages(mock_llm)
        assert len(msgs) == 2  # probe + forced final call
        assert all("up to 5 concrete tasks" in m for m in msgs), (
            "max_tasks must be untouched by a probe reply"
        )

    def test_probe_clears_both_ladder_flags(
        self, cfg, cluster_and_base, monkeypatch
    ) -> None:
        """AC-P2-1/2 at the closure boundary: a reply that is BOTH a probe
        and structurally truncated still short-circuits, proving the
        invariant belongs to the probe branch and not to
        _parse_candidates_ex's internals."""
        cluster, base_dir = cluster_and_base
        monkeypatch.setattr(
            arch_probe, "extract_probe_request", _fake_parser(_OPS)
        )
        reviewer = _reviewer(cfg)
        hybrid = _truncated_payload("One", "Twoooo") + "\n" + _PROBE_PAYLOAD

        with patch(
            "tools.llm_stream.request_completion", return_value=hybrid
        ) as mock_llm:
            reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# AC-P2-3 / AC-P2-7 / AC-P2-8 — when a reply is NOT a probe
# ─────────────────────────────────────────────────────────────────────────────

class TestNonProbeRepliesAreUnaffected:

    def test_parser_returning_empty_falls_through_to_h5(
        self, cfg, cluster_and_base, monkeypatch
    ) -> None:
        """AC-P2-3: a malformed request (parser returns []) is not a probe."""
        cluster, base_dir = cluster_and_base
        monkeypatch.setattr(
            arch_probe, "extract_probe_request", lambda text, **_kw: []
        )
        reviewer = _reviewer(cfg)

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE_PAYLOAD, _good_payload("Recovered task")],
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        assert [r.title for r in results] == ["Recovered task"]

    def test_probe_disabled_never_calls_the_parser(
        self, cfg_probe_off, cluster_and_base, monkeypatch
    ) -> None:
        """AC-P2-7: with probe_enabled=false the prefix is just prose, and
        extract_probe_request is not consulted at all."""
        calls: list[str] = []

        def _tripwire(text: str, **_kwargs):
            calls.append(text)
            return list(_OPS)

        cluster, base_dir = cluster_and_base
        monkeypatch.setattr(arch_probe, "extract_probe_request", _tripwire)
        reviewer = _reviewer(cfg_probe_off)

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE_PAYLOAD, _good_payload("Recovered task")],
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert calls == [], "parser must not run when probe_enabled=false"
        assert mock_llm.call_count == 2, "AUTO-H5 must still handle it as prose"
        assert [r.title for r in results] == ["Recovered task"]

    def test_parser_exception_is_contained(
        self, cfg, cluster_and_base, monkeypatch
    ) -> None:
        """AC-P2-8: a raising parser degrades to 'not a probe', never to a
        crashed batch."""
        def _boom(text: str, **_kwargs):
            raise RuntimeError("parser blew up")

        cluster, base_dir = cluster_and_base
        monkeypatch.setattr(arch_probe, "extract_probe_request", _boom)
        reviewer = _reviewer(cfg)

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_PROBE_PAYLOAD, _good_payload("Recovered task")],
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        assert [r.title for r in results] == ["Recovered task"]

    def test_real_parser_recognises_the_protocol(self) -> None:
        """AC-P2-3, at the source. This assertion is the AUTO-P1 flip the
        AUTO-P2 stub was written to anticipate: before AUTO-P1 both lines
        returned []; now the first returns the ops it names. Full parser
        coverage lives in test_auto_p1_probe_parser.py — this is only here to
        pin the fact that everything else in THIS file exercises a live
        parser, not a stub that made the guard look correct for free."""
        assert arch_probe.extract_probe_request(_PROBE_PAYLOAD) == _OPS
        assert arch_probe.extract_probe_request("") == []


# ─────────────────────────────────────────────────────────────────────────────
# AC-P2-4 / AC-P2-5 / AC-P2-6 — regression net for the 4-tuple change
# ─────────────────────────────────────────────────────────────────────────────

class TestExistingLaddersStillWork:
    """Deliberate overlap with test_auto_h4/h5_*.py — see module docstring."""

    def test_unsalvageable_still_drives_h5(self, cfg, cluster_and_base) -> None:
        """AC-P2-4: probe ON, but a genuinely empty reply still retries."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[_EMPTY_PAYLOAD, _good_payload("Recovered task")],
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        assert [r.title for r in results] == ["Recovered task"]

    def test_h5_exhaustion_budget_intact(self, cfg, cluster_and_base) -> None:
        """AC-P2-4: the full 1 + empty_response_retry_max attempt budget
        survives the arity change."""
        cluster, base_dir = cluster_and_base
        cfg.set("architect", "empty_response_retry_max", "3")
        cfg.set("architect", "retry_delays_sec", "")
        reviewer = _reviewer(cfg)

        with patch(
            "tools.llm_stream.request_completion", return_value=_PROSE_PAYLOAD
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 4  # 1 initial + 3 retries
        assert results == []

    def test_truncated_still_drives_h4_shrink(self, cfg, cluster_and_base) -> None:
        """AC-P2-5: probe ON, but a truncated array still shrinks max_tasks."""
        cluster, base_dir = cluster_and_base
        reviewer = _reviewer(cfg)

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=[
                _truncated_payload("One", "Twoooooo"),
                _good_payload("Shrunk task"),
            ],
        ) as mock_llm:
            results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

        assert mock_llm.call_count == 2
        msgs = _user_messages(mock_llm)
        assert "up to 5 concrete tasks" in msgs[0]
        assert "up to 2 concrete tasks" in msgs[1], "AUTO-H4 must still halve max_tasks"
        assert "Shrunk task" in [r.title for r in results]

    def test_transient_failure_still_returns_none(self, cfg, cluster_and_base) -> None:
        """AC-P2-6: the LLM-failure early return was widened to a 4-tuple
        too; it must still signal 'do not checkpoint this batch'."""
        cluster, base_dir = cluster_and_base
        cfg.set("architect", "retry_delays_sec", "")
        reviewer = _reviewer(cfg)

        with patch(
            "tools.llm_stream.request_completion",
            side_effect=ConnectionRefusedError("Connection refused"),
        ):
            out = reviewer._review_one_cluster(cluster, base_dir, "improve code")

        assert out is None, "a failed call must stay distinguishable from 0 candidates"


# ─────────────────────────────────────────────────────────────────────────────
# AC-P2-9 — a probe reply that also carried usable tasks
# ─────────────────────────────────────────────────────────────────────────────

def test_probe_reply_keeps_salvaged_candidates(
    cfg, cluster_and_base, monkeypatch
) -> None:
    """AC-P2-9: short-circuiting the ladders must not discard tasks the
    same response legitimately contained."""
    cluster, base_dir = cluster_and_base
    monkeypatch.setattr(arch_probe, "extract_probe_request", _fake_parser(_OPS))
    reviewer = _reviewer(cfg)
    mixed = _good_payload("Kept task") + "\n" + _PROBE_PAYLOAD

    with patch(
        "tools.llm_stream.request_completion", return_value=mixed
    ) as mock_llm:
        results = reviewer.review_clusters([cluster], base_dir, goal="improve code")

    assert mock_llm.call_count == 1
    assert [r.title for r in results] == ["Kept task"]
