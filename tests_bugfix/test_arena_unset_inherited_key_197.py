"""tests_bugfix/test_arena_unset_inherited_key_197.py — bug 207: `profile set key=` and `model unset-role` do nothing for a key set in the committed `contest.ini`.

Bug: `models.write_profile_keys` writes `contest.local.ini` only. When the key
came from the committed `contest.ini` there was nothing to remove, so the command
printed its "after: (removed)" preview, wrote the file unchanged, exited 0 and
said nothing — and `issue create` went on writing the ticket with the model that
was meant to be removed. An empty override is not a removal in the ini format, so
such a remove is now a refusal naming the file, nothing written.
"""

from __future__ import annotations

import pytest

from tools.arena import cli, models, profile

BASE = "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = test/a\n"
INHERITED = "[arena.profile.default]\nreviewer = prov/m\nbranch = main\n"


def _repo(tmp_path, committed="", local=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "contest.ini").write_text(BASE + committed, encoding="utf-8")
    if local is not None:
        (repo / "contest.local.ini").write_text(local, encoding="utf-8")
    return repo


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = _repo(tmp_path, INHERITED)
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    return r


def test_write_profile_keys_refuses_a_key_the_committed_ini_sets(repo):
    local = repo / "contest.local.ini"
    with pytest.raises(models.ModelError, match="contest\\.ini"):
        models.write_profile_keys(repo, "default", {"reviewer": None})
    assert not local.exists()  # the refusal came before the one write


def test_unset_role_refuses_and_writes_nothing(repo, capsys):
    assert cli.main(["model", "unset-role", "reviewer", "-p", "default", "-y"]) == 2
    err = capsys.readouterr().err
    assert "contest.ini" in err
    assert "unchanged" in err
    assert not (repo / "contest.local.ini").exists()
    profiles, _ = profile.load_profiles(repo)
    assert profiles["default"]["reviewer"] == "prov/m"


def test_profile_set_refuses_the_same_key(repo, capsys):
    assert cli.main(["profile", "set", "default", "branch=", "-y"]) == 2
    err = capsys.readouterr().err
    assert "contest.ini" in err
    assert not (repo / "contest.local.ini").exists()


def test_the_local_file_is_left_byte_for_byte(tmp_path, monkeypatch):
    kept = "[arena.profile.default]\nvariant = v1\n"
    repo = _repo(tmp_path, INHERITED, local=kept)
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    assert cli.main(["model", "unset-role", "reviewer", "-p", "default", "-y"]) == 2
    assert (repo / "contest.local.ini").read_text(encoding="utf-8") == kept


def test_a_key_only_in_the_local_file_is_still_removed(tmp_path):
    repo = _repo(tmp_path, "", local="[arena.profile.default]\nvariant = v1\n")
    models.write_profile_keys(repo, "default", {"variant": None})
    assert "variant" not in (repo / "contest.local.ini").read_text(encoding="utf-8")


def test_unset_role_still_removes_a_local_only_role(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, "", local="[arena.profile.default]\nreviewer = prov/local\n")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    assert cli.main(["model", "unset-role", "reviewer", "-p", "default", "-y"]) == 0
    assert "reviewer" not in (repo / "contest.local.ini").read_text(encoding="utf-8")


def test_setting_a_key_next_to_the_refused_one_is_still_refused(tmp_path):
    """The whole write is refused, not the one key: no partial file."""
    repo = _repo(tmp_path, INHERITED)
    with pytest.raises(models.ModelError, match="contest\\.ini"):
        models.write_profile_keys(repo, "default", {"variant": "v1", "reviewer": None})
    assert not (repo / "contest.local.ini").exists()
