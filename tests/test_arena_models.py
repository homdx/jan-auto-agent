"""tests/test_arena_models.py — AR-59: `arena model available|use|drop` over a faked Kilo list."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tools.arena import cli, models

TOOLS = {"toolcall": True, "input": {"text": True}, "output": {"text": True}}


def _meta(price: float, ctx: int = 1000) -> dict:
    return {"cost": {"input": price, "output": price}, "capabilities": TOOLS,
            "limit": {"context": ctx}}


LISTING = {
    "prov-a": {"x:free": _meta(0), "y-free": _meta(0), "z": _meta(0), "w": _meta(1),
               "hy3:free": _meta(0), "devstral-2:free": _meta(0)},
    "prov-b": {"b-free": _meta(0, 2000), "a:free": _meta(0)},
}

INI = """[contest]
out_dir = contest-out

[contest_gate_llm]
model = prov-b/b-free
"""


@pytest.fixture
def kilo(monkeypatch):
    """`models.KILO_LIST` faked; records each call's providers."""
    calls: list = []

    def fake(repo, providers):
        calls.append(list(providers))
        return {p: LISTING[p] for p in (providers or LISTING)}
    monkeypatch.setattr(models, "KILO_LIST", fake)
    return calls


@pytest.fixture
def repo(tmp_path, monkeypatch, kilo) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    return repo


def _rows(capsys) -> list[list[str]]:
    return [line.split() for line in capsys.readouterr().out.splitlines()[1:]]


# 1
def test_available_lists_all_and_free_keeps_free_and_maybe(repo, capsys):
    assert cli.main(["model", "available"]) == 0
    assert len(_rows(capsys)) == 8
    assert cli.main(["model", "available", "prov-a", "prov-b", "--free"]) == 0
    rows = {r[0]: r for r in _rows(capsys)}
    assert rows["x:free"][2] == "yes" and rows["y-free"][2] == "yes"
    assert rows["z"][2] == "maybe"
    assert "w" not in rows
    assert rows["b-free"][1] == "prov-b"


# 2
def test_search_filters_and_nothing_found_exits_3(repo, capsys):
    assert cli.main(["model", "available", "--search", "HY3"]) == 0
    assert [r[0] for r in _rows(capsys)] == ["hy3:free"]
    assert cli.main(["model", "available", "--search", "nothing-here"]) == 3
    assert capsys.readouterr().err.count("\n") == 1


# 3
def test_use_writes_the_local_ini_only(repo):
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    before = (repo / "contest.ini").read_bytes()
    assert cli.main(["model", "use", "a:free,a:free,x:free", "-p", "p1", "-y"]) == 0
    assert "models = a:free,a:free,x:free" in (repo / "contest.local.ini").read_text()
    assert (repo / "contest.ini").read_bytes() == before


# 4
@pytest.mark.parametrize("yes", [[], ["-y"]])
def test_a_missing_suffix_is_refused_with_the_hint(repo, capsys, yes):
    assert cli.main(["model", "use", "hy3", "-p", "p1", *yes]) == 2
    assert capsys.readouterr().err.strip() == \
        "arena: 'hy3' is not a model — did you mean: hy3:free"
    assert not (repo / "contest.local.ini").exists()


# 5
def test_a_typo_gets_the_close_name_and_nothing_close_the_search_hint(repo, capsys):
    assert cli.main(["model", "use", "devstral2", "-y"]) == 2
    assert "did you mean: devstral-2:free" in capsys.readouterr().err
    assert cli.main(["model", "use", "qqqqqq", "-y"]) == 2
    assert capsys.readouterr().err.strip() == \
        "arena: 'qqqqqq' is not a model (arena model available --search qqqqqq)"


# 6
def test_one_bad_name_writes_nothing(repo):
    assert cli.main(["model", "use", "x:free,nope,a:free", "-y"]) == 2
    assert not (repo / "contest.local.ini").exists()


# 7
def test_a_judge_model_is_refused(repo, capsys):
    (repo / "contest.ini").write_text(INI, encoding="utf-8")
    assert cli.main(["model", "use", "x:free,b-free", "-y"]) == 2
    assert "judge" in capsys.readouterr().err
    assert not (repo / "contest.local.ini").exists()


