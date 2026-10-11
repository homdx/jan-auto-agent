"""Review of cc-epic-docs: arena model replace/drop take OLD as provider/model, and a relative --target is recorded absolute in votes.json."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.arena import cli, models  # noqa: E402
from tools.claimcheck.target import Target  # noqa: E402

LISTING = {"p1": {m: {"cost": {"input": 0, "output": 0}, "limit": {"context": 1000}, "tool_call": True}
                  for m in ("a:free", "b-free", "hy3:free")}}
ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
       "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(models, "KILO_LIST", lambda repo, providers: LISTING)
    path = tmp_path / "contest.local.ini"
    path.write_text("[arena.profile.p1]\nmodels = a:free@high,b-free,a:free\n")
    return path


def test_replace_takes_old_as_provider_slash_model(local, capsys):
    assert cli.main(["-p", "p1", "model", "replace", "p1/a:free", "hy3:free", "-y"]) == 0, capsys.readouterr().err
    assert local.read_text() == "[arena.profile.p1]\nmodels = hy3:free,b-free,hy3:free\n"


def test_drop_takes_a_name_as_provider_slash_model(local, capsys):
    assert cli.main(["-p", "p1", "model", "drop", "p1/a:free", "-y"]) == 0, capsys.readouterr().err
    assert local.read_text() == "[arena.profile.p1]\nmodels = b-free\n"


@pytest.mark.parametrize("entry,name,same", [
    ("a:free", "p1/a:free", True), ("p1/a:free", "a:free", True), ("p1/a:free@high", "a:free", True),
    ("a:free", "a:free", True), ("ba:free", "a:free", False), ("p1/a:free", "p2/a:free", False)])
def test_same_model(entry, name, same):
    assert models.same_model(entry, name) is same


def test_a_relative_target_is_recorded_absolute(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "a.py").write_text("x = 1\n")
    env = {**os.environ, **ENV}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True, env=env)
    monkeypatch.chdir(tmp_path)
    with Target.open("repo", "HEAD", scratch=tmp_path / "s") as t:
        assert t.repo == str(r)
    monkeypatch.chdir(r)
    with Target.open(".", "HEAD", scratch=tmp_path / "s") as t:
        assert t.repo == str(r)


def test_an_unchanged_replace_asks_kilo_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(models, "KILO_LIST", lambda repo, providers: calls.append(providers) or LISTING)
    monkeypatch.setattr(models, "check_not_judge", lambda repo, names: (_ for _ in ()).throw(
        models.ModelError("is a judge")))
    path = tmp_path / "contest.local.ini"
    path.write_text("[arena.profile.p1]\nmodels = a:free,b-free\n")
    assert cli.main(["-p", "p1", "model", "replace", "a:free", "a:free", "-y"]) == 0
    assert "(unchanged)" in capsys.readouterr().out and calls == []
