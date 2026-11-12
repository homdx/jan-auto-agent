"""Round 355 follow-up: SLOW-1 sees the `.update(...)`, `setattr` and annotated writes that change the object make_config returns."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFIX = ("class Cfg:\n    def __init__(self, error_retry_backoff_sec):\n"
          "        self.error_retry_backoff_sec = error_retry_backoff_sec\n\n")


def _slow1():
    spec = importlib.util.spec_from_file_location("b98_check_slow1", ROOT / "contest-bench/slow1/check_slow1.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


BODIES = [
    "kw = dict(error_retry_backoff_sec=0)\n    kw.update(error_retry_backoff_sec=1)\n    return Cfg(**kw)",
    "kw = dict(error_retry_backoff_sec=1)\n    kw.update(error_retry_backoff_sec=0)\n    return Cfg(**kw)",
    'kw = {"error_retry_backoff_sec": 0}\n    kw.update({"error_retry_backoff_sec": 1})\n    return Cfg(**kw)',
    'kw = {"error_retry_backoff_sec": 1}\n    kw.update({"error_retry_backoff_sec": 0})\n    return Cfg(**kw)',
    'kw = {"error_retry_backoff_sec": 1}\n    kw.update(dict(error_retry_backoff_sec=0))\n    return Cfg(**kw)',
    "cfg = Cfg(error_retry_backoff_sec=0)\n    cfg.error_retry_backoff_sec: float = 1\n    return cfg",
    "cfg = Cfg(error_retry_backoff_sec=1)\n    cfg.error_retry_backoff_sec: float = 0\n    return cfg",
    'cfg = Cfg(error_retry_backoff_sec=0)\n    setattr(cfg, "error_retry_backoff_sec", 1)\n    return cfg',
    'cfg = Cfg(error_retry_backoff_sec=1)\n    setattr(cfg, "error_retry_backoff_sec", 0)\n    return cfg',
    "kw: dict = dict(error_retry_backoff_sec=0)\n    kw.update(error_retry_backoff_sec=1)\n    return Cfg(**kw)",
    "kw = dict(error_retry_backoff_sec=1)\n    other = dict(error_retry_backoff_sec=0)\n    other.update(error_retry_backoff_sec=0)\n    return Cfg(**kw)",
]


@pytest.mark.parametrize("body", BODIES)
def test_the_verdict_is_the_value_the_returned_object_really_has(body):
    source = "def make_config():\n    " + body + "\n"
    namespace = {}
    exec(compile(PREFIX + source, "<make_config>", "exec"), namespace)
    really_zero = namespace["make_config"]().error_retry_backoff_sec == 0
    assert _slow1().backoff_zero(source) is really_zero
