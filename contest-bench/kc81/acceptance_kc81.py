"""contest-bench/kc81/acceptance_kc81.py — the judge's suite for KC-81 (round 128).

    KC81_WT=../rounds/128-<agent> python3 -m pytest contest-bench/kc81/acceptance_kc81.py -q -p no:xdist

Every scene is `_scene.py` in its own process against the entry's worktree: the
round runs as `cmd_run` wires it (see there), two agents prompted into a fake
Kilo that answers nothing, `kilo_silent_sec = 2`, a heartbeat every 0.4 s, the
stall edge at 600 s. Scenes are cached per session, so each runs once.

`KC81_DIAG=1` adds the diagnostic that is not scored: `contest status` names
the spell.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WT = Path(os.environ.get("KC81_WT", ".")).resolve()
SCRATCH = Path(os.environ.get("KC81_TMP", "/tmp")) / f"kc81-{WT.name}"
SCENES = ("silent", "log_grows", "one_busy", "two_spells", "off", "no_log",
          "cpu_full", "cpu_half")


@lru_cache(maxsize=None)
def scene(name: str) -> dict:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    out = SCRATCH / f"{name}.json"
    out.unlink(missing_ok=True)
    try:
        subprocess.run([sys.executable, str(HERE / "_scene.py"), str(WT), name, str(out)],
                       capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"fatal": "scene timed out"}
    try:
        return json.loads(out.read_text())
    except (OSError, ValueError):
        return {"fatal": "scene wrote nothing"}


def ok(name: str) -> dict:
    r = scene(name)
    assert "fatal" not in r, r["fatal"]
    assert not r["err"], r["err"]
    assert r["sessions"] == 2, "sessions never prompted"
    return r


def msgs(r) -> list:
    return [x["msg"] for x in r["lines"]]


# ── the silent spell ─────────────────────────────────────────────────────────

def test_silent_server_warns_exactly_once():
    r = ok("silent")
    assert len(r["lines"]) == 1, msgs(r)


def test_warning_is_a_warning_level_line():
    r = ok("silent")
    assert r["lines"] and all(x["level"] == "WARNING" for x in r["lines"]), r["lines"]


def test_warning_names_the_pid():
    r = ok("silent")
    assert r["lines"] and f"pid {r['pid']}" in r["lines"][0]["msg"], msgs(r)


def test_warning_names_the_logs_last_change_time():
    r = ok("silent")
    assert r["lines"], "no warning"
    m = r["lines"][0]["msg"]
    want = {time.strftime("%H:%M:%S", time.localtime(r["log_mtime"] + d)) for d in (-1, 0, 1)}
    assert "log idle since" in m and any(w in m for w in want), (m, want)


def test_warning_reads_cpu_off_proc():
    r = ok("silent")
    assert r["lines"], "no warning"
    assert re.search(r"cpu \d+%", r["lines"][0]["msg"]), msgs(r)


def test_warning_counts_the_live_agents_and_says_what_to_do():
    r = ok("silent")
    assert r["lines"], "no warning"
    m = r["lines"][0]["msg"]
    assert "kilo serve silent" in m
    assert re.search(r"\b2 live agents?\b", m), m
    assert f"kill {r['pid']}" in m and "--fresh" in m, m


def test_state_json_carries_server_silent():
    r = ok("silent")
    s = r["state_server_silent"]
    assert isinstance(s, dict), s
    assert s.get("pid") == r["pid"], s
    secs = [v for k, v in s.items() if k != "pid" and isinstance(v, (int, float))]
    assert secs and max(secs) >= 2, s


# ── either side moving is a live server ──────────────────────────────────────

def test_a_growing_log_is_no_warning():
    r = ok("log_grows")
    assert r["lines"] == [], msgs(r)


def test_one_agent_receiving_events_is_no_warning():
    r = ok("one_busy")
    assert r["lines"] == [], msgs(r)


def test_a_second_spell_warns_again():
    r = ok("two_spells")
    assert r["first_spell_lines"] == 1, (r["first_spell_lines"], msgs(r))
    assert len(r["lines"]) == 2, msgs(r)


def test_zero_is_off():
    r = ok("off")
    assert r["lines"] == [], msgs(r)
    assert r["state_server_silent"] in (None, False, {}), r["state_server_silent"]


def test_no_log_path_counts_events_and_says_log_unknown():
    r = ok("no_log")
    assert len(r["lines"]) == 1, msgs(r)
    assert re.search(r"log (idle since )?\?", r["lines"][0]["msg"]), msgs(r)


# ── a server under load that answers nobody is still silent ──────────────────

def _cpu(r):
    m = re.search(r"cpu (\d+)%", r["lines"][0]["msg"]) if r["lines"] else None
    return int(m.group(1)) if m else None


def test_a_server_burning_a_core_with_no_request_is_silent_and_reads_high_cpu():
    r = ok("cpu_full")
    assert len(r["lines"]) == 1, msgs(r)
    assert (_cpu(r) or 0) >= 80, msgs(r)


def test_a_server_at_moderate_load_with_no_request_is_silent_and_reads_its_cpu():
    r = ok("cpu_half")
    assert len(r["lines"]) == 1, msgs(r)
    assert 15 <= (_cpu(r) or -1) <= 85, msgs(r)


# ── it never acts ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", SCENES)
def test_the_server_lives_and_no_session_is_aborted(name):
    r = ok(name)
    assert r["alive"] or r["pid"] is None, "server pid was killed"
    assert r["aborts"] == [], r["aborts"]


# ── the key ──────────────────────────────────────────────────────────────────

def _roster(tmp_path, value):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(WT / "contest.ini")
    if value is not None:
        cp["contest"]["kilo_silent_sec"] = value
    elif cp.has_option("contest", "kilo_silent_sec"):
        cp.remove_option("contest", "kilo_silent_sec")
    p = tmp_path / "contest.ini"
    with p.open("w") as fh:
        cp.write(fh)
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from tools.contest.roster import load_roster;"
            "print(getattr(load_roster(sys.argv[2]), 'kilo_silent_sec', 'MISSING'))")
    env = dict(os.environ)
    for var in set(re.findall(r"\$\{(\w+)\}", p.read_text())):
        env.setdefault(var, "bench-dummy")     # placeholders only; never a real key
    r = subprocess.run([sys.executable, "-c", code, str(WT), str(p)], env=env,
                       capture_output=True, text=True, cwd=str(tmp_path), timeout=60)
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-400:]


def test_contest_ini_documents_the_key():
    text = (WT / "contest.ini").read_text()
    assert re.search(r"^\s*#?\s*kilo_silent_sec\s*=", text, re.M), "kilo_silent_sec not in contest.ini"


def test_roster_reads_the_key(tmp_path):
    assert float(_roster(tmp_path, "7")) == 7


def test_roster_default_is_600(tmp_path):
    assert float(_roster(tmp_path, None)) == 600


# ── diagnostic, not scored ───────────────────────────────────────────────────

@pytest.mark.skipif(not os.environ.get("KC81_DIAG"), reason="diagnostic")
def test_diag_harvesting_agents_are_no_false_alarm():
    """Every live agent in HARVESTING: pytest runs, Kilo rightly says nothing.
    The ticket says "no live agent" and does not name this case, so not scored."""
    r = ok("harvest")
    assert r["states_seen"] == ["HARVESTING"], r["states_seen"]
    assert r["lines"] == [], msgs(r)


@pytest.mark.skipif(not os.environ.get("KC81_DIAG"), reason="diagnostic")
def test_diag_status_names_the_spell():
    r = ok("silent")
    assert "silent" in r["status"].lower(), r["status"][-300:]
