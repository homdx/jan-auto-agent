#!/usr/bin/env python3
"""2legs/check_2legs.py — KC-77: did both legs of the two-leg run do their job?

Reads what ``run_2legs.sh`` left in ``$TARGET`` and answers with exact,
on-disk facts — no LLM, no network — in the style of scripts/check_runbook.py:

* each round produced at least one READY agent;
* both tickets are marked landed and the winners are in history;
* ``Calc`` behaves as tickets 01 and 02 demand, and the target's tests pass;
* leg 2 built on leg 1: its winning commit did not touch ``__repr__``.

Usage:  python3 2legs/check_2legs.py <TARGET>
Exit code 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROUNDS = {1: "01", 2: "02.1"}

_BEHAVIOUR = """
from calc import Calc
assert repr(Calc(0)) == "Calc(value=0)"
assert repr(Calc(42)) == "Calc(value=42)"
assert (Calc(3) == Calc(3)) is True
assert (Calc(3) == Calc(4)) is False
assert Calc(3).__eq__(3) is NotImplemented
"""


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def _winners(target: Path) -> dict[int, tuple[str, str]]:
    path = target / "contest-out" / "winners.txt"
    out: dict[int, tuple[str, str]] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            n, agent, sha = line.split()
            out[int(n)] = (agent, sha)
    return out


def checks(target: Path) -> list[tuple[bool, str]]:
    res: list[tuple[bool, str]] = []
    winners = _winners(target)

    for n, sub in ROUNDS.items():
        state = target / "contest-out" / sub / "state.json"
        if not state.is_file():
            res.append((False, f"leg {n}: no {state}"))
            continue
        ready = [a["agent"]["name"] for a in json.loads(state.read_text())["agents"]
                 if a["state"] == "READY"]
        res.append((bool(ready), f"leg {n}: READY = {', '.join(ready) or 'none'}"))

        ticket = next((target / "epic-tasks").glob(f"0{n}-*.md"), None)
        landed = bool(ticket) and "**Status:** landed" in ticket.read_text()
        res.append((landed, f"leg {n}: ticket 0{n} marked landed"))

        if n in winners:
            agent, sha = winners[n]
            ok = _run(["git", "merge-base", "--is-ancestor", sha, "HEAD"], target).returncode == 0
            res.append((ok, f"leg {n}: winner {agent} {sha[:8]} is in history"))
        else:
            res.append((False, f"leg {n}: no winner recorded in contest-out/winners.txt"))

    beh = _run([sys.executable, "-c", _BEHAVIOUR], target)
    res.append((beh.returncode == 0, "Calc: __repr__ and __eq__ behave as tickets demand"
                + ("" if beh.returncode == 0 else f" — {beh.stderr.strip().splitlines()[-1]}")))

    tests = (target / "tests" / "test_calc.py").read_text()
    for name in ("test_repr", "test_eq"):
        res.append((re.search(rf"^def {name}\b", tests, re.M) is not None,
                    f"tests/test_calc.py defines {name}"))
    py = _run([sys.executable, "-m", "pytest", "-q", "-p", "no:xdist",
               "-p", "no:cacheprovider", "tests"], target)
    res.append((py.returncode == 0, "target tests pass"))

    if 2 in winners:
        diff = _run(["git", "show", "--format=", winners[2][1], "--", "calc.py"], target).stdout
        touched = [l for l in diff.splitlines()
                   if l[:1] in "+-" and not l.startswith(("+++", "---")) and "__repr__" in l]
        res.append((not touched, "leg 2 built on leg 1: its commit leaves __repr__ untouched"))
    return res


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.split("Usage:")[1].split("\n")[0].strip(), file=sys.stderr)
        return 2
    results = checks(Path(argv[0]).resolve())
    for ok, msg in results:
        print(f"{'PASS' if ok else 'FAIL'}  {msg}")
    failed = sum(not ok for ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
