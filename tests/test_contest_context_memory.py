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
from tools.contest import runner as runner_mod  # noqa: E402
from tools.contest.roster import ContestConfig, load_roster  # noqa: E402


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


def _prompt_texts(fake) -> list:
    """The text of every ``prompt_async`` the fake saw, in order."""
    return ["".join(part.get("text", "") for part in (body or {}).get("parts", []))
            for path, body in _posts(fake) if path.endswith("/prompt_async")]


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
        "limit": SIZE, "last_ok": FULL_81, "prompt": 200_400, "output": None,
        "grew": None,
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
    # round 145: a last_ok under context_min_window sizes nothing; with the
    # floor off (0) it is what it always was
    assert cm.size_of(_record(limit=None, last_ok=999), 0) == 999
    assert cm.size_of(_record(limit=None, last_ok=999)) is None
    assert cm.size_of(_record(limit=None, last_ok=None)) is None
    # a neighbour's overflow says nothing about this model
    assert cm.smallest_size(records, "kenary", "unknown:free") is None
    assert cm.smallest_size(records, "other", "agent-a:free") == 1_000
    # a memory that is not a list of records is no size at all
    for bad in (None, {}, "text", 42, "[]"):
        assert cm.smallest_size(bad, "kenary", "agent-a:free") is None


def test_a_last_ok_is_a_floor_so_the_largest_one_sizes_the_model(tmp_path):
    """KC-73's live run: three agnes-2-0-flash overflows that named no limit —
    17 382, 254 613 and 26 321 OK. Each only proves the window is at least that
    big, so the model is sized at 254 613, not at 17 382 (where the runner
    compacted every turn and the agent gave up). A named limit still wins when
    it is smaller, and the smallest named limit wins among named ones."""
    path = tmp_path / "context-memory.json"
    for ok in (17_382, 254_613, 26_321):
        cm.add(path, _record(limit=None, last_ok=ok))
    records = cm.load(path)
    assert cm.smallest_size(records, "kenary", "agent-a:free") == 254_613
    assert cm.remembered(records, "kenary", "agent-a:free") == (254_613, None)
    cm.add(path, _record(limit=200_000, last_ok=199_000))
    cm.add(path, _record(limit=300_000, last_ok=299_000))
    records = cm.load(path)
    assert cm.smallest_size(records, "kenary", "agent-a:free") == 200_000


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
    # KC-69: the 32 000 requested output is not the prompt's to use
    assert record.output == 32_000
    assert cm.smallest_size([record], "kenary", "agent-a:free") == 230_144


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


def test_an_overflow_one_step_jumped_into_is_remembered_but_sizes_nothing(tmp_path):
    """KC-73's live runs: the last reply went through at 14 179 and asked for a
    pile of `read`s; their results (~134 000 tokens) overflowed a ~120 000
    window in one step. `grew` is recorded, and such a floor sizes nothing — at
    80 % of 14 179 the runner compacted every turn and the agent gave up."""
    memory = _memory(tmp_path)
    scenario = _overflow_scenario(KENARY_OVERFLOW, 14_179)
    scenario["turns"][0]["tool_parts"] = [
        {"tool": "read", "status": "completed", "input": {"filePath": f"ballast/part{i:02d}.txt"},
         "output": "w" * 67_000} for i in range(8)]
    _sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory=memory,
                                   max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    (record,) = cm.load(memory)[1:]
    assert record.last_ok == 14_179
    assert record.grew == 8 * 67_000 // runner_mod.SUMMARY_CHARS_PER_TOKEN
    assert cm.size_of(record) is None
    assert cm.smallest_size([record], "kenary", "agent-a:free") is None


def test_a_last_ok_is_a_size_only_when_the_overflow_grew_a_little_past_it():
    """KC-73: within `LOOSE_FLOOR_SHARE` of `last_ok` the floor is tight and
    sizes the model as before; far past it, nothing; a record written before
    `grew` existed, and a named limit, are what they always were."""
    tight = _record(limit=None, last_ok=247_828, grew=3_000)
    loose = _record(limit=None, last_ok=17_382, grew=269_086)
    old = _record(limit=None, last_ok=118_488)
    named = _record(limit=262_144, last_ok=17_382, grew=269_086)
    assert cm.size_of(tight) == 247_828
    assert cm.size_of(loose) is None
    assert cm.size_of(old) == 118_488
    assert cm.size_of(named) == 262_144
    assert cm.smallest_size([loose, tight], "kenary", "agent-a:free") == 247_828
    assert cm.OverflowRecord.from_dict(loose.to_dict()) == loose
    assert cm.OverflowRecord.from_dict(old.to_dict()).grew is None


