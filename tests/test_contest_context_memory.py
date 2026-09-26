"""tests/test_contest_context_memory.py — KC-67: a context overflow is remembered
for 7 days, and the next session of the same model compacts at 80 % of it.

Kilo compacts a session on its own only where it knows the model's context size.
For the models it does not know the session grows until the provider rejects the
request, and the same model then overflows the same way round after round:
rounds 74, 112 and 113 sent sensenova-6.7 at exactly 262 144 tokens each time.
The provider's answer already carries the size, so ``tools/contest/
context_memory.py`` keeps it — one record per overflow in a file shared by every
round, aged out after ``context_memory_days``, and read back before every prompt
that goes into a session which already holds a conversation.

The cases are the ticket's acceptance list:

  1. an overflow that names the limit and one that does not both land in the
     file with the right fields; a record older than 7 days is dropped on the
     next write; a missing or broken file is no memory, never a failed round;
  2. a session at 81 % of a remembered size is ``summarize``d before the
     rework prompt, ``summarize`` first in the request log; at 79 % it is not,
     and when Kilo reports its own ``limit.context`` the runner never compacts;
  3. the fake Kilo only — no live provider, and no memory file outside
     ``tmp_path``.

The runner tests reuse the sandbox and harness of ``test_contest_runner.py``:
the same git worktrees, the same ``FakeKiloServer`` (``tests/_kilo_fake.py``
gained the ``POST /session/{id}/summarize`` route this module needs), and a
``context_memory_file`` that always points inside ``tmp_path``.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_runner as tr  # noqa: E402
from tools.contest import cli as contest_cli  # noqa: E402
from tools.contest import context_memory as cm  # noqa: E402
from tools.contest.roster import ContestConfig, load_roster  # noqa: E402

# every runner test below binds an ephemeral-port HTTP server
pytestmark = pytest.mark.xdist_group(name="port_bound_http_servers")

DAY = 86400.0

#: Round 113's sensenova payload: the limit and the prompt the provider named.
SENSENOVA_OVERFLOW = {
    "name": "ContextOverflowError",
    "data": {"message": (
        "This model's maximum context length is 262144 tokens. However, you "
        "requested 32000 output tokens and your prompt contains at least 262514 "
        "input tokens")},
}
#: Kenary's wording: no number at all, so only ``last_ok`` is known.
KENARY_OVERFLOW = {
    "name": "ContextOverflowError",
    "data": {"message": "the request exceeds the model's maximum context length"},
}

#: The size the compact scenarios remember: ``200 000``, so 81 % is
#: ``162 000`` and 79 % is ``158 000``, both read out of a reply's ``tokens``.
SIZE = 200_000
FULL_81 = 162_000
FULL_79 = 158_000


def _record(**over) -> cm.OverflowRecord:
    """One record of the memory, with *over* on top of a readable default."""
    base = dict(at=time.time(), round="113", agent="agent-a",
                provider="kenary", model="agent-a:free",
                limit=SIZE, last_ok=FULL_81, prompt=200_400)
    base.update(over)
    return cm.OverflowRecord(**base)


def _config(tmp_path, memory=None, **over) -> ContestConfig:
    """One agent, and the memory pointed at a file inside ``tmp_path``."""
    config = tr.make_config(["agent-a"])
    kwargs = dict(context_memory_file=str(memory or tmp_path / "context-memory.json"),
                  context_memory_days=cm.DEFAULT_DAYS,
                  compact_at_percent=cm.DEFAULT_COMPACT_AT_PERCENT)
    kwargs.update(over)
    return replace(config, **kwargs)


def _memory(tmp_path, **over) -> Path:
    """The memory file with one remembered size in it, inside ``tmp_path``."""
    path = tmp_path / "context-memory.json"
    assert cm.add(path, _record(**over)) is True
    return path


def _run(tmp_path, scenario, memory=None, **over):
    """``run_agent`` against *scenario*, with the memory at *memory*."""
    return tr._run_one(tmp_path, scenario, _config(tmp_path, memory=memory, **over))


def _posts(fake) -> list:
    """``(path, body)`` of every POST the fake saw, in order."""
    return [(record["path"], record["body"]) for record in fake.calls("POST")]


# ─────────────────────────────────────────────────────────────────────────────
# the provider's words
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_overflow_reads_the_named_limit_and_the_named_prompt():
    assert cm.parse_overflow(SENSENOVA_OVERFLOW["data"]["message"]) == (262_144, 262_514)
    # thousands separators are the other half of the same message
    assert cm.parse_overflow(
        "limit: 1,000,000 tokens; prompt contains 120,000 input tokens") == (1_000_000, 120_000)


def test_parse_overflow_names_nothing_when_the_provider_names_nothing():
    assert cm.parse_overflow(KENARY_OVERFLOW["data"]["message"]) == (None, None)
    assert cm.parse_overflow(None) == (None, None)
    assert cm.parse_overflow(42) == (None, None)
    # a bare number is not a limit: without a word for it there is nothing to say
    assert cm.parse_overflow("262144 tokens") == (None, None)


# ─────────────────────────────────────────────────────────────────────────────
# the file: one overflow, one line, kept 7 days
# ─────────────────────────────────────────────────────────────────────────────

def test_add_writes_one_record_atomically_and_load_reads_it_back(tmp_path):
    path = tmp_path / "memory" / "context-memory.json"
    assert cm.add(path, _record()) is True
    assert cm.add(path, _record(agent="agent-b")) is True

    assert not list(path.parent.glob("context-memory.json.*")), "a temp file must not survive"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [entry["agent"] for entry in data] == ["agent-a", "agent-b"]
    assert data[0] == {
        "at": pytest.approx(time.time(), abs=60), "round": "113", "agent": "agent-a",
        "provider": "kenary", "model": "agent-a:free",
        "limit": SIZE, "last_ok": FULL_81, "prompt": 200_400,
    }

    records = cm.load(path)
    assert [record.agent for record in records] == ["agent-a", "agent-b"]
    assert records[0].limit == SIZE and records[0].last_ok == FULL_81


def test_add_drops_the_records_past_the_age_cut(tmp_path):
    path = tmp_path / "context-memory.json"
    fresh = _record(at=time.time() - 2 * DAY)
    assert cm.add(path, _record(at=time.time() - 8 * DAY, agent="stale")) is True
    assert cm.add(path, fresh) is True

    # the third write is where the pruning shows: two records, not three
    assert cm.add(path, _record(at=time.time() - 6 * DAY, agent="young")) is True
    records = cm.load(path)
    assert [record.agent for record in records] == ["agent-a", "young"]
    assert (records[0].at, records[0].agent) == (fresh.at, "agent-a")

    # on read the same cut applies, so a file an older runner left behind ages
    # out on its own, without a write at all
    assert cm.load(path, now=time.time() + 8 * DAY) == []
    assert cm.load(path, days=1) == []


def test_load_of_a_broken_or_missing_file_is_no_memory(tmp_path):
    missing = tmp_path / "no-such" / "context-memory.json"
    assert cm.load(missing) == []

    broken = tmp_path / "context-memory.json"
    for raw in ("{not json", json.dumps({"a": 1}), json.dumps("nope"),
                "[1, {}, {\"provider\": \"\"}, {\"at\": \"never\", "
                "\"provider\": \"kenary\", \"model\": \"m:free\"}]"):
        broken.write_text(raw, encoding="utf-8")
        assert cm.load(broken) == [], raw

    # a directory in place of the file is no memory either
    (tmp_path / "dir-memory").mkdir()
    assert cm.load(tmp_path / "dir-memory") == []

    # and neither read nor write raises: a record that cannot be aged is kept by
    # nobody, a path that cannot be written is a warning for the runner
    assert cm.load(broken, days=0) == []
    assert cm.load(broken, days=None) == []
    assert cm.load(broken, days=float("nan")) == []
    assert cm.load(broken, now=None) == []


def test_add_refuses_what_it_cannot_write(tmp_path):
    target = tmp_path / "context-memory.json"
    assert cm.add(tmp_path, _record()) is False, "a path that is a directory"
    assert cm.add(target, None) is False, "not a record"
    assert cm.add(target, _record(at=float("nan"))) is False, "a time that is not a time"
    assert cm.add(target, _record(), days=float("nan")) is False
    assert cm.load(target) == []
    # and a broken file is just an empty one: the write overwrites it
    target.write_text("{not json", encoding="utf-8")
    assert cm.add(target, _record()) is True
    assert len(cm.load(target)) == 1


def test_smallest_size_is_the_smallest_the_model_has_ever_given(tmp_path):
    path = tmp_path / "context-memory.json"
    cm.add(path, _record(at=time.time() - 6 * DAY, limit=262_144, last_ok=262_144))
    cm.add(path, _record(at=time.time() - 3 * DAY, limit=None, last_ok=104_065))
    cm.add(path, _record(at=time.time() - 1 * DAY, model="other:free", limit=1_000))
    cm.add(path, _record(at=time.time() - 1 * DAY, provider="other", limit=1_000))
    cm.add(path, _record(at=time.time() - 1 * DAY, limit=None, last_ok=None))

    records = cm.load(path)
    # the named limit wins over the last OK of the same record, but the smallest
    # record still wins over it: hy3's 104 065 in the ticket's table
    assert cm.smallest_size(records, "kenary", "agent-a:free") == 104_065
    assert cm.size_of(_record(limit=500, last_ok=900)) == 500
    assert cm.size_of(_record(limit=None, last_ok=999)) == 999
    assert cm.size_of(_record(limit=None, last_ok=None)) is None
    # a neighbour's overflow says nothing about this model
    assert cm.smallest_size(records, "kenary", "unknown:free") is None
    assert cm.smallest_size(records, "other", "agent-a:free") == 1_000
    # a memory that is not a list of records is no size at all
    for bad in (None, {}, "text", 42, "[]"):
        assert cm.smallest_size(bad, "kenary", "agent-a:free") is None


def test_memory_path_and_the_two_numbers_are_fail_open(tmp_path):
    out_dir = tmp_path / "contest-out" / "114"
    default = tmp_path / "contest-out" / "context-memory.json"
    # no key at all, an empty one and no config: next to the rounds' output
    for config in (None, replace(tr.make_config(["agent-a"]), context_memory_file="")):
        assert cm.memory_path(config, out_dir) == default

    assert cm.memory_path(
        replace(tr.make_config(["agent-a"]), context_memory_file="/tmp/kilo/memory.json"),
        out_dir) == Path("/tmp/kilo/memory.json")

    class Opaque:
        @property
        def context_memory_file(self):
            raise RuntimeError("unreadable")

    assert cm.memory_path(Opaque(), out_dir) == default
    assert cm.days_of(None) == cm.DEFAULT_DAYS
    assert cm.days_of(Opaque()) == cm.DEFAULT_DAYS
    assert cm.compact_at_percent(None) == cm.DEFAULT_COMPACT_AT_PERCENT
    assert cm.compact_at_percent(Opaque()) == cm.DEFAULT_COMPACT_AT_PERCENT

    cases = (
        (dict(context_memory_days="seven", compact_at_percent="eighty"), 0.0,
         cm.DEFAULT_COMPACT_AT_PERCENT),
        (dict(context_memory_days=0, compact_at_percent=0), 0.0, 0.0),
        (dict(context_memory_days=30, compact_at_percent=101), 30.0,
         cm.DEFAULT_COMPACT_AT_PERCENT),
        (dict(context_memory_days=float("nan"), compact_at_percent=60), 0.0, 60.0),
        (dict(context_memory_days=1.5, compact_at_percent=0.5), 1.5, 0.5),
    )
    for over, days, percent in cases:
        config = replace(tr.make_config(["agent-a"]), **over)
        assert cm.days_of(config) == days, over
        assert cm.compact_at_percent(config) == percent, over


def test_plan_lines_is_one_line_per_model_with_a_remembered_size(tmp_path):
    path = tmp_path / "context-memory.json"
    cm.add(path, _record(model="agent-a:free"))
    cm.add(path, _record(model="agent-b:free", limit=None, last_ok=None))

    agents = tr.make_config(["agent-a", "agent-b"]).agents
    lines = cm.plan_lines(cm.load(path), agents)
    assert lines == [f"context memory: kenary/agent-a:free = {SIZE} (compact at 80 %)"]
    assert cm.plan_lines([], agents) == []
    assert cm.plan_lines(cm.load(path), None) == []
    assert cm.plan_lines(cm.load(path), agents, percent=0) == \
        [f"context memory: kenary/agent-a:free = {SIZE} (compact at 0 %)"]


# ─────────────────────────────────────────────────────────────────────────────
# the config keys
# ─────────────────────────────────────────────────────────────────────────────

_ROSTER = """\
[contest]
max_parallel = 2
gate_llm_profile = gate

