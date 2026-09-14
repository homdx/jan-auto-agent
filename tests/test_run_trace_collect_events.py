"""tests/test_run_trace_collect_events.py — M4: runtime counters.

epic-tasks/19. Tier 0 and Tier 1 are static; Tier 2 needs the run to say what
happened, so this file pins the four collect events, the Gate-1 stage split and
the two readers of those numbers.

`--dry-run` never reaches the coder, so it cannot emit `collect_block`. These
are the unit tests that drive the producers instead:

  * CollectBridge.context_for, through a build, a memo hit and an over-budget
    block, with a recording tracer in place of the real one;
  * Gate1Filter.filter against a stubbed request_completion returning
    confirmed / rejected / "" (and one answer that only came from the re-ask
    ladder);
  * the two readers — analyze_logs' runtime counter block and
    scripts/trace_round_snapshot.py.

`CollectBridge._shrink` is not modified: the shrink is observed from
`context_for` by comparing lengths and reading `shrink_calls`, and
`test_context_for_shrink_observations_do_not_change_shrink` pins the part of
its behaviour that must survive.
"""

from __future__ import annotations

import configparser
import importlib.util
import io
import json
import re
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.collect_bridge import CollectBridge
from tools.auto.context_assembler import (
    _PACK_ROWS,
    build_collect_context_block,
    build_collect_context_block_stats,
)
from tools.auto.gate1_filter import (
    FilterResult,
    Gate1Filter,
    _UNKNOWN_STAGE,
    format_gate1_split,
    gate1_outcome,
    split_gate1_results,
)
from tools.auto.run_trace import RunTrace
from tools.collect.loader import CollectModel, STATUS_ABSENT, STATUS_FRESH, STATUS_STALE
from tools.collect.model import FunctionRecord, ModuleRecord

from analyze_logs import analyze, render_run_summary


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures and helpers
# ─────────────────────────────────────────────────────────────────────────────

