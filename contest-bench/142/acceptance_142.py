"""Judge's acceptance suite for round 142 (AR-61 + AR-62), written from the ticket alone.

`arena profile set NAME KEY=VALUE [-y]` through `tools.arena.cli.main` on a
throw-away repo (`cli.REPO_ROOT` patched), and the run line through
`rounds.build_run_line` / `profile view`. Nothing starts a round or a kilo.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/142/acceptance_142.py -n 8 -q
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402
from tools.arena import profile  # noqa: E402
from tools.arena import rounds  # noqa: E402

LOCAL = "contest.local.ini"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "contest.ini").write_text("[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = p/m-a\n")
    for mod in (cli, rounds):
        if isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", r)
    monkeypatch.chdir(tmp_path)  # cwd must not matter
    return r


def run(capsys, *argv):
    try:
        rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code
    c = capsys.readouterr()
    return rc, c.out, c.err


def view_flags(capsys, name: str) -> list[str]:
    rc, out, err = run(capsys, "-o", "json", "profile", "view", name)
    assert rc == 0, err
    rows = {r["SETTING"]: r["VALUE"] for r in json.loads(out)}
    return shlex.split(rows["flags"])


def line(prof: dict, passthrough: list[str]) -> list[str]:
    argv = rounds.build_run_line(5, prof, passthrough)
    i = argv.index("--base")
    return argv[i + 2:]  # everything after `--base REF`


def values(words: list[str], flag: str) -> list[str]:
    """Every value *flag* gets in *words*, `--flag V` and `--flag=V` alike."""
    got = []
    for i, w in enumerate(words):
        if w == flag:
            got.append(words[i + 1] if i + 1 < len(words) else "")
        elif w.startswith(flag + "="):
            got.append(w.split("=", 1)[1])
    return got


def count(words: list[str], flag: str) -> int:
    return sum(1 for w in words if w == flag or w.startswith(flag + "="))


# 1
def test_set_creates_file_and_section(repo, capsys):
    assert not (repo / LOCAL).exists()
    rc, out, err = run(capsys, "profile", "set", "p", "max_parallel=8", "-y")
    assert rc == 0, err
    text = (repo / LOCAL).read_text()
    assert "[arena.profile.p]" in text
    assert values(view_flags(capsys, "p"), "--max-parallel") == ["8"]
    assert "--max-parallel" in out


def test_set_adds_section_to_existing_file(repo, capsys):
    (repo / LOCAL).write_text("[contest]\nlegs = 1\n")
    rc, _, err = run(capsys, "profile", "set", "q", "variant=high", "-y")
    assert rc == 0, err
    text = (repo / LOCAL).read_text()
    assert text.startswith("[contest]\nlegs = 1\n")
    assert values(view_flags(capsys, "q"), "--variant") == ["high"]


# 2
ORIG = (
    "# operator notes — keep me\n"
    "[contest.provider.x]\n"
    "api_key = sk-SECRET-123456   ; inline\n"
    "base_url = http://h/v1\n"
    "\n"
    "; a comment between sections\n"
    "[arena.profile.p]\n"
    "models = a/b,a/b,c/d\n"
    "# comment inside the profile\n"
    "legs = 2\n"
    "\n"
    "[arena.profile.other]\n"
    "max_parallel = 3\n"
)


def test_other_lines_byte_for_byte(repo, capsys):
    (repo / LOCAL).write_text(ORIG)
    rc, out, err = run(capsys, "profile", "set", "p", "max_parallel=8", "variant=high", "-y")
    assert rc == 0, err
    new = (repo / LOCAL).read_text()
    old_lines = ORIG.splitlines()
    new_lines = new.splitlines()
    # every original line is still there, in order
    it = iter(new_lines)
    assert all(any(l == n for n in it) for l in old_lines), new
    added = [l for l in new_lines if l not in old_lines]
    assert sorted(l.replace(" ", "") for l in added) == ["max_parallel=8", "variant=high"]
    # the added lines are inside [arena.profile.p], before [arena.profile.other]
    other = new_lines.index("[arena.profile.other]")
    psec = new_lines.index("[arena.profile.p]")
    assert all(psec < new_lines.index(l) < other for l in added)
    assert "sk-SECRET-123456" not in out + err
    f = view_flags(capsys, "p")
    assert values(f, "--max-parallel") == ["8"] and values(f, "--variant") == ["high"]
    assert values(view_flags(capsys, "other"), "--max-parallel") == ["3"]


def test_change_existing_key_in_place(repo, capsys):
    (repo / LOCAL).write_text(ORIG)
    rc, _, err = run(capsys, "profile", "set", "p", "legs=3", "-y")
    assert rc == 0, err
    new = (repo / LOCAL).read_text()
    assert new.replace("legs = 3", "legs = 2").replace("legs=3", "legs = 2") == ORIG


def test_model_use_still_works(repo, capsys):
    (repo / LOCAL).write_text(ORIG)
    run(capsys, "profile", "set", "p", "max_parallel=8", "-y")
    assert "models = a/b,a/b,c/d" in (repo / LOCAL).read_text()


# 3
def test_without_y_writes_nothing(repo, capsys):
    (repo / LOCAL).write_text(ORIG)
    rc, out, err = run(capsys, "profile", "set", "p", "max_parallel=8")
    assert rc == 0, err
    assert (repo / LOCAL).read_text() == ORIG
    assert "--max-parallel 8" in out or "--max-parallel=8" in out


def test_without_y_on_missing_file_creates_nothing(repo, capsys):
    rc, _, err = run(capsys, "profile", "set", "p", "max_parallel=8")
    assert rc == 0, err
    assert not (repo / LOCAL).exists()


# 4
@pytest.mark.parametrize("v", ["yes", "true", "1"])
def test_fresh_yes(repo, capsys, v):
    rc, _, err = run(capsys, "profile", "set", "p", f"fresh={v}", "-y")
    assert rc == 0, err
    f = view_flags(capsys, "p")
    assert f.count("--fresh") == 1
    i = f.index("--fresh")
    assert i + 1 == len(f) or f[i + 1].startswith("--")  # a switch, no value


@pytest.mark.parametrize("v", ["no", "false", "0"])
def test_fresh_no(repo, capsys, v):
    rc, _, err = run(capsys, "profile", "set", "p", "max_parallel=2", f"fresh={v}", "-y")
    assert rc == 0, err
    assert "--fresh" not in view_flags(capsys, "p")


def test_fresh_maybe_refused(repo, capsys):
    rc, _, err = run(capsys, "profile", "set", "p", "fresh=maybe", "-y")
    assert rc == 2 and len(err.strip().splitlines()) == 1
    assert not (repo / LOCAL).exists() or "maybe" not in (repo / LOCAL).read_text()


def test_fresh_bad_value_refused_on_read(repo, capsys):
    (repo / LOCAL).write_text("[arena.profile.p]\nfresh = maybe\n")
    rc, _, err = run(capsys, "profile", "view", "p")
    assert rc == 2 and len(err.strip().splitlines()) == 1
    assert "Traceback" not in err


def test_fresh_is_a_known_key():
    assert "fresh" in profile.KNOWN_KEYS


# 5
@pytest.mark.parametrize("word", ["models=x", "nosuch=1", "max_parallel=0", "max_parallel=abc",
                                  "max_parallel=-2", "base=x", "ticket=3", "max_parallel"])
def test_refusals(repo, capsys, word):
    (repo / LOCAL).write_text(ORIG)
    rc, out, err = run(capsys, "profile", "set", "p", word, "-y")
    assert rc == 2, (word, out, err)
    assert len(err.strip().splitlines()) == 1 and "Traceback" not in err
    assert (repo / LOCAL).read_text() == ORIG


def test_one_bad_word_writes_nothing(repo, capsys):
    (repo / LOCAL).write_text(ORIG)
    rc, _, _ = run(capsys, "profile", "set", "p", "variant=high", "nosuch=1", "-y")
    assert rc == 2
    assert (repo / LOCAL).read_text() == ORIG


# 6
def test_empty_value_removes_line(repo, capsys):
    (repo / LOCAL).write_text(ORIG)
    rc, _, err = run(capsys, "profile", "set", "p", "legs=", "-y")
    assert rc == 0, err
    new = (repo / LOCAL).read_text()
    assert new == ORIG.replace("legs = 2\n", "")
    assert "--legs" not in view_flags(capsys, "p")


# 7
def test_passthrough_wins_over_key():
    words = line({"max_parallel": "3"}, ["--max-parallel", "8"])
    assert count(words, "--max-parallel") == 1 and values(words, "--max-parallel") == ["8"]


def test_equals_form_is_same_flag():
    words = line({"extra": "--max-parallel 8"}, ["--max-parallel=8"])
    assert count(words, "--max-parallel") == 1 and values(words, "--max-parallel") == ["8"]


def test_equals_in_profile_space_in_passthrough():
    words = line({"max_parallel": "3", "extra": "--variant=low"}, ["--variant", "high"])
    assert values(words, "--variant") == ["high"] and values(words, "--max-parallel") == ["3"]


def test_extra_wins_over_key():
    words = line({"max_parallel": "3", "extra": "--max-parallel 5"}, [])
    assert values(words, "--max-parallel") == ["5"]


@pytest.mark.parametrize("flag", ["--max-parallel", "--variant", "--legs", "--backend",
                                  "--provider", "--models"])
def test_every_value_flag_once(flag):
    key = flag[2:].replace("-", "_")
    words = line({key: "1", "extra": f"{flag} 2"}, [f"{flag}=3"])
    assert count(words, flag) == 1 and values(words, flag) == ["3"]


# 8
def test_fresh_once():
    words = line({"fresh": "yes", "extra": "--fresh"}, ["--fresh"])
    assert words.count("--fresh") == 1


# 9
def test_unknown_flags_pass_in_order():
    words = line({"max_parallel": "3", "extra": "--no-gate --zz 1"}, ["--yy", "--max-parallel", "8"])
    assert values(words, "--max-parallel") == ["8"]
    rest = [w for w in words if w not in ("--max-parallel", "8")]
    assert rest == ["--no-gate", "--zz", "1", "--yy"]


def test_view_and_run_line_agree(repo, capsys):
    (repo / LOCAL).write_text("[arena.profile.p]\nmax_parallel = 3\nfresh = yes\n"
                              "extra = --max-parallel 6 --fresh --no-gate\n")
    vf = view_flags(capsys, "p")
    profiles, _ = profile.load_profiles(repo)
    assert line(profiles["p"], []) == vf
    assert values(vf, "--max-parallel") == ["6"] and vf.count("--fresh") == 1


# 10
@pytest.mark.parametrize("pt", [["--base", "X"], ["--ticket=5"]])
def test_base_ticket_after_dash_refused(pt):
    with pytest.raises(rounds.RoundError):
        rounds.build_run_line(5, {}, pt)


def test_base_in_extra_refused():
    with pytest.raises(profile.ProfileError):
        profile.profile_flags({"extra": "--base X"})


def test_set_never_starts_a_process(repo, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("profile set started a process")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    rc, _, err = run(capsys, "profile", "set", "p", "max_parallel=4", "-y")
    assert rc == 0, err
