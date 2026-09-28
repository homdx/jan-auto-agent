"""tests/test_contest_runner_summary.py — KC-40: a nearly-full session with a
diff already in it writes its own summary, copied out to a file, before
anything is compacted.

Kilo's own compact shrinks the session's history and says nothing to the
operator. On a clean tree that is the right answer — there is nothing to
explain. On a tree that already holds an uncommitted diff it is not: the diff
is what the session was in the middle of, and nobody outside the session ever
gets to read why it looks the way it does. So at `summary_at_percent` of a
known size, once per session, the runner asks the model for a short account of
the work in chat and copies the reply to `<out_dir>/<agent>.summary.md`. If the
ask comes back empty, the KC-39 fresh-session edge takes over instead, with the
worktree's lines.

The cases are the ticket's acceptance list:

  1. a fill at or above `summary_at_percent` on a dirty tree, the first ask of
     the session: one extra `prompt_async` carrying `SUMMARY_PROMPT`, before
     any `summarize`; `<agent>.summary.md` exists with the reply; the next
     prompt carries the summary on; a clean tree at the same fill is compacted
     by KC-10 with no summary prompt at all; a second crossing in the same
     session is not asked again;
  2. a summary prompt that gets an empty reply, and one that ends in
     `session.error`: both abort and open a second session whose first prompt
     carries the `git status` lines;
  3. a captured summary followed by a KC-39 reset in the same attempt: the new
     session's opening prompt carries the summary's text, not only the file
     names;
   4. a second session of the same attempt appends to the same file rather than
      overwriting the first.

And the edge's own limits, which are what a fill at either end would trip on:
the shipped default is 90 and the committed `contest.ini` agrees with it; a
session at or past its own size is compacted, never asked; and the 0 that
switches `compact_at_percent` off switches the summary off too.


Every test runs the scripted `FakeKiloServer` of `tests/_kilo_fake.py` — no
`kilo` binary, no live provider. The feasibility question a fake cannot answer,
whether a real nearly-full model can still write a truthful summary, is the
hand-run probe in `contest-bench/kc40/live_probe.py`.
"""

from __future__ import annotations

import configparser
import sys
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_runner as tr  # noqa: E402
from tools.contest.runner import (  # noqa: E402
    SUMMARY_CONTINUES_NOTE, SUMMARY_FELL_BACK_NOTE, SUMMARY_PROMPT,
)

pytestmark = pytest.mark.xdist_group("port_bound_http_servers")

#: The window the fallback sizes a model against — `contest.ini`'s `FALLBACK`.
FALLBACK = 32_768
#: 24 000 of 32 768 is 73.2 % — at the summary's 70 %, below the compact's 80 %.
SUMMARY_FILL = 24_000
#: 27 000 of 32 768 is 82.4 % — at both, so the ask comes first and the compact
#: right after it.
BOTH_FILL = 27_000
#: 30 000 of 32 768 is 91.6 % — at the shipped 90 %, above the compact's 80 %.
DEFAULT_FILL = 30_000
#: 40 000 of 32 768 is 122 % — past the session's own size, where no ask can fit.
OVER_FILL = 40_000
#: What these tests set the knob to, so a fill under the compact's percent still
#: earns an ask. The shipped default is 90, pinned by one test below.
SUMMARY_AT_PERCENT = 70

REPLY_ONE = "I changed pkg/thing.py to return 7; the ticket still needs a test."
REPLY_TWO = ("Still pkg/thing.py at 7: I retried the edit, nothing is committed, "
             "the test file is next.")


def _config(tmp_path, **over):
    """One agent on a model the provider declares no limit for, so only
    `context_limit_fallback` can size it — KC-10's case, which is where the
    summary edge gets a fill to compare against. `summary_at_percent` is 70
    here, so a fill under the compact's 80 % still earns an ask."""
    over.setdefault("summary_at_percent", SUMMARY_AT_PERCENT)
    config = tr._kc10_config(tmp_path, **over)
    return replace(config, context_memory_file=str(tmp_path / "context-memory.json"))


def _summary_path(sb):
    return sb.out_dir / "agent-a.summary.md"


def _prompt_requests(fake):
    """The POSTs the fake saw that were `prompt_async`, in the order sent."""
    return [record for record in fake.requests
            if record["method"] == "POST" and record["path"].endswith("/prompt_async")]


def _prompt_text(record):
    return "".join(part.get("text", "") for part in
                   (record.get("body") or {}).get("parts", [])
                   if isinstance(part, dict))