class _Transcript:
    def __init__(self, messages):
        self._messages = messages

    def messages(self, session):
        if isinstance(self._messages, Exception):
            raise self._messages
        return self._messages


def test_last_reply_reads_the_tokens_and_what_its_tool_results_added():
    reply = {"info": {"role": "assistant", "tokens": {"input": 17_000, "output": 382}},
             "parts": [{"type": "text", "text": "reading"},
                       {"type": "tool", "tool": "read", "state": {"status": "completed",
                                                                  "output": "x" * 400_000}},
                       {"type": "tool", "tool": "read", "state": {"status": "completed",
                                                                  "output": "y" * 676_347}}]}
    refused = {"info": {"role": "assistant", "tokens": {"input": 0, "output": 0}}, "parts": []}
    summary = {"info": {"role": "assistant", "summary": True, "tokens": {"output": 2_000}},
               "parts": [{"type": "text", "text": "summary"}]}
    assert runner_mod._last_reply(_Transcript([reply, refused, summary]), None) == (
        17_382, 1_076_347 // runner_mod.SUMMARY_CHARS_PER_TOKEN)
    plain = {"info": {"role": "assistant", "tokens": {"input": 9_000}}, "parts": []}
    assert runner_mod._last_reply(_Transcript([plain]), None) == (9_000, 0)
    assert runner_mod._last_reply(_Transcript([refused]), None) == (0, None)
    assert runner_mod._last_reply(_Transcript(RuntimeError("down")), None) == (0, None)


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


def test_the_summary_kilo_starts_on_the_overflow_is_not_the_last_ok(tmp_path):
    """Round 49's agnes-2-0-flash: on the overflow Kilo starts its own chunked
    compact at once, and the session's last assistant message is a summary with
    every count zero by the time the runner reads it. That summary is the fill
    of a compacted session (KC-69), not the last reply that went through — it
    left `last_ok: 0`, which is no size, when the reply before it held 247 386."""
    memory = _memory(tmp_path)
    scenario = _overflow_scenario(KENARY_OVERFLOW, 110_000)
    scenario["turns"][1]["message_info"] = {
        "summary": True,
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
    # a FILE where the folder should be: the write fails for every user — a
    # chmod 0o500 folder is writable for root, which is who a container runs as
    memory.parent.write_text("not a folder", encoding="utf-8")
    _sb, _fake, _h, run, _ = _run(tmp_path, _overflow_scenario(SENSENOVA_OVERFLOW, 260_000),
                                   memory=memory, max_continues_per_attempt=0)
    assert run.state is tr.AgentState.STALLED
    assert run.last_error == "context overflow"
    assert any("context memory" in record.message for record in caplog.records)


def test_a_broken_memory_file_never_raises_into_the_run(tmp_path):
    """A broken memory is no memory; with the fallback off too, the run is
    unsized exactly as it was before KC-10."""
    memory = tmp_path / "context-memory.json"
    memory.write_text("{not json", encoding="utf-8")
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory=memory,
                                  context_limit_fallback=0)
    assert run.state is tr.AgentState.READY
    (turn,) = run.turns
    assert turn["context_source"] == "none" and turn["context_size"] is None
    assert turn["fill"] is None and turn["compacted"] is False


def test_a_broken_memory_file_falls_back_to_the_fallback_window(tmp_path):
    """KC-10: the same broken memory, with the fallback on — the model is sized
    by ``context_limit_fallback`` instead, the run still never raises."""
    memory = tmp_path / "context-memory.json"
    memory.write_text("{not json", encoding="utf-8")
    scenario = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, _fake, _h, run, _ = _run(tmp_path, scenario, memory=memory,
                                  context_limit_fallback=32_768)
    assert run.state is tr.AgentState.READY
    (turn,) = run.turns
    assert turn["context_source"] == "fallback" and turn["context_size"] == 32_768
    assert turn["fill"] == 0.0 and turn["compacted"] is False


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
    assert any("context 162,000 tokens = 81.0% of 200,000 (remembered), at 80% — "
               "compacting before rework" in line for line in lines), lines
    assert any("compact finished in" in line
               and "context 162,000 -> 12,000 tokens (-150,000, -93%) = 6.0% of 200,000"
               in line for line in lines), lines
    assert any("the round continues in the same session with the rework prompt"
               in r.getMessage() for r in caplog.records)
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
    assert any("= 79.0% of 200,000 (remembered), below 80% — no compact before rework"
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


def test_a_model_kilo_reports_a_limit_for_compacts_at_the_same_percent(tmp_path):
    """KC-69: ``spec.context_limit`` is set — intake's, or the remembered size
    KC-69 hands Kilo — and the runner compacts at the same 80 % a remembered
    size does: Kilo's own compact comes only after the step that crossed it."""
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    config = replace(_config(tmp_path, memory=memory),
                     agents=(replace(_config(tmp_path).agents[0], context_limit=SIZE),))
    sb, fake, _h, run, _ = tr._run_one(tmp_path, _rework_scenario(FULL_81), config)
    tr._assert_ready(run, sb.ws("agent-a"))

    assert _summarize_path(run.session_id) in [path for path, _ in _posts(fake)]
    first, second = run.turns
    assert first["fill"] == 0.0 and second["fill"] == 81.0
    for turn in run.turns:
        assert turn["context_size"] == SIZE and turn["context_source"] == "kilo"
    assert first["compacted"] is False and second["compacted"] is True
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


def _run_swapping(tmp_path, scenario, config):
    """``run_agent`` on the fake that replays ``turns_after`` in a second session."""
    sb = tr.Sandbox(tmp_path)
    with tr._OverflowFake(scenario) as fake:
        run = tr.Harness(sb, fake, config).go()
    return sb, fake, run


def test_a_refused_compact_goes_on_in_a_new_session_with_the_task(tmp_path, caplog):
    """KC-69: the server answers anything but 204 to ``summarize``: the prompt
    going out as it stands is the overflow the compact was for, so the work goes
    on in a new session — the round prompt, the continue note, then the rework."""
    caplog.set_level("INFO", logger="tools.contest.runner")
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    scenario = dict(_rework_scenario(FULL_81), summarize_status=500,
                    turns_after=[{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}])
    sb, fake, run = _run_swapping(tmp_path, scenario, _config(tmp_path, memory=memory))
    tr._assert_ready(run, sb.ws("agent-a"))

    first, second = fake.sessions()[:2]
    posts = [path for path, _ in _posts(fake)]
    assert _summarize_path(first.id) in posts
    assert posts[-1] == f"/session/{second.id}/prompt_async"
    turn = run.turns[1]
    assert turn["compacted"] is False and turn["fill"] == 81.0
    assert turn["swapped_from"] == first.id and turn["new_session"] == second.id
    text = _prompt_texts(fake)[-1]
    assert "You are continuing unfinished work" in text
    assert "(the previous session wrote no summary)" in text
    lines = [r.getMessage() for r in caplog.records]
    assert any("compact did not finish" in line and "new session" in line for line in lines)


def test_a_compact_that_frees_too_little_goes_on_in_a_new_session(tmp_path, caplog):
    """KC-69: the compact finished but its summary is still at or past the
    threshold — a new session, carrying the summary text."""
    caplog.set_level("INFO", logger="tools.contest.runner")
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    scenario = dict(_rework_scenario(FULL_81), summary_tokens=170_000,
                    summary_text="did steps 1-3 of the ticket",
                    turns_after=[{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}])
    sb, fake, run = _run_swapping(tmp_path, scenario, _config(tmp_path, memory=memory))
    tr._assert_ready(run, sb.ws("agent-a"))
    turn = run.turns[1]
    assert turn["compacted"] is True and turn["context_after"] == 170_000
    assert turn["new_session"] == fake.sessions()[1].id
    assert "did steps 1-3 of the ticket" in _prompt_texts(fake)[-1]
    lines = [r.getMessage() for r in caplog.records]
    assert any("the compact left 85.0% — still at or past 80%" in line for line in lines)


def test_a_model_with_no_size_at_all_is_prompted_as_today(tmp_path):
    """KC-10's ``context_limit_fallback = 0`` is the pre-KC-10 answer: no number
    at all, so no fill and no compact — this is what KC-67's "no remembered size"
    case is now, with the fallback turned off."""
    _sb, fake, _h, run, _ = _run(tmp_path, _rework_scenario(FULL_81),
                                 context_limit_fallback=0)
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
                                  context_memory_days=0, context_limit_fallback=0)
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
    # KC-69: the 32 000 requested output is not the prompt's to use
    assert record.output == 32_000
    assert cm.smallest_size([record], "kenary", "agent-a:free") == 230_144

    # 215 000 of a remembered 230 144 budget (262 144 - 32 000) is 93.4 %:
    # past the 80 % the runner compacts at
    sb, fake, _h, run, _ = _run(tmp_path / "round-2", _rework_scenario(215_000), memory=memory)
    tr._assert_ready(run, sb.ws("agent-a"))
    posts = [path for path, _ in _posts(fake)]
    assert _summarize_path(run.session_id) in posts
    second = run.turns[1]
    assert second["context_source"] == "remembered" and second["compacted"] is True
    assert second["context_size"] == 230_144 and second["fill"] == 93.4


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


# ─────────────────────────────────────────────────────────────────────────────
# KC-69: the remembered size reaches Kilo as `limit.context`
# ─────────────────────────────────────────────────────────────────────────────

#: kenary's overflow, word for word from laguna-s-2-1:free on 2026-09-26: no
#: number in it, so only the last reply that went through is remembered
KENARY_MESSAGE = ("the request exceeds the model's maximum context length. "
                  "reduce the input or max_tokens")


def _overlay(content) -> dict:
    """The `limit` of every model in a `KILO_CONFIG_CONTENT` string."""
    providers = json.loads(content)["provider"]
    return {f"{pid}/{mid}": model.get("limit")
            for pid, body in providers.items() for mid, model in body["models"].items()}


def _threshold(content):
    """The `compaction.threshold_percent` of a `KILO_CONFIG_CONTENT` string."""
    return (json.loads(content).get("compaction") or {}).get("threshold_percent")



def test_parse_output_reads_the_requested_output_and_nothing_else():
    """sensenova names the output it reserved; kenary names nothing."""
    assert cm.parse_output(SENSENOVA_OVERFLOW["data"]["message"]) == 32_000
    assert cm.parse_output(KENARY_MESSAGE) is None
    assert cm.parse_output(None) is None


def test_a_record_before_kc69_keeps_its_size():
    """No output in the record is today's size; a named output is subtracted."""
    assert cm.size_of(_record(limit=262_144)) == 262_144
    assert cm.size_of(_record(limit=262_144, output=32_000)) == 230_144
    assert cm.size_of(_record(limit=10, output=32_000)) == 10, "a reserve that leaves nothing"
    assert cm.OverflowRecord.from_dict({"at": 1.0, "provider": "p", "model": "m",
                                        "limit": 5}).output is None


def test_kilo_limit_is_the_real_window_and_input_is_the_compact_point():
    """``context`` is the real window — a hard wall in Kilo; ``input`` is 80 % of
    the budget plus Kilo's 20 000 reserve, where Kilo compacts after a step."""
    assert cm.kilo_limit(230_144, 32_000) == {"context": 262_144, "input": 204_115,
                                               "output": 32_000}
    # no output named: Kilo's own 32 000 — a `limit` without `output` is a
    # ConfigInvalidError that skips the whole KILO_CONFIG_CONTENT (live, 7.6.2)
    assert cm.kilo_limit(149_359, None) == {"context": 181_359, "input": 139_487,
                                             "output": 32_000}
    # 0 % is the runner's compact off: Kilo's own at the full budget
    assert cm.kilo_limit(230_144, 32_000, percent=0) == {"context": 262_144,
                                                         "input": 230_144, "output": 32_000}
    assert cm.kilo_limit(None, 32_000) is None


def test_the_file_one_overflow_wrote_is_what_the_next_round_hands_kilo(tmp_path, monkeypatch):
    """The whole ticket: an overflow that names the limit is written to the
    shared file, and the next round's server gets that size as `limit.context`,
    with the budget on the spec."""
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    memory = tmp_path / "context-memory.json"
    overflow = _overflow_scenario(SENSENOVA_OVERFLOW, 260_000)
    _run(tmp_path / "round-1", overflow, memory=memory, max_continues_per_attempt=0)
    assert json.loads(memory.read_text(encoding="utf-8"))[0]["output"] == 32_000

    config = _config(tmp_path, memory=memory)
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "71", None)
    (agent,) = out.agents
    assert agent.context_limit == 230_144
    assert _overlay(content) == {agent.model: {"context": 262_144, "input": 204_115,
                                               "output": 32_000}}
    assert _threshold(content) is None, "no global threshold: other models keep theirs"


