#!/usr/bin/env python3
"""purge_memory_wall_records.py — drop the context-memory records Kilo's own wall wrote.

    scripts/purge_memory_wall_records.py                 what it would drop, writes nothing
    scripts/purge_memory_wall_records.py --apply         drop them; the old file is kept
    scripts/purge_memory_wall_records.py --repo ../qwen26   another checkout's contest-out/

Round 146: Kilo's own ``ContextOverflowError: Compaction exhausted: context still
exceeds model limits after 3 attempts`` was stored in `contest-out/context-memory.json`
as the model's window — a number nobody but the session itself said (apertus-70b's
declared 32 000 became 22 561, apertus-v1.5-70b-thinking's 30 241). The runner no
longer writes them (`runner._KILO_WALL_RE`); the records it wrote before stay in
the file for `context_memory_days` and size the next agents. This finds them.

A record goes only when both hold, so a provider's own overflow is never touched:
  * it names no limit, no prompt and no output — the shape of a record whose only
    number is `last_ok`; and
  * the agent's own `contest-out/<round>/<agent>/events.jsonl` holds a `Compaction
    exhausted` error that completed within `--window` seconds before the record was
    written (a record whose round folder is gone stays: nothing proves it).

Run it only when no round is running: a live runner adds records to the same file.
The file as it was is copied to `context-memory.json.bak-before-wall-purge`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

#: The message Kilo ends a compaction it could not finish with.
WALL_TEXT = "Compaction exhausted"
BACKUP_SUFFIX = ".bak-before-wall-purge"
_COMPLETED_MS = re.compile(r'"completed": (\d{13})')


def _has_numbers(record: dict) -> bool:
    return bool(record.get("limit") or record.get("prompt") or record.get("output"))


def _wall_times(events: Path) -> list:
    """Seconds (epoch) at which the agent's messages ended in Kilo's wall."""
    times = []
    try:
        with events.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if WALL_TEXT not in line:
                    continue
                found = _COMPLETED_MS.search(line)
                if found:
                    times.append(int(found.group(1)) / 1000.0)
    except OSError:
        return []
    return times


def is_wall_record(record: dict, contest_out: Path, window: float, cache: dict) -> bool:
    """True when *record* was written by a `Compaction exhausted` of its own agent."""
    if not isinstance(record, dict) or _has_numbers(record):
        return False
    at = record.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    key = (str(record.get("round")), str(record.get("agent")))
    if key not in cache:
        cache[key] = _wall_times(contest_out / key[0] / key[1] / "events.jsonl")
    return any(0.0 <= at - done <= window for done in cache[key])


def split(records: list, contest_out: Path, window: float = 60.0) -> tuple:
    """``(keep, drop)`` of *records*, each in file order."""
    cache: dict = {}
    keep, drop = [], []
    for record in records:
        (drop if is_wall_record(record, contest_out, window, cache) else keep).append(record)
    return keep, drop


def _line(record: dict) -> str:
    when = time.strftime("%m-%d %H:%M", time.gmtime(float(record.get("at") or 0)))
    return (f"{when} r{record.get('round')} {record.get('agent')} "
            f"{record.get('provider')}/{record.get('model')} "
            f"last_ok={record.get('last_ok')} grew={record.get('grew')}")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1],
                        help="the checkout whose contest-out/ to read (default: this one)")
    parser.add_argument("--memory", type=Path, default=None,
                        help="the memory file (default: <repo>/contest-out/context-memory.json)")
    parser.add_argument("--window", type=float, default=60.0,
                        help="seconds a Compaction exhausted may precede its record (60)")
    parser.add_argument("--apply", action="store_true", help="write; the default is a dry run")
    args = parser.parse_args(argv)

    contest_out = args.repo / "contest-out"
    memory = args.memory or contest_out / "context-memory.json"
    try:
        records = json.loads(memory.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        print(f"{memory}: {err}", file=sys.stderr)
        return 1
    if not isinstance(records, list):
        print(f"{memory}: not a list of records", file=sys.stderr)
        return 1

    keep, drop = split(records, contest_out, args.window)
    print(f"{len(records)} records: keep {len(keep)}, drop {len(drop)}")
    for record in drop:
        print("  drop", _line(record))
    if not args.apply or not drop:
        return 0

    backup = memory.with_name(memory.name + BACKUP_SUFFIX)
    shutil.copy2(memory, backup)
    tmp = memory.with_name(memory.name + ".tmp")
    tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(memory)
    print(f"written; the old file is {backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