def _summary_asks(fake):
    """The prompt requests that carried `SUMMARY_PROMPT` — the asks themselves."""
    return [record for record in _prompt_requests(fake)
            if SUMMARY_PROMPT in _prompt_text(record)]


def test_a_dirty_tree_at_the_summary_percent_earns_its_own_summary_prompt(tmp_path):
    """The first ask: 73.2 % of a known size, a diff in the worktree, nothing
    committed. The session gets `SUMMARY_PROMPT` as a prompt of its own — before
    any `summarize`, and none at all here, because 73.2 % is under the compact's
    80 % — and the reply is copied out to `<agent>.summary.md`."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(SUMMARY_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario, _config(tmp_path))
    tr._assert_ready(run, sb.ws("agent-a"))

    asks = _summary_asks(fake)
    assert len(asks) == 1
    # the ask is its own prompt, sent before anything is compacted — and here the
    # fill never reached the compact's percent, so the compact never ran at all
    assert _prompt_text(asks[0]).startswith(SUMMARY_PROMPT)
    assert run.compactions == 0

    assert run.summary == REPLY_ONE and run.summaries == 1
    path = _summary_path(sb)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert REPLY_ONE in text
    # the title is written once, with the agent's name in it
    assert text.count("# The agent's own account of its uncommitted work") == 1
    assert "agent-a" in text

    (turn,) = [t for t in run.turns if t.get("summary_attempted")]
    assert turn["summary_captured"] is True
    assert turn["summary_outcome"] == "captured"
    assert turn["summary_fill"] == 73.2 and turn["summary_at"] == 70.0


def test_the_prompt_after_the_ask_carries_the_summary_on(tmp_path):
    """The summary is what the conversation is about now, so the prompt right
    after the ask says so instead of restating the ticket."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(SUMMARY_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario, _config(tmp_path))
    tr._assert_ready(run, sb.ws("agent-a"))

    prompts = [_prompt_text(record) for record in _prompt_requests(fake)]
    assert len(prompts) == 3
    # the ask, then the continue that carries the summary and the worktree
    assert prompts[1].startswith(SUMMARY_PROMPT)
    assert SUMMARY_CONTINUES_NOTE in prompts[2]
    assert "pkg/thing.py" in prompts[2]
    # the round prompt itself was never restated in the second prompt
    assert "implementing one ticket from an epic round" not in prompts[2]


def test_the_ask_comes_before_the_compact_when_both_would_fire(tmp_path):
    """82.4 % crosses both percents, so the summary is asked for first and the
    compact that follows has the reply behind it, not ahead of it."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(BOTH_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ], "summary_tokens": 4_000}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario, _config(tmp_path))
    tr._assert_ready(run, sb.ws("agent-a"))

    posts = [(record["path"], _prompt_text(record))
             for record in fake.requests if record["method"] == "POST"]
    ask = [i for i, (_, text) in enumerate(posts) if SUMMARY_PROMPT in text]
    compact = [path for path, _ in posts].index(f"/session/{run.session_id}/summarize")
    assert ask and ask[0] < compact, "the ask is before the compact that follows it"
    assert [path for path, _ in posts].count(f"/session/{run.session_id}/summarize") == 1
    assert run.summaries == 1 and run.compactions == 1
    assert _summary_path(sb).exists()


def test_a_clean_tree_at_the_same_fill_is_compacted_without_a_summary_prompt(tmp_path):
    """Nothing uncommitted is nothing to explain: at the same fill a committed
    tree gets KC-10's plain compact and no summary prompt at all."""
    sb, fake, _h, run, _ = tr._run_one(tmp_path, tr._rework_scenario(BOTH_FILL),
                                        _config(tmp_path))
    tr._assert_ready(run, sb.ws("agent-a"))

    assert not _summary_asks(fake)
    assert run.summaries == 0 and run.summary == ""
    assert run.compactions == 1
    assert not _summary_path(sb).exists()
    for turn in run.turns:
        assert "summary_attempted" not in turn


