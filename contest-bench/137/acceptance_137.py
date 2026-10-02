"""Judge's acceptance suite for round 137 (AR-2), written from the ticket alone.

Drives `tools.arena.profile` (KNOWN_KEYS, ProfileError, load_profiles,
profile_flags) and `tools.arena.cli.main` for `profile list|view`. The ticket
leaves the repo seam's name open ("for example cli.REPO_ROOT"), so `_seam`
patches `REPO_ROOT` (or any module-level Path equal to the checkout) in cli and
profile; `test_repo_seam_is_the_checkout_root` checks that seam's real value.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/137/acceptance_137.py -n 8 -q
Red on the base (5c67796): there is no tools/arena/profile.py.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = "5c67796"

from tools.arena import cli  # noqa: E402

profile = pytest.importorskip("tools.arena.profile")


def _write(repo: Path, committed: str | None = None, local: str | None = None) -> Path:
    if committed is not None:
        (repo / "contest.ini").write_text(committed)
    if local is not None:
        (repo / "contest.local.ini").write_text(local)
    return repo


@pytest.fixture
def seam(monkeypatch):
    def apply(repo: Path) -> None:
        hit = False
        for mod in (cli, profile):
            for name, val in list(vars(mod).items()):
                if isinstance(val, Path) and (name == "REPO_ROOT" or val.resolve() == ROOT):
                    monkeypatch.setattr(mod, name, repo)
                    hit = True
        assert hit, "no module-level repo seam in tools.arena.cli"
        monkeypatch.chdir(repo)  # cwd must not matter; set it anyway
    return apply


def test_repo_seam_is_the_checkout_root():
    seams = [v for m in (cli, profile) for k, v in vars(m).items()
             if isinstance(v, Path) and (k == "REPO_ROOT" or v.resolve() == ROOT)]
    assert seams and all(Path(v).resolve() == ROOT for v in seams), seams


def _kv(row: dict) -> tuple[str, str]:
    # Column names are the entry's choice (a "key" column is masked by emit),
    # so read a view row as its first two values.
    vals = list(row.values())
    return str(vals[0]), vals[1]


def _cli(capsys, *argv):
    rc = cli.main(list(argv))
    c = capsys.readouterr()
    return rc, c.out, c.err


# ── KNOWN_KEYS ───────────────────────────────────────────────────────────────

def test_known_keys_registry():
    k = profile.KNOWN_KEYS
    assert k["models"] == "--models" and k["legs"] == "--legs"
    assert k["max_parallel"] == "--max-parallel" and k["backend"] == "--backend"
    assert k["provider"] == "--provider" and k["variant"] == "--variant"
    assert k["branch"] is None and k["trailer"] is None
    assert "extra" in k and "base" not in k and "ticket" not in k


def test_profile_error_is_exception():
    assert issubclass(profile.ProfileError, Exception)


# ── load_profiles ────────────────────────────────────────────────────────────

def test_local_overrides_key_by_key(tmp_path):
    _write(tmp_path,
           "[arena.profile.default]\nlegs = 1\nmax_parallel = 4\n",
           "[arena.profile.default]\nlegs = 3\n")
    profs, active = profile.load_profiles(tmp_path)
    assert profs["default"]["legs"] == "3"
    assert profs["default"]["max_parallel"] == "4"
    assert active == "default"


def test_profile_only_in_local(tmp_path):
    _write(tmp_path, "[arena.profile.a]\nlegs = 1\n", "[arena.profile.b]\nlegs = 2\n")
    profs, _ = profile.load_profiles(tmp_path)
    assert set(profs) == {"a", "b"}


def test_missing_files_are_skipped(tmp_path):
    profs, active = profile.load_profiles(tmp_path)
    assert profs == {} and active == "default"


def test_active_from_arena_section(tmp_path):
    _write(tmp_path, "[arena]\nprofile = fast\n[arena.profile.fast]\nlegs = 1\n")
    assert profile.load_profiles(tmp_path)[1] == "fast"


def test_active_local_wins(tmp_path):
    _write(tmp_path, "[arena]\nprofile = a\n[arena.profile.a]\n[arena.profile.b]\n",
           "[arena]\nprofile = b\n")
    assert profile.load_profiles(tmp_path)[1] == "b"


def test_other_sections_ignored(tmp_path):
    _write(tmp_path, "[contest]\nfoo = bar\n[arena.profilex]\ncolour = 1\n"
                     "[arena.profile.p]\nlegs = 1\n")
    profs, _ = profile.load_profiles(tmp_path)
    assert list(profs) == ["p"]


def test_inline_comment_and_percent(tmp_path):
    _write(tmp_path, "[arena.profile.default]\nlegs = 2   ; two legs\n"
                     "extra = --note 100%done\n")
    profs, _ = profile.load_profiles(tmp_path)
    assert profs["default"]["legs"] == "2"
    assert "100%done" in profs["default"]["extra"]


@pytest.mark.parametrize("key", ["colour", "base", "ticket"])
def test_unknown_key_names_section_and_key(tmp_path, key):
    _write(tmp_path, f"[arena.profile.default]\n{key} = red\n")
    with pytest.raises(profile.ProfileError) as e:
        profile.load_profiles(tmp_path)
    msg = str(e.value)
    assert "[arena.profile.default]" in msg and key in msg


def test_unknown_key_in_local_is_refused(tmp_path):
    _write(tmp_path, "[arena.profile.default]\nlegs = 1\n",
           "[arena.profile.default]\ncolour = red\n")
    with pytest.raises(profile.ProfileError):
        profile.load_profiles(tmp_path)


def test_never_reads_agents_128k(tmp_path):
    (tmp_path / "agents_128k.ini").write_text("[arena.profile.leak]\nlegs = 9\n")
    assert profile.load_profiles(tmp_path)[0] == {}


# ── profile_flags ────────────────────────────────────────────────────────────

FULL = {"extra": "--fresh --x 'a b'", "variant": "high", "provider": "pv",
        "backend": "kilo", "max_parallel": "4", "legs": "2", "models": "m/a,m/b",
        "branch": "arena", "trailer": "T: x"}


def test_flags_fixed_order():
    assert profile.profile_flags(dict(FULL)) == [
        "--models", "m/a,m/b", "--legs", "2", "--max-parallel", "4",
        "--backend", "kilo", "--provider", "pv", "--variant", "high",
        "--fresh", "--x", "a b"]


def test_flags_does_not_mutate():
    p = dict(FULL)
    profile.profile_flags(p, {"legs": "3"})
    assert p == FULL


@pytest.mark.parametrize("v", ["", "   "])
def test_empty_value_gives_nothing(v):
    assert profile.profile_flags({"legs": v, "max_parallel": "4"}) == ["--max-parallel", "4"]


def test_override_replaces():
    assert profile.profile_flags({"legs": "1"}, {"legs": "3"}) == ["--legs", "3"]


def test_override_none_is_noop():
    assert profile.profile_flags({"legs": "1"}, None) == ["--legs", "1"]


def test_branch_trailer_give_nothing():
    assert profile.profile_flags({"branch": "x", "trailer": "y"}) == []


def test_empty_profile():
    assert profile.profile_flags({}) == []


@pytest.mark.parametrize("extra", ["--base X", "--ticket=5", "--base=X", "--fresh --ticket 5"])
def test_extra_base_ticket_refused(extra):
    with pytest.raises(profile.ProfileError) as e:
        profile.profile_flags({"extra": extra})
    assert str(e.value) == "--base/--ticket are set by arena, not by a profile"


def test_extra_base_via_override_refused():
    with pytest.raises(profile.ProfileError):
        profile.profile_flags({}, {"extra": "--base X"})


def test_extra_similar_words_allowed():
    assert profile.profile_flags({"extra": "--baseline --tickets 3"}) == [
        "--baseline", "--tickets", "3"]


# ── CLI: profile list ────────────────────────────────────────────────────────

LIST_INI = ("[arena]\nprofile = fast\n"
            "[arena.profile.fast]\nmodels = a/x,a/x,b/y,c/z,c/z\nlegs = 2\nbranch = arena\n"
            "[arena.profile.default]\nlegs = 1\n")


def test_list_table(tmp_path, seam, capsys):
    seam(_write(tmp_path, LIST_INI))
    rc, out, _ = _cli(capsys, "profile", "list")
    assert rc == 0
    for col in ("NAME", "ACTIVE", "MODELS", "LEGS", "BRANCH"):
        assert col in out
    assert "5 agents (3 models)" in out
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines[1].split()[0] == "default" and lines[2].split()[0] == "fast"
    assert "*" in lines[2] and "*" not in lines[1]


def test_list_json(tmp_path, seam, capsys):
    seam(_write(tmp_path, LIST_INI))
    rc, out, _ = _cli(capsys, "-o", "json", "profile", "list")
    assert rc == 0
    rows = json.loads(out)
    by = {str(r.get("NAME", r.get("name"))): r for r in rows}
    assert set(by) == {"default", "fast"}
    flat = json.dumps(by["fast"])
    assert "5 agents (3 models)" in flat and "*" in flat
    assert "*" not in json.dumps(by["default"])


def test_list_p_marks_named(tmp_path, seam, capsys):
    seam(_write(tmp_path, LIST_INI))
    rc, out, _ = _cli(capsys, "-p", "default", "profile", "list")
    assert rc == 0
    line = [l for l in out.splitlines() if l.startswith("default")][0]
    assert "*" in line
    assert "*" not in [l for l in out.splitlines() if l.startswith("fast")][0]


def test_list_no_profiles_exit3(tmp_path, seam, capsys):
    seam(_write(tmp_path, "[contest]\nx = 1\n"))
    rc, out, err = _cli(capsys, "profile", "list")
    assert rc == 3
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")
    assert "[arena.profile.*]" in err and not out.strip()


def test_list_no_files_exit3(tmp_path, seam, capsys):
    seam(tmp_path)
    assert _cli(capsys, "profile", "list")[0] == 3


def test_list_p_unknown_refused(tmp_path, seam, capsys):
    seam(_write(tmp_path, LIST_INI))
    rc, _, err = _cli(capsys, "-p", "nosuch", "profile", "list")
    assert rc == 2 and len(err.splitlines()) == 1
    assert "unknown profile 'nosuch' (known: default, fast)" in err


def test_list_bad_key_refused(tmp_path, seam, capsys):
    seam(_write(tmp_path, "[arena.profile.default]\ncolour = red\n"))
    rc, _, err = _cli(capsys, "profile", "list")
    assert rc == 2 and len(err.splitlines()) == 1
    assert err.startswith("arena: ") and "colour" in err


# ── CLI: profile view ────────────────────────────────────────────────────────

VIEW_INI = ("[arena]\nprofile = default\n"
            "[arena.profile.default]\nlegs = 1\nmax_parallel = 4\n"
            "[arena.profile.fast]\nlegs = 3\nextra = --fresh\n")


def _flags_line(out: str) -> str:
    for l in out.splitlines():
        if l.split()[:1] == ["flags"]:
            return l
    raise AssertionError(out)


def test_view_active(tmp_path, seam, capsys):
    seam(_write(tmp_path, VIEW_INI))
    rc, out, _ = _cli(capsys, "profile", "view")
    assert rc == 0
    assert _flags_line(out).split(None, 1)[1].strip() == "--legs 1 --max-parallel 4"


def test_view_named(tmp_path, seam, capsys):
    seam(_write(tmp_path, VIEW_INI))
    rc, out, _ = _cli(capsys, "profile", "view", "fast")
    assert rc == 0 and "--legs 3 --fresh" in _flags_line(out)


def test_view_p(tmp_path, seam, capsys):
    seam(_write(tmp_path, VIEW_INI))
    rc, out, _ = _cli(capsys, "-p", "fast", "profile", "view")
    assert rc == 0 and "--fresh" in _flags_line(out)


def test_view_json_rows(tmp_path, seam, capsys):
    seam(_write(tmp_path, VIEW_INI))
    rc, out, _ = _cli(capsys, "-o", "json", "profile", "view", "fast")
    assert rc == 0
    rows = json.loads(out)
    vals = dict(_kv(r) for r in rows)
    assert vals["legs"] == "3" and vals["extra"] == "--fresh"
    assert shlex.split(vals["flags"]) == ["--legs", "3", "--fresh"]
    assert list(vals)[-1] == "flags"


def test_view_unknown_name_refused(tmp_path, seam, capsys):
    seam(_write(tmp_path, VIEW_INI))
    rc, _, err = _cli(capsys, "profile", "view", "nosuch")
    assert rc == 2 and len(err.splitlines()) == 1
    assert "nosuch" in err and "default" in err and "fast" in err


def test_view_p_unknown_refused(tmp_path, seam, capsys):
    seam(_write(tmp_path, VIEW_INI))
    rc, _, err = _cli(capsys, "-p", "x", "profile", "view")
    assert rc == 2 and "unknown profile 'x' (known: default, fast)" in err


def test_view_extra_base_refused(tmp_path, seam, capsys):
    seam(_write(tmp_path, "[arena.profile.default]\nextra = --base X\n"))
    rc, _, err = _cli(capsys, "profile", "view")
    assert rc == 2
    assert err.strip() == "arena: --base/--ticket are set by arena, not by a profile"


@pytest.mark.parametrize("fmt", [[], ["-o", "json"]])
def test_view_never_prints_secrets(tmp_path, seam, capsys, fmt):
    seam(_write(tmp_path, "[arena.profile.default]\nextra = --x api_key=sekrit\n"
                          "trailer = token=sekrit2\n"))
    rc, out, err = _cli(capsys, *fmt, "profile", "view")
    assert rc == 0
    assert "sekrit" not in out + err


def test_view_quotes_flags(tmp_path, seam, capsys):
    seam(_write(tmp_path, "[arena.profile.default]\nextra = --x 'a b'\n"))
    rc, out, _ = _cli(capsys, "-o", "json", "profile", "view")
    rows = json.loads(out)
    f = dict(_kv(r) for r in rows)["flags"]
    assert shlex.split(f) == ["--x", "a b"]


# ── AR-1 untouched, repo untouched ───────────────────────────────────────────

def test_other_verbs_still_unimplemented(capsys):
    rc, _, err = _cli(capsys, "run", "start", "5")
    assert rc == 2 and "AR-3" in err


def test_ar1_tests_unchanged():
    cp = subprocess.run(["git", "diff", "--quiet", BASE, "--", "tests/test_arena_cli.py"],
                        cwd=ROOT)
    assert cp.returncode == 0


def test_no_change_under_tools_contest():
    cp = subprocess.run(["git", "diff", "--name-only", BASE, "--", "tools/contest/"],
                        cwd=ROOT, capture_output=True, text=True)
    assert cp.stdout.strip() == ""


def test_contest_ini_example_all_commented():
    old = subprocess.run(["git", "show", f"{BASE}:contest.ini"], cwd=ROOT,
                         capture_output=True, text=True).stdout
    new = (ROOT / "contest.ini").read_text()
    assert new.startswith(old.rstrip("\n")), "existing sections changed"
    added = new[len(old.rstrip("\n")):].splitlines()
    assert any("arena.profile.default" in l for l in added)
    for l in added:
        assert not l.strip() or l.lstrip().startswith("#"), l


def test_real_checkout_has_no_live_profile_parse_error():
    profile.load_profiles(ROOT)


def test_ticket_tests_exist_and_pass():
    t = ROOT / "tests" / "test_arena_profile.py"
    assert t.exists()
    cp = subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", "0", str(t)],
                        cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert cp.returncode == 0, cp.stdout[-2000:]


# ── one-line refusals for broken input (AR-1 principle, not a traceback) ─────

@pytest.mark.parametrize("ini", [
    "[arena.profile.default]\nextra = --x 'oops\n",   # unbalanced quote
    "[arena.profile.default\nlegs = 1\n",             # broken section header
    "legs = 1\n",                                      # key before any section
])
def test_broken_input_is_one_line_refusal(tmp_path, seam, capsys, ini):
    seam(_write(tmp_path, ini))
    rc, _, err = _cli(capsys, "profile", "view")
    assert rc == 2, err
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")
