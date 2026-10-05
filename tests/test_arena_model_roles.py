"""AR-63: `arena model set-role` / `unset-role` — the ticket's writer and reviewer per profile.

The role is one `PROVIDER/MODEL` on a *direct* provider: the draft is one HTTP
call, not a Kilo session. The provider's own `/models` list is a fake, Kilo is
patched to know `kiloprov/model-k` only, and no network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.arena import cli, models, profile

SECRET = "sk-test-ROLESECRET"

#: The checkout's local file: one direct provider, and nothing else.
DIRECT_INI = (
    "# a direct provider for the ticket roles — keep this comment\n"
    "[arena.provider.direct]\n"
    f"api_key = ${{T_DIRECT_KEY}}\n"
    "base_url = http://127.0.0.1:9/v1\n"
)

#: What Kilo knows — one provider, and not `direct`.
KILO_LISTING = {
    "kiloprov": {"model-k": {
        "cost": {"input": 0, "output": 0},
        "capabilities": {"toolcall": True, "input": {"text": True}, "output": {"text": True}},
        "limit": {"context": 4000},
    }}
}

#: What the direct provider's own `/models` answers, as records.
DIRECT_RECORDS = [
    {"provider": "direct", "model": "model-a", "free": "yes", "ctx": 4000, "via": "direct"},
    {"provider": "direct", "model": "model-b", "free": "yes", "ctx": 4000, "via": "direct"},
]

P1 = DIRECT_INI + "[arena.profile.p1]\n"


def run(argv, capsys):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throw-away checkout with one direct provider and no round of its own."""
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("T_DIRECT_KEY", SECRET)
    for name in ("ARENA_KEY_DIRECT", "ARENA_URL_DIRECT", "ARENA_KEY_KILOPROV"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "contest.local.ini").write_text(DIRECT_INI, encoding="utf-8")
    return tmp_path


@pytest.fixture
def direct(monkeypatch):
    """The provider's own `/models`: a fake, so no request leaves the process."""
    monkeypatch.setattr(
        models, "_direct_for",
        lambda provider, url, key, now: [dict(r) for r in DIRECT_RECORDS])

    def kilo_list(repo, providers):
        if not providers:
            return {p: dict(m) for p, m in KILO_LISTING.items()}
        for provider in providers:
            if provider not in KILO_LISTING:
                # `kilo models NAME` for a provider missing from kilo.jsonc.
                raise models.KiloError(f"kilo models {provider}: unknown provider")
        return {p: dict(m) for p, m in KILO_LISTING.items() if p in providers}

    monkeypatch.setattr(models, "KILO_LIST", kilo_list)
    return KILO_LISTING


def local(repo):
    return (repo / "contest.local.ini").read_text(encoding="utf-8")


# ── 1: set-role writes the profile ───────────────────────────────────────────
def test_set_role_writes_the_role_and_keeps_every_other_line(repo, direct, capsys):
    code, out, err = run(["model", "set-role", "writer", "direct/model-a", "-p", "p1", "-y"], capsys)
    assert code == 0 and err == ""
    assert "profile 'p1' — writer =" in out
    assert "before: (none)" in out and "after:  direct/model-a" in out
    text = local(repo)
    assert text == DIRECT_INI + "\n[arena.profile.p1]\nwriter = direct/model-a\n"
    assert f"api_key = ${{T_DIRECT_KEY}}" in text, "the api_key line is untouched"


def test_set_role_takes_p_and_y_after_the_verb(repo, direct, capsys):
    code, _, _ = run(["model", "set-role", "reviewer", "direct/model-b", "-p", "p1", "-y"], capsys)
    assert code == 0
    assert "reviewer = direct/model-b" in local(repo)


