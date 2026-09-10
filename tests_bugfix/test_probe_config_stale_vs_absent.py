"""tests_bugfix/test_probe_config_stale_vs_absent.py — L2: `probe_config`
must say `stale`, not `no_artifact`.

`architect._get_probe` traced one reason string for two different situations.
`CollectBridge.usable` is `False` whenever `status != "fresh"`, which covers
both `"stale"` and `"absent"`, so a run whose `.collect/artifact.json` was
sitting on disk at 2.28 MB — simply older than the newest tracked source —
reported `usable=False reason=no_artifact` and made zero probe operations.
Nothing in the log, `progress.json` or the trace said the data was
recoverable, and one `--collect --refresh` would have recovered it.

The stance that stale is *used* exactly like absent is deliberate and
unchanged here (`collect_bridge`'s module docstring, item 3). This ticket
changes only what the operator is told:

  L2-1   `CollectBridge.status` reports fresh / stale / absent.
  L2-2   `status` is fail-open: no model, no attribute, a non-string value
         and a raising model all read as `"absent"` and never raise.
  L2-3   `usable` is unchanged for all four cases.
  L2-4   A stale bridge traces `usable=False reason="stale_artifact"`.
  L2-5   An absent one (no bridge, or a bridge whose model is absent) still
         traces `usable=False reason="no_artifact"`.
  L2-6   The stale warning names `--collect --refresh`; the absent one does
         not.
  L2-7   `analyze_logs` still parses the new reason, with a readable label.
  L2-8   A bridge that reports no status at all keeps the original wording
         (fail-open rather than a new crash path).
  L2-9   `_shrink` still runs: the new status plumbing went in front of it,
         not instead of it.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import analyze_logs
from tools.agent_trace import tracer
from tools.auto.architect import ClusterReviewer
from tools.auto import collect_bridge as cb
from tools.collect.loader import CollectModel, STATUS_ABSENT, STATUS_FRESH, STATUS_STALE


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(**over: str) -> configparser.ConfigParser:
    arch = {
        "temperature": "0.2", "max_tokens": "512",
        "probe_enabled": "true", "probe_max_rounds": "1",
        "retry_delays_sec": "",
    }
    arch.update(over)
    c = configparser.ConfigParser()
    c.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1", "api_key": "test",
            "model": "test-model", "api_format": "openai",
        },
        "architect": arch,
        "loop":      {"timeout_seconds": "10"},
    })
    return c


def _reviewer() -> ClusterReviewer:
    return ClusterReviewer(
        config=_cfg(), base_url="http://localhost:1337/v1", api_key="test",
        model="test-model", api_format="openai", verify_ssl=False,
    )


class _Bridge:
    """A stand-in with only what `_get_probe` reads off the bridge."""

    def __init__(self, status: str, usable: bool = False):
        self.status = status
        self.usable = usable


def _model(status: str) -> CollectModel:
    return CollectModel(status=status)


def _trace(tmp_path: Path, reviewer: ClusterReviewer, bridge, name="t.jsonl") -> dict:
    """Build the probe via the real `_get_probe` and return its probe_config."""
    path = tmp_path / name
    tracer.configure(enabled=True, path=str(path), console_echo=False)
    try:
        with patch(
            "tools.auto.collect_bridge.make_collect_bridge",
            return_value=bridge,
        ):
            assert reviewer._get_probe(tmp_path) is None
    finally:
        tracer.configure(enabled=False)
    events = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    cfgs = [e for e in events if e.get("kind") == "probe_config"]
    assert len(cfgs) == 1, "exactly one probe_config per run"
    return cfgs[0]["params"]


# ─────────────────────────────────────────────────────────────────────────────
# CollectBridge.status
# ─────────────────────────────────────────────────────────────────────────────

class TestBridgeStatus:

    def test_reports_each_status(self) -> None:
        """L2-1"""
        assert cb.CollectBridge(_model(STATUS_FRESH)).status == "fresh"
        assert cb.CollectBridge(_model(STATUS_STALE)).status == "stale"
        assert cb.CollectBridge(_model(STATUS_ABSENT)).status == "absent"

    def test_absent_model_reports_absent_without_raising(self) -> None:
        """L2-1: this is the value the ticket's acceptance names outright."""
        assert cb.CollectBridge(None).status == "absent"

    def test_usable_unchanged_for_every_status(self) -> None:
        """L2-3: lifting the read into a property must not move the gate."""
        assert cb.CollectBridge(_model(STATUS_FRESH)).usable is True
        assert cb.CollectBridge(_model(STATUS_STALE)).usable is False
        assert cb.CollectBridge(_model(STATUS_ABSENT)).usable is False
        assert cb.CollectBridge(None).usable is False

    def test_status_fails_open(self) -> None:
        """L2-2: a broken artifact never raises into a run."""
        assert cb.CollectBridge(SimpleNamespace()).status == "absent"            # no attribute
        assert cb.CollectBridge(SimpleNamespace(status=3)).status == "absent"    # wrong type
        assert cb.CollectBridge(SimpleNamespace(status=None)).status == "absent"

        class _Broken:
            def __getattr__(self, _name):
                raise RuntimeError("artifact unreadable")

        assert cb.CollectBridge(_Broken()).status == "absent"

    def test_status_and_usable_agree_when_broken(self) -> None:
        """L2-2 + L2-3: the fail-open default must also read as unusable."""
        b = cb.CollectBridge(SimpleNamespace(status=3))
        assert b.status == "absent" and b.usable is False

    def test_status_is_read_only(self) -> None:
        """The property must not be settable."""
        b = cb.CollectBridge(_model(STATUS_STALE))
        import pytest as _pt
        with _pt.raises(AttributeError):
            b.status = "fresh"  # type: ignore[misc]

    def test_status_never_raises_on_a_broken_model(self) -> None:
        """A model whose status accessor blows up (half-written artifact)
        must fail open — and leave context_for / pull_symbol operational."""
        class _BrokenStatus:
            def __getattribute__(self, name):
                if name == "status":
                    raise RuntimeError("artifact is unreadable")
                return object.__getattribute__(self, name)

        b = cb.CollectBridge(_BrokenStatus())
        assert b.status == "absent"
        assert b.usable is False
        assert b.context_for("tools/x.py") == ""
        assert b.pull_symbol("fn") == ""


