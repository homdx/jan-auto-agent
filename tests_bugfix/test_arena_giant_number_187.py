"""tests_bugfix/test_arena_giant_number_187.py — bug 187: a run or ticket number past the interpreter's int limit.

Bug: CPython 3.11+ refuses `int()` of a string over 4300 digits with a ValueError.
`arena run view`, `arena run rerun` and `arena issue view` matched the argument
with `\\d+` and then called `int()` on it outside any `try`, so a number that long
(a pasted blob, a fat-fingered key repeat) ended in a traceback instead of the
one-line refusal every other bad argument gets. The run and ticket numbers are
now at most 18 digits.
"""

from __future__ import annotations

import pytest

from tools.arena import cli, rounds

INI = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"
GIANT = "9" * 5000


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "contest.ini").write_text(INI, encoding="utf-8")
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return tmp_path


@pytest.mark.parametrize("argv,needle", [
    (["run", "view", GIANT], "not NN or NN.K"),
    (["run", "view", f"1.{GIANT}"], "not NN or NN.K"),
    (["run", "rerun", GIANT, "--failed"], "not NN or NN.K"),
    (["issue", "view", GIANT], "not a ticket number"),
])
def test_a_giant_number_is_a_refusal_not_a_traceback(repo, capsys, argv, needle):
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert needle in err and len(err.strip().splitlines()) == 1
    assert len(err) < 300, "the refusal must not echo the whole argument"
