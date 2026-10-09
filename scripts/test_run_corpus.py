#!/usr/bin/env python3
"""Corpus of the test runs agents make in a round: one JSON row per pytest command.

    python3 scripts/test_run_corpus.py contest-out/900 [contest-out/901 …] [--out corpus.jsonl] [--summary]

Reads `<round>/<agent>/events.jsonl` (Kilo's tool events) and writes, for every
`bash` call that runs pytest: the agent, the command, the roots and flags it
parsed into, the wall time (tool start/end), whether another pytest of the same
agent was running at the same moment, how many edit/write calls came before it
(`edits_before`: the tree-change counter a result cache would key on), and the
head/tail of the output. `--summary` prints the repeat/overlap/timing digest.

The corpus is the replay input for a test-output cache in the permission policy:
a repeat is a row whose (normalised command, edits_before) equals an earlier
row's, and its `duration_s` is what the cache would have saved.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import shlex
import sys

_EDIT_TOOLS = ("edit", "write", "patch", "multiedit")


def parse_command(cmd: str) -> dict:
    """The pytest part of *cmd*: roots, flags, and what the shell does to the output."""
    pipes = [w for w in re.split(r"\s*\|\s*", cmd)[1:]]
    head = re.split(r"\s*\|\s*", cmd)[0]
    try:
        words = shlex.split(head)
    except ValueError:
        words = head.split()
    roots, flags = [], []
    seen_pytest = False
    skip = False
    for i, w in enumerate(words):
        if skip:
            skip = False
            continue
        if not seen_pytest:
            seen_pytest = w == "pytest" or (w == "-m" and i + 1 < len(words) and words[i + 1] == "pytest")
            continue
        if w == "pytest":
            continue
        if re.fullmatch(r"\d*>+&?\d*", w):  # a redirect (`2>&1`), shell plumbing, not a root
            continue
        if w.startswith("-"):
            flags.append(w)
            if w in ("-n", "-k", "-p", "-m", "--tb", "--maxfail", "--durations") and i + 1 < len(words) and not words[i + 1].startswith("-"):
                flags.append(words[i + 1])
                skip = True
        else:
            roots.append(w)
    return {"roots": roots, "flags": flags, "pipes": pipes, "timeout_prefix": bool(re.match(r"\s*timeout\s", cmd))}


def normalise(row: dict) -> str:
    """The cache key's command part: roots and flags, order-insensitive, shell plumbing dropped."""
    return json.dumps([sorted(row["roots"]), sorted(row["flags"])])


def rows_for(round_dir: str) -> list[dict]:
    models = {}
    try:
        ent = json.load(open(os.path.join(round_dir, "entrants.json")))
        for e in ent if isinstance(ent, list) else ent.get("entrants", []):
            models[e.get("name")] = e.get("model")
    except Exception:
        pass
    out = []
    for f in sorted(glob.glob(os.path.join(round_dir, "*", "events.jsonl"))):
        agent = f.split(os.sep)[-2]
        parts, order = {}, []
        for line in open(f, errors="replace"):
            try:
                ev = json.loads(line)["event"]
            except Exception:
                continue
            if ev.get("type") != "message.part.updated":
                continue
            p = ev["properties"]["part"]
            if p.get("type") == "tool":
                if p["id"] not in parts:
                    order.append(p["id"])
                parts[p["id"]] = p
        edits = 0
        agent_rows = []
        for pid in order:
            p = parts[pid]
            st = p.get("state", {})
            if p.get("tool") in _EDIT_TOOLS and st.get("status") == "completed":
                edits += 1
                continue
            cmd = (st.get("input") or {}).get("command", "")
            if p.get("tool") != "bash" or "pytest" not in cmd or st.get("status") != "completed":
                continue
            t = st.get("time", {})
            start, end = t.get("start"), t.get("end")
            parsed = parse_command(cmd)
            output = st.get("output") or ""
            agent_rows.append({
                "round": os.path.basename(round_dir.rstrip("/")), "agent": agent, "model": models.get(agent),
                "command": cmd, **parsed, "start_ms": start, "end_ms": end,
                "duration_s": round((end - start) / 1000, 1) if start and end else None,
                "edits_before": edits, "output_head": output[:200], "output_tail": output[-300:],
                "output_len": len(output),
            })
        for r in agent_rows:
            r["overlaps_own_run"] = any(
                o is not r and o["start_ms"] and r["start_ms"] and o["start_ms"] < r["end_ms"] and r["start_ms"] < o["end_ms"]
                for o in agent_rows)
            r["key"] = normalise(r)
        out += agent_rows
    return out


def summary(rows: list[dict]) -> str:
    lines = [f"{len(rows)} pytest runs"]
    by_agent = collections.defaultdict(list)
    for r in rows:
        by_agent[(r["round"], r["agent"])].append(r)
    for (rnd, agent), rs in sorted(by_agent.items()):
        seen, repeats, saved = {}, 0, 0.0
        for r in sorted(rs, key=lambda r: r["start_ms"] or 0):
            k = (r["key"], r["edits_before"])
            if k in seen:
                repeats += 1
                saved += r["duration_s"] or 0
            seen[k] = r
        overl = sum(1 for r in rs if r["overlaps_own_run"])
        lines.append(f"{rnd}/{agent}: {len(rs)} runs, {repeats} repeats on an unchanged tree "
                     f"(would save {saved:.0f} s), {overl} overlapping its own other run; "
                     f"{sum(r['duration_s'] or 0 for r in rs):.0f} s of pytest")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("rounds", nargs="+")
    ap.add_argument("--out")
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args(argv)
    rows = [r for d in a.rounds for r in rows_for(d)]
    if a.out:
        with open(a.out, "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)
    if a.summary or not a.out:
        print(summary(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
