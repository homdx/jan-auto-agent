#!/usr/bin/env python3
"""CC-0: run the fixture's code and observe what each claim says it does.

    python3 behaviours.py TREE      # TREE: the fixture at one commit (base or head)

Prints a JSON object ``{claim id: true|false|null}`` for every code, world, mixed
and dangling claim: the claim's statement evaluated by *running* the six modules
(``null`` when the symbol the claim names is gone). ``make_fixture.py`` computes the
key from probe strings; this is the independent second reading — a claim whose probe
matches but whose behaviour differs (or the other way round) is a wrong key.
``tests/test_claimcheck_bench.py`` compares the two at both shas.

The ticket and commit claims are about files and git, not behaviour: the key reads
those directly, so they are not here.
"""

from __future__ import annotations

import configparser
import contextlib
import copy
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types


def _parser(text=""):
    cp = configparser.ConfigParser()
    cp.read_string(text)
    return cp


def _tmp():
    # /tmp keeps the path lower-case: marker_names runs the root through norm_path.
    return tempfile.mkdtemp(dir="/tmp", prefix="ccb")


def _store(m, data=None):
    d = _tmp()
    s = m["store"].Store(os.path.join(d, "s.json"))
    s._data = dict(data or {})
    return s, d


def _raises(exc, fn, *a):
    try:
        fn(*a)
    except exc:
        return True
    return False


def _proc(code):
    return types.SimpleNamespace(returncode=code)


class _Stop(Exception):
    pass


def _forever(m, error):
    """True when retry_call keeps retrying *error* until our sleep stops it."""
    seen = []

    def sleep(_):
        seen.append(1)
        if len(seen) >= 50:
            raise _Stop

    def fn():
        raise error

    try:
        m["retry"].retry_call(fn, sleep=sleep)
    except _Stop:
        return True
    except Exception:
        return False
    return False


def _delays(m):
    waits, left = [], [3]

    def fn():
        if left[0]:
            left[0] -= 1
            raise m["retry"].TransientError("timeout")
        return "ok"

    m["retry"].retry_call(fn, delay=0.5, sleep=waits.append)
    return waits


def _wrote_outside(m):
    root = _tmp()
    name = "../" + os.path.basename(root) + "_out"
    outside = os.path.normpath(os.path.join(root, name))
    try:
        m["paths"].write_marker(root, name, "x")
    except ValueError:
        pass
    wrote = os.path.exists(outside)
    if wrote:
        os.remove(outside)
    shutil.rmtree(root, ignore_errors=True)
    return wrote


def _symlink_followed(m):
    root, out = _tmp(), _tmp()
    os.symlink(out, os.path.join(root, "link"))
    inside = m["paths"].is_inside(os.path.join(root, "link", "f"), root)
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    return not inside      # realpath would see the link leave the root


def _mtime_order(m):
    root = _tmp()
    for name, stamp in (("a.marker", 2_000_000_000), ("b.marker", 1_000_000_000)):
        path = os.path.join(root, name)
        open(path, "w").close()
        os.utime(path, (stamp, stamp))
    got = m["paths"].marker_names(root)
    shutil.rmtree(root, ignore_errors=True)
    return got == ["b.marker", "a.marker"]


def _opens_and_leaks(m):
    d = _tmp()
    path = os.path.join(d, "c.ini")
    open(path, "w").write("[a]\nb=1\n")
    kept, real_open = [], open

    def tracking(*a, **k):
        kept.append(real_open(*a, **k))
        return kept[-1]

    m["config"].open = tracking
    try:
        m["config"].read_config(path)
    finally:
        del m["config"].open
    leaked = bool(kept) and not kept[0].closed
    for handle in kept:
        handle.close()
    shutil.rmtree(d, ignore_errors=True)
    return leaked


def _saved_in_place(m):
    s, d = _store(m, {"a": 1})
    calls, real = [], (os.replace, os.rename)
    os.replace = os.rename = lambda *a, **k: calls.append(a)
    try:
        s.save()
    finally:
        os.replace, os.rename = real
    wrote = os.path.exists(s.path)
    shutil.rmtree(d, ignore_errors=True)
    return wrote and not calls


def _saved_text(m, data):
    s, d = _store(m, data)
    s.save()
    text = open(s.path, encoding="utf-8").read()
    shutil.rmtree(d, ignore_errors=True)
    return text


def _bad_json_gives_empty(m):
    s, d = _store(m)
    open(s.path, "w").write("{bad")
    try:
        got = s.load()
    except Exception:
        got = None
    shutil.rmtree(d, ignore_errors=True)
    return got == {}