def test_set_role_asks_and_a_no_changes_nothing(repo, direct, capsys, monkeypatch):
    before = (repo / "contest.local.ini").read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, out, err = run(["model", "set-role", "writer", "direct/model-a", "-p", "p1"], capsys)
    assert code == 2 and out and "not applied" in err
    assert len(err.splitlines()) == 1
    assert (repo / "contest.local.ini").read_bytes() == before

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert run(["model", "set-role", "writer", "direct/model-a", "-p", "p1"], capsys)[0] == 2
    assert (repo / "contest.local.ini").read_bytes() == before


def test_set_role_the_same_value_again_is_unchanged_and_writes_nothing(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(
        P1 + "writer = direct/model-a\n", encoding="utf-8")
    path = repo / "contest.local.ini"
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns
    code, out, err = run(["model", "set-role", "writer", "direct/model-a", "-p", "p1", "-y"], capsys)
    assert code == 0 and err == ""
    assert out == "profile 'p1': writer = direct/model-a (unchanged)\n"
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp


def test_set_role_replaces_an_old_role_and_keeps_the_rest(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(
        P1 + "legs = 2\nwriter = direct/model-b\n", encoding="utf-8")
    assert run(["model", "set-role", "writer", "direct/model-a", "-p", "p1", "-y"], capsys)[0] == 0
    assert local(repo) == P1 + "legs = 2\nwriter = direct/model-a\n"


# ── 4: refusals ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("argv,needle", [
    (["model", "set-role", "judge", "direct/model-a", "-p", "p1", "-y"], "judge"),
    (["model", "set-role", "writer", "model-a", "-p", "p1", "-y"], "name the provider"),
    (["model", "set-role", "writer", "direct/model-a@high", "-p", "p1", "-y"], "variant"),
    (["model", "set-role", "writer", "direct/a,direct/b", "-p", "p1", "-y"], "list"),
    (["model", "set-role", "writer", "direct/nosuch", "-p", "p1", "-y"], "is not a model"),
    (["model", "set-role", "writer", "kiloprov/model-k", "-p", "p1", "-y"],
     "needs a direct provider"),
])
def test_set_role_refusals_are_one_line_and_write_nothing(repo, direct, capsys, argv, needle):
    (repo / "contest.local.ini").write_text(P1 + "legs = 2\n", encoding="utf-8")
    before = (repo / "contest.local.ini").read_bytes()
    code, out, err = run(argv, capsys)
    assert code == 2 and out == ""
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")
    assert needle in err, err
    assert (repo / "contest.local.ini").read_bytes() == before


def test_set_role_an_unknown_model_gets_the_hint(repo, direct, capsys):
    code, _, err = run(["model", "set-role", "writer", "direct/model-a2", "-p", "p1", "-y"], capsys)
    assert code == 2
    assert "did you mean" in err and "direct/model-a" in err, err
    code, _, err = run(["model", "set-role", "writer", "direct/nosuch", "-p", "p1", "-y"], capsys)
    assert code == 2
    assert "--search direct/nosuch" in err, err


