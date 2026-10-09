"""tests_bugfix/test_arena_owned_flag_abbrev_172.py — pins ticket 172: an abbreviated owned flag is still owned."""

from __future__ import annotations

import pytest

from tools.arena import profile, rounds


@pytest.mark.parametrize("word,flag", [
    ("--tick", "--ticket"), ("--ou", "--out"), ("--bas=x", "--base"),
    ("--targ", "--target"), ("--ticket=7", "--ticket"),
])
def test_172_run_line_refuses_an_abbreviated_owned_flag(word, flag):
    with pytest.raises(rounds.RoundError, match=f"^{flag} is set by arena"):
        rounds.build_run_line(7, {}, [word, "9"])
    with pytest.raises(rounds.RoundError, match=f"^{flag} is set by arena"):
        rounds.build_rerun_line(7, "arena-round/7", {}, [word, "9"], None)


@pytest.mark.parametrize("word", ["--backend", "--models", "--resume", "--no-gate",
                                  "--max-parallel", "--fresh", "-x", "value"])
def test_172_real_runner_options_pass(word):
    assert rounds._owned(word) is None


@pytest.mark.parametrize("extra", ["--tic 9", "--bas=other", "--ou /tmp/x", "--targ /other"])
def test_172_profile_extra_refuses_an_abbreviation(extra):
    with pytest.raises(profile.ProfileError, match="set by arena"):
        profile.profile_flags({"extra": extra})
    assert profile.profile_flags({"extra": "--backend kilo"}) == ["--backend", "kilo"]


def test_172_the_abbreviation_is_the_runners_own():
    """The premise: `tools.contest run` really takes `--tick` for `--ticket` (allow_abbrev)."""
    from tools.contest import cli as contest_cli

    args = contest_cli._parser().parse_args(["run", "--tick", "9", "--ou", "/tmp/x"])
    assert (args.ticket, args.out) == (9, "/tmp/x")