def test_a_second_crossing_in_the_same_session_is_not_asked_again(tmp_path):
    """One ask per session. The fill crosses twice in one session — the summary
    turn's own reply does not lower it — and the second crossing sends the
    continue as it stands."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(SUMMARY_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(SUMMARY_FILL)},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario,
                                       _config(tmp_path, max_continues_per_attempt=2))
    tr._assert_ready(run, sb.ws("agent-a"))

    asks = _summary_asks(fake)
    assert len(asks) == 1, "the second crossing of the same session is not asked"
    assert run.summaries == 1
    asked = [t for t in run.turns if t.get("summary_attempted")]
    assert len(asked) == 1 and asked[0]["kind"] == "continue"
    # both crossings read the same fill, so the gate did see it twice
    assert [t["fill"] for t in run.turns if t.get("kind") == "continue"] == [73.2, 73.2]
    text = _summary_path(sb).read_text(encoding="utf-8")
    assert text.count(REPLY_ONE) == 1


@pytest.mark.parametrize(
    "error_turn",
    ({"events": ["busy", "idle"], "assistant": ""},          # an idle that said nothing
     {"error": {"name": "KiloDbError", "message": "database is locked"},
      "message_info": tr._fill_tokens(SUMMARY_FILL)}),       # no reply at all
    ids=("empty_reply", "session_error"),
)
def test_a_summary_turn_that_ends_in_an_error_falls_back_to_a_fresh_session(tmp_path, error_turn):
    """An empty reply, and a `session.error`: neither is an answer. The session
    is aborted and a fresh one opens, whose first prompt carries the round prompt
    and the `git status` lines — KC-39's edge, not a second one."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(SUMMARY_FILL)},
        error_turn,
    ], "turns_after": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = tr._run_overflow_one(tmp_path, scenario, _config(tmp_path))

    asks = _summary_asks(fake)
    assert len(asks) == 1, "the ask happened once, and its answer was the failure"
    assert run.summaries == 0 and run.summary == ""
    assert not _summary_path(sb).exists()

    asked = [t for t in run.turns if t.get("summary_attempted")]
    assert asked and asked[0]["summary_captured"] is False

    (old, new) = fake.sessions()
    assert len(fake.sessions()) == 2
    paths = [record["path"] for record in fake.requests if record["method"] == "POST"]
    # the abort of the old session goes out before the session that replaces it
    aborted = paths.index(f"/session/{old.id}/abort")
    assert aborted < paths.index("/session", aborted)
    opening = [record for record in _prompt_requests(fake)
               if record["path"] == f"/session/{new.id}/prompt_async"]
    assert opening
    text = _prompt_text(opening[0])
    # the `git status` lines the worktree still holds — the point of the fallback
    assert "pkg/thing.py" in text
    assert SUMMARY_PROMPT not in text


def test_a_fallback_after_a_reset_carries_the_captured_summary(tmp_path):
    """A summary captured in one session, then a KC-39 reset in the same
    attempt: the replacement's opening prompt carries the summary's text, not
    only the `git status` lines."""
    # five turns of session 1: the ask, then the same bytes three times, which is
    # the repeated diff KC-39 resets on. `turns_after` is the replacement's script.
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(SUMMARY_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"]},
    ], "turns_after": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb, fake, _h, run, _ = tr._run_overflow_one(
        tmp_path, scenario,
        _config(tmp_path, max_continues_per_attempt=2, max_sessions_per_attempt=2))
    tr._assert_ready(run, sb.ws("agent-a"))

    (old, new) = fake.sessions()
    assert run.sessions == 2 and run.attempt == 0
    assert run.summary == REPLY_ONE and run.summaries == 1
    reset = [t for t in run.turns if t.get("session_reason") == "repeat"]
    assert len(reset) == 1 and reset[0]["new_session"] == new.id

    opening = _prompt_text([record for record in _prompt_requests(fake)
                            if record["path"] == f"/session/{new.id}/prompt_async"][0])
    # the summary's own words, not only the file names it changed
    assert REPLY_ONE in opening
    assert SUMMARY_FELL_BACK_NOTE.split("\n\n")[0] in opening
    assert "pkg/thing.py" in opening


