"""A tool refused for a full context during the summary ask never aborts that ask.

The refusal used to arm the KC-69 stop (`CONTEXT_ABORT_DELAY_SEC`, 0.5 s) meant
for a working turn; the cancel came only after the ask's wait, so a summary
reply slower than the delay was aborted mid-reply — under 3x test load
`test_a_tool_asked_during_the_summary_does_not_stop_the_prompt_after_it` saw the
`/abort`. With the delay at zero the race is decided every time: the stop must
not be armed at all while the summary is being asked.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_runner as tr  # noqa: E402
import test_contest_runner_summary as ts  # noqa: E402
from tools.contest import runner  # noqa: E402


def test_a_zero_delay_stop_is_not_armed_by_the_summary_ask(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CONTEXT_ABORT_DELAY_SEC", 0.0)
    scenario = {"turns": [
        {"on_prompt": tr.work_edit_no_commit, "events": ["busy", "idle"],
         "message_info": tr._fill_tokens(ts.DEFAULT_FILL)},
        {"assistant": ts.REPLY_ONE, "events": ["busy", "idle"], "delay": 0.5,
         "permission": ts._asked_permission()},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ], "summary_tokens": 3_000}
    sb, fake, _h, run, _ = tr._run_one(tmp_path, scenario,
                                       ts._config(tmp_path, summary_at_percent=90))
    tr._assert_ready(run, sb.ws("agent-a"))

    (replied,) = fake.events_of("permission.replied")
    assert replied["properties"]["reply"] == "reject"
    assert not [p for p in ts._post_paths(fake) if p.endswith("/abort")]
    assert run.summary == ts.REPLY_ONE and run.compactions == 1
