"""Judge's acceptance suite for round 140 (AR-59), written from the ticket alone.

Black box: a fake `kilo` binary on PATH answers `kilo models [P] [--pure] [--verbose]`
from a JSON fixture, `HOME` is a tmp folder (no VS Code fallback), and every
command goes through `tools.arena.cli.main` with `REPO_ROOT` patched.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/140/acceptance_140.py -n 8 -q
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402

FREE = {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}
PAID = {"input": 1.5, "output": 3, "cache": {"read": 0.1, "write": 0}}


def meta(cost, ctx=131072):
    return {"cost": cost, "limit": {"context": ctx, "output": 8192},
            "capabilities": {"toolcall": True, "input": {"text": True}, "output": {"text": True}}}


CATALOG = {
    "pa": {"x:free": meta(FREE), "y-free": meta(FREE), "z": meta(FREE), "w": meta(PAID),
           "hy3:free": meta(FREE), "devstral-2:free": meta(FREE), "judge-m:free": meta(FREE)},
    "pb": {"b-free": meta(FREE, 65536), "a:free": meta(FREE), "paid-b": meta(PAID)},
}

FAKE_KILO = r'''#!/usr/bin/env python3
import json, os, sys
cat = json.load(open(os.environ["BENCH_CATALOG"]))
if os.environ.get("BENCH_KILO_FAIL"):
    print("boom: kilo crashed", file=sys.stderr); sys.exit(3)
a = [x for x in sys.argv[1:] if not x.startswith("-")]
if not a or a[0] != "models":
    sys.exit(0)
verbose = "--verbose" in sys.argv
provs = a[1:] or list(cat)
out = []
for p in provs:
    if p not in cat:
        print(f"provider {p} not found", file=sys.stderr); sys.exit(1)
    for m, d in cat[p].items():
        out.append(f"{p}/{m}")
        if verbose:
            out.append(json.dumps(d, indent=2))
print("\n".join(out))
'''


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    k = bindir / "kilo"
    k.write_text(FAKE_KILO)
    k.chmod(k.stat().st_mode | stat.S_IXUSR)
    catf = tmp_path / "catalog.json"
    catf.write_text(json.dumps(CATALOG))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("BENCH_CATALOG", str(catf))
    monkeypatch.delenv("BENCH_KILO_FAIL", raising=False)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)  # cwd must not matter
    for name in ("cli", "models", "profile", "rounds"):
        mod = sys.modules.get(f"tools.arena.{name}")
        if mod is None:
            try:
                mod = __import__(f"tools.arena.{name}", fromlist=["x"])
            except ImportError:
                continue
        if isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", repo)
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    return repo


def roster(repo: Path, extra: str = "") -> None:
    (repo / "contest.ini").write_text(
        "[contest]\nout_dir = out\n" + extra + "\n[contest.agent.a]\nmodel = pa/x:free\n")


def _call(argv):
    try:
        return cli.main(list(argv))
    except SystemExit as e:  # argparse
        return e.code


def _globals_first(argv):
    """`-o X` / `-p X` / `-y` moved before the object: arena's global-flag form."""
    front, rest, i = [], [], 0
    while i < len(argv):
        if argv[i] in ("-o", "-p"):
            front += argv[i:i + 2]
            i += 2
        elif argv[i] == "-y":
            front.append("-y")
            i += 1
        else:
            rest.append(argv[i])
            i += 1
    return front + rest


def run(capsys, *argv):
    """The ticket writes flags after the verb; arena's globals go first. Either is fine."""
    code = _call(argv)
    o = capsys.readouterr()
    if code == 2 and ("unrecognized arguments" in o.err or "invalid choice" in o.err):
        code = _call(_globals_first(list(argv)))
        o = capsys.readouterr()
    return code, o.out, o.err


def names(out: str) -> set[str]:
    return {w for line in out.splitlines() for w in line.split()}


def local(repo):
    p = repo / "contest.local.ini"
    return p.read_text() if p.exists() else ""