def test_a_provider_that_names_no_size_is_handed_its_last_ok(tmp_path, monkeypatch):
    """kenary (laguna) says nothing but "exceeds": the last reply that went
    through is the size, with no output in the overlay."""
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    memory = _memory(tmp_path, limit=None, last_ok=215_042, output=None)
    config = _config(tmp_path, memory=memory)
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "71", None)
    assert out.agents[0].context_limit == 215_042
    assert _overlay(content) == {out.agents[0].model: {"context": 247_042, "input": 192_033,
                                                       "output": 32_000}}


def test_intake_s_own_limit_wins_and_nothing_is_sent(tmp_path, monkeypatch):
    """A model Kilo knows the size of keeps Kilo's number; no overlay at all."""
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    config = _config(tmp_path, memory=_memory(tmp_path, limit=262_144, output=32_000))
    config = replace(config, agents=tuple(replace(a, context_limit=128_000) for a in config.agents))
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "71", None)
    assert out is config and content is None


def test_the_overlay_merges_over_intake_s_registration(tmp_path, monkeypatch):
    """KC-35's registered model keeps its entry and gains the limit."""
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    config = _config(tmp_path, memory=_memory(tmp_path, limit=262_144, output=32_000))
    agent = config.agents[0]
    registered = json.dumps({"provider": {agent.provider_id: {"models": {
        agent.model_id: {"name": agent.model_id, "reasoning": True}}}}})
    _out, content = contest_cli._with_remembered_limits(config, tmp_path / "o" / "1", registered)
    model = json.loads(content)["provider"][agent.provider_id]["models"][agent.model_id]
    assert model == {"name": agent.model_id, "reasoning": True,
                     "limit": {"context": 262_144, "input": 204_115, "output": 32_000}}