# ─────────────────────────────────────────────────────────────────────────────
# probe_config reason
# ─────────────────────────────────────────────────────────────────────────────

class TestProbeConfigReason:

    def test_stale_traces_stale_artifact(self, tmp_path: Path) -> None:
        """L2-4"""
        p = _trace(tmp_path, _reviewer(), _Bridge("stale"), "stale.jsonl")
        assert p["usable"] == "False"
        assert p["reason"] == "stale_artifact"

    def test_no_bridge_traces_no_artifact(self, tmp_path: Path) -> None:
        """L2-5"""
        p = _trace(tmp_path, _reviewer(), None, "none.jsonl")
        assert p["usable"] == "False"
        assert p["reason"] == "no_artifact"

    def test_absent_model_traces_no_artifact(self, tmp_path: Path) -> None:
        """L2-5: a real bridge over an absent model is absent, not stale."""
        p = _trace(tmp_path, _reviewer(),
                   cb.CollectBridge(_model(STATUS_ABSENT)), "absent.jsonl")
        assert p["usable"] == "False"
        assert p["reason"] == "no_artifact"

    def test_stale_warning_names_refresh_absent_does_not(self, tmp_path: Path, caplog):
        """L2-6: the whole point — stale is one command away, absent is not."""
        with caplog.at_level("WARNING"):
            _trace(tmp_path, _reviewer(), _Bridge("stale"), "warn_stale.jsonl")
        stale_msgs = [r.getMessage() for r in caplog.records]
        assert any("--collect --refresh" in m for m in stale_msgs)

        caplog.clear()
        with caplog.at_level("WARNING"):
            _trace(tmp_path, _reviewer(), None, "warn_absent.jsonl")
        assert not any("--collect --refresh" in r.getMessage()
                       for r in caplog.records)

    def test_stale_still_tells_the_operator_they_will_get_no_probes(
        self, tmp_path: Path, caplog
    ) -> None:
        """The remedy must not replace the consequence."""
        with caplog.at_level("WARNING"):
            _trace(tmp_path, _reviewer(), _Bridge("stale"), "warn_still.jsonl")
        assert any(
            "--collect --refresh" in r.getMessage() and "without probes" in r.getMessage()
            for r in caplog.records
        )

    def test_bridge_without_status_keeps_the_absent_wording(self, tmp_path: Path, caplog):
        """L2-8: a bridge that reports no status at all must not become a new
        crash path or a fabricated `stale_artifact`."""
        with caplog.at_level("WARNING"):
            p = _trace(tmp_path, _reviewer(), SimpleNamespace(usable=False),
                       "no_status.jsonl")
        assert p["reason"] == "no_artifact"
        assert not any("--collect --refresh" in r.getMessage() for r in caplog.records)

    def test_stale_bridge_is_never_memoized(self, tmp_path: Path) -> None:
        """The fix is diagnostic only — stale must not become usable."""
        r = _reviewer()
        _trace(tmp_path, r, cb.CollectBridge(_model(STATUS_STALE)), "memo.jsonl")
        assert r._probe is None

    def test_fresh_still_traces_ok(self, tmp_path: Path) -> None:
        """AC-L2-5: the stale/absent branching must not disturb the happy path."""
        r = _reviewer()
        path = tmp_path / "ok.jsonl"
        tracer.configure(enabled=True, path=str(path), console_echo=False)
        try:
            with patch(
                "tools.auto.collect_bridge.make_collect_bridge",
                return_value=cb.CollectBridge(_model(STATUS_FRESH)),
            ):
                r._get_probe(tmp_path)
        finally:
            tracer.configure(enabled=False)
        events = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        cfgs = [e for e in events if e.get("kind") == "probe_config"]
        assert len(cfgs) == 1
        assert cfgs[0]["params"]["usable"] == "True"
        assert cfgs[0]["params"]["reason"] == "ok"

    def test_bridge_error_reason_is_untouched(self, tmp_path: Path) -> None:
        """The construction-failure branch is a separate third state."""
        r = _reviewer()
        path = tmp_path / "err.jsonl"
        tracer.configure(enabled=True, path=str(path), console_echo=False)
        try:
            with patch(
                "tools.auto.collect_bridge.make_collect_bridge",
                side_effect=RuntimeError("collector exploded"),
            ):
                assert r._get_probe(tmp_path) is None
        finally:
            tracer.configure(enabled=False)
        p = [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]
        cfgs = [e for e in p if e.get("kind") == "probe_config"]
        assert len(cfgs) == 1
        assert cfgs[0]["params"]["usable"] == "False"
        assert cfgs[0]["params"]["reason"] == "bridge_error"
        # `detail` travels as the event content, not as a param.
        assert cfgs[0]["content"] == "collector exploded"


