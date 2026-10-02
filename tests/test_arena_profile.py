"""AR-2: `[arena.profile.NAME]` loading, the run flag line, and `arena profile list|view`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.arena import cli
from tools.arena.profile import ProfileError, load_profiles, profile_flags

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    return tmp_path


def write(repo, committed="", local=None):
    (repo / "contest.ini").write_text(committed, encoding="utf-8")
    if local is not None:
        (repo / "contest.local.ini").write_text(local, encoding="utf-8")


def test_local_key_wins_and_committed_only_key_survives(repo):
    write(
        repo,
        "[arena.profile.default]\nlegs = 1\nbranch = arena\n",
        "[arena.profile.default]\nlegs = 3\n",
    )
    profiles, active = load_profiles(repo)
    assert active == "default"
    assert profiles["default"] == {"legs": "3", "branch": "arena"}


def test_flag_order_is_fixed_then_extra():
    prof = {
        "extra": "--fresh --x 1",
        "variant": "v",
        "provider": "p",
        "backend": "b",
        "max_parallel": "4",
        "legs": "2",
        "models": "a,b",
        "branch": "arena",
        "trailer": "T",
    }
    assert profile_flags(prof) == [
        "--models", "a,b", "--legs", "2", "--max-parallel", "4",
        "--backend", "b", "--provider", "p", "--variant", "v",
        "--fresh", "--x", "1",
    ]


def test_empty_legs_gives_no_flag():
    assert profile_flags({"models": "a", "legs": "  "}) == ["--models", "a"]


def test_override_replaces_profile_value():
    assert profile_flags({"legs": "1"}, {"legs": "3"}) == ["--legs", "3"]


@pytest.mark.parametrize("extra", ["--base X", "--ticket=5"])
def test_extra_base_or_ticket_is_refused(extra):
    with pytest.raises(ProfileError, match="--base/--ticket are set by arena"):
        profile_flags({"extra": extra})


def test_unknown_key_names_section_and_key(repo):
    write(repo, "[arena.profile.default]\ncolour = red\n")
    with pytest.raises(ProfileError) as exc:
        load_profiles(repo)
    assert str(exc.value) == "[arena.profile.default] unknown key 'colour'"


@pytest.mark.parametrize("fmt", ["table", "json"])
def test_view_never_prints_secrets(repo, capsys, fmt):
    write(
        repo,
        "[arena.profile.default]\nmodels = m/a\n"
        "extra = --x api_key=sekrit\ntrailer = token=sekrit2\n",
    )
    assert cli.main(["-o", fmt, "profile", "view"]) == 0
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "sekrit" not in text and "sekrit2" not in text
    if fmt == "json":
        rows = json.loads(captured.out)
        assert rows[-1]["SETTING"] == "flags"


def test_list_without_profiles_exits_three(repo, capsys):
    write(repo, "[contest]\nlegs = 1\n")
    assert cli.main(["profile", "list"]) == 3
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and "[arena.profile.*]" in err[0]


def test_inline_comment_and_percent(repo):
    write(repo, "[arena.profile.default]\nlegs = 2   ; two legs\ntrailer = 100% done\n")
    profiles, _ = load_profiles(repo)
    assert profiles["default"]["legs"] == "2"
    assert profiles["default"]["trailer"] == "100% done"


def test_unknown_dash_p_lists_known_names(repo, capsys):
    write(repo, "[arena.profile.default]\nlegs = 1\n[arena.profile.fast]\nlegs = 2\n")
    assert cli.main(["-p", "nosuch", "profile", "list"]) == 2
    err = capsys.readouterr().err.strip()
    assert err == "arena: unknown profile 'nosuch' (known: default, fast)"


def test_list_counts_models_and_marks_active(repo, capsys):
    write(
        repo,
        "[arena]\nprofile = fast\n"
        "[arena.profile.default]\nlegs = 1\n"
        "[arena.profile.fast]\nmodels = a,a,b,c,c\nbranch = arena\n",
    )
    assert cli.main(["-o", "json", "profile", "list"]) == 0
    rows = {r["NAME"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["fast"]["MODELS"] == "5 agents (3 models)"
    assert rows["fast"]["ACTIVE"] == "*"
    assert rows["default"]["ACTIVE"] == ""
    assert rows["default"]["MODELS"] == ""


# ── From space-bunny-alpha-bynara's entry (round 137) ────────────────────────


def test_base_is_not_a_key(repo):
    write(repo, "[arena.profile.default]\nbase = HEAD\n")
    with pytest.raises(ProfileError, match="base"):
        load_profiles(repo)


@pytest.mark.parametrize(
    "committed",
    [
        '[arena.profile.default]\nextra = --x "abc\n',  # unbalanced quote
        "[arena.profile.default]\nlegs = 1\n[arena.profile.default]\n",  # dup
        "legs = 1\n",  # a key before any section
    ],
)
def test_broken_input_is_a_one_line_refusal_not_a_traceback(repo, capsys, committed):
    write(repo, committed)
    assert cli.main(["profile", "view"]) == 2
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")


def test_view_defaults_to_the_active_profile_and_accepts_a_name(repo, capsys):
    write(repo, "[arena]\nprofile = fast\n"
                "[arena.profile.default]\nmodels = p/a\n"
                "[arena.profile.fast]\nmodels = p/b\nmax_parallel = 4\n")
    assert cli.main(["profile", "view"]) == 0
    assert "p/b" in capsys.readouterr().out
    assert cli.main(["profile", "view", "default"]) == 0
    out = capsys.readouterr().out
    assert "p/a" in out and "max_parallel" not in out
    assert cli.main(["-p", "default", "profile", "view"]) == 0
    assert "p/a" in capsys.readouterr().out


def test_a_profile_error_inside_view_is_refused(repo, capsys):
    write(repo, "[arena.profile.default]\nextra = --base HEAD\n")
    assert cli.main(["profile", "view"]) == 2
    assert capsys.readouterr().err.strip() == (
        "arena: --base/--ticket are set by arena, not by a profile"
    )


def test_arena_never_reads_the_agents_ini(repo):
    (repo / "agents_128k.ini").write_text("[arena.profile.leak]\nlegs = 9\n")
    write(repo, "[arena.profile.default]\nlegs = 1\n")
    assert load_profiles(repo) == ({"default": {"legs": "1"}}, "default")


def test_the_repo_is_this_checkout_and_not_the_working_directory(tmp_path):
    # The launcher runs from wherever the operator is; a contest.ini in the cwd
    # is nobody's profile file.
    assert cli.REPO_ROOT == REPO_ROOT
    (tmp_path / "contest.ini").write_text("[arena.profile.from_cwd]\nlegs = 1\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "arena"), "profile", "list"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert proc.returncode in (0, 3), proc.stderr
    assert "from_cwd" not in proc.stdout + proc.stderr


def test_a_repo_that_is_not_there_degrades_to_no_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path / "gone")
    assert cli.main(["profile", "list"]) == 3