class RecordingTracer:
    """Stands in for `tools.agent_trace.tracer`: records, writes nothing.

    The bridge imports the module and reads the singleton, so patching
    `tools.agent_trace.tracer` is what a real trace sink would look like to
    it — including a broken one, which is why every emit site is wrapped.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def event(self, source=None, target=None, kind=None, params=None, **extra) -> None:
        self.events.append({
            "source": source,
            "target": target,
            "kind": kind,
            "params": dict(params or {}),
        })

    def kinds(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["kind"] == kind]


def _symbol(qualname: str, signature: str = "") -> FunctionRecord:
    module = qualname.rsplit(".", 1)[0] if "." in qualname else qualname
    return FunctionRecord(qualname=qualname, module=module, lineno=1, signature=signature)


def _fresh(modules=()) -> CollectModel:
    return CollectModel(status=STATUS_FRESH, modules=tuple(modules))


def _heavy_module(target: str = "pkg/a.py", n: int = 40) -> ModuleRecord:
    """A module with 40 symbols — enough to overflow a 200-char budget."""
    return ModuleRecord(
        path=target,
        public_symbols=tuple(
            _symbol(f"pkg.a.func_{i}", f"func_{i}(x, y, z) -> int") for i in range(n)
        ),
    )


def _minimal_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1",
            "api_key": "test",
            "model": "test-model",
            "api_format": "openai",
        },
        # 2 re-asks, not 6: enough to prove a verdict reached from the ladder,
        # and this suite is not the place to pay for the whole ladder.
        "gate1": {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false",
                  "unparseable_max_retries": "2"},
        "loop":  {"timeout_seconds": "10"},
    })
    return cfg


def _filter() -> Gate1Filter:
    return Gate1Filter(
        config=_minimal_config(),
        base_url="http://localhost:1337/v1",
        api_key="test",
        model="test-model",
        api_format="openai",
        verify_ssl=False,
    )


def _candidate(title: str, file: str, *, symbol: str | None = None,
               line_start: int | None = None, line_end: int | None = None) -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction="do the thing described in the claim",
        target_files=[file],
        acceptance_check="python -m pytest tests/ -q",
        cited_location=CitedLocation(
            file=file, symbol=symbol, line_start=line_start, line_end=line_end,
        ),
        cluster="agents",
    )


def _load_trace_round_snapshot():
    """scripts/ has no __init__.py, so load it by path."""
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


# ─────────────────────────────────────────────────────────────────────────────
# collect_block / collect_shrink / collect_miss / collect_summary
# ─────────────────────────────────────────────────────────────────────────────

def test_context_for_emits_block_and_shrink_and_summary(monkeypatch):
    """A build, a memo hit, and one LLM shrink — all four events."""
    rec = RecordingTracer()
    monkeypatch.setattr("tools.agent_trace.tracer", rec)

    # A parse_error that alone exceeds the budget is the case where the
    # assembler's row cut cannot help and _shrink still fires.
    module = ModuleRecord(
        path="pkg/a.py",
        public_symbols=tuple(
            _symbol(f"pkg.a.func_{i}", f"func_{i}(x, y, z) -> int") for i in range(5)),
        parse_error="E" * 500,
    )
    calls = []

    def _summarizer(system, user):
        calls.append(1)
        return "shrunk to fit"

    bridge = CollectBridge(_fresh([module]), max_context_chars=200,
                           summarizer_call=_summarizer)

    first = bridge.context_for("pkg/a.py", task_id="AUTO-T1")
    assert first == "shrunk to fit"
    assert bridge.context_for("pkg/a.py", task_id="AUTO-T1") == "shrunk to fit"
    assert len(calls) == 1          # the memo held the second call
    assert bridge.shrink_calls == 1

    shrinks = rec.kinds("collect_shrink")
    assert len(shrinks) == 1
    assert shrinks[0]["params"]["path"] == "llm"
    assert shrinks[0]["params"]["before"] > shrinks[0]["params"]["after"]
    assert shrinks[0]["params"]["task_id"] == "AUTO-T1"

    blocks = rec.kinds("collect_block")
    assert len(blocks) == 2, "a memo hit is a block too — it is what gets injected"
    assert blocks[0]["params"]["memo_hit"] is False
    assert blocks[1]["params"]["memo_hit"] is True
    for event in blocks:
        p = event["params"]
        assert p["target_file"] == "pkg/a.py"
        assert p["chars"] == len("shrunk to fit")
        assert p["task_id"] == "AUTO-T1"
        # M4's reason for the new stats helper: without it these are unobservable.
        assert p["rows_kept"] + p["rows_cut"] == len(_PACK_ROWS)

    assert rec.kinds("collect_miss") == []
    bridge.emit_collect_summary()
    (summary,) = rec.kinds("collect_summary")
    assert summary["params"] == {
        "blocks": 2,
        "chars": 2 * len("shrunk to fit"),
        "memo_hits": 1,
        "shrink_calls": 1,
        "shrink_by_path": {"llm": 1},
        "misses": {},
    }


def test_context_for_static_cut_reports_the_rows_path(monkeypatch):
    """V6's assembler cut is the shrink that actually happens — path "rows"."""
    rec = RecordingTracer()
    monkeypatch.setattr("tools.agent_trace.tracer", rec)

    bridge = CollectBridge(_fresh([_heavy_module()]), max_context_chars=200,
                           summarizer_call=None)
    block = bridge.context_for("pkg/a.py", task_id="AUTO-T2")

    assert len(block) <= 200
    assert bridge.shrink_calls == 0            # no LLM, so path must not be llm
    (shrink,) = rec.kinds("collect_shrink")
    assert shrink["params"]["path"] == "rows"
    assert shrink["params"]["before"] > shrink["params"]["after"]

    (block_event,) = rec.kinds("collect_block")
    assert block_event["params"]["rows_cut"] > 0
    assert block_event["params"]["memo_hit"] is False


def test_context_for_truncates_and_reports_the_truncate_path(monkeypatch):
    """No summarizer and a head that cannot be cut: path "truncate"."""
    rec = RecordingTracer()
    monkeypatch.setattr("tools.agent_trace.tracer", rec)

    module = ModuleRecord(
        path="pkg/a.py",
        public_symbols=tuple(_symbol(f"pkg.a.func_{i}") for i in range(5)),
        parse_error="E" * 500,
    )
    bridge = CollectBridge(_fresh([module]), max_context_chars=200,
                           summarizer_call=None)
    block = bridge.context_for("pkg/a.py")

    assert "truncated by CollectBridge" in block
    assert bridge.shrink_calls == 0
    (shrink,) = rec.kinds("collect_shrink")
    assert shrink["params"]["path"] == "truncate"


