"""AR-60: `arena model test` and direct providers with a key — scores on record, key never shown."""

from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from tools.arena import cli, models

DAY = 86400.0
SECRET = "sk-test-0123456789"


def kmeta(ctx=1000):
    """Kilo's verbose metadata for one free model."""
    return {
        "cost": {"input": 0, "output": 0},
        "capabilities": {"toolcall": True, "input": {"text": True}, "output": {"text": True}},
        "limit": {"context": ctx},
    }


class Server:
    """A local OpenAI-style `/models` endpoint on port 0; remembers the requests' headers."""

    def __init__(self, ids, secret=SECRET):
        self.seen: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.seen.append(dict(self.headers))
                if self.headers.get("Authorization") != f"Bearer {secret}":
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                body = json.dumps({"data": [
                    {"id": i, "pricing": {"prompt": "0", "completion": "0"}, "context_length": 4000}
                    for i in ids]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    srv = Server(["m-a:free", "m-b:free"])
    yield srv
    srv.close()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    for name in ("ARENA_KEY_MYPROV", "ARENA_URL_MYPROV", "ARENA_KEY_MY_PROV",
                 "ARENA_URL_MY_PROV", "ARENA_KEY_DP"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.fixture
def kilo(monkeypatch):
    """The Kilo seam; `kilo.listing` is `{provider: {model: metadata}}`."""

    class Fake:
        listing = {"kp": {"k-a:free": kmeta(), "k-b:free": kmeta()}}

    fake = Fake()

    def fake_list(repo, providers):
        return {p: m for p, m in fake.listing.items() if not providers or p in providers}

    monkeypatch.setattr(models, "KILO_LIST", fake_list)
    return fake


@pytest.fixture
def runner(monkeypatch):
    """The test-run seam; `runner.results[model]` is the answer, `runner.specs` the calls."""

    class Fake:
        results: dict = {}
        specs: list = []
        active = 0
        peak = 0
        delay = 0.0

    fake = Fake()
    lock = threading.Lock()

    def fake_run(repo, spec):
        with lock:
            fake.specs.append(dict(spec))
            fake.active += 1
            fake.peak = max(fake.peak, fake.active)
        try:
            time.sleep(fake.delay)
            return fake.results.get(spec["model"], {"score": 15, "max": 15, "error": ""})
        finally:
            with lock:
                fake.active -= 1

    fake.specs = []
    monkeypatch.setattr(models, "RUN_TEST", fake_run)
    return fake


def run(argv, capsys):
    code = cli.main(argv)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def ini(repo: Path, text: str) -> None:
    (repo / "contest.local.ini").write_text(text, encoding="utf-8")


def scores(repo: Path) -> list:
    return json.loads((repo / ".arena" / "model-scores.json").read_text(encoding="utf-8"))


# ── 1: the key is sent and never shown ───────────────────────────────────────
def test_ini_env_reference_key_is_sent_and_never_shown(repo, server, kilo, capsys, monkeypatch):
    """An `api_key = ${ENV}` section lists directly; the key rides the request, no output."""
    ini(repo, f"[arena.provider.dp]\nbase_url = {server.url}\napi_key = ${{ARENA_KEY_DP}}\n")
    monkeypatch.setenv("ARENA_KEY_DP", SECRET)
    for fmt in ("table", "json"):
        code, out, err = run(["-o", fmt, "model", "available", "dp"], capsys)
        assert code == 0
        assert "m-a:free" in out
        assert SECRET not in out + err
    assert server.seen and all(h["Authorization"] == f"Bearer {SECRET}" for h in server.seen)


# ── 2: a literal key is refused ──────────────────────────────────────────────
def test_literal_key_in_ini_is_refused_naming_the_section(repo, kilo, capsys):
    ini(repo, f"[arena.provider.dp]\nbase_url = http://x/v1\napi_key = {SECRET}\n")
    code, out, err = run(["model", "available", "dp"], capsys)
    assert code == 2
    assert "[arena.provider.dp]" in err
    assert SECRET not in out + err
    assert len(err.strip().splitlines()) == 1


# ── 3: no key, provider not in Kilo ──────────────────────────────────────────
def test_no_key_for_a_provider_outside_kilo_is_a_hint(repo, kilo, capsys):
    code, out, err = run(["model", "available", "kp", "myprov"], capsys)
    assert code == 0
    assert "k-a:free" in out
    assert ("arena: no key for 'myprov' — export ARENA_KEY_MYPROV=… or add "
            "[arena.provider.myprov] api_key = ${ARENA_KEY_MYPROV} to contest.local.ini") in err


def test_a_provider_kilo_fails_on_is_a_hint_not_a_refusal(repo, kilo, capsys, monkeypatch):
    """Real Kilo exits 1 on `kilo models NAME` for a provider not in kilo.jsonc."""
    def strict(repo_, providers):
        unknown = [p for p in providers if p not in kilo.listing]
        if unknown:
            raise models.KiloError(f"provider {unknown[0]} not found")
        return {p: m for p, m in kilo.listing.items() if not providers or p in providers}

    monkeypatch.setattr(models, "KILO_LIST", strict)
    code, out, err = run(["model", "available", "kp", "my-prov"], capsys)
    assert code == 0 and "k-a:free" in out
    assert ("arena: no key for 'my-prov' — export ARENA_KEY_MY_PROV=… or add "
            "[arena.provider.my-prov] api_key = ${ARENA_KEY_MY_PROV} to contest.local.ini") in err
    monkeypatch.setenv("ARENA_KEY_MY_PROV", SECRET)
    code, out, err = run(["model", "available", "my-prov", "kp"], capsys)
    assert code == 0 and "k-a:free" in out
    assert "arena: no URL for 'my-prov' — export ARENA_URL_MY_PROV=… or pass --url" in err
    assert SECRET not in out + err


def test_a_failed_direct_list_of_a_provider_kilo_lacks_goes_on(repo, kilo, capsys, monkeypatch):
    """The direct list fails, Kilo has no such provider: the hint, not a second line."""
    def strict(repo_, providers):
        if [p for p in providers if p not in kilo.listing]:
            raise models.KiloError("provider not found")
        return {p: m for p, m in kilo.listing.items() if not providers or p in providers}

    monkeypatch.setattr(models, "KILO_LIST", strict)
    monkeypatch.setenv("ARENA_KEY_MYPROV", SECRET)
    monkeypatch.setenv("ARENA_URL_MYPROV", "http://127.0.0.1:9/v1")
    code, out, err = run(["model", "available", "myprov", "kp"], capsys)
    assert code == 0 and "k-a:free" in out
    assert "kilo models failed" not in err and "trying Kilo" in err
    assert SECRET not in out + err


def test_kilo_itself_failing_is_still_a_refusal(repo, kilo, capsys, monkeypatch):
    def broken(repo_, providers):
        raise models.KiloError("boom")

    monkeypatch.setattr(models, "KILO_LIST", broken)
    code, _, err = run(["model", "available", "kp", "myprov"], capsys)
    assert code == 2 and len(err.strip().splitlines()) == 1


# ── 4: a direct model missing from Kilo ──────────────────────────────────────
def test_direct_model_missing_from_kilo_gets_the_kilo_jsonc_hint(repo, server, kilo, capsys,
                                                                  monkeypatch):
    kilo.listing = {"myprov": {"m-a:free": kmeta()}}
    monkeypatch.setenv("ARENA_KEY_MYPROV", SECRET)
    monkeypatch.setenv("ARENA_URL_MYPROV", server.url)
    code, out, err = run(["model", "available", "myprov"], capsys)
    assert code == 0
    assert "arena: myprov/m-b:free is not in kilo.jsonc — a round cannot run it until it is " \
           "added there" in err
    assert "m-a:free is not in kilo.jsonc" not in err


# ── 5: one direct and one Kilo provider in one call ──────────────────────────
def test_direct_and_kilo_providers_in_one_available_call(repo, server, kilo, capsys, monkeypatch):
    monkeypatch.setenv("ARENA_KEY_MYPROV", SECRET)
    monkeypatch.setenv("ARENA_URL_MYPROV", server.url)
    code, out, err = run(["-o", "json", "model", "available", "kp", "myprov", "--free"], capsys)
    assert code == 0
    got = {(r["PROVIDER"], r["NAME"]) for r in json.loads(out)}
    assert got == {("kp", "k-a:free"), ("kp", "k-b:free"),
                   ("myprov", "m-a:free"), ("myprov", "m-b:free")}


# ── 6: model test a,b writes two records; available shows LAST-TEST ──────────
def test_model_test_writes_records_and_available_shows_them(repo, kilo, runner, capsys):
    runner.results = {"k-a:free": {"score": 14, "max": 15, "error": ""}}
    code, out, err = run(["model", "test", "kp/k-a:free,kp/k-b:free"], capsys)
    assert code == 0
    recs = scores(repo)
    assert {(r["provider"], r["model"], r["via"]) for r in recs} == {
        ("kp", "k-a:free", "kilo"), ("kp", "k-b:free", "kilo")}
    assert {r["model"]: (r["score"], r["max"]) for r in recs}["k-a:free"] == (14, 15)
    code, out, err = run(["model", "available", "kp"], capsys)
    lines = {ln.split()[0]: ln for ln in out.splitlines()[1:]}
    assert "14/15 0m" in lines["k-a:free"]
    assert "15/15 0m" in lines["k-b:free"]


# ── 7: a failing test records the reason ─────────────────────────────────────
def test_failing_test_writes_error_and_the_column_shows_it(repo, kilo, runner, capsys):
    runner.results = {"k-a:free": {"score": None, "max": None, "error": "HTTP 401: nope"}}
    code, out, err = run(["model", "test", "kp/k-a:free"], capsys)
    assert code == 1
    assert scores(repo)[0]["error"] == "HTTP 401: nope"
    assert scores(repo)[0]["score"] is None
    code, out, err = run(["model", "available", "kp"], capsys)
    row = next(ln for ln in out.splitlines() if ln.startswith("k-a:free"))
    assert "401 0m" in row


# ── 8: aging and atomic write ────────────────────────────────────────────────
def test_old_scores_are_dropped_and_the_write_is_atomic(repo, kilo, runner, capsys):
    path = repo / ".arena" / "model-scores.json"
    path.parent.mkdir()
    old = time.time() - 8 * DAY
    path.write_text(json.dumps([{"provider": "kp", "model": "gone", "via": "kilo",
                                "score": 3, "max": 15, "error": "", "at": old}]))
    store = models.score_store(repo)
    assert store.load() == []
    run(["model", "test", "kp/k-a:free"], capsys)
    assert [r["model"] for r in scores(repo)] == ["k-a:free"]
    assert not list(path.parent.glob("*.tmp"))
    path.write_text("{broken")
    assert models.score_store(repo).load() == []
    run(["model", "test", "kp/k-b:free"], capsys)
    assert [r["model"] for r in scores(repo)] == ["k-b:free"]


def test_newest_record_per_model_wins(repo, kilo, runner, capsys):
    run(["model", "test", "kp/k-a:free"], capsys)
    runner.results = {"k-a:free": {"score": 9, "max": 15, "error": ""}}
    run(["model", "test", "kp/k-a:free"], capsys)
    assert [(r["model"], r["score"]) for r in scores(repo)] == [("k-a:free", 9)]


# ── 9: a typo runs nothing ───────────────────────────────────────────────────
def test_typo_in_model_test_is_a_refusal_and_nothing_runs(repo, kilo, runner, capsys):
    code, out, err = run(["model", "test", "kp/k-a:free,k-b:fre"], capsys)
    assert code == 2
    assert "did you mean: k-b:free" in err
    assert runner.specs == []
    assert not (repo / ".arena" / "model-scores.json").exists()


# ── 10: no ini at all ────────────────────────────────────────────────────────
def test_env_only_provider_lists_and_tests_directly_without_an_ini(repo, server, kilo, runner,
                                                                    capsys, monkeypatch):
    monkeypatch.setenv("ARENA_KEY_MY_PROV", SECRET)
    monkeypatch.setenv("ARENA_URL_MY_PROV", server.url)
    code, out, err = run(["model", "available", "my-prov", "--free"], capsys)
    assert code == 0 and "m-a:free" in out
    code, out, err = run(["model", "test", "my-prov/m-a:free"], capsys)
    assert code == 0
    spec = runner.specs[0]
    assert (spec["via"], spec["url"], spec["key"]) == ("direct", server.url, SECRET)
    assert SECRET not in out + err
    assert scores(repo)[0]["via"] == "direct"
    assert SECRET not in (repo / ".arena" / "model-scores.json").read_text()
    assert not list(repo.glob("*.ini"))


def test_url_flag_does_the_same_and_needs_one_provider(repo, server, kilo, runner, capsys,
                                                       monkeypatch):
    monkeypatch.setenv("ARENA_KEY_MY_PROV", SECRET)
    code, out, err = run(["model", "available", "my-prov", "--url", server.url], capsys)
    assert code == 0 and "m-b:free" in out
    code, out, err = run(["model", "test", "my-prov/m-a:free", "--url", server.url], capsys)
    assert code == 0 and runner.specs[-1]["via"] == "direct"
    code, out, err = run(["model", "available", "my-prov", "kp", "--url", server.url], capsys)
    assert code == 2 and "--url needs exactly one provider" in err
    assert not list(repo.glob("*.ini"))


def test_key_without_a_url_gives_the_url_hint_and_goes_through_kilo(repo, kilo, capsys,
                                                                    monkeypatch):
    monkeypatch.setenv("ARENA_KEY_MYPROV", SECRET)
    code, out, err = run(["model", "available", "kp", "myprov"], capsys)
    assert code == 0 and "k-a:free" in out
    assert "arena: no URL for 'myprov' — export ARENA_URL_MYPROV=… or pass --url" in err
    assert SECRET not in out + err
    assert not list(repo.glob("*.ini"))


def test_failed_direct_list_does_not_leak_the_key_and_falls_back(repo, kilo, capsys, monkeypatch):
    kilo.listing = {"myprov": {"k-a:free": kmeta()}}
    srv = Server(["m-a:free"], secret="another")
    try:
        monkeypatch.setenv("ARENA_KEY_MYPROV", SECRET)
        monkeypatch.setenv("ARENA_URL_MYPROV", srv.url)
        code, out, err = run(["model", "available", "myprov"], capsys)
    finally:
        srv.close()
    assert code == 0 and "k-a:free" in out
    assert "HTTP 401" in err and SECRET not in out + err


# ── 11: at most 4 at once; -o json parses ────────────────────────────────────
def test_model_test_runs_at_most_four_at_once_and_json_parses(repo, kilo, runner, capsys):
    kilo.listing = {"kp": {f"m{i}:free": kmeta() for i in range(9)}}
    runner.delay = 0.05
    names = ",".join(f"kp/m{i}:free" for i in range(9))
    code, out, err = run(["-o", "json", "model", "test", names], capsys)
    assert code == 0
    assert 1 < runner.peak <= 4
    parsed = json.loads(out)
    assert len(parsed) == 9 and parsed[0]["SCORE"] == "15/15"
    assert len(scores(repo)) == 9


def test_available_test_tests_every_listed_row(repo, kilo, runner, capsys):
    code, out, err = run(["model", "available", "kp", "--test"], capsys)
    assert code == 0
    assert {s["model"] for s in runner.specs} == {"k-a:free", "k-b:free"}
    assert out.count("15/15") == 2


def test_bare_name_resolves_through_kilo(repo, kilo, runner, capsys):
    code, out, err = run(["model", "test", "k-a:free"], capsys)
    assert code == 0
    assert (runner.specs[0]["provider"], runner.specs[0]["model"]) == ("kp", "k-a:free")