# 1
def test_available_lists_all_two_providers(env, capsys):
    roster(env)
    code, out, err = run(capsys, "model", "available", "pa", "pb")
    assert code == 0, err
    for m in ("x:free", "y-free", "z", "w", "b-free", "paid-b"):
        assert any(m in w for w in names(out)), m
    assert "NAME" in out and "FREE" in out


def test_available_no_provider_is_every_provider(env, capsys):
    roster(env)
    code, out, _ = run(capsys, "model", "available")
    assert code == 0
    assert any("paid-b" in w for w in names(out)) and any("y-free" in w for w in names(out))


def test_free_filter_and_maybe(env, capsys):
    roster(env)
    code, out, _ = run(capsys, "model", "available", "pa", "--free")
    assert code == 0
    words = names(out)
    assert any("x:free" in w for w in words) and any("y-free" in w for w in words)
    assert not any(w.endswith("/w") or w == "w" for w in words)
    zline = [l for l in out.splitlines() if l.split() and l.split()[0] in ("z", "pa/z")]
    assert zline and "maybe" in zline[0]


# 2
def test_search_and_nothing_found(env, capsys):
    roster(env)
    code, out, _ = run(capsys, "model", "available", "pa", "--search", "DEVSTRAL")
    assert code == 0 and "devstral-2:free" in out and "hy3" not in out
    code, out, err = run(capsys, "model", "available", "pa", "--search", "nothing-like-it")
    assert code == 3 and len(err.strip().splitlines()) == 1


# 3
def test_use_writes_local_only(env, capsys):
    roster(env)
    before = (env / "contest.ini").read_bytes()
    code, _, err = run(capsys, "model", "use", "x:free,x:free,b-free", "-p", "p1", "-y")
    assert code == 0, err
    txt = local(env)
    assert "[arena.profile.p1]" in txt
    line = [l for l in txt.splitlines() if l.replace(" ", "").startswith("models=")][0]
    vals = [v.strip().split("/")[-1] for v in line.split("=", 1)[1].split(",")]
    assert vals == ["x:free", "x:free", "b-free"]
    assert (env / "contest.ini").read_bytes() == before


# 4
@pytest.mark.parametrize("yes", [[], ["-y"]])
def test_missing_suffix_hint(env, capsys, yes):
    roster(env)
    code, _, err = run(capsys, "model", "use", "hy3", "-p", "p1", *yes)
    assert code == 2
    assert "hy3:free" in err and len(err.strip().splitlines()) == 1
    assert local(env) == ""


# 5
def test_typo_and_nothing_close(env, capsys):
    roster(env)
    code, _, err = run(capsys, "model", "use", "devstral2", "-p", "p1", "-y")
    assert code == 2 and "devstral-2:free" in err
    code, _, err = run(capsys, "model", "use", "qqqqqq", "-p", "p1", "-y")
    assert code == 2 and "--search" in err
    assert local(env) == ""


# 6
def test_one_bad_name_writes_nothing(env, capsys):
    roster(env)
    run(capsys, "model", "use", "x:free", "-p", "p1", "-y")
    before = local(env)
    code, _, _ = run(capsys, "model", "use", "x:free,devstral2,b-free", "-p", "p1", "-y")
    assert code == 2 and local(env) == before


# 7
def test_judge_model_refused(env, capsys):
    roster(env, "gate_llm_profile = contest_gate_llm\n\n[contest_gate_llm]\nmodel = judge-m:free\n")
    code, _, err = run(capsys, "model", "use", "judge-m:free", "-p", "p1", "-y")
    assert code == 2, err
    assert "[arena.profile.p1]" not in local(env)