def test_context_for_miss_reasons_are_distinct(monkeypatch):
    """absent / stale / dirty / unknown_module — the four miss reasons."""
    rec = RecordingTracer()
    monkeypatch.setattr("tools.agent_trace.tracer", rec)

    CollectBridge(CollectModel(status=STATUS_ABSENT)).context_for("pkg/a.py", task_id="T")

    CollectBridge(CollectModel(
        status=STATUS_STALE, modules=(ModuleRecord(path="pkg/a.py"),),
    )).context_for("pkg/a.py", task_id="T")

    bridge = CollectBridge(_fresh([_heavy_module()]))
    assert bridge.context_for("pkg/a.py") != ""
    bridge.invalidate(["pkg/a.py"])
    assert bridge.context_for("pkg/a.py") == ""
    bridge._dirty_paths = set()            # clear dirt to test the fourth reason
    assert bridge.context_for("pkg/unknown.py") == ""

    reasons = [e["params"]["reason"] for e in rec.kinds("collect_miss")]
    assert reasons == ["absent", "stale", "dirty", "unknown_module"]
    for event in rec.kinds("collect_miss"):
        assert event["params"]["target_file"] in ("pkg/a.py", "pkg/unknown.py")

    # The tally the run.log line reads, and the tally the summary event reads.
    assert bridge.collect_misses == {"dirty": 1, "unknown_module": 1}
    assert "dirty=1" in bridge.summary()
    assert "unknown_module=1" in bridge.summary()


def test_context_for_shrink_observations_do_not_change_shrink(monkeypatch):
    """M4 observes _shrink from the outside. Its behaviour is unchanged."""
    rec = RecordingTracer()
    monkeypatch.setattr("tools.agent_trace.tracer", rec)

    module = ModuleRecord(
        path="pkg/a.py",
        public_symbols=tuple(_symbol(f"pkg.a.func_{i}") for i in range(5)),
        parse_error="E" * 500,
    )

    # An overbudget reply falls through to a hard truncation.
    bridge = CollectBridge(_fresh([module]), max_context_chars=200,
                           summarizer_call=lambda s, u: "x" * 10_000)
    block = bridge.context_for("pkg/a.py")
    assert "truncated by CollectBridge" in block
    assert bridge.shrink_calls == 1          # the call was still counted
    (shrink,) = rec.kinds("collect_shrink")
    assert shrink["params"]["path"] == "llm"

    # A broken summarizer fails open to truncation, as before.
    bridge2 = CollectBridge(_fresh([module]), max_context_chars=200,
                            summarizer_call=lambda s, u: (_ for _ in ()).throw(RuntimeError("boom")))
    block2 = bridge2.context_for("pkg/a.py")
    assert "truncated by CollectBridge" in block2
    assert bridge2.shrink_calls == 1


def test_context_for_many_threads_task_id(monkeypatch):
    rec = RecordingTracer()
    monkeypatch.setattr("tools.agent_trace.tracer", rec)

    module_a = ModuleRecord(path="pkg/a.py",
                            public_symbols=(_symbol("pkg.a.foo", "foo() -> int"),))
    bridge = CollectBridge(_fresh([module_a]), max_context_chars=5000)
    joined = bridge.context_for_many(["pkg/a.py", "pkg/nope.py"], task_id="AUTO-T3")

    assert "pkg/a.py" in joined
    blocks = rec.kinds("collect_block")
    assert [b["params"]["task_id"] for b in blocks] == ["AUTO-T3"]
    assert [m["params"]["reason"] for m in rec.kinds("collect_miss")] == ["unknown_module"]


def test_no_bridge_no_events_and_zeroed_split(monkeypatch):
    """Acceptance: no bridge wired in -> no events, and the line still parses."""
    rec = RecordingTracer()
    monkeypatch.setattr("tools.agent_trace.tracer", rec)

    # Every emit site is inside the bridge, so with none wired in there is
    # nothing that could fire. format_gate1_split on an empty counter dict —
    # what a stubbed filter_candidates leaves behind — prints all zeros.
    assert format_gate1_split({}) == (
        "existence=0 presence_confirmed=0 presence_rejected=0 "
        "presence_fail_closed=0 presence_reask=0 duplicate=0 non_py=0"
    )
    # Unknown fields are dropped, not appended: the field order is the contract.
    assert format_gate1_split({"accepted": 9, "rejected": 3, "_uncounted": 2}) == (
        "existence=0 presence_confirmed=0 presence_rejected=0 "
        "presence_fail_closed=0 presence_reask=0 duplicate=0 non_py=0"
    )
    assert rec.events == []


