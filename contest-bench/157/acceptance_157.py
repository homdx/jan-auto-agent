"""Judge's acceptance suite for round 157, written from the ticket alone.

`arena run start`'s lock file must exist from before the runner child is
spawned: a child that reads it at once finds a pid, a spawn that fails leaves no lock behind.
Checked by behaviour only (`rounds.SPAWN` stubs, `cli.main`, `rounds.round_alive`);
no private helper of any entry is named.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/157/acceptance_157.py -n 0 -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_arena_run_start import _draft, _ticket, repo  # noqa: E402,F401
from tools.arena import cli, rounds  # noqa: E402

READS_AT_ONCE = """
import os
from pathlib import Path
Path("seen-lock").write_text(Path(".arena/locks/7.pid").read_text())
Path("my-pid").write_text(str(os.getpid()))
raise SystemExit({code})
"""


def _lock(repo):
    return repo / ".arena" / "locks" / "7.pid"


def _slow(script, delay=0.5, before=None):
    """SPAWN: Popen the script, sleep *delay* before handing the child back."""
    seen = {}

    def spawn(line, cwd):
        if before:
            before(seen)
        child = subprocess.Popen([sys.executable, "-c", script], cwd=cwd)
        time.sleep(delay)
        return child
    spawn.seen = seen
    return spawn


def test_a_child_reading_the_lock_at_once_finds_a_pid(repo, monkeypatch):
    _draft(repo, _ticket())
    monkeypatch.setattr(rounds, "SPAWN", _slow(READS_AT_ONCE.format(code=0)))
    assert cli.main(["run", "start", "7"]) == 0
    assert (repo / "seen-lock").read_text().strip().isdigit()
    assert not _lock(repo).exists()


def test_the_lock_exists_before_spawn_is_called_and_names_a_live_pid(repo, monkeypatch):
    _draft(repo, _ticket())

    def before(seen):
        seen["exists"] = _lock(repo).exists()
        text = _lock(repo).read_text().strip() if seen["exists"] else ""
        seen["text"] = text
        if text.isdigit():
            try:
                os.kill(int(text), 0)
                seen["alive"] = True
            except OSError:
                seen["alive"] = False

    spawn = _slow("raise SystemExit(0)", before=before)
    monkeypatch.setattr(rounds, "SPAWN", spawn)
    assert cli.main(["run", "start", "7"]) == 0
    assert spawn.seen["exists"] and spawn.seen["text"].isdigit()
    assert spawn.seen["alive"] is True
    assert not _lock(repo).exists()


def test_the_lock_ends_up_holding_the_childs_pid(repo, monkeypatch):
    _draft(repo, _ticket())
    stub = """
import os, time
from pathlib import Path
time.sleep(1.5)
Path("seen-lock").write_text(Path(".arena/locks/7.pid").read_text())
Path("my-pid").write_text(str(os.getpid()))
"""
    monkeypatch.setattr(rounds, "SPAWN", _slow(stub, delay=0.2))
    assert cli.main(["run", "start", "7"]) == 0
    assert (repo / "seen-lock").read_text().strip() == (repo / "my-pid").read_text().strip()



def test_a_spawn_that_raises_oserror_is_a_refusal_with_no_lock(repo, monkeypatch, capsys):
    _draft(repo, _ticket())

    def spawn(line, cwd):
        raise OSError("no such runner")
    monkeypatch.setattr(rounds, "SPAWN", spawn)
    assert cli.main(["run", "start", "7"]) == 2
    assert "no such runner" in capsys.readouterr().err
    assert not _lock(repo).exists()
    assert not list(_lock(repo).parent.glob("7.pid*"))


def test_a_spawn_that_raises_anything_leaves_no_lock(repo, monkeypatch):
    _draft(repo, _ticket())

    def spawn(line, cwd):
        raise RuntimeError("boom")
    monkeypatch.setattr(rounds, "SPAWN", spawn)
    with pytest.raises(RuntimeError):
        cli.main(["run", "start", "7"])
    assert not list(_lock(repo).parent.glob("7.pid*"))


def test_no_temp_file_is_left_after_a_run(repo, monkeypatch):
    _draft(repo, _ticket())
    monkeypatch.setattr(rounds, "SPAWN", _slow("raise SystemExit(0)", delay=0.1))
    assert cli.main(["run", "start", "7"]) == 0
    assert list(_lock(repo).parent.iterdir()) == []


@pytest.mark.parametrize("script,expected", [
    ("raise SystemExit(0)", 0), ("raise SystemExit(1)", 1),
    ("raise SystemExit(9)", 1), ("raise SystemExit(2)", 1),
])
def test_the_exit_mapping_is_unchanged(repo, monkeypatch, script, expected):
    _draft(repo, _ticket())
    monkeypatch.setattr(rounds, "SPAWN", _slow(script, delay=0.1))
    assert cli.main(["run", "start", "7"]) == expected
    assert not _lock(repo).exists()
