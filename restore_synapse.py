#!/usr/bin/env python3

"""restore_synapse.py — regenerate synopsis.md + story_bible.md from chapter
files WITHOUT a full --auto run (no architect/coder/gates).

Use it to iterate fast on fact extraction quality: edit a chapter, run this,
eyeball books/story_bible.md and books/synopsis.md.

Fixes vs the original:
  * loads the config with inline_comment_prefixes=(';','#') — the bare
    ConfigParser() crashed on "story_bible_creative = true ; comment";
  * processes chapters IN ORDER (so the bible accumulates and the CR-25-1
    "only new facts" + CR-25-2 gender continuity actually get exercised — a
    single chapter shows no dedup);
  * --fresh wipes synopsis.md/story_bible.md first for a clean test;
  * prints the resulting bible + synopsis.

Examples:
  python3 restore_synapse.py --base ../books --fresh
  python3 restore_synapse.py --base ../books chapter_1.txt chapter_2.txt
"""
from __future__ import annotations

import argparse
import configparser
import re
import sys
from pathlib import Path

from tools.auto.summary_memory import make_summary_memory
from tools.auto.story_bible import make_story_bible


def _chapter_key(name: str) -> tuple:
    m = re.search(r"(\d+)", name)
    return (int(m.group(1)) if m else 1_000_000, name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="../books", help="book directory")
    ap.add_argument("--config", default="agents_32k.ini")
    ap.add_argument("--fresh", action="store_true",
                    help="delete synopsis.md / story_bible.md before running")
    ap.add_argument("chapters", nargs="*",
                    help="chapter files (relative to --base); default: all chapter_*.{txt,md} in order")
    args = ap.parse_args()

    base = Path(args.base)
    # AUTO-FIX (medium-priority audit): this script is the fast edit-run-
    # eyeball loop for iterating on story-bible/synopsis quality — a typo'd
    # --base used to fail confusingly deep inside the first chapter read
    # instead of immediately and clearly.
    if not base.exists():
        print(f"error: --base directory does not exist: {base}", file=sys.stderr)
        sys.exit(1)
    if not base.is_dir():
        print(f"error: --base is not a directory: {base}", file=sys.stderr)
        sys.exit(1)

    # IMPORTANT: same loader as main.py / controller.py — strips inline ; and # comments.
    # encoding="utf-8" is REQUIRED on Windows: Python there defaults to the
    # locale codec (cp1252) and crashes on the UTF-8 bytes in the .ini
    # (UnicodeDecodeError: 'charmap' codec can't decode byte 0x90 ...).
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    # AUTO-FIX (medium-priority audit): a malformed --config file used to
    # raise a raw configparser.Error with no indication of which file, deep
    # into argument setup instead of at the point of failure.
    try:
        read_files = cfg.read(args.config, encoding="utf-8")
    except configparser.Error as exc:
        print(f"error: --config {args.config} is not a valid .ini file: {exc}", file=sys.stderr)
        sys.exit(1)
    # AUTO-FIX: ConfigParser.read() skips missing files silently, so a
    # typo'd --config ran with an entirely empty config instead of failing.
    if not read_files:
        print(f"error: --config file not found: {args.config}", file=sys.stderr)
        sys.exit(1)

    if args.fresh:
        for f in ("synopsis.md", "story_bible.md"):
            p = base / f
            if p.exists():
                p.unlink()
                print(f"[fresh] removed {p}")

    # Resolve chapters in numeric order.
    if args.chapters:
        chapters = list(args.chapters)
    else:
        found: list[str] = []
        for pat in ("chapter_*.txt", "chapter_*.md", "chapter*.txt", "chapter*.md"):
            found += [p.name for p in base.glob(pat)]
        chapters = sorted(set(found), key=_chapter_key)
    if not chapters:
        print(f"no chapter files found in {base}")
        return
    print(f"chapters (in order): {chapters}")

    # Build the same memory components the pipeline uses.
    mem = make_summary_memory(cfg, base_dir=str(base), task_mode="creative")

    active = cfg.get("api", "active", fallback="local")
    sec = f"api_{active}"
    bible = make_story_bible(
        cfg,
        base_url=cfg.get(sec, "base_url", fallback="http://localhost:11434"),
        api_key=cfg.get(sec, "api_key", fallback="ollama"),
        model=cfg.get(sec, "model", fallback="llama3.1:8b"),
        api_format=cfg.get(sec, "api_format", fallback="ollama"),
        base_dir=str(base),
    )
    if bible is None:
        print("NOTE: story_bible_creative=false — bible disabled; only synopsis will be built.")

    for ch in chapters:
        # AUTO-FIX (medium-priority audit): an unreadable/missing chapter
        # file (typo in the positional chapters list, permission issue,
        # non-UTF-8 file) used to kill the whole run partway through —
        # annoying specifically because this script exists for a fast
        # iterate-and-eyeball loop where you want partial progress, not an
        # all-or-nothing crash.
        try:
            text = (base / ch).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  [!] skipping {ch}: could not read ({exc})", file=sys.stderr)
            continue
        print(f"\n=== processing {ch} ({len(text)} chars) ===")
        mem.update(ch, base_dir=str(base))           # synopsis section
        if bible is not None:
            bible.update(text)                        # durable facts (extract→verify→merge)

    print("\n================ story_bible.md ================")
    bp = base / "story_bible.md"
    print(bp.read_text(encoding="utf-8") if bp.exists() else "(none)")
    print("\n================ synopsis.md ===================")
    sp = base / "synopsis.md"
    print(sp.read_text(encoding="utf-8") if sp.exists() else "(none)")


if __name__ == "__main__":
    main()