def test_build_collect_context_block_stats_keeps_the_string_wrapper():
    """M4 exposes the row cut through a new helper, not a changed return type."""
    model = _fresh([_heavy_module()])
    assert isinstance(build_collect_context_block(model, "pkg/a.py"), str)

    block, stats = build_collect_context_block_stats(
        model, "pkg/a.py", budget=150, pack_enabled=True)
    assert build_collect_context_block(model, "pkg/a.py", budget=150) == block
    assert stats["chars"] == len(block)
    assert stats["rows_kept"] + stats["rows_cut"] == len(_PACK_ROWS)
    assert stats["rows_cut"] > 0
    assert stats["chars_uncapped"] > stats["chars"]
    assert stats["budget"] == 150

    # No budget: nothing was ever cut, and the two char counts agree.
    _block, uncapped = build_collect_context_block_stats(model, "pkg/a.py", budget=None)
    assert uncapped["chars"] == uncapped["chars_uncapped"]
    assert uncapped["budget"] is None
    assert uncapped["rows_kept"] + uncapped["rows_cut"] == len(_PACK_ROWS)
    assert uncapped["rows_kept"] >= stats["rows_kept"]

    # An unknown module: no block, no cut, no crash.
    empty, empty_stats = build_collect_context_block_stats(model, "pkg/nope.py", budget=150)
    assert empty == ""
    assert empty_stats["chars"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Gate-1 stage split
# ─────────────────────────────────────────────────────────────────────────────

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


def _m4_repo(tmp_path: Path) -> list[CandidateTask]:
    """Five files, five candidates: one per split bucket, plus one non-.py cite."""
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    for i in range(1, 5):
        (tmp_path / "tools" / f"c{i}.py").write_text(
            textwrap.dedent(f"""\
                def parse_config_{i}(raw):
                    # TODO: validate input
                    return raw
            """),
            encoding="utf-8",
        )
    (tmp_path / "docs" / "notes.md").write_text(
        # First line >= 8 chars: a shorter quote fails the evidence check and
        # the candidate is rejected as unsupported, which is not what this
        # fixture is for — it exists to be counted in `non_py`.
        "# a notes file with some prose in it\n\nMore prose below the heading.\n",
        encoding="utf-8",
    )
    return [
        _candidate("c1 confirmed", "tools/c1.py", symbol="parse_config_1"),
        _candidate("c2 rejected", "tools/c2.py", symbol="parse_config_2"),
        _candidate("c3 empty", "tools/c3.py", symbol="parse_config_3"),
        _candidate("c4 reask", "tools/c4.py", symbol="parse_config_4"),
        _candidate("c5 nonpy", "docs/notes.md", line_start=1, line_end=5),
    ]


def test_gate1_split_line(monkeypatch):
    """The acceptance test: confirmed / rejected / "" against a stubbed call."""
    filt = _filter()
    replies = {
        "tools/c1.py": [_confirmed],
        "tools/c2.py": [_rejected],
        # One step, re-asked until the ladder is exhausted: the fail-closed case.
        "tools/c3.py": [lambda user_msg: ""],
        "tools/c4.py": [lambda user_msg: "", _confirmed],
        "docs/notes.md": [_confirmed],
    }
    consumed: dict[str, int] = {}
    total_calls = 0

    def fake_request_completion(url, headers, payload, **kwargs):
        nonlocal total_calls
        total_calls += 1
        user_msg = payload["messages"][-1]["content"]
        location = re.search(r"Location:\s*(.+)$", user_msg, re.M)
        path = location.group(1).split(",")[0].strip() if location else ""
        plan = replies[path]
        index = min(consumed.get(path, 0), len(plan) - 1)
        consumed[path] = index + 1
        step = plan[index]
        return step(user_msg) if callable(step) else step

    counts: dict = {}
    with tempfile.TemporaryDirectory() as raw:
        tmp_path = Path(raw)
        candidates = _m4_repo(tmp_path)

        with patch("tools.llm_stream.request_completion",
                   side_effect=fake_request_completion):
            accepted, rejected = filt.filter(candidates, tmp_path, counters=counts)

    line = format_gate1_split(counts)
    assert "existence=0" in line
    assert "presence_confirmed=3" in line        # c1, c4 (on the re-ask), c5
    assert "presence_rejected=1" in line          # c2
    assert "presence_fail_closed=1" in line       # c3, "" after the ladder
    assert "presence_reask=1" in line             # c4 only
    assert "duplicate=0" in line
    assert "non_py=1" in line                     # docs/notes.md

    # Every field, in order, always.
    assert line == (
        "existence=0 presence_confirmed=3 presence_rejected=1 "
        "presence_fail_closed=1 presence_reask=1 duplicate=0 non_py=1"
    )
    # The buckets add up to the candidates they describe.
    assert len(accepted) == 3
    assert len(rejected) == 2
    assert counts["presence_rejected"] + counts["presence_fail_closed"] == len(rejected)
    # And the counter the log line used to hide: without it, c3 and c2 look
    # the same.
    assert filt.presence_reask == 1
    assert filt.non_py_requests == 1
    assert total_calls > 2, "the empty reply went through the ladder"


def test_gate1_split_all_fields_zero_for_existence_only_mode(monkeypatch):
    """skip_llm: nothing in any presence bucket, but every field is printed."""
    with tempfile.TemporaryDirectory() as raw:
        tmp_path = Path(raw)
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "a.py").write_text(
            "def parse_config(raw):\n    return raw\n", encoding="utf-8")
        candidate = _candidate("t", "tools/a.py", symbol="parse_config")

        cfg = _minimal_config()
        cfg.set("gate1", "skip_llm", "true")
        filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1",
                           api_key="test", model="test-model",
                           api_format="openai", verify_ssl=False)

        counts: dict = {}
        with patch("tools.llm_stream.request_completion") as mock_llm:
            accepted, rejected = filt.filter([candidate], tmp_path, counters=counts)
            mock_llm.assert_not_called()

    assert len(accepted) == 1 and not rejected
    assert format_gate1_split(counts) == (
        "existence=0 presence_confirmed=0 presence_rejected=0 "
        "presence_fail_closed=0 presence_reask=0 duplicate=0 non_py=0"
    )
    # An existence-only acceptance is not a model verdict, so it is not counted
    # as one: counting it would make the presence number exceed the calls made.
    assert counts["presence_confirmed"] == 0