# 8
def test_drop_removes_every_copy_and_refuses_bad_drops(repo, capsys):
    assert cli.main(["model", "use", "x:free,a:free,x:free", "-y"]) == 0
    assert cli.main(["model", "drop", "x:free", "-y"]) == 0
    assert "models = a:free\n" in (repo / "contest.local.ini").read_text()
    assert cli.main(["model", "drop", "hy3", "-y"]) == 2
    assert "did you mean: hy3:free" in capsys.readouterr().err
    assert cli.main(["model", "drop", "z", "-y"]) == 2
    assert "not in profile 'default'" in capsys.readouterr().err
    assert cli.main(["model", "drop", "a:free", "-y"]) == 2
    assert capsys.readouterr().err.strip() == \
        "arena: profile 'default' would be empty — 1 model must stay"
    assert "models = a:free\n" in (repo / "contest.local.ini").read_text()


def test_drop_without_any_profile_is_refused(repo, capsys):
    assert cli.main(["model", "drop", "x:free", "-y"]) == 2
    assert "no profile 'default'" in capsys.readouterr().err


def test_use_asks_kilo_only_for_a_name_the_cache_lacks(repo, kilo):
    assert cli.main(["model", "available"]) == 0
    calls = len(kilo)
    assert cli.main(["model", "use", "x:free", "-y"]) == 0
    assert len(kilo) == calls


# 9
def test_cache_ages_out_on_read_and_write_and_fails_open(repo):
    now = time.time()
    old = {"provider": "p", "model": "old", "free": "no", "ctx": 0, "at": now - 8 * 86400}
    new = {"provider": "p", "model": "new", "free": "no", "ctx": 0, "at": now}
    path = repo / models.CACHE_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([old, new]), encoding="utf-8")
    assert [r["model"] for r in models.load_cache(repo)] == ["new"]
    models.save_cache(repo, [old, new])
    assert [r["model"] for r in json.loads(path.read_text())] == ["new"]
    assert [p.name for p in path.parent.iterdir()] == ["models-cache.json"]
    path.write_text("{broken", encoding="utf-8")
    assert models.load_cache(repo) == []
    path.write_text('{"a": 1}', encoding="utf-8")
    assert models.load_cache(repo) == []


def test_zero_cache_days_is_no_cache(repo):
    (repo / "contest.local.ini").write_text("[arena]\nmodel_cache_days = 0\n")
    models.save_cache(repo, [{"provider": "p", "model": "m", "at": time.time()}])
    assert not (repo / models.CACHE_FILE).exists()


# 10
def test_kilo_failing_is_one_refusal_line(repo, monkeypatch, capsys):
    def broken(repo, providers):
        raise models.KiloError("kilo models prov-a: exit 1")
    monkeypatch.setattr(models, "KILO_LIST", broken)
    assert cli.main(["model", "available"]) == 2
    assert capsys.readouterr().err == "arena: kilo models prov-a: exit 1\n"


def test_a_missing_kilo_binary_is_a_kilo_error(tmp_path, monkeypatch):
    monkeypatch.setattr(models.py_model_test.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(models.KiloError):
        models.kilo_bin(tmp_path)


# 11
def test_json_output_parses(repo, capsys):
    assert cli.main(["-o", "json", "model", "available", "prov-b"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["NAME"] for r in rows} == {"a:free", "b-free"}
    assert rows[0]["LAST-TEST"] == ""


# 12
def test_no_ini_at_all(repo, capsys):
    assert cli.main(["model", "available"]) == 0
    assert cli.main(["model", "available", "--free"]) == 0
    assert capsys.readouterr().err == ""
    assert cli.main(["model", "use", "a:free", "-p", "p1", "-y"]) == 0
    assert (repo / "contest.local.ini").read_text() == "[arena.profile.p1]\nmodels = a:free\n"
    assert not (repo / "contest.ini").exists()


def test_in_profile_names_the_profile(repo, capsys):
    assert cli.main(["model", "use", "a:free", "-p", "p1", "-y"]) == 0
    capsys.readouterr()
    assert cli.main(["-o", "json", "model", "available", "prov-b"]) == 0
    rows = {r["NAME"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["a:free"]["IN-PROFILE"] == "p1" and rows["b-free"]["IN-PROFILE"] == ""