def _missing_file_raises(m):
    s, d = _store(m)
    s.path = os.path.join(d, "nope.json")
    try:
        s.load()
        out = False
    except m["store"].StoreError:
        out = True
    shutil.rmtree(d, ignore_errors=True)
    return out


def _run_all_drops_rest(m):
    def ok(c):
        return "ok"

    def bad(c):
        raise ValueError("x")

    return m["pipeline"].run_all([ok, bad, ok], {}) == ["ok"]


def _run_step_retries(m):
    calls = []

    def step(c):
        calls.append(1)
        raise ValueError("x")

    try:
        m["pipeline"].run_step(step, {})
    except m["pipeline"].StepError:
        pass
    return len(calls) == 2


def _run_command_check_true(m):
    seen = {}
    real = m["pipeline"].subprocess.run

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    m["pipeline"].subprocess.run = spy
    try:
        m["pipeline"].run_command(["true"], "/tmp")
    finally:
        m["pipeline"].subprocess.run = real
    return seen


def _keyerror_swallowed(m):
    def step(context):
        raise KeyError("k")

    try:
        return m["pipeline"].run_step(step, {}) is None
    except m["pipeline"].StepError:
        return False


def _gate_applies_early(m):
    calls = []
    m["gates"].run_gate(["c"], False, calls.append)
    return calls == [["c"]]


def _deadline_after_call(m):
    called = []

    def fn():
        called.append(1)
        return "r"

    try:
        m["retry"].with_deadline(fn, 0, clock=lambda: 100)
    except TimeoutError:
        return called == [1]
    return False


def _log_step_prints(m):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m["pipeline"].log_step("s", "ok")
    return out.getvalue().strip() == "s: ok"


def _header_first_line_only(m):
    return m["pipeline"].parse_header("Files: a b\n  c d\nOther: x") == ["a", "b"]


def _count_skips_first(m):
    return m["pipeline"].count_steps([1, None, None]) == 0


