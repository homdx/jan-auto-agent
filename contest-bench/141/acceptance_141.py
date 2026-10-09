"""Judge's acceptance suite for round 141 (AR-60), written from the ticket alone.

Black box, one layer under every entry's own seams: a fake `kilo` binary on PATH
answers `kilo models [P] [--verbose]` from a JSON fixture and `kilo run -m M PROMPT`
with the code task's answer (good / half / no code by model name); the direct
provider is a real OpenAI-compatible HTTP server on port 0 in this process
(`/models`, `/chat/completions`), which records every Authorization header.
`scripts/py_model_test.py`'s 15 checks run for real on what comes back.
`HOME` is a tmp folder, every command goes through `tools.arena.cli.main` with
`REPO_ROOT` patched.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/141/acceptance_141.py -n 8 -q
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402

SECRET = "sk-BENCH-a1b2c3d4e5f6-SECRET"

GOOD = '''```python
import re
def merge_intervals(intervals):
    xs = sorted((min(a, b), max(a, b)) for a, b in intervals)
    out = []
    for a, b in xs:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out
def parse_duration(s):
    m = re.fullmatch(r"(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?", s or "")
    if not s or not m:
        raise ValueError(s)
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se
```'''
# merge is right, parse_duration never raises: 10/15
HALF = GOOD.replace("    if not s or not m:\n        raise ValueError(s)\n",
                    "    if not m:\n        return 0\n")

FREE = {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}


def meta(ctx=131072):
    return {"cost": FREE, "limit": {"context": ctx, "output": 8192},
            "capabilities": {"toolcall": True, "input": {"text": True}, "output": {"text": True}}}


CATALOG = {"pk": {f"good-{c}:free": meta() for c in "abcdef"}
           | {"half:free": meta(), "nocode:free": meta()},
           # the direct provider is in Kilo too, but Kilo lacks m-a and deny
           "dp": {"m-b:free": meta()}}

FAKE_KILO = r'''#!/usr/bin/env python3
import json, os, sys, time
cat = json.load(open(os.environ["BENCH_CATALOG"]))
a = [x for x in sys.argv[1:] if not x.startswith("-")]
log = os.environ["BENCH_LOG"]
if a and a[0] == "run":
    m = sys.argv[sys.argv.index("-m") + 1]
    run_dir = os.environ["BENCH_RUNS"]
    me = os.path.join(run_dir, str(os.getpid()))
    open(me, "w").close()
    with open(log, "a") as f:
        f.write(json.dumps({"run": m, "live": len(os.listdir(run_dir))}) + "\n")
    time.sleep(0.6)
    os.remove(me)
    ans = {"good": os.environ["BENCH_GOOD"], "half": os.environ["BENCH_HALF"]}
    key = next((k for k in ans if k in m), None)
    text = ans[key] if key else "I cannot write code today."
    print(json.dumps({"type": "text", "part": {"text": text}}))
    print(json.dumps({"type": "step_finish", "part": {"tokens": {"input": 10, "output": 5}, "cost": 0}}))
    sys.exit(0)
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

DIRECT_MODELS = ["m-a:free", "m-b:free", "deny:free"]


class Direct:
    """The direct provider: /models and /chat/completions, key-checked."""

    def __init__(self) -> None:
        self.auth: list[str] = []
        self.chats: list[str] = []
        direct = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _ok_key(self):
                h = self.headers.get("Authorization", "")
                direct.auth.append(h)
                if h != f"Bearer {SECRET}":
                    self._send(401, {"error": {"message": "401 bad key"}})
                    return False
                return True

            def do_GET(self):
                if not self._ok_key():
                    return
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"data": [
                        {"id": m, "pricing": {"prompt": "0", "completion": "0"},
                         "context_length": 65536} for m in DIRECT_MODELS]})
                else:
                    self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not self._ok_key():
                    return
                model = body.get("model", "")
                direct.chats.append(model)
                if "deny" in model:
                    self._send(401, {"error": {"message": "unauthorized"}})
                    return
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": GOOD}}]})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def direct():
    d = Direct()
    yield d
    d.close()


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
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setenv("BENCH_CATALOG", str(catf))
    monkeypatch.setenv("BENCH_LOG", str(tmp_path / "kilo.log"))
    monkeypatch.setenv("BENCH_RUNS", str(runs))
    monkeypatch.setenv("BENCH_GOOD", GOOD)
    monkeypatch.setenv("BENCH_HALF", HALF)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    for v in list(os.environ):
        if v.startswith(("ARENA_KEY_", "ARENA_URL_")):
            monkeypatch.delenv(v)
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


def kilo_log(repo: Path) -> list[dict]:
    p = repo.parent / "kilo.log"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def ini(repo: Path, direct: Direct | None = None, key: str = "${ARENA_KEY_DP}",
        extra: str = "") -> None:
    (repo / "contest.ini").write_text(
        "[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = pk/good-a:free\n")
    if direct is not None:
        (repo / "contest.local.ini").write_text(
            f"{extra}[arena.provider.dp]\nbase_url = {direct.url}\napi_key = {key}\n")


def _call(argv):
    try:
        return cli.main(list(argv))
    except SystemExit as e:  # argparse
        return e.code


def _globals_first(argv):
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


def scores_path(repo: Path) -> Path:
    return repo / ".arena" / "model-scores.json"


def scores(repo: Path) -> list[dict]:
    p = scores_path(repo)
    return json.loads(p.read_text()) if p.exists() else []


def rec(repo: Path, model_part: str) -> dict:
    hits = [r for r in scores(repo) if model_part in str(r.get("model", ""))]
    assert hits, (model_part, scores(repo))
    return hits[-1]


def row(out: str, model_part: str) -> str:
    rows = [l for l in out.splitlines() if model_part in l]
    assert rows, (model_part, out)
    return rows[0]


def no_secret(*texts):
    for t in texts:
        assert SECRET not in t
        assert SECRET[3:20] not in t


# 1
def test_ini_env_key_goes_to_the_request_never_out(env, direct, capsys, monkeypatch):
    ini(env, direct)
    monkeypatch.setenv("ARENA_KEY_DP", SECRET)
    code, out, err = run(capsys, "model", "available", "dp", "--free")
    assert code == 0, err
    assert "m-a:free" in out and "m-b:free" in out
    assert f"Bearer {SECRET}" in direct.auth
    no_secret(out, err)
    code, out, err = run(capsys, "model", "available", "dp", "-o", "json")
    assert code == 0, err
    json.loads(out)
    no_secret(out, err)
    code, out, err = run(capsys, "model", "test", "dp/m-a:free", "-o", "json")
    assert code == 0, err
    no_secret(out, err, scores_path(env).read_text())


# 2
def test_literal_key_refused_naming_section(env, direct, capsys):
    ini(env, direct, key=SECRET)
    code, out, err = run(capsys, "model", "available", "dp")
    assert code not in (0, None)
    assert "arena.provider.dp" in err and len(err.strip().splitlines()) == 1
    assert direct.auth == []
    no_secret(out, err)


# 3
def test_no_key_hint_and_goes_on(env, capsys):
    ini(env)
    code, out, err = run(capsys, "model", "available", "pk", "nokey")
    assert code == 0, err
    assert "good-a:free" in out
    hint = [l for l in err.splitlines() if "no key for 'nokey'" in l]
    assert hint and "ARENA_KEY_NOKEY" in hint[0]


# 4
def test_direct_model_missing_from_kilo_hint(env, direct, capsys, monkeypatch):
    ini(env, direct)
    monkeypatch.setenv("ARENA_KEY_DP", SECRET)
    code, out, err = run(capsys, "model", "available", "dp")
    assert code == 0, err
    errs = err
    # the ticket does not say which command prints it: available, test or use
    errs += run(capsys, "model", "test", "dp/m-a:free")[2]
    errs += run(capsys, "model", "use", "dp/m-a:free", "-p", "p1", "-y")[2]
    hits = [l for l in errs.splitlines() if "kilo.jsonc" in l]
    assert any("m-a:free" in l for l in hits), errs
    assert not any("m-b:free" in l for l in hits), errs


# 5
def test_direct_and_kilo_in_one_call(env, direct, capsys, monkeypatch):
    ini(env, direct)
    monkeypatch.setenv("ARENA_KEY_DP", SECRET)
    code, out, err = run(capsys, "model", "available", "dp", "pk")
    assert code == 0, err
    assert "m-b:free" in out and "good-c:free" in out


# 6
def test_test_two_writes_two_records_and_last_test(env, capsys):
    ini(env)
    code, out, err = run(capsys, "model", "test", "good-a:free,half:free")
    assert code == 0, err
    a, h = rec(env, "good-a:free"), rec(env, "half:free")
    for r in (a, h):
        assert {"provider", "model", "via", "score", "max", "error", "at"} <= set(r)
        assert r["via"] == "kilo" and r["provider"] == "pk"
    assert (a["score"], a["max"]) == (15, 15)
    assert (h["score"], h["max"]) == (10, 15)
    assert len(kilo_log(env)) == 2
    code, out, err = run(capsys, "model", "available", "pk")
    assert code == 0, err
    assert "15/15" in row(out, "good-a:free")
    assert "10/15" in row(out, "half:free")
    assert "15/15" not in row(out, "good-b:free")


def test_direct_test_record(env, direct, capsys, monkeypatch):
    ini(env, direct)
    monkeypatch.setenv("ARENA_KEY_DP", SECRET)
    run(capsys, "model", "available", "dp")
    code, out, err = run(capsys, "model", "test", "dp/m-b:free")
    assert code == 0, err
    r = rec(env, "m-b:free")
    assert r["via"] == "direct" and r["provider"] == "dp" and r["score"] == 15
    assert direct.chats == ["m-b:free"] and kilo_log(env) == []


# 7
def test_failure_writes_error_and_column(env, direct, capsys, monkeypatch):
    ini(env, direct)
    monkeypatch.setenv("ARENA_KEY_DP", SECRET)
    run(capsys, "model", "available", "dp")
    run(capsys, "model", "test", "dp/deny:free")
    r = rec(env, "deny:free")
    assert "401" in str(r["error"]) and not r["score"]
    code, out, err = run(capsys, "model", "available", "dp")
    assert "401" in row(out, "deny:free")
    run(capsys, "model", "test", "nocode:free")
    r = rec(env, "nocode:free")
    assert r["error"] and not r["score"]


def test_newest_record_wins(env, capsys):
    ini(env)
    run(capsys, "model", "test", "half:free")
    p = scores_path(env)
    data = json.loads(p.read_text())
    data[0]["score"] = 3
    data[0]["at"] = _older(data[0]["at"], 3600)
    data.append(dict(data[0], score=12, at=_older(data[0]["at"], -1800)))
    p.write_text(json.dumps(data))
    code, out, _ = run(capsys, "model", "available", "pk")
    assert "12/15" in row(out, "half:free") and "3/15" not in row(out, "half:free")


# 8
def _older(at, seconds):
    if isinstance(at, (int, float)):
        return at - seconds
    t = dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    return (t - dt.timedelta(seconds=seconds)).isoformat()


def test_old_records_dropped_and_atomic(env, capsys):
    ini(env)
    run(capsys, "model", "test", "good-a:free")
    p = scores_path(env)
    data = json.loads(p.read_text())
    ghost = dict(data[0], model=data[0]["model"].replace("good-a", "good-f"),
                 at=_older(data[0]["at"], 8 * 86400))
    p.write_text(json.dumps(data + [ghost]))
    code, out, _ = run(capsys, "model", "available", "pk")
    assert "15/15" not in row(out, "good-f:free")  # dropped on read
    run(capsys, "model", "test", "good-b:free")    # and on write
    models = [r["model"] for r in scores(env)]
    assert not any("good-f" in m for m in models)
    assert any("good-a" in m for m in models) and any("good-b" in m for m in models)
    assert sorted(x.name for x in p.parent.iterdir() if "score" in x.name or x.suffix == ".tmp") \
        == ["model-scores.json"]


def test_broken_scores_file_is_empty(env, capsys):
    ini(env)
    scores_path(env).parent.mkdir(parents=True, exist_ok=True)
    scores_path(env).write_text("{nope")
    code, out, err = run(capsys, "model", "available", "pk")
    assert code == 0 and "Traceback" not in err
    code, out, err = run(capsys, "model", "test", "good-a:free")
    assert code == 0, err
    assert rec(env, "good-a:free")["score"] == 15


# 9
def test_typo_refused_nothing_run(env, capsys):
    ini(env)
    code, out, err = run(capsys, "model", "test", "good-a:free,goood-b")
    assert code == 2
    assert "good-b:free" in err and len(err.strip().splitlines()) == 1
    assert kilo_log(env) == [] and scores(env) == []


def test_use_without_score(env, capsys):
    ini(env)
    code, _, err = run(capsys, "model", "use", "good-a:free", "-p", "p1", "-y")
    assert code == 0, err


# 10
def test_no_ini_env_only(env, direct, capsys, monkeypatch):
    monkeypatch.setenv("ARENA_KEY_MY_PROV", SECRET)
    monkeypatch.setenv("ARENA_URL_MY_PROV", direct.url)
    code, out, err = run(capsys, "model", "available", "my-prov", "--free")
    assert code == 0, err
    assert "m-a:free" in out
    code, out, err = run(capsys, "model", "test", "my-prov/m-a:free")
    assert code == 0, err
    assert direct.chats == ["m-a:free"]
    r = rec(env, "m-a:free")
    assert r["via"] == "direct" and r["score"] == 15
    no_secret(out, err)
    assert not (env / "contest.local.ini").exists() and not (env / "contest.ini").exists()


def test_no_ini_url_flag(env, direct, capsys, monkeypatch):
    monkeypatch.setenv("ARENA_KEY_MY_PROV", SECRET)
    code, out, err = run(capsys, "model", "available", "my-prov", "--url", direct.url)
    assert code == 0, err
    assert "m-b:free" in out and f"Bearer {SECRET}" in direct.auth
    assert not (env / "contest.local.ini").exists()


def test_no_ini_key_without_url_hint(env, capsys, monkeypatch):
    monkeypatch.setenv("ARENA_KEY_MY_PROV", SECRET)
    code, out, err = run(capsys, "model", "available", "my-prov", "pk")
    hint = [l for l in err.splitlines() if "no URL for 'my-prov'" in l]
    assert hint and "ARENA_URL_MY_PROV" in hint[0] and "--url" in hint[0]
    no_secret(out, err)
    assert not (env / "contest.local.ini").exists()


# 11
def test_at_most_four_at_once_and_json(env, capsys):
    ini(env)
    names = ",".join(f"good-{c}:free" for c in "abcdef")
    t0 = time.time()
    code, out, err = run(capsys, "model", "test", names, "-o", "json")
    assert code == 0, err
    json.loads(out)
    log = kilo_log(env)
    assert len(log) == 6
    assert max(e["live"] for e in log) <= 4
    assert time.time() - t0 < 6 * 0.6 + 30
    assert len({r["model"] for r in scores(env)}) == 6