def test_an_attached_server_gets_nothing(tmp_path, monkeypatch):
    """An attached server never reads the overlay."""
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    config = _config(tmp_path, memory=_memory(tmp_path, limit=262_144),
                     server="http://127.0.0.1:4096")
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "71", None)
    assert out is config and content is None


def test_a_turned_off_compact_sends_the_window_at_the_full_budget(tmp_path, monkeypatch):
    """0 % is the runner's compact off: Kilo still learns the window, and keeps
    its own compact at the full size."""
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    config = _config(tmp_path, memory=_memory(tmp_path, limit=262_144), compact_at_percent=0.0)
    _out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "71", None)
    assert list(_overlay(content).values())[0]["input"] == 262_144


def test_an_operator_s_own_compaction_is_left_alone(tmp_path, monkeypatch):
    """The overlay adds, never replaces: the operator's `compaction` stays as is."""
    monkeypatch.setenv("KILO_CONFIG_CONTENT", json.dumps({"compaction": {"threshold_percent": 60}}))
    config = _config(tmp_path, memory=_memory(tmp_path, limit=262_144))
    _out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "71", None)
    assert _threshold(content) == 60


def test_a_broken_operator_value_is_the_round_unchanged(tmp_path, monkeypatch):
    """Fail-open: a `KILO_CONFIG_CONTENT` that is not JSON is no overlay."""
    monkeypatch.setenv("KILO_CONFIG_CONTENT", "not json")
    config = _config(tmp_path, memory=_memory(tmp_path, limit=262_144))
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "71", None)
    assert out is config and content is None


