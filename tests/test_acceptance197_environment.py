"""AUDIT-8 — the round-197 ``server`` fixture gives ``NO_PROXY`` / ``no_proxy`` back.

The fixture is loaded by path from ``acceptance_197.py`` and exercised through a real
nested pytest session (``pytester``): a module-scoped fixture only restores the
environment when its scope is actually finalized, which is exactly the behaviour here.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "contest-bench" / "197" / "acceptance_197.py"
PROXIES = ("NO_PROXY", "no_proxy")

import http.server as _http_server  # noqa: E402
_REAL_TSHS = _http_server.ThreadingHTTPServer

#: Rebinds the real module-scoped ``server`` fixture into the inner test module.
HEADER = f"""import importlib.util, os, sys
import pytest
from pathlib import Path
_spec = importlib.util.spec_from_file_location("acceptance197_under_test", {str(TARGET)!r})
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)
server = _mod.server
"""


def _set_before(monkeypatch, state):
    """state maps a variable to its value; a missing key (or a ``None``) means absent."""
    for var in PROXIES:
        want = state.get(var)
        if want is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, want)


def _run_inner(pytester, body):
    module = pytester.makepyfile(HEADER + body)
    result = pytester.runpytest(str(module), "-p", "no:cacheprovider", "-q")
    outcomes = result.parseoutcomes()
    return result, outcomes


@pytest.fixture(autouse=True)
def _stdlib_restored():
    """The failing-build test swaps a stdlib attribute in the inner session; belt and braces."""
    yield
    _http_server.ThreadingHTTPServer = _REAL_TSHS


def test_both_absent_before_are_absent_after(pytester, monkeypatch):
    _set_before(monkeypatch, {})
    _, outcomes = _run_inner(pytester, """
def test_during(server):
    assert os.environ["NO_PROXY"] == "127.0.0.1"
    assert os.environ["no_proxy"] == "127.0.0.1"
""")
    assert outcomes.get("passed") == 1, outcomes
    assert "NO_PROXY" not in os.environ and "no_proxy" not in os.environ


def test_different_original_values_are_restored_each_to_itself(pytester, monkeypatch):
    _set_before(monkeypatch, {"NO_PROXY": "big.company", "no_proxy": "small.company"})
    _, outcomes = _run_inner(pytester, """
def test_during(server):
    assert os.environ["NO_PROXY"] == "127.0.0.1"
    assert os.environ["no_proxy"] == "127.0.0.1"
""")
    assert outcomes.get("passed") == 1, outcomes
    assert os.environ["NO_PROXY"] == "big.company"
    assert os.environ["no_proxy"] == "small.company"


def test_one_absent_one_set(pytester, monkeypatch):
    _set_before(monkeypatch, {"no_proxy": "only.one"})
    _, outcomes = _run_inner(pytester, """
def test_during(server):
    assert os.environ["NO_PROXY"] == "127.0.0.1"
    assert os.environ["no_proxy"] == "127.0.0.1"
""")
    assert outcomes.get("passed") == 1, outcomes
    assert "NO_PROXY" not in os.environ
    assert os.environ["no_proxy"] == "only.one"


def test_an_exception_in_the_consumer_still_restores(pytester, monkeypatch):
    _set_before(monkeypatch, {"NO_PROXY": "before", "no_proxy": "before"})
    _, outcomes = _run_inner(pytester, """
def test_consumer_blows_up(server):
    raise RuntimeError("consumer blew up")
""")
    assert outcomes.get("failed") == 1, outcomes
    assert os.environ["NO_PROXY"] == "before"
    assert os.environ["no_proxy"] == "before"


def test_a_failing_server_build_still_restores(pytester, monkeypatch):
    _set_before(monkeypatch, {"NO_PROXY": "before", "no_proxy": "before"})
    _, outcomes = _run_inner(pytester, """
class Boom:
    def __init__(self, *a, **k):
        raise OSError("no sockets today")
_mod.http.server.ThreadingHTTPServer = Boom

def test_build_fails(server):
    pass

def test_env_restored():
    assert os.environ["NO_PROXY"] == "before"
    assert os.environ["no_proxy"] == "before"
""")
    assert outcomes.get("errors") == 1 and outcomes.get("passed") == 1, outcomes
    assert os.environ["NO_PROXY"] == "before"
    assert os.environ["no_proxy"] == "before"


def test_the_socket_is_closed_after_the_fixture(pytester, monkeypatch):
    _set_before(monkeypatch, {})
    port_file = pytester.path / "port.txt"
    closed_file = pytester.path / "closed.txt"
    _, outcomes = _run_inner(pytester, f"""
import http.server as _hs
_real = _hs.ThreadingHTTPServer
class Recorded(_real):
    def server_close(self):
        Path({str(closed_file)!r}).write_text("closed", encoding="utf-8")
        super().server_close()
_hs.ThreadingHTTPServer = Recorded

@pytest.fixture(autouse=True, scope="module")
def _restore_stdlib():
    yield
    _hs.ThreadingHTTPServer = _real

def test_capture(server):
    Path({str(port_file)!r}).write_text(server.rsplit(":", 1)[1], encoding="utf-8")
""")
    assert outcomes.get("passed") == 1, outcomes
    assert closed_file.read_text(encoding="utf-8") == "closed", "server_close was never called"
    port = int(port_file.read_text(encoding="utf-8"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_a_thread_that_cannot_start_restores_the_variables_and_closes_the_socket(pytester, monkeypatch):
    """The server is built, its thread cannot start: no hang, the socket is released, both variables are back."""
    _set_before(monkeypatch, {"NO_PROXY": "upper.example"})
    report = pytester.path / "report.json"
    pytester.makeconftest(f"""
import json, os, socket
def pytest_sessionfinish(session, exitstatus):
    d = json.load(open({str(report)!r})) if os.path.exists({str(report)!r}) else {{}}
    d["after"] = [os.environ.get("NO_PROXY"), os.environ.get("no_proxy")]
    if "port" in d:
        try:
            socket.create_connection(("127.0.0.1", d["port"]), timeout=1).close()
            d["listening"] = True
        except OSError:
            d["listening"] = False
    json.dump(d, open({str(report)!r}, "w"))
""")
    pytester.makepyfile(test_thread_that_cannot_start=HEADER + f"""
import http.server as _hs, json, types
_real = _hs.ThreadingHTTPServer

class Recorded(_real):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        json.dump({{"port": self.server_address[1]}}, open({str(report)!r}, "w"))

class Boom:
    def __init__(self, *a, **k):
        pass
    def start(self):
        raise RuntimeError("cannot start the thread")

_hs.ThreadingHTTPServer = Recorded
_mod.threading = types.SimpleNamespace(Thread=Boom)

def test_never_reached(server):
    raise AssertionError("the fixture must fail")
""")
    # a plain subprocess: pytester points HOME at its temp folder, which hides the user's site-packages
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path(pytest.__file__).resolve().parents[1]), os.environ.get("PYTHONPATH", "")]))
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "test_thread_that_cannot_start.py"],
                          cwd=pytester.path, env=env, capture_output=True, text=True, timeout=60)
    assert "1 error" in done.stdout, done.stdout[-400:]
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["after"] == ["upper.example", None]
    assert data["listening"] is False, "the socket of a server whose thread never started is still open"
