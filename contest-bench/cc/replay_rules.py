#!/usr/bin/env python3
"""CC-11: replay recorded free-model verdicts through the two acceptance rules, offline.

    python3 contest-bench/cc/replay_rules.py [--voters a b c ...] [--rule all|committed|both]

`free_votes.json` holds, for 15 free voters, a verdict per fixture claim (150 rows = 70 at base +
80 at head; 1 TRUE, -1 FALSE, 0 none, the quote already checked by code) and the fixture's truth.
A claim is *accepted* under

    all        every voter of the set committed (a 0 is an abstention that blocks), one verdict   (today's rule)
    committed  at least 3 voters committed, one verdict among the committed (a 0 does not block)  (CC-11)

and *wrong* when the accepted verdict is not the truth. The numbers in tickets/293-cc-11-*.md come from
here; a candidate's `tally` fed the same verdicts must give the same counts.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DATA = Path(__file__).with_name("free_votes.json")
QUORUM = 3

SETS = {
    "3": ["nemotron_3_ultra_550b_a55b", "muse_spark_1_3_contributor", "agnes_3_0_flash"],
    "5": ["nemotron_3_ultra_550b_a55b", "muse_spark_1_3_contributor", "agnes_3_0_flash", "step_5_preview", "hy3"],
    "7": ["nemotron_3_ultra_550b_a55b", "muse_spark_1_3_contributor", "agnes_3_0_flash", "step_5_preview", "hy3",
          "nemotron_3_super_120b_a12b", "vercel_ling_3_1_flash"],
    "9": ["nemotron_3_ultra_550b_a55b", "muse_spark_1_3_contributor", "agnes_3_0_flash", "step_5_preview", "hy3",
          "nemotron_3_super_120b_a12b", "vercel_ling_3_1_flash", "agnes_2_5_flash", "mimo_v2_6_flash"],
    # the default trio of the runbook, and a five with two voters that are wrong alone
    "default3": ["mimo_v2_6_flash", "muse_spark_1_3_contributor", "nemotron_3_ultra_550b_a55b"],
    "5weak": ["nemotron_3_ultra_550b_a55b", "muse_spark_1_3_contributor", "agnes_3_0_flash", "laguna_s_2_1",
              "north_mini_code"],
}


def accept(votes: list, rule: str, quorum: int = QUORUM) -> int:
    """+1 / -1 accepted TRUE / FALSE, 0 not accepted. *votes*: one entry per voter of the set.

    T144: a vote that is not an int in -1, 0, 1 (a bool, a float, a string, a
    value outside the set) is refused (ValueError) before anything is
    counted, including a uniform bad set — ``[2, 2, 2]`` used to come back
    accepted as +1-TRUE through the quorum check, and ``[2, 1, 1]`` was only
    refused later, by the disagreement check.
    """
    # `True == 1` and `False == 0`, and `1.0 == 1`: only a real int from the set is a vote.
    bad = [v for v in votes if type(v) is not int or v not in (-1, 0, 1)]
    if bad:
        raise ValueError(f"vote {bad[0]!r} is outside -1, 0, 1 — refusing the row")
    committed = [v for v in votes if v != 0]
    if len(set(committed)) != 1 or len(committed) < quorum:
        return 0
    if rule == "all" and len(committed) != len(votes):
        return 0
    return committed[0]


def replay(voters: list, rule: str, data: dict | None = None) -> tuple:
    data = data or json.loads(DATA.read_text(encoding="utf-8"))
    settled = wrong = 0
    for i, row in enumerate(data["claims"]):
        if not row["truth"]:
            continue
        a = accept([data["voters"][v][i] for v in voters], rule)
        if a:
            settled += 1
            wrong += a != row["truth"]
    return settled, wrong


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voters", nargs="+")
    ap.add_argument("--rule", choices=("all", "committed", "both"), default="both")
    opts = ap.parse_args(argv)
    sets = {"given": opts.voters} if opts.voters else SETS
    rules = ("all", "committed") if opts.rule == "both" else (opts.rule,)
    print(f"{'set':9} {'voters':>6}  " + "  ".join(f"{r:>9} settled/wrong" for r in rules))
    for name, vs in sets.items():
        try:
            cells = ["%d / %d" % replay(vs, r) for r in rules]
        except ValueError as exc:
            # T144: a recorded vote outside -1, 0, 1 is refused, not counted — a clear error and a nonzero exit.
            print(f"replay_rules: set {name!r}: {exc}", file=sys.stderr)
            return 2
        print(f"{name:9} {len(vs):6d}  " + "  ".join(f"{c:>23}" for c in cells))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
