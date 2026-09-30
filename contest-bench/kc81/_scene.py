"""contest-bench/kc81/_scene.py — one KC-81 scene, run in its own process.

    python3 _scene.py <worktree> <scene> <out.json>

The round is driven the way `cmd_run` drives it, through the entry's own code:
`cli._make_backends` builds the backends (its `_start_server` swapped for a
`KiloServer` over `tests/_kilo_fake.py`, carrying a real child pid — a `sleep`
— and a real `kilo-serve.log`), and `run_round` gets exactly the keywords the
entry's own `cmd_run` passes it, read off the AST of its `cli.py`. So a heartbeat
that works only when a test hands it the log path, and not when the round does,
reads `log ?` here too.

Two agents, sessions prompted, the fake answers with nothing. The stall edge is
far away (600 s), so nothing but the heartbeat can speak. The scene's result is
a JSON: the WARNING lines naming a silent server, `state.json`, whether the
server pid is alive, and every abort the fake saw. `os._exit` at the end — the
round's workers are still waiting on the fake, on purpose.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import fields, replace
from pathlib import Path

WT = Path(sys.argv[1]).resolve()
SCENE, OUT = sys.argv[2], Path(sys.argv[3])
for p in (str(WT / "tests"), str(WT)):
    sys.path.insert(0, p)

from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.auto.llm_profile import LlmSettings  # noqa: E402
from tools.contest import cli  # noqa: E402
from tools.contest.kilo_client import KiloServer  # noqa: E402
from tools.contest.roster import AgentSpec, ContestConfig  # noqa: E402
from tools.contest.runner import run_round  # noqa: E402
from tools.contest.workspace import Workspace  # noqa: E402

SILENT = 2          # kilo_silent_sec
BEAT = 0.4          # progress_every_sec
AGENTS = ("agent-a", "agent-b")
ROUND = 45
TICKET = "45-kc81-bench.md"


def git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def sandbox(root: Path):
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "thing.py").write_text("def thing():\n    return 1\n")
    (repo / ".gitignore").write_text("runs/\n__pycache__/\n")
    ticket = root / "epic-tasks" / TICKET
    ticket.parent.mkdir(parents=True)
    ticket.write_text("# KC-81 bench\n\n**File:** `pkg/thing.py`\n\nbody\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    out = root / "out"
    out.mkdir()
    wss = []
    for a in AGENTS:
        path = root / "wt" / a
        branch = f"contest/{ROUND}/{a}"
        git(repo, "worktree", "add", "-q", "-b", branch, str(path), base)
        wss.append(Workspace(agent=a, path=path.resolve(), branch=branch,
                             base_sha=base, kind="worktree"))
    return ticket, out, wss


def config(**over) -> ContestConfig:
    specs = tuple(AgentSpec(name=a, provider_id="kenary", model_id=f"{a}:free") for a in AGENTS)
    gate = LlmSettings(base_url="https://gate-test/v1", api_key="k", model="test/gate",
                       api_format="openai", response_format=True, temperature=0.0,
                       max_tokens=256)
    kw = dict(agents=specs, max_parallel=len(AGENTS), max_rework=0, turn_timeout_sec=600,
              turn_extend_sec=0, idle_event_timeout_sec=600, max_questions_per_turn=3,
              error_retry_backoff_sec=0, first_touch_sec=0, max_continues_per_attempt=0,
              tmp_roots=("/tmp/*",), gate_max_calls_per_session=20, gate_settings=gate,
              progress_every_sec=BEAT, kilo_silent_sec=SILENT)
    kw.update(over)
    names = {f.name for f in fields(ContestConfig)}
    missing = [k for k in kw if k not in names]
    if missing:
        raise SystemExit(f"ContestConfig has no {missing}")
    return ContestConfig(**kw)


def cli_wiring() -> tuple[set, set]:
    """Keywords `cmd_run` passes to `run_round`/`run_leg`, and the ones it sets on
    the config with `replace(config, ...)` — as the entry's own cli.py reads."""
    tree = ast.parse((WT / "tools" / "contest" / "cli.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "cmd_run")
    call_kw, cfg_kw = set(), set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        name = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        if name in ("run_round", "run_leg"):
            call_kw |= {k.arg for k in n.keywords if k.arg}
        if name == "replace":
            cfg_kw |= {k.arg for k in n.keywords if k.arg}
    return call_kw, cfg_kw


def main() -> None:
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="kc81-"))
    ticket, out, wss = sandbox(root)
    lines: list = []

    class Grab(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if record.levelno >= logging.WARNING and "silent" in msg.lower() \
                    and "kilo" in msg.lower():
                lines.append({"t": time.monotonic() - t0, "msg": msg,
                              "level": record.levelname})

    logging.getLogger().addHandler(Grab())
    logging.getLogger().setLevel(logging.INFO)

    no_log = SCENE == "no_log"
    log = out / "kilo-serve.log"
    log.write_text("INFO service=server listening\n")
    past = time.time() - 30
    os.utime(log, (past, past))
    if no_log:
        log.unlink()        # a server the round did not start: no log anywhere
    # the server's pid: idle (`sleep`), or a process that burns CPU with no
    # request going anywhere — flat out (cpu_full), or about half the time
    # (cpu_half). The CPU is only what the line reports; silence is still
    # events + log, so both of these are silent servers.
    burn = {"cpu_full": "while True: pass",
            "cpu_half": ("import time\n"
                         "while True:\n"
                         "    t = time.monotonic() + 0.05\n"
                         "    while time.monotonic() < t: pass\n"
                         "    time.sleep(0.05)\n")}.get(SCENE)
    sleeper = subprocess.Popen([sys.executable, "-c", burn] if burn else ["sleep", "600"])
    harvest = SCENE == "harvest"
    turn = {"events": ["busy", "idle"]} if harvest else {"events": [], "idle": False}
    fake = FakeKiloServer({"turns": [turn]}).start()
    if harvest:
        # the agents' work is committed and claimed before the round, so the
        # idle turn goes straight to HARVESTING; the roots then take their time
        # while Kilo, rightly, has nothing to say
        for ws in wss:
            d = ws.path
            (d / "pkg" / "thing.py").write_text("def thing():\n    return 42\n")
            (d / "tests").mkdir(exist_ok=True)
            (d / "tests" / "test_thing.py").write_text(
                "from pkg.thing import thing\n\n\ndef test_thing():\n    assert thing() == 42\n")
            git(d, "add", "-A")
            git(d, "commit", "-q", "-m", "KC-81: thing")
            sha = git(d, "rev-parse", "HEAD")
            row = d / "runs" / ws.agent / "PROGRESS.csv"
            row.parent.mkdir(parents=True)
            row.write_text(f"ticket,finding,outcome,commit,note\n{TICKET},,FIXED,{sha},bench\n")
        import tools.contest.harvest as harvest_module

        def slow_roots(cwd, **kw):
            time.sleep(3 * SILENT + 3)
            return "tests:PASS tests_bugfix:absent .smoke_tests:absent .regression_tests:absent", []
        harvest_module.run_tests_detail = slow_roots
    server = KiloServer(fake.url, _process=None if no_log else sleeper,
                        _log_path=None if no_log else str(log), _attached=no_log)
    pid = None if no_log else sleeper.pid

    cfg = config(kilo_silent_sec=0) if SCENE == "off" else config()
    call_kw, cfg_kw = cli_wiring()
    for k in cfg_kw:
        if "log" in k and k in {f.name for f in fields(ContestConfig)}:
            cfg = replace(cfg, **{k: server.log_path or ""})
    cli._start_server = lambda *a, **k: server
    srv, mk = cli._make_backends(cfg, out)

    def make_backend(ws):
        before = fake.subscribers
        b = mk(ws)
        deadline = time.monotonic() + 30
        while fake.subscribers <= before and time.monotonic() < deadline:
            time.sleep(0.01)
        return b

    kwargs = {"make_backend": make_backend, "out_dir": out}
    if harvest:
        kwargs["run_tests"] = True
    for k in call_kw:
        if k == "server_pid":
            kwargs[k] = srv.pid
        elif "log" in k:
            kwargs[k] = srv.log_path

    t0 = time.monotonic()
    err: list = []

    def go():
        try:
            run_round(cfg, ROUND, ticket, wss, **kwargs)
        except BaseException as e:  # noqa: BLE001
            err.append(f"{type(e).__name__}: {e}")

    threading.Thread(target=go, daemon=True).start()
    # every session prompted before the clock of the scene starts
    deadline = time.monotonic() + 20
    while len(fake.sessions()) < len(AGENTS) and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    sessions = list(fake.sessions())

    def sid(s):
        return getattr(s, "id", None) or (s.get("id") if isinstance(s, dict) else s)

    def event(s):
        fake._emit({"type": "session.status",
                    "properties": {"sessionID": sid(s), "status": "busy"}})

    stop = threading.Event()
    if SCENE == "log_grows":
        def grow():
            while not stop.wait(0.3):
                with log.open("a") as fh:
                    fh.write("INFO tick\n")
        threading.Thread(target=grow, daemon=True).start()
        time.sleep(3 * SILENT + 1)
    elif SCENE == "one_busy":
        def beat():
            while not stop.wait(0.3):
                event(sessions[0])
        threading.Thread(target=beat, daemon=True).start()
        time.sleep(3 * SILENT + 1)
    elif SCENE == "two_spells":
        time.sleep(2 * SILENT + 1)
        marks = len(lines)
        event(sessions[0])
        time.sleep(2 * SILENT + 1.5)
    elif harvest:
        seen = set()
        end = time.monotonic() + 3 * SILENT + 2
        while time.monotonic() < end:
            try:
                seen |= {a.get("state") for a in json.loads((out / "state.json").read_text())["agents"]}
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.2)
    else:                   # silent, off, no_log, cpu_full, cpu_half
        time.sleep(3 * SILENT + 1)
    stop.set()

    state = None
    try:
        state = json.loads((out / "state.json").read_text())
    except (OSError, ValueError):
        pass
    aborts = [c for c in fake.calls() if "abort" in str(c)]
    status_out = ""
    try:
        r = subprocess.run([sys.executable, "-m", "tools.contest", "status",
                            "--ticket", str(ROUND), "--out", str(out)],
                           cwd=str(WT), capture_output=True, text=True, timeout=60)
        status_out = r.stdout + r.stderr
    except Exception as e:  # noqa: BLE001
        status_out = f"status failed: {e}"
    OUT.write_text(json.dumps({
        "lines": lines, "state_server_silent": (state or {}).get("server_silent"),
        "pid": pid, "alive": sleeper.poll() is None, "aborts": [str(a) for a in aborts],
        "log_mtime": past, "err": err, "sessions": len(sessions),
        "call_kw": sorted(call_kw), "cfg_kw": sorted(cfg_kw),
        "status": status_out,
        "first_spell_lines": locals().get("marks"),
        "reasons": [(a.get("name"), a.get("state"), str(a.get("last_error"))[:200], str((a.get("turns") or [{}])[-1].get("harvest"))[:300]) for a in (state or {}).get("agents", [])],
        "states_seen": sorted(x for x in (locals().get("seen") or ()) if x),
    }, default=str))
    sleeper.kill()
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        OUT.write_text(json.dumps({"fatal": str(e)}))
        os._exit(0)
    except BaseException as e:  # noqa: BLE001
        OUT.write_text(json.dumps({"fatal": f"{type(e).__name__}: {e}"}))
        os._exit(0)