def test_gate1_split_counts_existence_and_duplicate(monkeypatch):
    with tempfile.TemporaryDirectory() as raw:
        tmp_path = Path(raw)
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "a.py").write_text(
            "def parse_config(raw):\n    return raw\n", encoding="utf-8")
        cfg = _minimal_config()
        cfg.set("gate1", "skip_llm", "true")
        filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1",
                           api_key="test", model="test-model",
                           api_format="openai", verify_ssl=False)

        candidates = [
            _candidate("missing file", "tools/nope.py", symbol="nope"),
            # Same title: the dedup fingerprint is file + anchor + title, so
            # two candidates that only differ in wording are not duplicates.
            _candidate("same claim", "tools/a.py", symbol="parse_config"),
            _candidate("same claim", "tools/a.py", symbol="parse_config"),
        ]
        counts: dict = {}
        accepted, rejected = filt.filter(candidates, tmp_path, counters=counts)

    assert counts["existence"] == 1
    assert counts["duplicate"] == 1
    assert len(accepted) == 1
    assert "existence=1" in format_gate1_split(counts)
    assert "duplicate=1" in format_gate1_split(counts)
    # The two uncounted acceptances are the expected case, not a gap.
    assert counts["_uncounted"] == 0


def test_gate1_outcome_buckets():
    """The classifier, table-driven: every reason Gate 1 can produce."""
    assert gate1_outcome("existence", "cited file not found: 'x'", False) == "existence"
    assert gate1_outcome("duplicate", "duplicate of an earlier candidate", False) == "duplicate"
    assert gate1_outcome("presence", "JSON decode failed (x) — failing closed", False) \
        == "presence_fail_closed"
    assert gate1_outcome("presence", "expected JSON object, got list — failing closed", False) \
        == "presence_fail_closed"
    assert gate1_outcome("presence", "unrecognised verdict 'maybe' — failing closed", False) \
        == "presence_fail_closed"
    assert gate1_outcome("presence", "LLM call failed: timeout (after 3 retries)", False) \
        == "presence_fail_closed"
    assert gate1_outcome("presence", "the model said it is fixed", False) == "presence_rejected"
    assert gate1_outcome("presence", "LLM confirmed problem is present", True) \
        == "presence_confirmed"
    assert gate1_outcome("presence", "existence check passed (LLM skipped)", True) == ""
    assert gate1_outcome("presence", "new file — existence check sufficient", True) == ""
    # V12's stage does not exist here: flagged as a gap, never a zero column
    # invented, and never forced into a presence bucket.
    assert gate1_outcome("already_safe", "guarded by the contract", True) == _UNKNOWN_STAGE


