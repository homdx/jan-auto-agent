#!/usr/bin/env python3
"""What the round's test cache did (ticket 212): hits, recorded runs, seconds not run.

    python3 scripts/test_cache_stats.py contest-out/199 [--watch]

Reads `<round>/test-cache.jsonl` (the runs it recorded) and every agent's
`decisions.jsonl` (a hit is a decision with `layer = "test-cache"`; its reason
holds the original run's seconds). `--watch` reprints every 20 s.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import time

_SECONDS = re.compile(r"\((?:run by [^,]+, )?([0-9.]+) s\)")


def stats(round_dir: str) -> str:
    recorded, by_agent = 0, {}
    try:
        for line in open(os.path.join(round_dir, "test-cache.jsonl"), errors="replace"):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") == "classify":
                continue
            recorded += 1
    except OSError:
        pass
    hits = saved = asked_bash = 0
    for f in sorted(glob.glob(os.path.join(round_dir, "*", "decisions.jsonl"))):
        agent = f.split(os.sep)[-2]
        a_hits = a_saved = 0
        for line in open(f, errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("permission") == "bash":
                asked_bash += 1
            if d.get("layer") == "test-cache":
                a_hits += 1
                m = _SECONDS.search(d.get("reason") or "")
                a_saved += float(m.group(1)) if m else 0.0
        if a_hits:
            by_agent[agent] = (a_hits, a_saved)
        hits += a_hits
        saved += a_saved
    lines = [f"{round_dir}: {recorded} runs recorded, {hits} hits, {saved:.0f} s of pytest not run, {asked_bash} bash asks"]
    lines += [f"  {a}: {h} hits, {s:.0f} s" for a, (h, s) in sorted(by_agent.items())]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("round_dir")
    ap.add_argument("--watch", action="store_true")
    a = ap.parse_args()
    while True:
        print(stats(a.round_dir), flush=True)
        if not a.watch:
            return 0
        time.sleep(20)
        print()


if __name__ == "__main__":
    raise SystemExit(main())