[gate]
base_url = https://example/v1
api_key = test-key
model = test/gate

[contest.agent.agent-a]
model = kenary/agent-a:free
"""


def _roster_with(**contest) -> str:
    """``_ROSTER`` with one more key in ``[contest]`` each."""
    extra = "\n".join(f"{key} = {value}" for key, value in contest.items())
    return _ROSTER.replace("[contest]\n", f"[contest]\n{extra}\n")


def test_the_roster_reads_the_three_keys_and_a_typo_is_the_default(tmp_path):
    roster_ini = tmp_path / "contest.ini"
    roster_ini.write_text(_ROSTER, encoding="utf-8")

    config = load_roster(roster_ini)
    assert (config.context_memory_file, config.context_memory_days,
            config.compact_at_percent) == ("", 7.0, 80.0)
    out_dir = tmp_path / "contest-out" / "114"
    assert cm.memory_path(config, out_dir) == tmp_path / "contest-out" / "context-memory.json"

    roster_ini.write_text(_roster_with(context_memory_file="/tmp/kilo/memory.json",
                                       context_memory_days=30,
                                       compact_at_percent=60), encoding="utf-8")
    config = load_roster(roster_ini)
    assert (config.context_memory_file, config.context_memory_days,
            config.compact_at_percent) == ("/tmp/kilo/memory.json", 30.0, 60.0)
    assert cm.memory_path(config, out_dir) == Path("/tmp/kilo/memory.json")

    # a typo is the default, and a nonsense value is the mechanism off: the round
    # runs rather than refusing to start because context_memory_days has letters
    for days, percent, want_days, want_percent in (("seven", "eighty", 7.0, 80.0),
                                                   ("0", "0", 0.0, 0.0),
                                                   ("-2", "400", 0.0, 400.0)):
        roster_ini.write_text(_roster_with(context_memory_days=days,
                                           compact_at_percent=percent), encoding="utf-8")
        config = load_roster(roster_ini)
        assert (config.context_memory_days, config.compact_at_percent) == (want_days,
                                                                           want_percent)
        assert cm.days_of(config) == want_days
        assert cm.compact_at_percent(config) == (want_percent if want_percent <= 100 else 80.0)


# ─────────────────────────────────────────────────────────────────────────────
# the overflow: one line in the shared file
# ─────────────────────────────────────────────────────────────────────────────

def _overflow_scenario(error, last_ok_input):
    """A first reply that reports a size and goes through, then the overflow.

    The first turn leaves a dirty file and no commit, so the harvest sends a
    rework prompt into the same session — that is the prompt the overflow lands
    on, and it is what KC-54 ends. ``max_continues_per_attempt = 0`` keeps it a
    STALLED turn with no second session, so the record is the only side effect.
    """
    return {
        "turns": [
            {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
             "message_info": {"tokens": {"input": last_ok_input, "output": 0}}},
            {"events": ["busy"], "error": error},
        ],
    }


def test_an_overflow_that_names_the_limit_is_remembered_with_all_three_numbers(tmp_path):
    memory = _memory(tmp_path)  # a memory that already exists, to prove the write appends
    scenario = _overflow_scenario(SENSENOVA_OVERFLOW, 260_000)
    sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory=memory,
                                 max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    assert run.last_error == "context overflow"
    assert [t["kind"] for t in run.turns] == ["initial", "rework"]

    records = cm.load(memory)
    (record,) = records[1:]
    assert (record.round, record.agent) == ("out", "agent-a")
    assert (record.provider, record.model) == ("kenary", "agent-a:free")
    assert record.limit == 262_144 and record.prompt == 262_514
    assert record.last_ok == 260_000, "input + cache read + reasoning + output"
    assert cm.smallest_size([record], "kenary", "agent-a:free") == 262_144


def test_an_overflow_that_names_no_limit_is_remembered_with_only_the_last_ok(tmp_path):
    memory = _memory(tmp_path)
    scenario = _overflow_scenario(KENARY_OVERFLOW, 110_000)
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory=memory,
                                   max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED

    (record,) = cm.load(memory)[1:]
    assert record.limit is None and record.prompt is None
    assert record.last_ok == 110_000
    assert cm.size_of(record) == 110_000


def test_the_empty_message_kilo_keeps_for_the_refused_reply_is_not_the_last_ok(tmp_path):
    """Round 114: after the overflow the session's last assistant message is the
    one Kilo opened for the refused reply, every count zero. The last OK is the
    reply before it — else a provider that names no limit leaves no size."""
    memory = _memory(tmp_path)
    scenario = _overflow_scenario(KENARY_OVERFLOW, 110_000)
    scenario["turns"][1]["message_info"] = {
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory=memory,
                                   max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED

    (record,) = cm.load(memory)[1:]
    assert record.last_ok == 110_000
    assert cm.size_of(record) == 110_000


def test_a_memory_the_runner_cannot_write_ends_the_overflow_the_way_it_does_today(tmp_path,
                                                                                   caplog):
    """The memory write fails: the overflow still ends the turn as KC-54 does,
    and the failure is a warning, not a raise into the round."""
    caplog.set_level("WARNING", logger="tools.contest.runner")
    memory = tmp_path / "read-only" / "context-memory.json"
    memory.parent.mkdir()
    memory.parent.chmod(0o500)
    try:
        _sb, _fake, _h, run, _ = _run(tmp_path, _overflow_scenario(SENSENOVA_OVERFLOW, 260_000),
                                       memory=memory, max_continues_per_attempt=0)
    finally:
        memory.parent.chmod(0o755)
    assert run.state is tr.AgentState.STALLED
    assert run.last_error == "context overflow"
    assert any("context memory" in record.message for record in caplog.records)


def test_a_broken_memory_file_never_raises_into_the_run(tmp_path):
    memory = tmp_path / "context-memory.json"
    memory.write_text("{not json", encoding="utf-8")
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory=memory)
    assert run.state is tr.AgentState.READY
    (turn,) = run.turns
    assert turn["context_source"] == "none" and turn["context_size"] is None
    assert turn["fill"] is None and turn["compacted"] is False


# ─────────────────────────────────────────────────────────────────────────────
# the compact: summarize before the prompt, at 80 % of the remembered size
# ─────────────────────────────────────────────────────────────────────────────

def _rework_scenario(full):
    """A commit with no test file, so the harvest sends a rework prompt into the
    same session — the second prompt of a session that already holds a
    conversation. ``full`` is the context the first reply reports, so the fill
    is that over the remembered ``SIZE``."""
    tokens = {"input": full - 6_000, "cache": {"read": 6_000}, "reasoning": 0, "output": 0}
    return {"turns": [
        {"on_prompt": tr.work_no_test, "events": ["busy", "idle"],
         "message_info": {"tokens": tokens}},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}


def _summarize_path(session_id):
    return f"/session/{session_id}/summarize"


def test_a_session_at_eighty_one_percent_compacts_before_the_rework_prompt(tmp_path):
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    sb, fake, _h, run, _ = _run(tmp_path, _rework_scenario(FULL_81), memory=memory)
    tr._assert_ready(run, sb.ws("agent-a"))
    assert [t["kind"] for t in run.turns] == ["initial", "rework"]

    posts = [path for path, _ in _posts(fake)]
    sid = run.session_id
    compact = posts.index(_summarize_path(sid))
    prompt = next(i for i in range(compact, len(posts))
                  if posts[i] == f"/session/{sid}/prompt_async")
    assert compact < prompt, "the compact is before the prompt that earned it"
    assert posts.count(_summarize_path(sid)) == 1
    # POST /session opens the run, and the first prompt is never preceded by one
    assert posts[0] == "/session" and posts[1] == f"/session/{sid}/prompt_async"

    first, second = run.turns
    assert first["compacted"] is False and second["compacted"] is True
    # the first prompt opens an empty session, so only the rework turns it up at 81 %
    assert first["fill"] == 0.0 and second["fill"] == 81.0
    for turn in run.turns:
        assert turn["context_size"] == SIZE and turn["context_source"] == "remembered"

    (line,) = tr._jsonl(sb.out_dir / "agent-a" / "turns.jsonl")[1:2]
    assert line["compacted"] is True and line["context_source"] == "remembered"
    assert line["fill"] == 81.0 and line["context_size"] == SIZE
    assert fake.events_of("session.compacted")


def test_the_console_says_when_the_compact_finished_and_how_much_it_saved(tmp_path, caplog):
    """The operator's view: a line before the compact, and one after it with the
    context before and after — the summary Kilo wrote is what it shrank to."""
    caplog.set_level("INFO", logger="tools.contest.runner")
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    scenario = dict(_rework_scenario(FULL_81), summary_tokens=12_000)
    sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory=memory)
    tr._assert_ready(run, sb.ws("agent-a"))

    lines = [r.getMessage() for r in caplog.records if "compact" in r.getMessage()]
    assert any("context 162,000 tokens = 81.0% of the 200,000 remembered, at 80% — "
               "compacting before rework" in line for line in lines), lines
    assert any("compact finished in" in line
               and "context 162,000 -> 12,000 tokens (-150,000, -93%)" in line
               and "the round continues with the rework prompt" in line
               for line in lines), lines
    second = run.turns[1]
    assert (second["context_before"], second["context_after"]) == (162_000, 12_000)


def test_a_compact_whose_size_is_not_reported_says_so(tmp_path, caplog):
    caplog.set_level("INFO", logger="tools.contest.runner")
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    _sb, _fake, _h, run, _ = _run(tmp_path, _rework_scenario(FULL_81), memory=memory)
    lines = [r.getMessage() for r in caplog.records]
    assert any("the size after is not reported yet" in line for line in lines), lines
    assert run.turns[1]["context_after"] is None


def test_a_prompt_below_the_threshold_says_no_compact(tmp_path, caplog):
    caplog.set_level("INFO", logger="tools.contest.runner")
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    _run(tmp_path, _rework_scenario(FULL_79), memory=memory)
    lines = [r.getMessage() for r in caplog.records]
    assert any("= 79.0% of the 200,000 remembered, below 80% — no compact before rework"
               in line for line in lines), lines


def test_a_session_at_seventy_nine_percent_does_not_compact(tmp_path):
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    _sb, fake, _h, run, _ = _run(tmp_path, _rework_scenario(FULL_79), memory=memory)
    assert [t["kind"] for t in run.turns] == ["initial", "rework"]

    assert not any("/summarize" in path for path, _ in _posts(fake))
    second = run.turns[1]
    assert second["compacted"] is False and second["idle_status"] == "idle"
    assert second["context_size"] == SIZE and second["context_source"] == "remembered"
    assert second["fill"] == 79.0


def test_the_runner_never_compacts_a_model_kilo_reports_a_limit_for(tmp_path):
    """``spec.context_limit`` is set: Kilo compacts those sessions on its own, so
    the runner records the size and its source and sends the prompt as today —
    at the same 81 % a remembered size would have compacted."""
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    config = replace(_config(tmp_path, memory=memory),
                     agents=(replace(_config(tmp_path).agents[0], context_limit=SIZE),))
    sb, fake, _h, run, _ = tr._run_one(tmp_path, _rework_scenario(FULL_81), config)
    tr._assert_ready(run, sb.ws("agent-a"))

    assert not any("/summarize" in path for path, _ in _posts(fake))
    first, second = run.turns
    assert first["fill"] == 0.0 and second["fill"] == 81.0
    for turn in run.turns:
        assert turn["context_size"] == SIZE and turn["context_source"] == "kilo"
        assert turn["compacted"] is False
    assert second["idle_status"] == "idle"


def test_a_last_ok_is_the_size_when_no_limit_was_named(tmp_path):
    """Kenary names no limit: the remembered size is the last reply that still
    went through, and a session full to it compacts like a named limit would."""
    memory = _memory(tmp_path, limit=None, last_ok=FULL_81)
    _sb, fake, _h, run, _ = _run(tmp_path, _rework_scenario(FULL_81), memory=memory)

    posts = [path for path, _ in _posts(fake)]
    assert _summarize_path(run.session_id) in posts
    second = run.turns[1]
    assert second["context_source"] == "remembered"
    assert second["context_size"] == FULL_81 and second["fill"] == 100.0
    assert second["compacted"] is True


def test_a_refused_compact_leaves_the_prompt_alone(tmp_path):
    """The server answers anything but 204 to ``summarize``: the prompt still
    goes out as it stands, the turn says it did not compact, and no exception
    reaches the round."""
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    scenario = dict(_rework_scenario(FULL_81), summarize_status=500)
    sb, fake, _h, run, _ = _run(tmp_path, scenario, memory=memory)
    tr._assert_ready(run, sb.ws("agent-a"))

    posts = [path for path, _ in _posts(fake)]
    assert _summarize_path(run.session_id) in posts
    assert posts[-1] == f"/session/{run.session_id}/prompt_async"
    second = run.turns[1]
    assert second["compacted"] is False and second["fill"] == 81.0
    assert run.last_error is None


def test_a_session_with_no_remembered_size_is_prompted_as_today(tmp_path):
    _sb, fake, _h, run, _ = _run(tmp_path, _rework_scenario(FULL_81))
    assert not any("/summarize" in path for path, _ in _posts(fake))
    for turn in run.turns:
        assert turn["context_size"] is None and turn["context_source"] == "none"
        assert turn["fill"] is None and turn["compacted"] is False


def test_no_assistant_message_is_no_fill(tmp_path):
    """The session replied without reporting tokens: the fill is 0, and 0 never
    crosses the percent, so the prompt goes out without a compact."""
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    scenario = {"turns": [
        {"on_prompt": tr.work_no_test, "events": ["busy", "idle"], "assistant": "no tokens"},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    _sb, fake, _h, run, _ = _run(tmp_path, scenario, memory=memory)
    assert not any("/summarize" in path for path, _ in _posts(fake))
    assert run.turns[1]["fill"] == 0.0 and run.turns[1]["compacted"] is False


def test_compacting_a_fresh_session_is_never_tried(tmp_path):
    """The first prompt opens an empty session, so a fill for it would be a read
    of the session that has nothing in it: only a rework, a continue or a retry
    may earn a compact."""
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"],
                           "message_info": {"tokens": {"input": FULL_81}}}]}
    _sb, fake, _h, run, _ = _run(tmp_path, scenario, memory=memory)
    assert not any("/summarize" in path for path, _ in _posts(fake))
    (turn,) = run.turns
    assert turn["kind"] == "initial" and turn["compacted"] is False
    # the session is still empty when its first prompt goes out, so the fill is 0
    assert turn["context_source"] == "remembered" and turn["fill"] == 0.0


def test_zero_percent_turns_the_runners_compact_off(tmp_path):
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    _sb, fake, _h, run, _ = _run(tmp_path, _rework_scenario(FULL_81), memory=memory,
                                  compact_at_percent=0)
    assert not any("/summarize" in path for path, _ in _posts(fake))
    second = run.turns[1]
    assert second["context_size"] == SIZE and second["fill"] == 81.0
    assert second["compacted"] is False


def test_zero_days_is_no_memory_at_all(tmp_path):
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    _sb, fake, _h, run, _ = _run(tmp_path, _rework_scenario(FULL_81), memory=memory,
                                  context_memory_days=0)
    assert not any("/summarize" in path for path, _ in _posts(fake))
    second = run.turns[1]
    assert second["context_source"] == "none" and second["context_size"] is None
    assert second["compacted"] is False


def test_an_overflow_is_remembered_and_the_next_session_compacts_in_time(tmp_path):
    """The whole ticket in one pass: an overflow that names the limit is written
    to the shared file, and the next session of the same model at 81 % of the
    remembered size is summarized before its rework prompt."""
    memory = tmp_path / "context-memory.json"
    overflow = _overflow_scenario(SENSENOVA_OVERFLOW, 260_000)
    _sb, _fake, _h, run, _ = _run(tmp_path / "round-1", overflow, memory=memory,
                                   max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    (record,) = cm.load(memory)
    assert record.limit == 262_144 and record.last_ok == 260_000
    assert cm.smallest_size([record], "kenary", "agent-a:free") == 262_144

    # 215 000 of a remembered 262 144 is 82 %: just past the 80 % the runner compacts at
    sb, fake, _h, run, _ = _run(tmp_path / "round-2", _rework_scenario(215_000), memory=memory)
    tr._assert_ready(run, sb.ws("agent-a"))
    posts = [path for path, _ in _posts(fake)]
    assert _summarize_path(run.session_id) in posts
    second = run.turns[1]
    assert second["context_source"] == "remembered" and second["compacted"] is True
    assert second["context_size"] == 262_144 and second["fill"] == 82.0


# ─────────────────────────────────────────────────────────────────────────────
# the plan: one line per model the round knows the size of
# ─────────────────────────────────────────────────────────────────────────────

def _plan(tmp_path, capsys, memory=None, **over) -> str:
    """The round's start plan, with the memory at *memory*."""
    intake = contest_cli.Intake(ticket_path=Path("epic-tasks/114-x.md"), title="KC-67",
                                base_sha="a" * 40, out_dir=tmp_path / "out")
    contest_cli._print_plan(intake, _config(tmp_path, memory=memory, **over),
                            tmp_path / "out", run_tests=True)
    return capsys.readouterr().out

def test_the_start_plan_prints_one_line_per_model_with_a_remembered_size(tmp_path, capsys):
    """The plan names the models the round will compact itself — one line each —
    and nothing at all when there is no memory, an empty one or a broken one."""
    # the memory in its own directory, so the default path holds no record
    memory = _memory(tmp_path / "mem", limit=SIZE, last_ok=None)
    out = _plan(tmp_path, capsys, memory=memory)
    assert "agents" in out and str(tmp_path / "out") in out
    assert f"context memory: kenary/agent-a:free = {SIZE} (compact at 80 %)" in out

    capsys.readouterr()
    assert "context memory:" not in _plan(tmp_path, capsys)
    capsys.readouterr()
    (memory.parent / "empty").write_text("[]", encoding="utf-8")
    assert "context memory:" not in _plan(tmp_path, capsys, memory=memory.parent / "empty")
    capsys.readouterr()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert "context memory:" not in _plan(tmp_path, capsys, memory=broken)