def test_split_gate1_results_marks_what_it_cannot_count():
    results = [
        FilterResult(candidate=None, accepted=True, stage="presence", reason="present"),
        FilterResult(candidate=None, accepted=False, stage="existence", reason="not found"),
        FilterResult(candidate=None, accepted=False, stage="duplicate", reason="dup"),
        FilterResult(candidate=None, accepted=False, stage="presence",
                     reason="JSON decode failed (x) — failing closed"),
        FilterResult(candidate=None, accepted=True, stage="already_safe", reason="guarded"),
    ]
    out = split_gate1_results(results, reask=2, non_py=4)
    assert out == {
        "existence": 1,
        "presence_confirmed": 1,
        "presence_rejected": 0,
        "presence_fail_closed": 1,
        "presence_reask": 2,
        "duplicate": 1,
        "non_py": 4,
        "_uncounted": 1,
    }


def test_filter_returns_the_same_tuple_without_counters(monkeypatch):
    """Backward compatible: no `counters` argument changes the return value."""
    with tempfile.TemporaryDirectory() as raw:
        tmp_path = Path(raw)
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "a.py").write_text(
            "def parse_config(raw):\n    return raw\n", encoding="utf-8")
        cfg = _minimal_config()
        cfg.set("gate1", "skip_llm", "true")
        filt = Gate1Filter(config=cfg, base_url="http://localhost:1337/v1",
                           api_key="test", model="test-model",
                           api_format="openai", verify_ssl=False)
        candidate = _candidate("t", "tools/a.py", symbol="parse_config")

        accepted, rejected = filt.filter([candidate], tmp_path)

    assert isinstance(accepted, list) and isinstance(rejected, list)
    assert len(accepted) == 1 and rejected == []


# ─────────────────────────────────────────────────────────────────────────────
# Readers
# ─────────────────────────────────────────────────────────────────────────────

def _event(kind, params=None, run_id="run1", ts="2026-01-01T00:00:00+00:00",
           source="collect_bridge", target="auto_run", content="") -> dict:
    return {
        "seq": 1, "ts": ts, "run_id": run_id,
        "source": source, "target": target, "kind": kind,
        "params": params or {}, "content": content,
    }


def test_run_trace_log_gate1_split(monkeypatch):
    # run_trace.py imports the singleton by name at module import time, so the
    # patch has to land where it imported it from — unlike collect_bridge,
    # which imports it lazily inside _event and therefore sees the module.
    rec = RecordingTracer()
    monkeypatch.setattr("tools.auto.run_trace.tracer", rec)
    run_trace = RunTrace(SimpleNamespace(log=lambda *args: None), "run123", None)

    run_trace.log_gate1_split({
        "accepted": 5, "rejected": 3, "existence": 3,
        "presence_confirmed": 5, "presence_rejected": 0,
        "presence_fail_closed": 2, "presence_reask": 0,
        "duplicate": 0, "non_py": 4, "_uncounted": 1,
    })

    (event,) = rec.kinds("gate1_split")
    assert event["params"] == {
        "run_id": "run123", "accepted": 5, "rejected": 3, "existence": 3,
        "presence_confirmed": 5, "presence_rejected": 0,
        "presence_fail_closed": 2, "presence_reask": 0, "duplicate": 0, "non_py": 4,
    }
    assert "_uncounted" not in event["params"]


def test_run_trace_log_gate1_rejected_carries_the_stage(monkeypatch):
    # The split says how many; the per-rejection event says which one. The
    # `stage` field is optional so the pre-M4 two-argument call is unchanged
    # (no key at all, not an empty one) and a reader can key on it instead of
    # on the reason-text vocabulary.
    rec = RecordingTracer()
    monkeypatch.setattr("tools.auto.run_trace.tracer", rec)
    run_trace = RunTrace(SimpleNamespace(log=lambda *args: None), "run123", None)

    run_trace.log_gate1_rejected("t1", "JSON decode failed: x", stage="presence")
    run_trace.log_gate1_rejected("t2", "no such file")

    first, second = rec.kinds("rejected")
    assert first["params"]["stage"] == "presence"
    assert first["params"]["reason"] == "JSON decode failed: x"
    assert "stage" not in second["params"]