def test_two_sessions_of_one_attempt_append_to_the_same_summary_file(tmp_path):
    """A second session of the same attempt asks again and appends: the operator
    opening `<agent>.summary.md` reads the chain from the first session to the
    second, not just the newest."""
    first = {"turns": [
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(SUMMARY_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"]},
    ]}
    second = {"turns": [
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(SUMMARY_FILL)},
        {"assistant": REPLY_TWO, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = tr._run_overflow_one(
        tmp_path, {**first, "turns_after": second["turns"]},
        _config(tmp_path, max_continues_per_attempt=2, max_sessions_per_attempt=2))
    tr._assert_ready(run, sb.ws("agent-a"))

    (old, new) = fake.sessions()
    assert run.sessions == 2 and run.attempt == 0
    assert run.summaries == 2
    assert _summary_asks(fake), "each session asked once"
    assert len(_summary_asks(fake)) == 2

    text = _summary_path(sb).read_text(encoding="utf-8")
    # one title, one section per session, in the order they were written
    assert text.count("# The agent's own account of its uncommitted work") == 1
    assert text.index(REPLY_ONE) < text.index(REPLY_TWO)
    assert f"## session {old.id}" in text and f"## session {new.id}" in text
    assert text.index(f"## session {old.id}") < text.index(f"## session {new.id}")


def test_the_summary_is_off_at_zero_like_the_compact_is(tmp_path):
    """`summary_at_percent = 0` is how a round turns it down: no ask on a dirty
    tree at the same fill, the compact still goes ahead and nothing is written."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(BOTH_FILL)},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario,
                                       _config(tmp_path, summary_at_percent=0))
    tr._assert_ready(run, sb.ws("agent-a"))

    assert not _summary_asks(fake)
    assert run.summaries == 0 and run.summary == ""
    assert run.compactions == 1
    assert not _summary_path(sb).exists()
    for turn in run.turns:
        assert "summary_attempted" not in turn


def test_the_default_sits_above_the_compact_percent_and_the_ini_agrees(tmp_path):
    """The shipped default is 90, above the compact's 80, and the committed
    `contest.ini` says the same — the two once read 90 and 70, and it was the
    lower of them that would have been the one to trip on. So the ask only fires
    of a session the compact would take anyway, and it goes out first of the two:
    at 82.4 % the tree gets KC-10's plain compact and is never asked."""
    assert tr.ContestConfig(agents=()).summary_at_percent == 90.0
    committed = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    committed.read(REPO_ROOT / "contest.ini", encoding="utf-8")
    assert committed.getfloat("contest", "summary_at_percent") == 90.0
    assert committed.getfloat("contest", "compact_at_percent") == 80.0

    # 91.6 % crosses both: the ask goes out first, the compact right after it
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(DEFAULT_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ], "summary_tokens": 4_000}
    sb, fake, _h, run, _ = tr._run_one(tmp_path / "asked", scenario,
                                       _config(tmp_path / "asked",
                                               summary_at_percent=90))
    tr._assert_ready(run, sb.ws("agent-a"))
    assert len(_summary_asks(fake)) == 1 and run.compactions == 1
    (turn,) = [t for t in run.turns if t.get("summary_attempted")]
    assert turn["summary_at"] == 90.0 and turn["summary_fill"] == 91.6
    assert _summary_path(sb).exists()

    # 82.4 % is at the compact's percent only: the plain compact, nothing asked
    sb, fake, _h, run, _ = tr._run_one(
        tmp_path / "compacted", tr._rework_scenario(BOTH_FILL),
        _config(tmp_path / "compacted", summary_at_percent=90))
    tr._assert_ready(run, sb.ws("agent-a"))
    assert not _summary_asks(fake)
    assert run.summaries == 0 and run.compactions == 1
    assert not _summary_path(sb).exists()


def test_a_session_past_its_own_size_earns_no_ask(tmp_path):
    """122 % of a known size: the session can no longer take a prompt, so there
    is no summary to ask for — the ask would overflow before it answered, and
    would hand the turn to a fresh session that has just as little. KC-69's
    compact is the only edge left, and it runs alone."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(OVER_FILL)},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ], "summary_tokens": 4_000}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario,
                                       _config(tmp_path, summary_at_percent=70))
    tr._assert_ready(run, sb.ws("agent-a"))

    assert not _summary_asks(fake)
    assert run.summaries == 0 and run.compactions == 1
    assert not _summary_path(sb).exists()
    for turn in run.turns:
        assert "summary_attempted" not in turn


def _post_paths(fake):
    return [record["path"] for record in fake.requests if record["method"] == "POST"]


def _asked_permission():
    """A tool ask the model makes during a turn — refused at a full context."""
    return tr._permission_turn(tr.work_edit_no_commit, ["/var/lib/*"])["permission"]


def test_a_tool_asked_during_the_summary_does_not_stop_the_prompt_after_it(tmp_path):
    """The ask goes out past the compact's percent, so a tool the model tries in
    its reply is refused for a full context (KC-69), and that refusal arms the
    stop meant for a working turn. The reply ends first; the stop is undone with
    it, so neither the compact nor the continue after it is aborted — and the
    refusal is not carried into the next turn's gate as a second compact."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(DEFAULT_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"],
         "permission": _asked_permission()},
        # the continue takes longer than the stop's delay, so a stop left armed
        # lands in it
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"], "delay": 2.0},
    ], "summary_tokens": 3_000}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario,
                                       _config(tmp_path, summary_at_percent=90))
    tr._assert_ready(run, sb.ws("agent-a"))

    (replied,) = fake.events_of("permission.replied")
    assert replied["properties"]["reply"] == "reject"
    assert not [p for p in _post_paths(fake) if p.endswith("/abort")]
    assert run.summary == REPLY_ONE and run.compactions == 1
    assert not any(turn.get("context_refused") for turn in run.turns)


