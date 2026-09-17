"""run_one.py <worktree> <scenarios.py> <scenario> — run ONE scenario against ONE entrant tree.

Prints a single line `@@RESULT@@{json}` (run_all.py parses it). Runs in a fresh
interpreter per call so the per-(url, model) memories in tools.llm_stream
(`mark_*_unsupported`) never leak between scenarios or entrants.

The entrant's code is exercised end-to-end through `Gate1Filter.filter()`; the
only substitutions are:
  * `urllib.request.urlopen`  → provider.FakeProvider (scripted SSE/NDJSON)
  * `tools.auto.gate1_filter.tracer` → an in-memory recorder
  * `time.sleep` → recorder (no real waiting; the requested durations are kept)
  * logging → an in-memory list handler at DEBUG
"""
from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import textwrap
import time
import traceback
import urllib.request
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
WT = Path(sys.argv[1]).resolve()
SCEN_FILE = Path(sys.argv[2]).resolve()
SCEN = sys.argv[3]
os.chdir(WT)
sys.path.insert(0, str(WT))
sys.path.insert(0, str(HARNESS))

import provider as P  # noqa: E402


def _load_scenarios(path: Path):
    spec = importlib.util.spec_from_file_location("scenarios", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scenarios"] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load_scenarios(SCEN_FILE)


class RecordingTracer:
    def __init__(self):
        self.events = []

    def event(self, source=None, target=None, kind=None, params=None, **extra):
        self.events.append({"source": source, "target": target, "kind": kind,
                            "params": dict(params or {}), "content": extra.get("content")})


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        self.records.append([record.levelname, record.name, msg])


def main():
    scen = S.SCENARIOS[SCEN]
    out = {"scenario": SCEN, "worktree": WT.name, "checks": [], "info": {}, "error": None}
    handler = ListHandler()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    sleeps = []
    real_sleep = time.sleep
    time.sleep = lambda s: sleeps.append(float(s))
    stdout = io.StringIO()
    old_stdout = sys.stdout
    t0 = time.monotonic()
    try:
        sys.stdout = stdout
        if scen.get("mode") == "stream":
            r = run_stream(scen, handler, sleeps)
        else:
            r = run_filter(scen, handler, sleeps)
        sys.stdout = old_stdout
        r.stdout = stdout.getvalue()
        r.wall = round(time.monotonic() - t0, 2)
        for c in scen["checks"](r):
            out["checks"].append(c)
        out["info"] = r.info
        out["wall"] = r.wall
    except Exception:
        sys.stdout = old_stdout
        out["error"] = traceback.format_exc()[-3000:]
        out["info"]["stdout_tail"] = stdout.getvalue()[-1500:]
        out["info"]["log_tail"] = handler.records[-30:]
    finally:
        time.sleep = real_sleep
    print("@@RESULT@@" + json.dumps(out, default=str))


class Result:
    """What a scenario's `checks(r)` can look at."""

    def __init__(self):
        self.info = {}
        self.logs = []      # [level, logger, message] in emission order
        self.sleeps = []    # durations handed to time.sleep

    def log_lines(self, needle: str | None = None, level: str | None = None):
        return [m for (lv, _n, m) in self.logs
                if (needle is None or needle in m) and (level is None or lv == level)]

    def calls(self, i: int):
        """Request payloads (parsed JSON bodies) candidate i received, in order."""
        return self.provider.by_key.get(f"tools/c{i}.py", [])

    @property
    def all_calls(self):
        return self.provider.requests


def _repo(tmp: Path, n: int):
    """n candidates, each citing its own tools/c{i}.py — the RUN-5 test fixture shape."""
    from tools.auto.architect import CandidateTask, CitedLocation
    (tmp / "tools").mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        (tmp / "tools" / f"c{i}.py").write_text(textwrap.dedent(f"""\
            def parse_config_{i}(raw):
                # TODO: validate input
                return raw
        """), encoding="utf-8")
        out.append(CandidateTask(
            title=f"candidate {i}", instruction="fix the problem described in the claim",
            target_files=[f"tools/c{i}.py"], acceptance_check="python -m pytest tests -q",
            cited_location=CitedLocation(file=f"tools/c{i}.py", symbol=f"parse_config_{i}"),
            cluster="agents"))
    return out


def _config(over: dict, api_format: str = "openai", extra: dict | None = None):
    import configparser
    base_url = "http://stub.local:11434" if api_format == "ollama" else "http://stub.local:1337/v1"
    gate1 = dict(getattr(S, "GATE1_DEFAULTS", {
        "temperature": "0.0", "max_tokens": "4096", "skip_llm": "false",
        "llm_call_retry_wait_sec": "0", "llm_call_retry_max": "1",
        "unparseable_retry_mode": "fast", "unparseable_learn": "false",
        "presence_workers": "1",
    }))
    gate1.update({k: str(v) for k, v in over.items()})
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": base_url, "api_key": "test",
                      "model": "test-model", "api_format": api_format, "num_ctx": "65536"},
        "gate1": gate1,
        "loop": {"timeout_seconds": "10"},
        **(extra or {}),
    })
    return cfg