def test_analyze_logs_renders_the_collect_and_gate1_lines(monkeypatch, capsys):
    monkeypatch.setattr("tools.agent_trace.tracer", RecordingTracer())

    events = [
        _event("run_start", {"goal": "improve code", "run_id": "run1"}),
        _event("collect_block", {"target_file": "pkg/a.py", "chars": 321,
                                 "rows_kept": 5, "rows_cut": 2, "memo_hit": False,
                                 "task_id": "AUTO-T1"}),
        _event("collect_block", {"target_file": "pkg/a.py", "chars": 321,
                                 "rows_kept": 5, "rows_cut": 2, "memo_hit": True,
                                 "task_id": "AUTO-T1"}, ts="2026-01-01T00:00:01+00:00"),
        _event("collect_shrink", {"target_file": "pkg/a.py", "path": "rows",
                                  "before": 640, "after": 321},
               ts="2026-01-01T00:00:02+00:00"),
        _event("collect_miss", {"reason": "dirty", "target_file": "pkg/a.py"},
               ts="2026-01-01T00:00:03+00:00"),
        _event("gate1_split", {"accepted": 4, "existence": 3, "presence_confirmed": 4,
                               "presence_rejected": 5, "presence_fail_closed": 2,
                               "presence_reask": 3, "duplicate": 1, "non_py": 4},
               source="controller", ts="2026-01-01T00:00:04+00:00"),
    ]
    runs = analyze(events)
    out = "\n".join(_capture(render_run_summary, run) for run in runs.values())
    assert "RUNTIME COUNTERS" in out
    assert "collect" in out and "blocks 2" in out and "642 chars" in out and "memo 1" in out
    assert "shrink 1 (1 rows)" in out and "miss 1 (1 dirty)" in out
    assert "gate1" in out and "accepted 4" in out and "existence 3" in out
    assert "confirmed 4 / rejected 5 / fail-closed 2 / re-ask 3" in out
    assert "duplicate 1" in out and "non-py 4" in out


def test_analyze_logs_collect_totals_fall_back_to_events(monkeypatch, capsys):
    """No collect_summary event: the per-block events are the source."""
    monkeypatch.setattr("tools.agent_trace.tracer", RecordingTracer())
    events = [
        _event("run_start", {"goal": "g", "run_id": "run2"}),
        _event("collect_block", {"chars": 100, "rows_kept": 1, "rows_cut": 0,
                                 "memo_hit": False, "task_id": "T"}),
    ]
    runs = analyze(events)
    out = "\n".join(_capture(render_run_summary, run) for run in runs.values())
    assert "blocks 1" in out and "100 chars" in out


def test_analyze_logs_skips_the_block_when_there_is_nothing(monkeypatch, capsys):
    """Pre-M4 trace: no collect events, no gate1 split, no probes — no section."""
    monkeypatch.setattr("tools.agent_trace.tracer", RecordingTracer())
    runs = analyze([_event("run_start", {"goal": "g", "run_id": "run3"})])
    out = "\n".join(_capture(render_run_summary, run) for run in runs.values())
    assert "RUNTIME COUNTERS" not in out


