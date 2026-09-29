"""KC-77 — the two-leg runbook (2legs/) is portable and its checker is honest.

The live run needs real models, so it is a script (2legs/run_2legs.sh), not a
test. What CAN rot silently is tested here, offline: every `tools.contest run`
command in HOW-WE-RUN-2LEGS.md still parses, `env.sh` derives its paths from
the checkout (no machine paths, no model names in git), `make_target.sh`
builds the demo repo and refuses to wipe anything else, and check_2legs.py
passes a correct two-leg history and fails one where leg 2 rewrote leg 1.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tools.contest.cli import _parser

REPO_ROOT = Path(__file__).resolve().parent.parent
LEGS = REPO_ROOT / "2legs"
RUNBOOK = LEGS / "HOW-WE-RUN-2LEGS.md"
COMMITTED = ["HOW-WE-RUN-2LEGS.md", "env.sh", "make_target.sh", "run_2legs.sh",
             "check_2legs.py", "01-kc-ext01-add-repr-to-calc.md",
             "02-kc-ext02-add-eq-to-calc.md"]


def _bash_blocks() -> list[str]:
    return [b.replace("\\\n", " ") for b in
            re.findall(r"```bash\n(.*?)```", RUNBOOK.read_text(), re.DOTALL)]


def _contest_runs() -> list[list[str]]:
    out = []
    for block in _bash_blocks():
        for line in block.splitlines():
            if "tools.contest run" in line:
                argv = shlex.split(line.split("#")[0])
                out.append(argv[argv.index("tools.contest") + 1:])
    return out


def test_runbook_contest_commands_parse():
    runs = _contest_runs()
    assert len(runs) >= 2, "expected round 1 and round 2 commands in the runbook"
    for argv in runs:
        args = _parser().parse_args(argv)
        assert args.models == "$MODELS", f"models must come from $MODELS: {argv}"
        assert args.target == "$TARGET"


@pytest.mark.parametrize("name", COMMITTED)
def test_no_machine_paths_or_model_ids(name):
    text = (LEGS / name).read_text()
    assert "/home/" not in text
    assert not re.search(r"\b(kenary|sensenova\d*|kenari\.id)\b", text), name


def _env(tmp_path: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("JAN", "TARGET")}
    env.update(HOME=str(tmp_path), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", **extra)
    return env


def _sh(cmd: str, env: dict, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", cmd], cwd=cwd, env=env,
                          capture_output=True, text=True)


def test_env_sh_derives_paths_from_the_checkout(tmp_path):
    r = _sh('source 2legs/env.sh >/dev/null 2>&1; echo "$JAN|$TARGET"', _env(tmp_path))
    assert r.stdout.strip() == f"{REPO_ROOT}|{REPO_ROOT.parent}/ext-demo-repo"


def test_run_2legs_needs_models(tmp_path):
    r = _sh("2legs/run_2legs.sh", _env(tmp_path, MODELS=""))
    assert r.returncode == 2 and "export MODELS" in r.stderr


def test_make_target_refuses_a_foreign_repo(tmp_path):
    foreign = tmp_path / "real"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("x")
    r = _sh("source 2legs/env.sh >/dev/null; 2legs/make_target.sh",
            _env(tmp_path, TARGET=str(foreign)))
    assert r.returncode == 2 and (foreign / "keep.txt").exists()


def _two_leg_history(tmp_path: Path, leg2_touches_repr: bool) -> Path:
    target = tmp_path / "ext-demo-repo"
    env = _env(tmp_path, TARGET=str(target))
    r = _sh("source 2legs/env.sh >/dev/null; 2legs/make_target.sh", env)
    assert r.returncode == 0, r.stderr
    repr_src = '    def __repr__(self):\n        return f"Calc(value={self.value})"\n'
    legs = {
        1: (repr_src, 'def test_repr():\n    assert repr(Calc(7)) == "Calc(value=7)"\n'),
        2: ('    def __eq__(self, other):\n        if not isinstance(other, Calc):\n'
            '            return NotImplemented\n        return self.value == other.value\n',
            "def test_eq():\n    assert Calc(3) == Calc(3)\n"),
    }
    winners = []
    for n, (code, test) in legs.items():
        calc = (target / "calc.py").read_text()
        if n == 2 and leg2_touches_repr:
            calc = calc.replace('"Calc(value=', '"Calc(value=')  # same text,
            calc = calc.replace("    def __repr__(self):", "    def __repr__(self):  # moved")
        (target / "calc.py").write_text(calc + "\n" + code)
        with (target / "tests" / "test_calc.py").open("a") as f:
            f.write("\n\n" + test)
        _sh(f"git add -A && git commit -qm leg{n}", env, target)
        sha = _sh("git rev-parse HEAD", env, target).stdout.strip()
        winners.append(f"{n} agent{n} {sha}")
        out = target / "contest-out" / ("01", "02.1")[n - 1]
        out.mkdir(parents=True)
        (out / "state.json").write_text(json.dumps(
            {"agents": [{"agent": {"name": f"agent{n}"}, "state": "READY"}]}))
        _sh(f"sed -i 's/^\\*\\*Status:\\*\\* open/**Status:** landed/' epic-tasks/0{n}-*.md"
            f" && git add epic-tasks && git commit -qm landed{n}", env, target)
    (target / "contest-out" / "winners.txt").write_text("\n".join(winners) + "\n")
    return target


def _check(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LEGS / "check_2legs.py"), str(target)],
                          capture_output=True, text=True)


def test_check_passes_a_correct_two_leg_run(tmp_path):
    r = _check(_two_leg_history(tmp_path, leg2_touches_repr=False))
    assert r.returncode == 0, r.stdout


def test_check_fails_when_leg_2_rewrote_leg_1(tmp_path):
    r = _check(_two_leg_history(tmp_path, leg2_touches_repr=True))
    assert r.returncode == 1
    assert "FAIL  leg 2 built on leg 1" in r.stdout