def _checks(m):
    cfg = _parser("[defaults]\ntimeout=5\n[pipeline]\ntimeout=9\nworkers=x\nitems= a , b ,\nsemi=a;b\n")
    empty = _parser()
    r = {}
    # ── the 20 fixed ────────────────────────────────────────────────────
    r["f001"] = m["config"].load_timeout(cfg) == 5.0
    r["f002"] = m["config"].parse_bool("0") is True
    r["f003"] = any(i != i.strip() for i in m["config"].get_list(cfg, "items"))
    r["f004"] = m["paths"].is_inside("/a/bc", "/a/b")
    r["f005"] = m["paths"].split_ext("abc.txt")[0] != "abc"
    r["f006"] = _wrote_outside(m)
    s, d = _store(m, {"a": [1]})
    r["f007"] = s.items()["a"] is s._data["a"]
    r["f008"] = _bad_json_gives_empty(m)
    s, d = _store(m, {"z": 0})
    r["f009"] = s.get("z", "D") == "D"
    r["f010"] = _raises(KeyError, s.delete, "nokey")
    r["f011"] = _keyerror_swallowed(m)
    r["f012"] = m["pipeline"].run_command(["false"], "/tmp") == 0
    r["f013"] = _header_first_line_only(m)
    r["f014"] = _count_skips_first(m)
    r["f015"] = m["gates"].check_exit(_proc(5))
    r["f016"] = m["gates"].gate_paths(["docs/a.md"], ["docs/"]) == ["docs/a.md"]
    r["f017"] = _gate_applies_early(m)
    r["f018"] = _forever(m, m["retry"].TransientError("timeout"))
    flaky = []

    def once():
        flaky.append(1)
        if len(flaky) == 1:
            raise ValueError("permanent")
        return "ok"

    try:
        r["f019"] = m["retry"].retry_call(once, sleep=lambda _: None) == "ok"
    except ValueError:
        r["f019"] = False
    r["f020"] = m["retry"].is_transient(Exception("Timeout")) is False
    # ── the 10 left ─────────────────────────────────────────────────────
    r["f021"] = m["config"].load_workers(cfg) == 0
    r["f022"] = _opens_and_leaks(m)
    r["f023"] = m["paths"].safe_join("/r", "/etc/x") == "/etc/x"
    r["f024"] = _saved_in_place(m)
    r["f025"] = _run_all_drops_rest(m)
    r["f026"] = _log_step_prints(m)
    r["f027"] = m["gates"].gate_size(5, 5, 6) is False
    r["f028"] = m["gates"].gate_flaky(["t1"], lambda ids: 0, 2) == "PASS*2"
    r["f029"] = m["retry"].backoff(30) > 1e8
    r["f030"] = _deadline_after_call(m)
    # ── renamed (GONE) and the two regressions (NEW) ────────────────────
    named = hasattr(m["paths"], "norm_path")
    r["f031"] = m["paths"].norm_path("/A/B") == "/a/b" if named else None
    r["f032"] = m["paths"].norm_path("/a//") == "/a" if named else None
    r["f033"] = not _saved_text(m, {"a": 1}).endswith("\n")
    waits = _delays(m)
    r["f034"] = len(set(waits)) == 1
    # ── near misses ─────────────────────────────────────────────────────
    r["f035"] = m["config"].load_timeout(empty) == 60
    r["f036"] = m["config"].load_name(empty) is None
    r["f037"] = m["config"].get_list(cfg, "semi") == ["a", "b"]
    r["f038"] = _symlink_followed(m)
    r["f039"] = m["paths"].split_ext(".bashrc")[1] == ".bashrc"
    r["f040"] = _mtime_order(m)
    r["f041"] = _missing_file_raises(m)
    s, d = _store(m, {"k": 1})
    r["f042"] = _raises(Exception, s.put, "k", 2)
    s, d = _store(m, {"a": 1, "b": None})
    r["f043"] = len(s) == 1
    r["f044"] = _raises(subprocess.CalledProcessError, m["pipeline"].run_command, ["false"], "/tmp")
    r["f045"] = _run_step_retries(m)
    r["f046"] = m["pipeline"].parse_header("files: a b") == ["a", "b"]
    r["f047"] = m["gates"].check_exit(_proc(1))
    r["f048"] = "PASS" in m["gates"].summary_line("g", True)
    r["f049"] = m["retry"].backoff(0) == 1
    r["f050"] = m["retry"].with_deadline.__defaults__[0] is time.time
    # ── world: Python itself ────────────────────────────────────────────
    r["f051"] = re.search("^b$", "a\nb", re.M) is not None
    r["f052"] = subprocess.run(["false"]).returncode != 0
    shared = {"a": [1]}
    r["f053"] = copy.copy(shared)["a"] is shared["a"]
    cp = _parser()
    cp.add_section("A")
    r["f054"] = not cp.has_section("a")
    r["f055"] = json.dumps("é") == '"\\u00e9"'
    r["f056"] = bool(re.findall("^b", "a\nb", re.S))
    r["f057"] = "a//".rstrip("/") == "a/"
    r["f058"] = {"a": None}.get("a", "D") == "D"
    r["f059"] = len(range(1, 5)) == 5
    r["f060"] = issubclass(KeyboardInterrupt, Exception)
    # ── mixed: the code part AND the world part ─────────────────────────
    r["f061"] = _run_command_check_true(m).get("check") is False and r["f052"]
    r["f062"] = r["f013"] and re.search("a.b", "a\nb") is None
    r["f063"] = "é" in _saved_text(m, {"k": "é"}) and "\\u00e9" not in _saved_text(m, {"k": "é"})
    d = _tmp()
    path = os.path.join(d, "c.ini")
    open(path, "w").write("[a]\nB=1\n")
    parser = m["config"].read_config(path)
    r["f064"] = type(parser) is configparser.ConfigParser and parser.optionxform("B") == "B"
    r["f065"] = "rfind" in inspect.getsource(m["paths"].split_ext) and "abc".rfind(".") == -1
    import pytest
    r["f066"] = m["gates"].check_exit(_proc(5)) and pytest.ExitCode.INTERNAL_ERROR.value == 5
    r["f067"] = m["retry"].retry_call.__defaults__[1] is time.sleep and time.sleep(0.0) is None
    s, d = _store(m, {"a": [1]})
    r["f068"] = s.items()["a"] is s._data["a"] and dict(s._data)["a"] is not s._data["a"]
    # ── dangling: the thing is not there ────────────────────────────────
    r["f069"] = os.path.exists("scheduler.py")
    r["f070"] = hasattr(m["store"].Store, "compact")
    r["f071"] = hasattr(m["gates"], "check_coverage")
    r["f072"] = hasattr(m["retry"], "retry_async")
    r["f073"] = hasattr(m["config"], "load_profile")
    r["f074"] = hasattr(m["paths"], "expand_user")
    return r


def main(argv=None) -> int:
    tree = (argv or sys.argv[1:] or ["."])[0]
    os.chdir(tree)
    sys.path.insert(0, os.getcwd())
    sys.dont_write_bytecode = True
    import importlib
    m = {name: importlib.import_module(name)
         for name in ("config", "paths", "store", "pipeline", "gates", "retry")}
    json.dump(_checks(m), sys.stdout, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