class _FullSeedFake(tr._BenchFake):
    """Every session opens already holding a reply at 91.6 % of the fallback
    window, so a tool asked in the first turn is asked in a full context."""

    SEED = {"info": {"role": "assistant", "finish": "tool-calls",
                     "tokens": {"input": DEFAULT_FILL, "output": 0, "reasoning": 0,
                                "cache": {"read": 0, "write": 0}}},
            "parts": [{"type": "text", "text": "read the whole tree"}]}

    def _create_session(self, body, directory):
        session = super()._create_session(body, directory)
        self._sessions[session["id"]].messages.append(dict(self.SEED))
        return session


def test_a_compact_forced_by_a_full_context_is_asked_for_the_summary_first(tmp_path):
    """Live, most sessions reach their compact this way (round 79: every one of
    sensenova-6-8-flash-lite-var2's): a tool ask refused for a full context stops
    the turn, and the next prompt is compacted whatever the read. The diff is just
    as unexplained there, so the summary is asked before that compact too."""
    scenario = {"turns": [
        dict(tr._permission_turn(tr.work_edit_no_commit, ["/var/lib/*"]),
             delay=3.0, message_info=tr._fill_tokens(DEFAULT_FILL)),
        {"assistant": REPLY_ONE, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(DEFAULT_FILL + 200)},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ], "summary_tokens": 3_000}
    sb = tr.Sandbox(tmp_path)
    with _FullSeedFake(scenario) as fake:
        run = tr.Harness(sb, fake, _config(tmp_path, summary_at_percent=90)).go()
    tr._assert_ready(run, sb.ws("agent-a"))

    (turn,) = [t for t in run.turns if t.get("context_refused")]
    assert turn.get("summary_attempted") is True and turn["summary_captured"] is True
    paths = _post_paths(fake)
    asks = [i for i, record in enumerate(r for r in fake.requests if r["method"] == "POST")
            if SUMMARY_PROMPT in _prompt_text(record)]
    assert len(asks) == 1
    assert asks[0] < paths.index(f"/session/{run.session_id}/summarize")
    assert REPLY_ONE in _summary_path(sb).read_text(encoding="utf-8")


def test_a_failed_summary_with_no_session_to_spare_is_compacted_in_place(tmp_path):
    """KC-39's ceiling at 1: the attempt has no session to fall back on, so an
    empty reply keeps the session and the compact runs as it did before KC-40 —
    no abort, no second `POST /session`."""
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(DEFAULT_FILL)},
        {"assistant": "", "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(DEFAULT_FILL + 200)},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ], "summary_tokens": 3_000}
    sb, fake, _h, run, _ = tr._run_one(
        tmp_path, scenario,
        _config(tmp_path, summary_at_percent=90, max_sessions_per_attempt=1))
    tr._assert_ready(run, sb.ws("agent-a"))

    assert len(_summary_asks(fake)) == 1
    assert len(fake.sessions()) == 1
    assert not [p for p in _post_paths(fake) if p.endswith("/abort")]
    assert run.compactions == 1 and run.summary == ""
    (turn,) = [t for t in run.turns if t.get("summary_attempted")]
    assert turn["summary_captured"] is False and turn["compacted"] is True


def test_a_reset_carries_the_summary_below_its_own_lines(tmp_path):
    """The captured summary goes to the next session as a note of its own, after
    a blank line — not run on into the last line of the round prompt."""
    s1 = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(DEFAULT_FILL)},
        {"assistant": REPLY_ONE, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"]},
        {"on_prompt": tr.work_edit_same, "events": ["busy", "idle"]},
    ], "summary_tokens": 3_000}
    s2 = {"turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"]}]}
    sb = tr.Sandbox(tmp_path)
    with tr._ScriptedFake([s1, s2]) as fake:
        run = tr.Harness(sb, fake, _config(tmp_path, summary_at_percent=90,
                                           max_continues_per_attempt=2)).go()
    tr._assert_ready(run, sb.ws("agent-a"))

    (_old, new) = fake.sessions()
    opening = next(_prompt_text(r) for r in _prompt_requests(fake)
                   if r["path"] == f"/session/{new.id}/prompt_async")
    note = SUMMARY_FELL_BACK_NOTE.format(note=REPLY_ONE)
    assert "\n\n" + note in opening
