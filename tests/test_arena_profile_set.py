"""AR-61/AR-62: `arena profile set NAME KEY=VALUE` and one runner flag reaching the runner once."""

from __future__ import annotations

import pytest

from tools.arena import cli, rounds
from tools.arena.profile import ProfileError, load_profiles, profile_flags

LOCAL = "contest.local.ini"

KEEP = (
    "; local settings — keep this comment\n"
    "[contest_gate_llm]\n"
    "api_key = ${GATE_KEY}  # never touched\n"
    "\n"
    "[arena.profile.p]\n"
    "# the round's models\n"
    "models = prov/model-a,prov/model-b\n"
    "legs = 2\n"
    "\n"
    "[arena]\n"
    "profile = p\n"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    return tmp_path


def local(repo):
    return (repo / LOCAL).read_text(encoding="utf-8")


def view_flags(repo, capsys, name="p"):
    capsys.readouterr()
    assert cli.main(["-o", "json", "profile", "view", name]) == 0
    import json
    rows = json.loads(capsys.readouterr().out)
    return next(r["VALUE"] for r in rows if r["SETTING"] == "flags")


def run_line(prof, passthrough):
    return rounds.build_run_line(142, prof, passthrough)[8:]


def test_set_creates_missing_file_and_section(repo, capsys):
    assert cli.main(["profile", "set", "p", "max_parallel=8", "-y"]) == 0
    assert "[arena.profile.p]" in local(repo)
    assert view_flags(repo, capsys) == "--max-parallel 8"


def test_set_keeps_every_other_line_byte_for_byte(repo):
    (repo / LOCAL).write_text(KEEP, encoding="utf-8")
    assert cli.main(["profile", "set", "p", "max_parallel=8", "variant=high", "-y"]) == 0
    after = local(repo)
    added = [ln for ln in after.splitlines() if ln not in KEEP.splitlines()]
    assert added == ["max_parallel = 8", "variant = high"]
    assert after.replace("max_parallel = 8\nvariant = high\n", "") == KEEP
    assert load_profiles(repo)[0]["p"]["models"] == "prov/model-a,prov/model-b"


def test_without_yes_nothing_is_written(repo, capsys):
    (repo / LOCAL).write_text(KEEP, encoding="utf-8")
    assert cli.main(["profile", "set", "p", "max_parallel=8"]) == 0
    out = capsys.readouterr().out
    assert "--max-parallel 8" in out and "before:" in out
    assert local(repo) == KEEP
    assert not (repo / ".arena").exists()


def test_missing_file_without_yes_stays_missing(repo):
    assert cli.main(["profile", "set", "p", "legs=2"]) == 0
    assert not (repo / LOCAL).exists()


def test_fresh_key(repo, capsys):
    assert cli.main(["profile", "set", "p", "fresh=yes", "-y"]) == 0
    assert view_flags(repo, capsys) == "--fresh"
    assert cli.main(["profile", "set", "p", "fresh=no", "-y"]) == 0
    assert view_flags(repo, capsys) == ""
    before = local(repo)
    assert cli.main(["profile", "set", "p", "fresh=maybe", "-y"]) == 2
    assert local(repo) == before


def test_bad_fresh_in_the_file_is_refused_on_read(repo):
    (repo / LOCAL).write_text("[arena.profile.p]\nfresh = maybe\n", encoding="utf-8")
    with pytest.raises(ProfileError):
        load_profiles(repo)


@pytest.mark.parametrize("pair", [
    "models=x", "nosuch=1", "max_parallel=0", "max_parallel=abc",
    "base=x", "ticket=1", "max_parallel",
])
def test_refusals_write_nothing(repo, capsys, pair):
    (repo / LOCAL).write_text(KEEP, encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["profile", "set", "p", pair, "-y"]) == 2
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert local(repo) == KEEP


def test_one_bad_pair_refuses_the_whole_set(repo):
    (repo / LOCAL).write_text(KEEP, encoding="utf-8")
    assert cli.main(["profile", "set", "p", "legs=3", "nosuch=1", "-y"]) == 2
    assert local(repo) == KEEP


def test_empty_value_removes_the_line(repo):
    (repo / LOCAL).write_text(KEEP.replace("legs = 2\n", "legs = 2\nmax_parallel = 3\n"),
                              encoding="utf-8")
    assert cli.main(["profile", "set", "p", "max_parallel=", "-y"]) == 0
    assert local(repo) == KEEP


def test_model_use_writer_unchanged():
    from tools.arena import models
    out = models._with_models(KEEP, "p", "prov/model-c")
    assert out == KEEP.replace("prov/model-a,prov/model-b", "prov/model-c")


def test_passthrough_wins_over_the_profile_key():
    line = run_line({"max_parallel": "3"}, ["--max-parallel", "8"])
    assert line == ["--max-parallel", "8"]


def test_passthrough_equals_form_wins_over_extra():
    line = run_line({"extra": "--max-parallel 8"}, ["--max-parallel=8"])
    assert line == ["--max-parallel=8"]
    assert sum(w.startswith("--max-parallel") for w in line) == 1


def test_extra_wins_over_the_key_inside_the_profile():
    assert profile_flags({"variant": "low", "extra": "--variant high"}) == ["--variant", "high"]


def test_fresh_once():
    assert profile_flags({"fresh": "yes", "extra": "--fresh"}) == ["--fresh"]
    assert run_line({"fresh": "yes"}, ["--fresh"]) == ["--fresh"]


def test_unknown_flags_pass_through_in_order():
    line = run_line({"legs": "2", "extra": "--no-gate --x 1"}, ["--y", "--legs", "3"])
    assert line == ["--no-gate", "--x", "1", "--y", "--legs", "3"]


def test_view_and_run_line_agree(repo, capsys):
    (repo / LOCAL).write_text(
        "[arena.profile.p]\nmax_parallel = 3\nextra = --max-parallel 8 --no-gate\n",
        encoding="utf-8")
    prof = load_profiles(repo)[0]["p"]
    assert view_flags(repo, capsys) == "--max-parallel 8 --no-gate"
    assert run_line(prof, []) == ["--max-parallel", "8", "--no-gate"]


def test_base_and_ticket_still_refused():
    with pytest.raises(rounds.RoundError):
        rounds.build_run_line(142, {}, ["--base", "x"])
    with pytest.raises(ProfileError):
        profile_flags({"extra": "--ticket=1"})