def run_filter(scen, handler, sleeps) -> Result:
    import tools.auto.gate1_filter as G
    r = Result()
    n = scen.get("candidates", 1)
    plans = scen["plans"](n) if callable(scen["plans"]) else scen["plans"]
    prov = P.FakeProvider(plans, default=scen.get("default"))
    r.provider = prov
    tracer = RecordingTracer()
    real_urlopen = urllib.request.urlopen
    real_tracer = G.tracer
    urllib.request.urlopen = prov
    G.tracer = tracer
    try:
        af = scen.get("api_format", "openai")
        cfg = _config(scen.get("config", {}), af, scen.get("extra_sections"))
        flt = G.Gate1Filter(config=cfg, base_url=cfg["api_local"]["base_url"], api_key="test",
                            model="test-model", api_format=af, verify_ssl=False)
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cands = _repo(tmp, n)
            counters = {}
            accepted, rejected = flt.filter(cands, base_dir=tmp, counters=counters)
    finally:
        urllib.request.urlopen = real_urlopen
        G.tracer = real_tracer
    r.logs = handler.records
    r.sleeps = list(sleeps)
    r.tracer = tracer
    r.filter = flt
    r.counters = counters
    r.accepted = [c.title for c in accepted]
    r.rejected = {res.candidate.title: (res.stage, res.reason) for res in rejected}
    try:
        r.split_line = G.format_gate1_split(counters)
    except Exception as e:  # noqa: BLE001
        r.split_line = f"<error {e}>"
    # per-candidate outcome from the presence log lines
    r.outcome = {}
    for i in range(n):
        t = f"candidate {i}"
        if any(m.startswith(f"Gate1[presence] UNKNOWN {t!r}") for m in r.log_lines()):
            r.outcome[t] = "unknown-kept" if t in r.accepted else "unknown-dropped"
        elif any(m.startswith(f"Gate1[presence] CONFIRMED {t!r}") for m in r.log_lines()):
            r.outcome[t] = "confirmed"
        elif any(m.startswith(f"Gate1[presence] REJECTED {t!r}") for m in r.log_lines()):
            r.outcome[t] = "rejected"
        else:
            r.outcome[t] = "accepted" if t in r.accepted else f"rejected@{r.rejected.get(t, ('?', '?'))[0]}"
    r.info["outcome"] = r.outcome
    r.info["counters"] = {k: v for k, v in counters.items() if not k.startswith("_")}
    r.info["split_line"] = r.split_line
    r.info["calls"] = {k: len(v) for k, v in prov.by_key.items()}
    r.info["params"] = {k: [(p.get("max_tokens"), p.get("temperature")) for p in v] for k, v in prov.by_key.items()}
    r.info["sleeps"] = r.sleeps[:20]
    r.info["warnings"] = [m[:200] for m in r.log_lines(level="WARNING")][:12]
    if os.environ.get("DUMP_LOGS"):
        r.info["all_logs"] = r.logs
    return r


def run_stream(scen, handler, sleeps) -> Result:
    """mode=stream: drive tools.llm_stream directly (no Gate1Filter)."""
    import tools.llm_stream as L
    r = Result()
    r.L = L
    prov = P.FakeProvider({}, default=scen["default"])
    r.provider = prov
    real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = prov
    try:
        r.results = scen["drive"](L, prov, r)
    finally:
        urllib.request.urlopen = real_urlopen
    r.logs = handler.records
    r.sleeps = list(sleeps)
    return r


if __name__ == "__main__":
    main()
