"""tests/test_gate1_vote_raise_review.py — a Gate 1 voter that raises on every vote is dropped (review batch m, №65).

A raise became an ``{"error": …}`` row with no ``requests``: it reset the voter's misses, so the voter was asked
for every candidate and, under ``vote_accept = all``, every vote had a voter with no votes and decided nothing.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto import gate1_vote as gv
from tools.auto.architect import CandidateTask, CitedLocation
from tools.claimcheck import judge as cc_judge
from tools.claimcheck.anchors import PathRepoView

MODELS = {"a": "kenary/agnes-3", "b": "openai/gpt-5", "c": "meta/llama-4", "d": "zhipu/glm-5", "e": "qwen/qwen-3"}


def _true(prompt: str) -> str:
    """One TRUE row quoting the pack's first code line."""
    m = re.search(r"\[\[([^\]]+)\]\][^\n]*\n\s*\d+\|\s?([^\n]+)", prompt)
    return '[{"id": 1, "verdict": "TRUE", "chunk": "%s", "quote": "%s"}]' % (m.group(1), m.group(2).rstrip())


def _voters(raising: str):
    calls = []

    def ask(ref, items, run, seed, timeout):
        calls.append(ref)
        if ref == raising:
            raise KeyError("api_key")                 # a key gone from the ini mid-run, a parse error
        got = cc_judge.ask_with_packs(items, run, seed, _true, batch=1, code_batch=1)
        return {"model": ref, "run": run, "votes": got["votes"], "accepted": got["accepted"],
                "rejected": got["rejected"]}

    ask.calls = calls
    return ask


def _candidate(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("def load(path):\n    data = open(path).read()\n    return data\n",
                                           encoding="utf-8")
    return CandidateTask("close the file", "the file is opened and never closed", ["pkg/m.py"], "true",
                         CitedLocation(file="pkg/m.py", symbol="load"), "pkg")


def test_a_voter_that_raises_on_every_vote_is_dropped_after_two(tmp_path, caplog):
    """Under `committed` the four others decide every vote; the raising one is asked twice, not five times."""
    ask = _voters(raising="c")
    vote = gv.Gate1Vote(gv.VoteConfig(True, tuple(MODELS), accept="committed", confirm=True,
                                      models=tuple(MODELS.items())), ask_fn=ask)
    cand, view = _candidate(tmp_path), PathRepoView(tmp_path)
    with caplog.at_level(logging.WARNING, logger=gv.logger.name):
        outs = [vote.presence_by_vote(cand, view) for _ in range(5)]
    assert [o.verdict for o in outs] == ["confirmed"] * 5
    assert ask.calls.count("c") == 2 and ask.calls.count("a") == 5
    assert vote._voters == ["a", "b", "d", "e"] and not vote.stopped
    assert "not asked again this run" in caplog.text


def test_under_all_the_vote_stops_instead_of_asking_on(tmp_path):
    """Under `all` a vote with a voter that gave no votes decides nothing: it was five requests a candidate for
    the whole plan, the single reader deciding alone. The voter is dropped after two, and the vote stops (it
    needs every voter): the later candidates cost no request."""
    ask = _voters(raising="c")
    vote = gv.Gate1Vote(gv.VoteConfig(True, tuple(MODELS), accept="all", confirm=True,
                                      models=tuple(MODELS.items())), ask_fn=ask)
    cand, view = _candidate(tmp_path), PathRepoView(tmp_path)
    outs = [vote.presence_by_vote(cand, view) for _ in range(5)]
    assert [o.requests for o in outs] == [5, 5, 0, 0, 0] and all(o.verdict is None for o in outs)
    assert vote.stopped and ask.calls.count("c") == 2
