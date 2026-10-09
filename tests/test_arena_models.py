"""AR-59: `arena model available|use|drop` — the model list from Kilo, by name, with a cache."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tools.arena import cli, models

DAY = 86400.0


def meta(cost=0, ctx=1000, tools=True):
    """Kilo's verbose metadata for one model, as `free_from_kilo` reads it."""
    return {
        "cost": {"input": cost, "output": cost},
        "capabilities": {"toolcall": tools, "input": {"text": True}, "output": {"text": True}},
        "limit": {"context": ctx},
    }


# p1: a free-by-name, a free-by-suffix, a price-0 model with no suffix, a paid one.
LISTING = {
    "p1": {"x:free": meta(0, 4000), "y-free": meta(0), "z": meta(0), "w": meta(0.5)},
    "p2": {"v:free": meta(0, 8000)},
}
SMALL = {"p1": {"a:free": meta(), "b-free": meta(), "hy3:free": meta(), "devstral-2:free": meta()}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def kilo(monkeypatch):
    """Replace the seam; `kilo.calls` records every `(providers)` asked."""

    class Fake:
        listing = LISTING
        calls: list = []
        error = None

    fake = Fake()
    fake.calls = []

    def fake_list(repo, providers):
        fake.calls.append(list(providers))
        if fake.error:
            raise fake.error
        return {p: m for p, m in fake.listing.items() if not providers or p in providers}

    monkeypatch.setattr(models, "KILO_LIST", fake_list)
    return fake


def run(argv, capsys):
    code = cli.main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def rows(out):
    """The table's rows as {column: cell}; cells are split on 2+ spaces."""
    lines = out.splitlines()
    head = lines[0]
    cols = ["NAME", "PROVIDER", "FREE", "CTX", "IN-PROFILE", "LAST-TEST"]
    starts = [head.index(c) for c in cols if c in head]
    out_rows = []
    for ln in lines[1:]:
        cells = [ln[a:b].strip() for a, b in zip(starts, starts[1:] + [None])]
        out_rows.append(dict(zip(cols, cells)))
    return out_rows


# ── 1–2: available ───────────────────────────────────────────────────────────
def test_available_lists_all_sorted_with_free_marks(repo, kilo, capsys):
    code, out, err = run(["model", "available"], capsys)
    assert code == 0 and err == ""
    got = rows(out)
    assert [(r["PROVIDER"], r["NAME"]) for r in got] == sorted(
        [("p1", "w"), ("p1", "x:free"), ("p1", "y-free"), ("p1", "z"), ("p2", "v:free")]
    )
    free = {r["NAME"]: r["FREE"] for r in got}
    assert free == {"x:free": "yes", "y-free": "yes", "z": "maybe", "w": "no", "v:free": "yes"}
    assert {r["NAME"]: r["CTX"] for r in got}["x:free"] == "4000"
    assert all(r["LAST-TEST"] == "" for r in got)


def test_available_free_keeps_yes_and_maybe_and_two_providers_in_one_call(repo, kilo, capsys):
    code, out, _ = run(["model", "available", "--free", "p1", "p2"], capsys)
    assert code == 0
    assert kilo.calls == [["p1", "p2"]]
    assert sorted(r["NAME"] for r in rows(out)) == ["v:free", "x:free", "y-free", "z"]


def test_available_one_provider_asks_only_that_provider(repo, kilo, capsys):
    _, out, _ = run(["model", "available", "p2"], capsys)
    assert kilo.calls == [["p2"]]
    assert [r["NAME"] for r in rows(out)] == ["v:free"]


def test_search_filters_case_insensitively_and_nothing_found_is_exit_three(repo, kilo, capsys):
    code, out, _ = run(["model", "available", "--search", "X:FR"], capsys)
    assert code == 0 and [r["NAME"] for r in rows(out)] == ["x:free"]
    code, out, err = run(["model", "available", "--search", "nope"], capsys)
    assert code == 3 and out == ""
    assert len(err.splitlines()) == 1 and err.startswith("arena:")


def test_in_profile_marks_the_profiles_models(repo, kilo, capsys):
    (repo / "contest.local.ini").write_text("[arena.profile.p]\nmodels = x:free,v:free@high\n")
    _, out, _ = run(["-p", "p", "model", "available"], capsys)
    marked = {r["NAME"] for r in rows(out) if r["IN-PROFILE"] == "yes"}
    assert marked == {"x:free", "v:free"}


def test_json_output_parses_and_keeps_numbers(repo, kilo, capsys):
    code, out, _ = run(["model", "available", "-o", "json", "--free"], capsys)
    assert code == 0
    data = json.loads(out)
    assert {r["NAME"] for r in data} == {"x:free", "y-free", "z", "v:free"}
    assert {r["NAME"]: r["CTX"] for r in data}["x:free"] == 4000
    assert set(data[0]) == {"NAME", "PROVIDER", "FREE", "CTX", "IN-PROFILE", "LAST-TEST"}


# ── 3–7: use ─────────────────────────────────────────────────────────────────
def local_ini(repo, text="[arena.profile.p1]\nmodels = old:free\n"):
    path = repo / "contest.local.ini"
    path.write_text(text)
    return path


def test_use_writes_models_in_the_local_file_only_and_keeps_order_and_repeats(repo, kilo, capsys):
    kilo.listing = SMALL
    committed = b"[arena.profile.p1]\nmodels = c:free\nlegs = 2\n"
    (repo / "contest.ini").write_bytes(committed)
    local = local_ini(repo)
    code, out, err = run(["-p", "p1", "model", "use", "a:free,a:free,b-free", "-y"], capsys)
    assert code == 0 and err == ""
    assert "models = a:free,a:free,b-free" in local.read_text()
    assert (repo / "contest.ini").read_bytes() == committed
    assert "before: old:free" in out and "after:  a:free,a:free,b-free" in out


def test_use_takes_p_and_y_after_the_verb_as_the_ticket_spells_it(repo, kilo, capsys):
    kilo.listing = SMALL
    local = local_ini(repo)
    code, _, _ = run(["model", "use", "a:free", "-p", "p1", "-y"], capsys)
    assert code == 0 and local.read_text() == "[arena.profile.p1]\nmodels = a:free\n"


def test_use_keeps_everything_else_in_the_local_file_byte_for_byte(repo, kilo, capsys):
    kilo.listing = SMALL
    text = (
        "# my box\n[contest_gate_llm]\napi_key = sk-secret  ; keep\n\n"
        "[arena.profile.p1]\nlegs = 2\n\n[arena]\nprofile = p1\n"
    )
    local = local_ini(repo, text)
    assert run(["model", "use", "a:free", "-y"], capsys)[0] == 0
    assert local.read_text() == text.replace("legs = 2\n", "legs = 2\nmodels = a:free\n")


def test_use_keeps_the_variant_suffix_and_replaces_a_multi_line_value(repo, kilo, capsys):
    kilo.listing = SMALL
    local = local_ini(repo, "[arena.profile.p1]\nmodels = old:free,\n    older:free\nlegs = 1\n")
    assert run(["-p", "p1", "model", "use", "a:free@high,b-free", "-y"], capsys)[0] == 0
    assert local.read_text() == "[arena.profile.p1]\nmodels = a:free@high,b-free\nlegs = 1\n"


def test_use_asks_and_a_no_changes_nothing(repo, kilo, capsys, monkeypatch):
    kilo.listing = SMALL
    local = local_ini(repo)
    before = local.read_bytes()
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, _, err = run(["-p", "p1", "model", "use", "a:free"], capsys)
    assert code == 2 and "not applied" in err and local.read_bytes() == before
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    assert run(["-p", "p1", "model", "use", "a:free"], capsys)[0] == 0
    assert "models = a:free" in local.read_text()


def test_use_eof_at_the_prompt_is_a_no(repo, kilo, capsys, monkeypatch):
    kilo.listing = SMALL
    local = local_ini(repo)
    before = local.read_bytes()

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert run(["-p", "p1", "model", "use", "a:free"], capsys)[0] == 2
    assert local.read_bytes() == before


@pytest.mark.parametrize("extra", [[], ["-y"]])
def test_use_a_missing_suffix_is_refused_with_the_name_never_substituted(repo, kilo, capsys, extra):
    kilo.listing = SMALL
    local = local_ini(repo)
    before = local.read_bytes()
    code, out, err = run(["-p", "p1", "model", "use", "hy3", *extra], capsys)
    assert code == 2 and out == ""
    assert err == "arena: 'hy3' is not a model — did you mean: hy3:free\n"
    assert local.read_bytes() == before


def test_use_a_typo_names_the_close_model_and_nothing_close_gets_the_search_hint(repo, kilo, capsys):
    kilo.listing = SMALL
    local = local_ini(repo)
    before = local.read_bytes()
    code, _, err = run(["-p", "p1", "model", "use", "devstral2", "-y"], capsys)
    assert code == 2
    assert err == "arena: 'devstral2' is not a model — did you mean: devstral-2:free\n"
    code, _, err = run(["-p", "p1", "model", "use", "xyz", "-y"], capsys)
    assert code == 2
    assert err == "arena: 'xyz' is not a model (arena model available --search xyz)\n"
    assert local.read_bytes() == before


def test_use_one_bad_name_among_good_ones_writes_nothing(repo, kilo, capsys):
    kilo.listing = SMALL
    code, _, err = run(["-p", "p1", "model", "use", "a:free,nope,b-free", "-y"], capsys)
    assert code == 2 and "'nope'" in err
    assert not (repo / "contest.local.ini").exists()
    local = local_ini(repo)
    before = local.read_bytes()
    assert run(["-p", "p1", "model", "use", "a:free,nope", "-y"], capsys)[0] == 2
    assert local.read_bytes() == before


def test_use_a_model_with_no_test_on_record_is_fine(repo, kilo, capsys):
    kilo.listing = SMALL
    code, _, err = run(["-p", "p1", "model", "use", "a:free", "-y"], capsys)
    assert code == 0 and err == ""


@pytest.mark.parametrize("key,role", [
    ("gate_llm_profile", "the gate model"),
    ("draft_llm_profile", "the ticket writer"),
    ("draft_review_llm_profile", "the ticket reviewer"),
])
def test_use_refuses_a_judge_model(repo, kilo, capsys, key, role):
    kilo.listing = SMALL
    (repo / "contest.ini").write_text(f"[contest]\n{key} = jp\n[jp]\nmodel = p1/b-free\n")
    local = local_ini(repo)
    before = local.read_bytes()
    code, _, err = run(["-p", "p1", "model", "use", "a:free,b-free", "-y"], capsys)
    assert code == 2 and "'b-free'" in err and role in err and len(err.splitlines()) == 1
    assert local.read_bytes() == before
    assert run(["-p", "p1", "model", "use", "a:free", "-y"], capsys)[0] == 0


# ── 8: drop ──────────────────────────────────────────────────────────────────
def profile_with(repo, models_line):
    return local_ini(repo, f"[arena.profile.p1]\nmodels = {models_line}\n")


def test_drop_removes_every_copy_and_keeps_the_order_of_the_rest(repo, kilo, capsys):
    kilo.listing = SMALL
    local = profile_with(repo, "a:free,b-free,a:free,hy3:free")
    code, out, _ = run(["-p", "p1", "model", "drop", "a:free", "-y"], capsys)
    assert code == 0
    assert local.read_text() == "[arena.profile.p1]\nmodels = b-free,hy3:free\n"
    assert "before: a:free,b-free,a:free,hy3:free" in out


def test_drop_works_when_the_models_come_from_the_committed_file(repo, kilo, capsys):
    kilo.listing = SMALL
    committed = b"[arena.profile.p1]\nmodels = a:free,b-free\n"
    (repo / "contest.ini").write_bytes(committed)
    assert run(["-p", "p1", "model", "drop", "a:free", "-y"], capsys)[0] == 0
    assert (repo / "contest.local.ini").read_text() == "[arena.profile.p1]\nmodels = b-free\n"
    assert (repo / "contest.ini").read_bytes() == committed


def test_drop_unknown_name_gets_the_use_refusal_and_hint(repo, kilo, capsys):
    kilo.listing = SMALL
    local = profile_with(repo, "a:free,b-free")
    before = local.read_bytes()
    code, _, err = run(["-p", "p1", "model", "drop", "hy3", "-y"], capsys)
    assert code == 2 and err == "arena: 'hy3' is not a model — did you mean: hy3:free\n"
    assert local.read_bytes() == before


def test_drop_a_name_not_in_the_profile_names_the_profiles_models(repo, kilo, capsys):
    kilo.listing = SMALL
    local = profile_with(repo, "a:free,b-free")
    before = local.read_bytes()
    code, _, err = run(["-p", "p1", "model", "drop", "hy3:free", "-y"], capsys)
    assert code == 2 and "profile 'p1'" in err and "a:free, b-free" in err
    assert local.read_bytes() == before


def test_drop_every_model_is_refused_and_the_file_is_unchanged(repo, kilo, capsys):
    kilo.listing = SMALL
    local = local_ini(repo, "[arena.profile.default]\nmodels = a:free,a:free\n")
    before = local.read_bytes()
    code, _, err = run(["model", "drop", "a:free", "-y"], capsys)
    assert code == 2
    assert err == "arena: profile 'default' would be empty — 1 model must stay\n"
    assert local.read_bytes() == before


def test_drop_removes_a_model_kilo_no_longer_lists_without_calling_kilo(repo, kilo, capsys):
    """168: a retired model is in the profile and not in Kilo's list; `drop` takes it out."""
    kilo.listing = SMALL
    local = profile_with(repo, "old:free,a:free")
    code, _, _ = run(["-p", "p1", "model", "drop", "old:free", "-y"], capsys)
    assert code == 0
    assert local.read_text() == "[arena.profile.p1]\nmodels = a:free\n"
    assert kilo.calls == []


def test_drop_of_a_profile_name_works_when_kilo_is_unreachable(repo, kilo, capsys):
    """168: `KiloError` from the list is no reason to keep a name the profile holds."""
    kilo.error = models.KiloError("no kilo binary found")
    local = profile_with(repo, "old:free,a:free")
    assert run(["-p", "p1", "model", "drop", "old:free", "-y"], capsys)[0] == 0
    assert local.read_text() == "[arena.profile.p1]\nmodels = a:free\n"


def test_drop_still_refuses_a_bad_variant_without_calling_kilo(repo, kilo, capsys):
    local = profile_with(repo, "a:free,b-free")
    before = local.read_bytes()
    code, _, err = run(["-p", "p1", "model", "drop", "a:free@bad!", "-y"], capsys)
    assert code == 2 and "bad variant" in err
    assert local.read_bytes() == before and kilo.calls == []


def test_drop_with_no_ini_names_the_missing_profile(repo, kilo, capsys):
    code, _, err = run(["model", "drop", "a:free", "-y"], capsys)
    assert code == 2 and "'default'" in err and len(err.splitlines()) == 1
    assert not (repo / "contest.local.ini").exists() and kilo.calls == []


# ── 9: the cache ─────────────────────────────────────────────────────────────
def record(model, age_days, provider="p1"):
    return {"provider": provider, "model": model, "free": "yes", "ctx": 0,
            "at": time.time() - age_days * DAY}


def write_cache(repo, data):
    path = repo / ".arena" / "models-cache.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_a_record_eight_days_old_is_gone_after_a_read(repo):
    write_cache(repo, [record("old:free", 8), record("fresh:free", 1)])
    got = models.model_cache(repo).load()
    assert [r["model"] for r in got] == ["fresh:free"]


def test_a_record_eight_days_old_is_gone_after_a_write_and_no_tmp_is_left(repo, kilo, capsys):
    path = write_cache(repo, [record("old:free", 8, "p9"), record("keep:free", 1, "p9")])
    assert run(["model", "available", "p2"], capsys)[0] == 0
    stored = json.loads(path.read_text())
    names = {(r["provider"], r["model"]) for r in stored}
    assert names == {("p9", "keep:free"), ("p2", "v:free")}
    assert [f.name for f in path.parent.iterdir()] == ["models-cache.json"]
    assert set(stored[0]) == {"provider", "model", "free", "ctx", "at"}


def test_available_refreshes_a_provider_it_listed_and_leaves_the_others(repo, kilo, capsys):
    path = write_cache(repo, [record("gone:free", 1, "p2"), record("other:free", 1, "p9")])
    assert run(["model", "available", "p2"], capsys)[0] == 0
    assert {(r["provider"], r["model"]) for r in json.loads(path.read_text())} == {
        ("p2", "v:free"), ("p9", "other:free")}


@pytest.mark.parametrize("junk", ["", "{not json", '{"a": 1}', '"text"', "[1, null, {}]"])
def test_a_broken_cache_file_is_an_empty_cache_never_an_error(repo, kilo, capsys, junk):
    write_cache(repo, junk)
    assert models.model_cache(repo).load() == []
    assert run(["model", "available"], capsys)[0] == 0


def test_use_with_every_name_in_the_cache_does_not_call_kilo(repo, kilo, capsys):
    write_cache(repo, [record("a:free", 1), record("b-free", 1)])
    assert run(["-p", "p1", "model", "use", "a:free,b-free", "-y"], capsys)[0] == 0
    assert kilo.calls == []


def test_use_calls_kilo_for_a_name_the_cache_lacks_and_caches_the_answer(repo, kilo, capsys):
    kilo.listing = SMALL
    write_cache(repo, [record("a:free", 1)])
    assert run(["-p", "p1", "model", "use", "a:free,b-free", "-y"], capsys)[0] == 0
    assert kilo.calls == [[]]
    assert run(["-p", "p1", "model", "use", "b-free", "-y"], capsys)[0] == 0
    assert kilo.calls == [[]]


@pytest.mark.parametrize("value", ["0", "junk", "-3", "nan"])
def test_cache_days_zero_or_junk_means_no_cache_file(repo, kilo, capsys, value):
    (repo / "contest.local.ini").write_text(f"[arena]\nmodel_cache_days = {value}\n")
    assert models.cache_days(repo) == 0
    assert run(["model", "available"], capsys)[0] == 0
    assert not (repo / ".arena").exists()


def test_cache_days_defaults_to_seven_and_reads_the_ini(repo):
    assert models.cache_days(repo) == 7
    (repo / "contest.local.ini").write_text("[arena]\nmodel_cache_days = 2\n")
    assert models.cache_days(repo) == 2
    write_cache(repo, [record("a:free", 3)])
    assert models.model_cache(repo).load() == []


# ── 10: Kilo failing ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("argv", [
    ["model", "available"],
    ["-p", "p1", "model", "use", "a:free", "-y"],
])
def test_kilo_failing_is_one_refusal_line_exit_two_no_traceback(repo, kilo, capsys, argv):
    kilo.error = models.KiloError("exit 7 boom")
    code, out, err = run(argv, capsys)
    assert code == 2 and out == ""
    assert len(err.splitlines()) == 1 and err.startswith("arena: kilo models failed:")
    assert "boom" in err and "Traceback" not in err


