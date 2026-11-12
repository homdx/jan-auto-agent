"""Round 354 follow-up: a queue file that cannot be removed refuses the agent's workspace; SLOW-1 judges the object make_config returns."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _script(rel):
    spec = importlib.util.spec_from_file_location("b97_" + Path(rel).stem, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _held(monkeypatch, name):
    real = Path.unlink

    def unlink(self, *a, **k):
        if self.name == name:
            raise PermissionError("held")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)


def test_a_progress_file_that_cannot_be_removed_refuses_the_workspace(tmp_path, monkeypatch):
    from tools.contest import workspace
    runs = tmp_path / "runs" / "a"
    runs.mkdir(parents=True)
    (runs / "PROGRESS.csv").write_text("ticket,finding,outcome,commit,note\n1,,done,abc,\n", encoding="utf-8")
    _held(monkeypatch, "PROGRESS.csv")
    with pytest.raises(workspace.WorkspaceError, match="PROGRESS.csv"):
        workspace._empty_runs_dir(tmp_path, "a")


def test_a_progress_file_that_can_be_removed_is_removed(tmp_path):
    from tools.contest import workspace
    runs = tmp_path / "runs" / "a"
    runs.mkdir(parents=True)
    (runs / "PROGRESS.csv").write_text("x", encoding="utf-8")
    workspace._empty_runs_dir(tmp_path, "a")
    assert not (runs / "PROGRESS.csv").exists()


HEAD = "def make_config():\n"
CASES = [
    ("cfg = Cfg(error_retry_backoff_sec=0)\n    other = Cfg(error_retry_backoff_sec=1)\n    return cfg", True),
    ("cfg = Cfg(error_retry_backoff_sec=1)\n    other = Cfg(error_retry_backoff_sec=0)\n    return cfg", False),
    ("cfg = Cfg(error_retry_backoff_sec=1)\n    cfg.error_retry_backoff_sec = 0\n    return cfg", True),
    ("cfg = Cfg(error_retry_backoff_sec=0)\n    cfg.error_retry_backoff_sec = 2\n    return cfg", False),
    ("cfg = Cfg()\n    cfg.error_retry_backoff_sec = 0\n    other = Cfg(error_retry_backoff_sec=3)\n    return cfg", True),
    ("d = {'error_retry_backoff_sec': 0}\n    k = Cfg(error_retry_backoff_sec=1)\n    return Cfg(**d)", True),
    ("return {'error_retry_backoff_sec': 0}", True),
    ("return {'error_retry_backoff_sec': 1}", False),
    ("return Cfg(error_retry_backoff_sec=0)", True),
    ("return Cfg(error_retry_backoff_sec=1)", False),
    ("def inner():\n        return Cfg(error_retry_backoff_sec=1)\n    return Cfg(error_retry_backoff_sec=0)", True),
]


@pytest.mark.parametrize("body,expected", CASES)
def test_slow1_judges_the_returned_configuration(body, expected):
    mod = _script("contest-bench/slow1/check_slow1.py")
    assert mod.backoff_zero(HEAD + "    " + body + "\n") is expected
