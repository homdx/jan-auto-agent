"""tests_bugfix/test_arena_profile_set_comment_183.py — bug 183: `profile set` writes no value the ini reads back cut short.

Bug: roster's parser reads `;` and `#` as an inline comment when whitespace
precedes them, and `write_profile_keys` always writes `key = value` — so a value
with ` ;` or ` #` in it (or one that starts with either) was written whole and read
back truncated. `profile set p 'extra=--note "a ; b"' -y` printed
`flags: --note 'a ; b'`, then left `extra = --note "a ; b"` on disk, which reads as
`--note "a` — an unbalanced quote. From then on `profile view`, `run start`, and
`profile set p extra= -y` (the repair) all refused with `extra: No closing
quotation`: the profile was only fixable by hand-editing the file. The preview
(`before`/`after`) was computed from the dict, not from what the ini would give
back, so it lied too. Now the value is refused up front, preview or `-y`.
"""

from __future__ import annotations

import pytest

from tools.arena import cli
from tools.arena.profile import load_profiles

LOCAL = "contest.local.ini"
BASE = "[arena.profile.p]\nlegs = 2\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    (tmp_path / LOCAL).write_text(BASE, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("value", [
    '--note "a ; b"',   # the reported case: a quote opened before the comment
    "x #y",
    ";z",               # starts with the comment character
    "#z",
    "a\u00a0#b",        # any str.isspace() character counts, a no-break space too
])
def test_a_value_the_ini_would_read_as_a_comment_is_refused(repo, capsys, value):
    code = cli.main(["profile", "set", "p", f"extra={value}", "-y"])
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("arena: extra:") and "comment" in err
    assert len(err.strip().splitlines()) == 1
    assert (repo / LOCAL).read_text(encoding="utf-8") == BASE, "a refused value was written"


def test_the_preview_refuses_it_too(repo, capsys):
    """Without -y the before/after would show a value the file cannot hold."""
    code = cli.main(["profile", "set", "p", "extra=a ; b"])
    cap = capsys.readouterr()
    assert code == 2 and "comment" in cap.err
    assert "not written" not in cap.out


def test_a_refused_set_leaves_the_profile_usable(repo, capsys):
    cli.main(["profile", "set", "p", 'extra=--note "a ; b"', "-y"])
    capsys.readouterr()
    assert cli.main(["profile", "view", "p"]) == 0


@pytest.mark.parametrize("key,value", [
    ("branch", "feature#1"),        # no whitespace before the character: not a comment
    ("extra", "--tag=a;b"),
    ("extra", "--note 'x;y' --other"),
    ("variant", "high"),
])
def test_what_set_writes_is_what_the_profile_loads_back(repo, capsys, key, value):
    assert cli.main(["profile", "set", "p", f"{key}={value}", "-y"]) == 0
    profiles, _ = load_profiles(repo)
    assert profiles["p"][key] == value