def test_an_oserror_from_the_seam_is_a_refusal_too(repo, kilo, capsys):
    kilo.error = FileNotFoundError("no such file")
    code, _, err = run(["model", "available"], capsys)
    assert code == 2 and len(err.splitlines()) == 1


def test_no_kilo_binary_is_a_refusal_naming_where_it_looked(repo, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(models.shutil, "which", lambda name: None)
    code, _, err = run(["model", "available"], capsys)
    assert code == 2 and len(err.splitlines()) == 1 and "no kilo binary" in err
    assert "Traceback" not in err


# ── the kilo binary and the default seam ─────────────────────────────────────
def fake_bin(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_kilo_bin_from_the_ini_wins_then_path_then_the_newest_extension(repo, monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    old = fake_bin(home / ".vscode/extensions/kilocode.kilo-code-7.6.2-linux-x64/bin/kilo")
    new = fake_bin(home / ".vscode/extensions/kilocode.kilo-code-7.10.0-linux-x64/bin/kilo")
    monkeypatch.setattr(models.shutil, "which", lambda name: None)
    assert models.find_kilo_bin(repo) == new and old != new
    monkeypatch.setattr(models.shutil, "which", lambda name: "/usr/bin/kilo")
    assert models.find_kilo_bin(repo) == "/usr/bin/kilo"
    mine = fake_bin(tmp_path / "mine" / "kilo")
    (repo / "contest.ini").write_text("[contest]\nkilo_bin = auto\n")
    assert models.find_kilo_bin(repo) == "/usr/bin/kilo"
    (repo / "contest.local.ini").write_text(f"[contest]\nkilo_bin = {mine}\n")
    assert models.find_kilo_bin(repo) == mine


def test_default_seam_uses_kilo_models_and_lists_providers_when_none_given(repo, monkeypatch):
    check = models._model_check()
    monkeypatch.setattr(models, "find_kilo_bin", lambda r: "kilo")
    seen = []
    monkeypatch.setattr(check, "run", lambda cmd, cwd, timeout, env=None: (
        "p1/a:free\n\x1b[0mp2/b\nnot a model line\n", "", 0))
    monkeypatch.setattr(check, "kilo_models", lambda kilo, prov, verbose: (
        seen.append((prov, verbose)) or {"m": {}}))
    assert models._kilo_list(repo, []) == {"p1": {"m": {}}, "p2": {"m": {}}}
    assert seen == [("p1", True), ("p2", True)]
    seen.clear()
    assert models._kilo_list(repo, ["p9"]) == {"p9": {"m": {}}} and seen == [("p9", True)]


def test_default_seam_turns_a_kilo_models_failure_into_kilo_error(repo, monkeypatch):
    import sys as _sys

    check = models._model_check()
    monkeypatch.setattr(models, "find_kilo_bin", lambda r: "kilo")

    def failing(kilo, prov, verbose):
        print(f"  kilo models {prov}: exit 1 login first", file=_sys.stderr)
        return {}

    monkeypatch.setattr(check, "kilo_models", failing)
    with pytest.raises(models.KiloError, match="exit 1 login first"):
        models._kilo_list(repo, ["p1"])


# ── 12: a fresh checkout with no ini file ────────────────────────────────────
def test_a_fresh_checkout_lists_filters_and_says_nothing_on_stderr(repo, kilo, capsys):
    assert not (repo / "contest.ini").exists() and not (repo / "contest.local.ini").exists()
    code, out, err = run(["model", "available"], capsys)
    assert code == 0 and err == "" and len(rows(out)) == 5
    assert all(r["IN-PROFILE"] == "" for r in rows(out))
    code, out, err = run(["model", "available", "--free"], capsys)
    assert code == 0 and err == "" and len(rows(out)) == 4
    assert (repo / ".arena" / "models-cache.json").is_file()


def test_a_fresh_checkout_use_creates_the_local_file_with_only_the_profile(repo, kilo, capsys):
    kilo.listing = SMALL
    code, _, err = run(["-p", "p1", "model", "use", "a:free", "-y"], capsys)
    assert code == 0 and err == ""
    assert (repo / "contest.local.ini").read_text() == "[arena.profile.p1]\nmodels = a:free\n"
    assert not (repo / "contest.ini").exists()


def test_a_fresh_checkout_use_without_p_writes_the_default_profile(repo, kilo, capsys):
    kilo.listing = SMALL
    assert run(["model", "use", "a:free,b-free", "-y"], capsys)[0] == 0
    assert (repo / "contest.local.ini").read_text() == (
        "[arena.profile.default]\nmodels = a:free,b-free\n")


def test_use_without_p_writes_the_profile_the_ini_names_active(repo, kilo, capsys):
    kilo.listing = SMALL
    local = local_ini(repo, "[arena]\nprofile = work\n")
    assert run(["model", "use", "a:free", "-y"], capsys)[0] == 0
    assert local.read_text() == (
        "[arena]\nprofile = work\n\n[arena.profile.work]\nmodels = a:free\n")


def test_use_refuses_a_profile_name_that_would_break_the_ini(repo, kilo, capsys):
    kilo.listing = SMALL
    code, _, err = run(["-p", "x]\nevil", "model", "use", "a:free", "-y"], capsys)
    assert code == 2 and len(err.splitlines()) == 1
    assert not (repo / "contest.local.ini").exists()


def test_a_broken_local_ini_is_a_refusal_for_use_not_a_traceback(repo, kilo, capsys):
    kilo.listing = SMALL
    (repo / "contest.local.ini").write_text("no section here\n")
    code, _, err = run(["-p", "p1", "model", "use", "a:free", "-y"], capsys)
    assert code == 2 and len(err.splitlines()) == 1 and "contest.local.ini" in err


def test_the_write_is_atomic_and_keeps_the_files_mode(repo, kilo, capsys):
    kilo.listing = SMALL
    local = local_ini(repo)
    local.chmod(0o640)
    assert run(["-p", "p1", "model", "use", "a:free", "-y"], capsys)[0] == 0
    assert (local.stat().st_mode & 0o777) == 0o640
    assert sorted(f.name for f in repo.iterdir() if f.name.startswith("contest.local")) == [
        "contest.local.ini"]