def test_set_role_a_provider_with_a_literal_key_is_refused(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(
        DIRECT_INI + "[arena.provider.literal]\n"
        "api_key = sk-literal-123\n"
        "base_url = http://127.0.0.1:9/v1\n", encoding="utf-8")
    before = (repo / "contest.local.ini").read_bytes()
    code, out, err = run(["model", "set-role", "writer", "literal/model-a", "-p", "p1", "-y"], capsys)
    assert code == 2 and out == ""
    assert "arena.provider.literal" in err and "${ENV}" in err, err
    assert "sk-literal-123" not in err
    assert (repo / "contest.local.ini").read_bytes() == before


def test_set_role_a_provider_with_no_key_at_all_is_refused(repo, direct, capsys, monkeypatch):
    monkeypatch.delenv("T_DIRECT_KEY", raising=False)
    code, _, err = run(["model", "set-role", "writer", "direct/model-a", "-p", "p1", "-y"], capsys)
    assert code == 2
    assert err == ("arena: writer needs a direct provider: 'DIRECT' has no base_url/api_key — "
                   "[arena.provider.direct] api_key = ${ENV} and base_url, "
                   "or ARENA_KEY_DIRECT / ARENA_URL_DIRECT\n"), err


# ── 5: the same-model hint ───────────────────────────────────────────────────
def test_set_role_the_other_role_on_the_same_model_is_a_hint(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(P1 + "writer = direct/model-a\n", encoding="utf-8")
    code, out, err = run(["model", "set-role", "reviewer", "direct/model-a", "-p", "p1", "-y"], capsys)
    assert code == 0
    assert "reviewer = direct/model-a" in local(repo)
    assert err == ("arena: writer and reviewer are the same model — issue create will refuse "
                   "without --same-model (or profile set p1 same_model_review=yes)\n"), err


def test_set_role_no_hint_when_same_model_review_is_on(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(
        P1 + "writer = direct/model-a\nsame_model_review = yes\n", encoding="utf-8")
    code, out, err = run(["model", "set-role", "reviewer", "direct/model-a", "-p", "p1", "-y"], capsys)
    assert code == 0 and err == ""


def test_set_role_no_hint_when_the_other_role_is_a_different_model(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(P1 + "writer = direct/model-a\n", encoding="utf-8")
    code, _, err = run(["model", "set-role", "reviewer", "direct/model-b", "-p", "p1", "-y"], capsys)
    assert code == 0 and err == ""


def test_set_role_the_same_model_case_insensitively(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(P1 + "writer = direct/model-A\n", encoding="utf-8")
    code, _, err = run(["model", "set-role", "reviewer", "direct/model-a", "-p", "p1", "-y"], capsys)
    assert code == 0 and "same model" in err


# ── 6: unset-role ─────────────────────────────────────────────────────────────
def test_unset_role_removes_only_that_line(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(
        P1 + "legs = 2\nwriter = direct/model-a\n", encoding="utf-8")
    code, out, err = run(["model", "unset-role", "writer", "-p", "p1", "-y"], capsys)
    assert code == 0 and err == ""
    assert local(repo) == P1 + "legs = 2\n"
    assert "profile 'p1' — writer =" in out
    assert "before: direct/model-a" in out and "after:  (removed)" in out


def test_unset_role_twice_refuses_and_unknown_profile_refuses(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(P1 + "writer = direct/model-a\n", encoding="utf-8")
    assert run(["model", "unset-role", "writer", "-p", "p1", "-y"], capsys)[0] == 0
    code, out, err = run(["model", "unset-role", "writer", "-p", "p1", "-y"], capsys)
    assert code == 2 and out == ""
    assert err == "arena: profile 'p1' has no writer\n"

    code, _, err = run(["model", "unset-role", "reviewer", "-p", "nobody", "-y"], capsys)
    assert code == 2 and err == "arena: profile 'nobody' has no reviewer\n", err


def test_unset_role_a_bad_role_is_refused(repo, direct, capsys):
    code, _, err = run(["model", "unset-role", "judge", "-p", "p1", "-y"], capsys)
    assert code == 2 and "judge" in err and len(err.splitlines()) == 1


def test_unset_role_without_yes_changes_nothing(repo, direct, capsys, monkeypatch):
    (repo / "contest.local.ini").write_text(P1 + "writer = direct/model-a\n", encoding="utf-8")
    before = (repo / "contest.local.ini").read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, out, err = run(["model", "unset-role", "writer", "-p", "p1"], capsys)
    assert code == 2 and "not applied" in err
    assert (repo / "contest.local.ini").read_bytes() == before


# ── 7–8: profile set / flags ─────────────────────────────────────────────────
def test_profile_set_refuses_the_roles(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(P1, encoding="utf-8")
    before = (repo / "contest.local.ini").read_bytes()
    for pair, key in (("writer=direct/model-a", "writer"), ("reviewer=direct/model-b", "reviewer")):
        code, _, err = run(["profile", "set", "p1", pair, "-y"], capsys)
        assert code == 2, pair
        assert key in err and "set-role" in err, err
        assert len(err.splitlines()) == 1
    assert (repo / "contest.local.ini").read_bytes() == before


def test_profile_set_checks_same_model_review_and_writes_it(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(P1, encoding="utf-8")
    code, _, err = run(["profile", "set", "p1", "same_model_review=maybe", "-y"], capsys)
    assert code == 2 and "same_model_review must be yes or no" in err, err
    assert "same_model_review" not in local(repo)
    assert run(["profile", "set", "p1", "same_model_review=yes", "-y"], capsys)[0] == 0
    assert "same_model_review = yes" in local(repo)
    assert run(["profile", "set", "p1", "same_model_review=", "-y"], capsys)[0] == 0
    assert "same_model_review" not in local(repo)


def test_a_bad_same_model_review_in_the_file_is_refused_on_read(repo, capsys):
    (repo / "contest.local.ini").write_text(P1 + "same_model_review = maybe\n", encoding="utf-8")
    with pytest.raises(profile.ProfileError) as exc:
        profile.load_profiles(repo)
    assert "same_model_review" in str(exc.value)


def test_the_role_keys_never_reach_the_runner(repo):
    base = {"legs": "2", "models": "direct/model-a", "backend": "kilo", "fresh": "yes"}
    with_roles = dict(base, writer="direct/model-a",
                      reviewer="direct/model-b", same_model_review="yes")
    assert profile.profile_flags(with_roles) == profile.profile_flags(base)
    assert profile.profile_flags(with_roles) == [
        "--models", "direct/model-a", "--legs", "2", "--backend", "kilo", "--fresh"]


def test_profile_view_shows_the_three_keys(repo, direct, capsys):
    (repo / "contest.local.ini").write_text(
        P1 + "writer = direct/model-a\nreviewer = direct/model-b\nsame_model_review = yes\n",
        encoding="utf-8")
    code, out, _ = run(["-o", "json", "-p", "p1", "profile", "view"], capsys)
    assert code == 0
    rows = {r["SETTING"]: r["VALUE"] for r in json.loads(out)}
    assert rows["writer"] == "direct/model-a"
    assert rows["reviewer"] == "direct/model-b"
    assert rows["same_model_review"] == "yes"
    assert rows["flags"] == "", "no flag leaks from a role key"


# ── 9: secrets ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("argv", [
    ["model", "set-role", "writer", "direct/model-a", "-p", "p1", "-y"],
    ["model", "set-role", "reviewer", "direct/model-a", "-p", "p1", "-y"],
    ["model", "set-role", "judge", "direct/model-a", "-p", "p1", "-y"],
    ["model", "set-role", "writer", "model-a", "-p", "p1", "-y"],
    ["model", "set-role", "writer", "direct/model-a@high", "-p", "p1", "-y"],
    ["model", "set-role", "writer", "direct/a,direct/b", "-p", "p1", "-y"],
    ["model", "set-role", "writer", "direct/nosuch", "-p", "p1", "-y"],
    ["model", "set-role", "writer", "kiloprov/model-k", "-p", "p1", "-y"],
    ["model", "set-role", "writer", "direct/model-a", "-p", "p1"],
    ["model", "unset-role", "writer", "-p", "p1", "-y"],
    ["model", "unset-role", "reviewer", "-p", "nobody", "-y"],
    ["profile", "set", "p1", "writer=direct/model-a", "-y"],
    ["profile", "set", "p1", "same_model_review=maybe", "-y"],
    ["-o", "json", "-p", "p1", "profile", "view"],
])
def test_the_key_never_reaches_stdout_or_stderr(repo, direct, capsys, argv):
    (repo / "contest.local.ini").write_text(
        P1 + "writer = direct/model-a\nreviewer = direct/model-b\n", encoding="utf-8")
    code, out, err = run(argv, capsys)
    assert SECRET not in out, out
    assert SECRET not in err, err
    assert code in (0, 2, 3)
