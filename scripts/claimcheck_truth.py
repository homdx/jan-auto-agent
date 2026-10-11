#!/usr/bin/env python3
"""CC-8: a claim check's `votes.json` into the competition's files — `truth.csv` and a pending list.

    python3 scripts/claimcheck_truth.py votes.json --out truth.csv --pending pending.md
    python3 scripts/claimcheck_truth.py votes.json --out validate1/truth.csv --append   # idempotent

A row is written only for a settled claim (``unanimous`` and TRUE -> ``REAL``, FALSE ->
``FALSE``); everything else is the pending list, the stage-3 queue. `votes.json` holds no pack
and no resolved anchors, so with a recorded ``target.sha`` both are rebuilt there, offline
(`tools.claimcheck.truth.rebuild`: a read-only worktree of that exact sha under ``--scratch``;
no model, no network, never fetched). ``--target`` overrides the recorded repository (a bench's
clone may be gone). A target that cannot be opened is a warning: the keys then come from the
cited chunk ids and the pending list names those.

``--append`` adds only rows whose ``finding`` is not in the file with the same ``truth``;
a ``finding`` already there with another ``truth`` is not overwritten: it is printed as a
conflict and the exit code is 3. A refusal (unreadable votes.json) is exit 2. The CSV and
the pending list are written atomically (a temporary file, a rename).
"""

from __future__ import annotations

import argparse
import configparser
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.claimcheck import truth as cc_truth  # noqa: E402


def profile_models(votes: dict, repo_root: Path) -> tuple:
    """({ref: model}, the ini parser): every voter ref that is an ini section naming a ``model``
    (``--profiles vote_agnes``), so `family_of` reads the model, not the section name."""
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    parser.read([repo_root / "contest.ini", repo_root / "contest.local.ini"], encoding="utf-8")
    out = {}
    for ref in cc_truth.live_voters(votes) + cc_truth.dead_voters(votes):
        if parser.has_section(ref) and parser.get(ref, "model", fallback=""):
            out[ref] = parser.get(ref, "model")
    return out, parser


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("votes", type=Path, help="claim_vote.py --out votes.json")
    ap.add_argument("--out", type=Path, required=True, help="the truth CSV to write")
    ap.add_argument("--pending", type=Path, help="the stage-3 pending list (default: pending.md next to --out)")
    ap.add_argument("--append", action="store_true",
                    help="add to an existing truth CSV: same finding + truth is skipped, another truth is a conflict (exit 3)")
    ap.add_argument("--checked-by", metavar="TEXT", help="the checked_by column (default claimcheck:<families>@<sha7>)")
    ap.add_argument("--target", metavar="REPO", help="the repository to rebuild at the recorded sha (default: votes.json's target.repo)")
    ap.add_argument("--scratch", type=Path, help="where the rebuild's worktree goes (default: claim-check-out/scratch)")
    ap.add_argument("--no-rebuild", action="store_true", help="do not open the target: keys and pending from votes.json alone")
    ap.add_argument("--repo-root", type=Path, default=Path.cwd(), help="where contest.ini / contest.local.ini live")
    args = ap.parse_args(argv)

    try:
        votes = json.loads(args.votes.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"claimcheck_truth: {args.votes}: {exc}", file=sys.stderr)
        return 2
    if isinstance(votes, list):          # an empty or bare list: no claims
        votes = {"claims": votes, "results": []}
    if not isinstance(votes, dict) or not isinstance(votes.get("claims", []), list):
        print(f"claimcheck_truth: {args.votes}: not a claim_vote.py votes.json", file=sys.stderr)
        return 2
    models, parser = profile_models(votes, args.repo_root)
    target = votes.get("target") or {}

    resolved = packs = base_sha = None
    repo = args.target or target.get("repo")
    if target.get("sha") and repo and not args.no_rebuild and votes.get("claims"):
        scratch = args.scratch or args.repo_root / "claim-check-out" / "scratch"
        try:
            resolved, packs, base_sha = cc_truth.rebuild(votes, repo, scratch=scratch, parser=parser)
        except Exception as exc:   # noqa: BLE001 — a target that cannot be opened is a warning, not a stop
            print(f"claimcheck_truth: warning: cannot rebuild at {target['sha'][:7]} of {repo}: "
                  f"{str(exc).splitlines()[0] if str(exc) else type(exc).__name__}; "
                  "keys from the cited chunk ids", file=sys.stderr)

    by = args.checked_by or cc_truth.checked_by(votes, models=models, base_sha=base_sha)
    rows = cc_truth.to_truth_rows(votes, checked_by=by, resolved=resolved)
    for finding in cc_truth.disagreements(rows):
        # two statements about one place (one true, one false) share the key; harvest_report.load_truth
        # keeps one row per finding (the last), so a reviewer is scored on whichever came last
        print(f"claimcheck_truth: note: claims about {finding} settled both REAL and FALSE; both rows "
              "kept, harvest_report --truth reads only the last: settle that finding by hand",
              file=sys.stderr)
    try:
        written, conflicts = cc_truth.write_truth(args.out, rows, append=args.append)
        pending = args.pending or args.out.with_name("pending.md")
        cc_truth.atomic_write(pending, cc_truth.pending_markdown(votes, packs, models=models))
    except OSError as exc:
        print(f"claimcheck_truth: cannot write: {exc}", file=sys.stderr)
        return 2
    for finding, old, new in conflicts:
        print(f"CONFLICT {finding}: {args.out} says {old}, this run says {new} (kept {old})")
    n_pending = len(cc_truth.pending_rows(votes))
    print(f"truth: {len(written)} rows {'appended to' if args.append else 'in'} {args.out} "
          f"({len(rows)} settled, {len(rows) - len(written) - len(conflicts)} already there, "
          f"{len(conflicts)} conflicts); pending: {n_pending} in {pending}; checked_by {by}")
    return 3 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
