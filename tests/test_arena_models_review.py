"""tests/test_arena_models_review.py — `arena model drop|replace` with a model name and no provider when the
profile holds that model of several providers (three sensenova providers of one model is a supported setup):
every provider's entry changes, as before, but the command says so and asks even with `-y` (it acted on all of
them without a word). A provider-qualified name, one provider's entries and repeats (var1, var2) are as before."""

from __future__ import annotations

import pytest

from tools.arena import cli, models


def meta():
    return {"cost": {"input": 0, "output": 0},
            "capabilities": {"toolcall": True, "input": {"text": True}, "output": {"text": True}},
            "limit": {"context": 4000}}


LISTING = {"s1": {"sense-6:free": meta(), "b-free": meta()}, "s2": {"sense-6:free": meta()},
           "s3": {"sense-6:free": meta(), "c:free": meta()}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(models, "KILO_LIST",
                        lambda repo, providers: {p: m for p, m in LISTING.items() if not providers or p in providers})
    return tmp_path


def _profile(repo, line):
    path = repo / "contest.local.ini"
    path.write_text(f"[arena.profile.p1]\nmodels = {line}\n", encoding="utf-8")
    return path


def _run(argv, capsys):
    code = cli.main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


THREE = "s1/sense-6:free,s2/sense-6:free,s3/sense-6:free,s1/b-free"


@pytest.mark.parametrize("argv, after", [
    (["model", "drop", "sense-6:free", "-y"], "s1/b-free"),
    (["model", "replace", "sense-6:free", "s3/c:free", "-y"], "s3/c:free,s3/c:free,s3/c:free,s1/b-free"),
])
def test_a_bare_name_of_several_providers_changes_them_all_after_a_question_even_with_y(repo, capsys, monkeypatch,
                                                                                         argv, after):
    local = _profile(repo, THREE)
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "n")
    code, out, err = _run(["-p", "p1", *argv], capsys)
    assert code == 2 and asked and "not applied" in err
    assert "is the model of 3 providers here" in out and "asked even with -y" in out
    assert local.read_text(encoding="utf-8") == f"[arena.profile.p1]\nmodels = {THREE}\n"
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert _run(["-p", "p1", *argv], capsys)[0] == 0
    assert local.read_text(encoding="utf-8") == f"[arena.profile.p1]\nmodels = {after}\n"


def test_with_no_terminal_the_question_is_a_no(repo, capsys, monkeypatch):
    local = _profile(repo, THREE)

    def eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert _run(["-p", "p1", "model", "drop", "sense-6:free", "-y"], capsys)[0] == 2
    assert "s2/sense-6:free" in local.read_text(encoding="utf-8")


def test_the_provider_qualified_name_acts_on_that_entry_only_and_y_is_enough(repo, capsys):
    local = _profile(repo, THREE)
    assert _run(["-p", "p1", "model", "drop", "s2/sense-6:free", "-y"], capsys)[0] == 0
    assert local.read_text(encoding="utf-8") == \
        "[arena.profile.p1]\nmodels = s1/sense-6:free,s3/sense-6:free,s1/b-free\n"


@pytest.mark.parametrize("line", ["s1/sense-6:free,sense-6:free,s1/b-free", "sense-6:free,sense-6:free,s1/b-free",
                                  "s1/sense-6:free,s1/sense-6:free@high,s1/b-free"])
def test_one_providers_entries_repeats_and_variants_need_no_question(repo, capsys, line):
    local = _profile(repo, line)
    code, out, _ = _run(["-p", "p1", "model", "drop", "sense-6:free", "-y"], capsys)
    assert code == 0 and "note:" not in out
    assert local.read_text(encoding="utf-8") == "[arena.profile.p1]\nmodels = s1/b-free\n"
