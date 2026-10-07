#!/usr/bin/env python3
"""Step 1 of the claim check: a report -> claims.json, for free.

Cuts the report at its ``##``/``###`` headings and sends each section to Lenz's
/extract (free, 1000 a day, no credits).  Each section comes back as
self-contained atomic claims.  Answers are cached on disk by section text, so
a second run sends nothing.  Verdicts are NOT bought here: the models judge
them in step 2 (``scripts/claim_vote.py``).

    python3 scripts/claim_extract.py report.md --out claims.json
    python3 scripts/claim_extract.py report.md --dry-run     # no network

The key comes from LENZ_API_KEY, else it is asked for on the console.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lenz_claim_filter as lf  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", type=Path)
    ap.add_argument("--out", type=Path, default=Path("claims.json"))
    ap.add_argument("--cache", type=Path, default=lf.DEFAULT_CACHE)
    ap.add_argument("--dry-run", action="store_true", help="list the sections, no network")
    args = ap.parse_args(argv)

    sections = lf.split_sections(args.report.read_text(encoding="utf-8"))
    if args.dry_run:
        for sec in sections:
            print("-", sec.splitlines()[0][:100])
        return 0
    cache = lf.Cache(args.cache)
    key = lf.api_key()
    seen: set[str] = set()
    claims: list[dict] = []
    for sec in sections:
        for c in lf.extract_claims(sec, key, cache):
            if lf.normalize(c) not in seen:
                seen.add(lf.normalize(c))
                claims.append({"claim": c, "section": sec.splitlines()[0][:80]})
    args.out.write_text(json.dumps(claims, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(sections)} sections -> {len(claims)} claims -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
