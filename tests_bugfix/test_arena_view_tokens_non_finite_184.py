"""tests_bugfix/test_arena_view_tokens_non_finite_184.py — bug 184: `run view -o json` on a state.json holding NaN or Infinity.

Bug: `json.loads` reads the bare words `NaN`, `Infinity` and `-Infinity` (and
`json.dumps` writes them for a float that is not finite), so a `state.json` can
carry them in an agent's `tokens`. `rounds._total_tokens` did `int(value)` on
every float: `int(nan)` is a ValueError and `int(inf)` an OverflowError, so
`arena run view NN -o json` ended in a traceback — the module's own promise is
"a broken input is a refusal or a `?` row, never a traceback". Same family as bug
182 (`human_duration`). A non-finite number counts as nothing.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tools.arena import cli, rounds

INI = "[contest]\nout_dir = contest-out\n\n[contest.agent.a]\nmodel = test/a\n"

NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("tokens,expected", [
    (NAN, 0),
    (INF, 0),
    (-INF, 0),
    ({"input": INF}, 0),
    ({"input": 5, "output": NAN, "reasoning": 2}, 7),     # the finite parts still count
    ({"input": -INF, "output": 4}, 4),
    ({"input": 100, "output": 20, "reasoning": 3}, 123),  # unchanged
    (12.9, 12),
])
def test_total_tokens_ignores_a_value_that_is_not_finite(tokens, expected):
    assert rounds._total_tokens(tokens) == expected


def _state_with_tokens(folder: Path, tokens_json: str) -> None:
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(
        '{"round_no": 1, "agents": [{"agent": {"name": "a"}, "state": "READY", '
        f'"attempt": 1, "commit": "abc", "tokens": {tokens_json}}}]}}', encoding="utf-8")


@pytest.mark.parametrize("tokens_json", [
    "NaN", "Infinity", "-Infinity", '{"input": Infinity, "output": 9}'])
def test_run_view_json_survives_a_non_finite_token_count(tmp_path, monkeypatch, capsys, tokens_json):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    _state_with_tokens(repo / "contest-out" / "01", tokens_json)
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    assert cli.main(["-o", "json", "run", "view", "1"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["agent"] == "a"
    assert isinstance(rows[0]["tokens"], int) and not isinstance(rows[0]["tokens"], bool)
    assert math.isfinite(rows[0]["tokens"])