class _FullFake(tr._BenchFake):
    """Every session opens already holding a reply at 81 % of ``SIZE`` — a
    permission asked in its first turn is asked in a full context."""

    SEED = {"info": {"role": "assistant", "finish": "tool-calls",
                     "tokens": {"input": FULL_81, "output": 0, "reasoning": 0,
                                "cache": {"read": 0, "write": 0}}},
            "parts": [{"type": "text", "text": "read the whole tree"}]}

    def _create_session(self, body, directory):
        session = super()._create_session(body, directory)
        self._sessions[session["id"]].messages.append(dict(self.SEED))
        return session


def test_a_permission_in_a_full_context_is_refused_and_the_next_prompt_compacts(tmp_path, caplog):
    """KC-69: the ask at 81 % is refused with the context reason, its line in
    decisions.jsonl and on the console carries the fill, and the prompt after
    the turn compacts first."""
    caplog.set_level("INFO", logger="tools.contest.runner")
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    # the turn holds 3 s after the ask, the way a model that goes on working does
    scenario = {"summary_tokens": 12_000, "turns": [
        dict(tr._permission_turn(tr.work_no_test, ["/var/lib/*"]), delay=3.0),
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb = tr.Sandbox(tmp_path)
    with _FullFake(scenario) as fake:
        run = tr.Harness(sb, fake, _config(tmp_path, memory=memory)).go()
    tr._assert_ready(run, sb.ws("agent-a"))

    (replied,) = fake.events_of("permission.replied")
    assert replied["properties"]["reply"] == "reject"
    (line,) = tr._jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert line["layer"] == "context" and line["reason"] == runner_mod.CONTEXT_FULL_REJECT
    assert line["context"] == {"tokens": FULL_81, "size": SIZE, "source": "remembered",
                               "fill": 81.0, "compact_at": 80.0}
    # the turn was stopped once after the refusal, so the compact comes now
    assert [p for p, _ in _posts(fake)].count(f"/session/{run.session_id}/abort") == 1
    turn = run.turns[1]
    assert turn["context_refused"] is True and turn["compacted"] is True
    assert _summarize_path(run.session_id) in [path for path, _ in _posts(fake)]
    lines = [r.getMessage() for r in caplog.records]
    assert any("permission" in l and "-> reject (context)" in l
               and "context 162,000 tokens = 81.0% of 200,000 (remembered), compact at 80%" in l
               for l in lines), lines


class _AbortErrorFake(_FullFake):
    """Kilo as round 49 saw it: the abort after the refusal lands while the next
    step has already started, and the turn ends in `session.error:
    MessageAbortedError` before its idle — not in a plain idle."""

    def _run_turn(self, session, turn, text):
        if not turn.get("abort_error"):
            return super()._run_turn(session, turn, text)
        plain = {k: v for k, v in turn.items() if k not in ("abort_error", "delay")}
        super()._run_turn(session, dict(plain, idle=False), text)
        until = time.monotonic() + 10
        while not session.aborted and time.monotonic() < until:
            time.sleep(0.05)
        self._emit({"type": "session.error", "properties": {
            "sessionID": session.id,
            "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}}}})
        self._emit({"type": "session.idle", "properties": {"sessionID": session.id}})


def test_the_runner_s_own_abort_for_a_full_context_is_not_an_error(tmp_path, caplog):
    """Round 49: the stop KC-69 sends after a refusal comes back as
    `MessageAbortedError`; the turn is a plain idle and the next prompt
    compacts — not ERROR, not Kilo's shutdown."""
    caplog.set_level("INFO", logger="tools.contest.runner")
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    scenario = {"summary_tokens": 12_000, "turns": [
        dict(tr._permission_turn(tr.work_no_test, ["/var/lib/*"]), abort_error=True),
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb = tr.Sandbox(tmp_path)
    with _AbortErrorFake(scenario) as fake:
        run = tr.Harness(sb, fake, _config(tmp_path, memory=memory)).go()
    tr._assert_ready(run, sb.ws("agent-a"))

    assert not run.resumable
    first, second = run.turns[0], run.turns[1]
    assert first["idle_status"] == "idle" and first["context_aborted"] is True
    assert second["kind"] in runner_mod._CONTEXT_GATE_KINDS
    assert second["context_refused"] is True and second["compacted"] is True
    assert _summarize_path(run.session_id) in [path for path, _ in _posts(fake)]
    assert not any("aborted by Kilo" in r.getMessage() for r in caplog.records)


def test_a_message_aborted_error_with_no_context_stop_is_still_kilo_s(tmp_path):
    """Round 87 unchanged: an abort the runner never sent is Kilo's shutdown —
    ERROR, and `resumable` for `--resume`."""
    err = {"name": "MessageAbortedError", "data": {"message": "Aborted"}}
    sb, _fake, _h, run, _ = tr._run_one(tmp_path, {"turns": [{"error": err}]})
    assert run.state is tr.AgentState.ERROR and run.resumable
    assert "context_aborted" not in run.turns[0]


def test_a_permission_below_the_threshold_is_decided_as_today_with_its_fill(tmp_path, caplog):
    """KC-69: under the threshold the policy decides; the line still says the fill."""
    caplog.set_level("INFO", logger="tools.contest.runner")
    memory = _memory(tmp_path, limit=SIZE, last_ok=None)
    cfg = _config(tmp_path, memory=memory, tmp_roots=("/tmp/*",))
    sb, _fake, _h, run, _ = tr._run_one(
        tmp_path, {"turns": [tr._permission_turn(tr.work_ready, ["/tmp/*"])]}, cfg,
        tr.make_policy(cfg, "reject"))
    tr._assert_ready(run, sb.ws("agent-a"))
    (line,) = tr._jsonl(sb.out_dir / "agent-a" / "decisions.jsonl")
    assert line["layer"] == "mechanical" and line["context"]["fill"] == 0.0
    assert any("-> once (mechanical), context 0 tokens = 0.0% of 200,000" in r.getMessage()
               for r in caplog.records)


def test_a_chunked_summary_with_no_tokens_is_sized_by_its_text(tmp_path):
    """KC-69: Kilo 7.6.2's chunked compact leaves the summary at 0 tokens — the
    runner sizes it by its text, 4 characters a token, instead of nothing."""
    backend = type("B", (), {"messages": lambda self, s: [
        {"info": {"role": "assistant", "summary": True, "tokens": {"input": 0, "output": 0}},
         "parts": [{"type": "text", "text": "x" * 4198}]}]})()
    assert runner_mod._summary_tokens(backend, None) == 1050
    empty = type("B", (), {"messages": lambda self, s: [
        {"info": {"role": "assistant", "summary": True, "tokens": {}}, "parts": []}]})()
    assert runner_mod._summary_tokens(empty, None) is None


def test_the_fill_after_a_compact_is_the_summary_not_the_reply_before_it():
    """KC-69: the summary starts the history — the reply before it is gone. Live
    on laguna the old 64 894 was read again and refused every later ask."""
    before = {"info": {"role": "assistant", "tokens": {"input": 64_000, "output": 894}},
              "parts": []}
    summary = {"info": {"role": "assistant", "summary": True, "tokens": {"input": 0, "output": 0}},
               "parts": [{"type": "text", "text": "y" * 4000}]}
    backend = type("B", (), {"messages": lambda self, s: [before, summary]})()
    assert runner_mod._context_tokens(backend, None) == 1000
    after = {"info": {"role": "assistant", "tokens": {"input": 11_000, "output": 50}}, "parts": []}
    backend = type("B", (), {"messages": lambda self, s: [before, summary, after]})()
    assert runner_mod._context_tokens(backend, None) == 11_050


# ─────────────────────────────────────────────────────────────────────────────
# round 149: a loose remembered window must not lock a model small
# ─────────────────────────────────────────────────────────────────────────────

def _loose_record(**over) -> cm.OverflowRecord:
    """A loose record — KC-73's pattern: last_ok far below what grew past it."""
    base = dict(at=time.time(), round="149", agent="agent-a",
                provider="kenary", model="agent-a:free",
                limit=None, last_ok=33_000, grew=200_000, prompt=None)
    base.update(over)
    return cm.OverflowRecord(**base)


def test_a_loose_record_far_below_the_declared_window_sizes_nothing():
    """Ticket 149 test 1: 33 000 / 200 000 under Kilo's 262 144 is 13 % of the
    wall — not evidence of it — so ``size_of`` returns None and
    ``_context_budget`` keeps Kilo's window."""
    record = _loose_record()
    assert cm.size_of(record, 32_000, declared=262_144) is None
    spec = replace(tr.make_config(["agent-a"]).agents[0], context_limit=262_144)
    assert runner_mod._context_budget(spec, [record.to_dict()]) == (262_144, "kilo")


def test_a_loose_record_with_no_window_and_a_fallback_sizes_nothing():
    """Ticket 149 test 2: no Kilo window, fallback 128 000 — 33 000 is 26 % of
    it, under the 60 % share, so KC-73 holds: the fallback is what sizes."""
    record = _loose_record()
    assert cm.size_of(record, 32_000, fallback=128_000) is None
    config = replace(tr.make_config(["agent-a"]), context_limit_fallback=128_000)
    assert runner_mod._context_budget(config.agents[0], [record.to_dict()],
                                      config) == (128_000, "fallback")


def test_glm_s_loose_record_near_the_declared_wall_still_sizes_the_model():
    """Ticket 149 test 3: glm-4.5-flash — 81 311 went through and ~42 000 more
    overflowed a 131 072 window: 62 % of the wall, so it counts."""
    record = _loose_record(last_ok=81_311, grew=42_000)
    assert cm.size_of(record, 32_000, declared=131_072) == 81_311
    spec = replace(tr.make_config(["agent-a"]).agents[0], context_limit=131_072)
    assert runner_mod._context_budget(spec, [record.to_dict()]) == (81_311, "remembered")


def test_a_tight_record_under_a_declared_window_still_sizes_the_model():
    """Ticket 149 test 4: a tight 40 000 under Kilo's 131 072 sizes as before."""
    record = _loose_record(last_ok=40_000, grew=1_000)
    assert cm.size_of(record, 32_000, declared=131_072) == 40_000
    spec = replace(tr.make_config(["agent-a"]).agents[0], context_limit=131_072)
    assert runner_mod._context_budget(spec, [record.to_dict()]) == (40_000, "remembered")


def test_a_small_declared_window_lowers_the_floor_for_a_tight_record():
    """Ticket 149 test 5: Kilo declares 32 768; a tight 28 000 is under
    context_min_window but at 85 % of the declared window — the floor drops
    to share × D and the model is remembered at 28 000."""
    record = _loose_record(last_ok=28_000, grew=500)
    assert cm.size_of(record, 32_000, declared=32_768) == 28_000
    spec = replace(tr.make_config(["agent-a"]).agents[0], context_limit=32_768)
    assert runner_mod._context_budget(spec, [record.to_dict()]) == (28_000, "remembered")


def test_a_fallback_window_lowers_the_floor_for_a_tight_record_with_no_declared():
    """Ticket 152 test 6: Kilo declares nothing, so the wall is the round's
    ``context_limit_fallback`` — the same number `size_of` already measures a
    loose record against, and now the same number the floor drops against. A
    tight 28 000 of a 32 768 fallback is 85 %, so the model is remembered.
    With neither a declared window nor a fallback the floor stands at
    context_min_window and the same record sizes nothing: there is no wall to
    call 28 000 close to."""
    record = _loose_record(last_ok=28_000, grew=500)
    share_pct = cm.full_refusal_percent(None)
    assert record.last_ok >= share_pct / 100.0 * 32_768, "85 % of the fallback"
    assert cm.size_of(record, 32_000, fallback=32_768) == 28_000
    assert cm.size_of(record, 32_000, declared=None, fallback=32_768) == 28_000
    assert cm.size_of(record, 32_000, fallback=None) is None
    assert cm.size_of(record, 32_000) is None
    config = replace(tr.make_config(["agent-a"]), context_limit_fallback=32_768)
    assert runner_mod._context_budget(config.agents[0], [record.to_dict()], config) == (
        28_000, "remembered")
    # a declared window above context_min_window lowers the floor not at all,
    # so the fallback is the only wall that saves this record
    assert cm.size_of(record, 32_000, declared=131_072) is None


def test_a_remembered_size_equal_to_kilo_s_window_is_kilo_s():
    """Ticket 149 test 7: a remembered size equal to Kilo's window exactly is
    not smaller, so the source is ``"kilo"``."""
    record = _loose_record(last_ok=262_144, grew=1_000)
    spec = replace(tr.make_config(["agent-a"]).agents[0], context_limit=262_144)
    assert runner_mod._context_budget(spec, [record.to_dict()]) == (262_144, "kilo")


def test_the_next_round_s_overlay_carries_nothing_for_a_loose_small_record(
        tmp_path, monkeypatch):
    """Ticket 149 test 1 (overlay half): a loose 33 000 under Kilo's 262 144
    hands Kilo nothing — intake's window stands, no ``limit`` in the overlay."""
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    memory = tmp_path / "context-memory.json"
    assert cm.add(memory, _loose_record()) is True
    base = _config(tmp_path, memory=memory)
    config = replace(base, agents=(replace(base.agents[0], context_limit=262_144),))
    out, content = contest_cli._with_remembered_limits(config, tmp_path / "out" / "149", None)
    assert out.agents[0].context_limit == 262_144
    assert content is None


def test_a_kc73_loop_guard_a_loose_record_does_not_compact_every_turn(tmp_path):
    """Ticket 149 test 6: the memory holds a loose 45 000 / 200 000 record and
    Kilo declares 262 144. Without the fix the record sizes the model at
    45 000, the runner compacts at 36 000, and every turn (the round prompt
    alone is 15–20k) earns a compact — KC-73's hy3/agnes failure. With the
    fix Kilo's window stays in charge: six turns under 80 % of 262 144 compact
    no more often than every 3 turns, and the run ends READY."""
    memory = tmp_path / "context-memory.json"
    assert cm.add(memory, _loose_record(last_ok=45_000, grew=200_000)) is True
    base = _config(tmp_path, memory=memory, max_rework=5)
    config = replace(base, agents=(replace(base.agents[0], context_limit=262_144),))
    fills = [50_000, 55_000, 60_000, 65_000, 70_000, 75_000]
    turns = []
    for i, fill in enumerate(fills):
        on_prompt = tr.work_ready if i == len(fills) - 1 else tr.work_no_test
        tokens = {"input": fill - 6_000, "cache": {"read": 6_000},
                  "reasoning": 0, "output": 0}
        turns.append({"on_prompt": on_prompt, "events": ["busy", "idle"],
                      "message_info": {"tokens": tokens}})
    sb, fake, _h, run, _ = tr._run_one(tmp_path, {"turns": turns}, config)
    tr._assert_ready(run, sb.ws("agent-a"))
    compacts = sum(1 for turn in run.turns if turn.get("compacted"))
    assert compacts <= len(run.turns) // 3, (
        f"{compacts} compacts in {len(run.turns)} turns — the loose record "
        "locked the model small again")
    for turn in run.turns:
        assert turn["context_source"] == "kilo"
        assert turn["context_size"] == 262_144
    assert not any(p.endswith("/summarize") for p, _ in _posts(fake))