def test_trace_round_snapshot_counts_events_falls_back_and_reads_all_traces(monkeypatch):
    monkeypatch.setattr("tools.agent_trace.tracer", RecordingTracer())
    module = _load_trace_round_snapshot()

    with tempfile.TemporaryDirectory() as raw:
        agent = Path(raw) / ".agent"
        agent.mkdir()

        def write_trace(name, events):
            with open(agent / name, "w", encoding="utf-8") as fh:
                for event in events:
                    fh.write(json.dumps(event) + "\n")

        write_trace("trace_a.jsonl", [
            _event("collect_block", {"chars": 100, "rows_kept": 3, "rows_cut": 1,
                                     "memo_hit": False, "task_id": "T"}, run_id="aaa"),
            _event("collect_block", {"chars": 100, "rows_kept": 3, "rows_cut": 1,
                                     "memo_hit": True, "task_id": "T"},
                   run_id="aaa", ts="2026-01-01T00:00:01+00:00"),
            _event("collect_shrink", {"path": "rows", "before": 500, "after": 100},
                   run_id="aaa", ts="2026-01-01T00:00:02+00:00"),
            _event("collect_miss", {"reason": "dirty", "target_file": "pkg/a.py"},
                   run_id="aaa", ts="2026-01-01T00:00:03+00:00"),
            # Per-run total: must not be summed a second time.
            _event("collect_summary", {"blocks": 2, "chars": 200, "memo_hits": 1,
                                       "shrink_calls": 1, "misses": {"dirty": 1}},
                   run_id="aaa", ts="2026-01-01T00:00:04+00:00"),
        ])
        write_trace("trace_b.jsonl", [
            _event("gate1_split", {"accepted": 1, "existence": 0, "presence_confirmed": 1,
                                   "presence_rejected": 0, "presence_fail_closed": 0,
                                   "presence_reask": 0, "duplicate": 0, "non_py": 2},
                   run_id="bbb", source="controller"),
            # A pre-M4 trace: the block is only visible as the prompt header.
            _event("llm_request", {}, run_id="bbb", source="coder", target="llm",
                   content="COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/a.py"),
            _event("llm_request", {}, run_id="bbb", source="coder", target="llm",
                   content="COLLECT MODEL (static facts, do not contradict):\nmodule: pkg/b.py",
                   ts="2026-01-02T00:00:01+00:00"),
        ])

        run = module.read_run(Path(raw))
        assert run["trace_files"] == 2, "every trace file of a tree is read"
        assert run["collect_source"] == "events"
        assert run["collect_block_occurrences"] == 2
        assert run["collect"]["chars"] == 200
        assert run["collect"]["memo_hits"] == 1
        assert run["collect"]["shrink"] == {"rows": 1}
        assert run["collect"]["miss"] == {"dirty": 1}
        assert run["gate1_split"]["non_py"] == 2
        # The grep fallback still sees both headers from the older trace.
        run_b = module.read_run(Path(raw), "bbb")
        assert run_b["gate1_split"]["accepted"] == 1
        assert run_b["collect_source"] == "grep"
        assert run_b["collect_block_occurrences"] == 2
        assert run_b["collect"]["blocks"] == 0


def test_readers_survive_the_stringified_wire_shape(monkeypatch, capsys):
    """What a real trace line looks like, not what a Python dict looks like.

    `AgentTracer._sanitize` stringifies every param: `memo_hit=False` lands as
    the string "False" (truthy), and the nested tallies of `collect_summary`
    (`shrink_by_path`, `misses`) land JSON-encoded. Both readers have to decode
    that or the whole runtime section silently disappears — analyze_logs'
    renderer swallows the `dict("{...}")` ValueError by design.
    """
    from tools.agent_trace import AgentTracer
    sanitize = AgentTracer()._sanitize
    monkeypatch.setattr("tools.agent_trace.tracer", RecordingTracer())

    def wire(kind, params, **kw):
        return _event(kind, sanitize(params), **kw)

    events = [
        wire("run_start", {"goal": "g"}),
        wire("collect_block", {"chars": 100, "rows_kept": 1, "rows_cut": 0,
                               "memo_hit": False, "task_id": "T"}),
        wire("collect_block", {"chars": 100, "rows_kept": 1, "rows_cut": 0,
                               "memo_hit": True, "task_id": "T"},
             ts="2026-01-01T00:00:01+00:00"),
        wire("collect_summary", {"blocks": 2, "chars": 200, "memo_hits": 1,
                                 "shrink_calls": 1, "shrink_by_path": {"rows": 1},
                                 "misses": {"dirty": 1}},
             ts="2026-01-01T00:00:02+00:00"),
    ]
    assert events[1]["params"]["memo_hit"] == "False"
    assert events[3]["params"]["misses"] == '{"dirty": 1}'

    runs = analyze(events)
    (run,) = runs.values()
    assert [b["memo_hit"] for b in run["collect_blocks"]] == [False, True]
    out = _capture(render_run_summary, run)
    assert "RUNTIME COUNTERS" in out
    assert "blocks 2" in out and "memo 1" in out
    assert "shrink 1 (1 rows)" in out and "miss 1 (1 dirty)" in out

    module = _load_trace_round_snapshot()
    with tempfile.TemporaryDirectory() as raw:
        agent = Path(raw) / ".agent"
        agent.mkdir()
        with open(agent / "trace_run1.jsonl", "w", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        snap = module.read_run(Path(raw))
        assert snap["collect"]["blocks"] == 2
        assert snap["collect"]["memo_hits"] == 1, '"False" must not count as a hit'
        # --run-id also matches the file name, the way every resume names it.
        assert module.read_run(Path(raw), "run1")["collect"]["blocks"] == 2