# 8
def test_drop(env, capsys):
    roster(env)
    run(capsys, "model", "use", "x:free,b-free,x:free", "-p", "p1", "-y")
    code, _, err = run(capsys, "model", "drop", "x:free", "-p", "p1", "-y")
    assert code == 0, err
    assert "x:free" not in local(env) and "b-free" in local(env)
    code, _, err = run(capsys, "model", "drop", "devstral2", "-p", "p1", "-y")
    assert code == 2 and "devstral-2:free" in err
    code, _, err = run(capsys, "model", "drop", "y-free", "-p", "p1", "-y")
    assert code == 2 and "b-free" in err  # names the profile's models
    before = local(env)
    code, _, err = run(capsys, "model", "drop", "b-free", "-p", "p1", "-y")
    assert code == 2 and "empty" in err and local(env) == before


# 9
def _cache(repo):
    return repo / ".arena" / "models-cache.json"


def test_cache_drops_old_on_read_and_write(env, capsys):
    roster(env)
    c = _cache(env)
    c.parent.mkdir(parents=True, exist_ok=True)
    old = time.time() - 8 * 86400
    c.write_text(json.dumps([{"provider": "pz", "model": "ghost:free", "free": "yes",
                              "ctx": 1, "at": old}]))
    code, _, _ = run(capsys, "model", "use", "ghost:free", "-p", "p1", "-y")
    assert code == 2  # the old record is not read as known
    run(capsys, "model", "available", "pa")
    recs = json.loads(c.read_text())
    assert isinstance(recs, list) and recs
    assert not any(r.get("model", "").endswith("ghost:free") for r in recs)
    assert all({"provider", "model", "free", "ctx", "at"} <= set(r) for r in recs)
    assert not [p for p in c.parent.iterdir() if p.name != c.name and "models-cache" in p.name
                or p.suffix == ".tmp"]


def test_broken_cache_is_empty(env, capsys):
    roster(env)
    c = _cache(env)
    c.parent.mkdir(parents=True, exist_ok=True)
    c.write_text("{not json")
    code, out, err = run(capsys, "model", "available", "pa")
    assert code == 0 and "Traceback" not in err
    code, _, err = run(capsys, "model", "use", "x:free", "-p", "p1", "-y")
    assert code == 0, err


def test_use_uses_cache_without_kilo(env, capsys, monkeypatch):
    roster(env)
    run(capsys, "model", "available", "pa", "pb")
    monkeypatch.setenv("BENCH_KILO_FAIL", "1")
    code, _, err = run(capsys, "model", "use", "x:free", "-p", "p1", "-y")
    assert code == 0, err


# 10
def test_kilo_failing(env, capsys, monkeypatch):
    roster(env)
    monkeypatch.setenv("BENCH_KILO_FAIL", "1")
    code, out, err = run(capsys, "model", "available", "pa")
    assert code == 2 and len(err.strip().splitlines()) == 1 and "Traceback" not in err


def test_kilo_missing(env, capsys, monkeypatch, tmp_path):
    roster(env)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    code, _, err = run(capsys, "model", "available", "pa")
    assert code == 2 and len(err.strip().splitlines()) == 1 and "Traceback" not in err


# 11
def test_json(env, capsys):
    roster(env)
    code, out, _ = run(capsys, "model", "available", "pa", "--free", "-o", "json")
    assert code == 0
    data = json.loads(out)
    assert isinstance(data, list) and len(data) >= 3


# 12
def test_no_ini_at_all(env, capsys):
    code, out, err = run(capsys, "model", "available", "pa", "pb")
    assert code == 0 and err.strip() == "", err
    code, out, err = run(capsys, "model", "available", "pa", "--free")
    assert code == 0 and "paid" not in out
    code, _, err = run(capsys, "model", "use", "x:free", "-p", "p1", "-y")
    assert code == 0, err
    lines = [l.strip() for l in local(env).splitlines() if l.strip()]
    assert lines[0] == "[arena.profile.p1]" and len(lines) == 2
    assert lines[1].replace(" ", "") in ("models=x:free", "models=pa/x:free")
    assert not (env / "contest.ini").exists()


def test_no_ini_drop_refused(env, capsys):
    code, _, err = run(capsys, "model", "drop", "x:free", "-p", "p9", "-y")
    assert code == 2 and "p9" in err