# ─────────────────────────────────────────────────────────────────────────────
# analyze_logs still parses the new reason
# ─────────────────────────────────────────────────────────────────────────────

def _ev(kind: str, **params) -> dict:
    return {"run_id": "r1", "kind": kind, "ts": "2026-01-01T00:00:00Z",
            "source": "architect", "target": "probe", "content": "",
            "params": params}


_RUN_START = {"run_id": "r1", "kind": "run_start", "ts": "2026-01-01T00:00:00Z",
              "source": "controller", "target": "auto", "params": {"goal": "g"}}


def _render(events, capsys) -> str:
    analyze_logs.render_run_summary(analyze_logs.analyze(events)["r1"])
    return capsys.readouterr().out


class TestReporting:

    def test_stale_artifact_reason_parses_and_labels(self, capsys) -> None:
        """L2-7"""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="False", reason="stale_artifact",
                max_rounds=1, max_total_chars=6000, allowed_ops="facts"),
        ], capsys)
        assert "enabled but unavailable" in out
        assert "--collect --refresh" in out

    def test_no_artifact_label_is_still_distinct(self, capsys) -> None:
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="False", reason="no_artifact",
                max_rounds=1, max_total_chars=6000, allowed_ops="facts"),
        ], capsys)
        assert "enabled but unavailable" in out
        assert "--collect --refresh" not in out

    def test_unknown_reason_still_falls_through(self, capsys) -> None:
        """The parser must not key off the exact string to survive future ones."""
        out = _render([
            _RUN_START,
            _ev("probe_config", usable="False", reason="something_new",
                max_rounds=1, max_total_chars=6000, allowed_ops="facts"),
        ], capsys)
        assert "something_new" in out


# ─────────────────────────────────────────────────────────────────────────────
# _shrink is untouched
# ─────────────────────────────────────────────────────────────────────────────

def test_shrink_still_runs_from_context_for() -> None:
    """L2-9: the new `status` read is one line above the budget gate, so it
    must not have eaten the shrink path."""
    calls: list[str] = []

    def _summarize(_system: str, user: str) -> str:
        calls.append(user)
        return "compact facts"

    bridge = cb.CollectBridge(
        _model(STATUS_FRESH), max_context_chars=50, summarizer_call=_summarize,
    )
    raw = "signature: fn() x" * 20
    assert len(raw) > 50
    with patch.object(
        cb, "build_collect_context_block",
        side_effect=lambda model, target, **_kw: raw,
    ):
        assert bridge.context_for("tools/example.py") == "compact facts"
    assert bridge.shrink_calls == 1 and len(calls) == 1
