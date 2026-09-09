#!/usr/bin/env python3
"""Split the collect epics into one self-contained ticket per file.

`docs/collect-epics/` holds three multi-ticket documents. The competition
machinery (`scripts/next_task.py` / `scripts/append_task.py`) reads a folder of
`NN-*.md` tickets with `**File:**` / `**Symbol:**` / `**Severity:**` fields —
the same shape `make_jira_tasks.py` emits. This script produces that folder,
so an epic round runs on exactly the machinery a fix round already uses.

    python3 scripts/split_epic_tickets.py
    python3 scripts/split_epic_tickets.py --out epic-tasks/ --order short

Sources (heading level differs per file, on purpose — it is what they use):

    docs/collect-epics/EPIC-M-metrics.md       ## M1 — ...
    docs/collect-epics/PLAN-v2.md              #### V1 — ...
    docs/collect-epics/EPIC-L-live-findings.md ## L1 — ...

The round order is `ROUND_ORDER` below and it is the point of the script:
ticket N assumes the merged result of tickets 1..N-1. Editing that list is how
you re-plan a round.

Exit codes: 0 wrote the folder · 1 a source file or a ticket id is missing.
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EPICS = os.path.join("docs", "collect-epics")

# (path, heading regex). The regex must capture (id, title).
SOURCES = [
    (os.path.join(EPICS, "EPIC-M-metrics.md"),
     re.compile(r"^##\s+(M\d+)\s+—\s+(.+?)\s*$", re.M)),
    (os.path.join(EPICS, "PLAN-v2.md"),
     re.compile(r"^####\s+(V\d+)\s+—\s+(.+?)\s*$", re.M)),
    (os.path.join(EPICS, "EPIC-L-live-findings.md"),
     re.compile(r"^##\s+(L\d+)\s+—\s+(.+?)\s*$", re.M)),
]

# The rounds. Each entry is one round: every agent does this ticket, you pick
# the best, merge it, then the next round starts from the merged tree.
ROUND_ORDER = [
    "L2",                                    # calibration — tiny, one right answer
    "M1",                                    # the baseline. Blocks everything after it.
    "L1",                                    # measured 10% of gate-1 calls, no plan dependency
    "V1", "V2", "V3", "V4", "V5", "V6",      # stage 1 — the pack
    "M2",                                    # did stage 1 move the redundancy number?
    "V7", "V8", "V9", "V10",                 # stage 2 — producer cost and honesty
    "M3",                                    # go/no-go: V12 and V13 only run if this says so
    "V11", "V12", "V13",                     # stage 3 — bug hunting
    "M4", "M5",                              # runtime counters, then the A/B
    "V14", "V15",                            # stage 4 — docs mode
    "M6",                                    # outcome against the adjudicated corpus
    "L3",                                    # measurement-only, can run any time
]

SHORT_PATH = ["L2", "M1", "L1", "V1", "V2", "V3", "V5", "V7", "V9", "V11", "M2"]

SEVERITY = {"highest": "CRITICAL", "critical": "CRITICAL", "high": "HIGH",
            "medium": "MEDIUM", "low": "LOW", "lowest": "LOW"}

GROUND_RULES = """## Ground rules — these are the scoring criteria

1. **Verify before implementing.** Grep the live source for what this ticket
   claims. It was written against `competition` at `68b78a0`; if the code has
   moved, the ticket is the stale one — say so and adapt rather than forcing it.
2. **`CollectBridge._shrink` is not to be modified, reordered, or replaced.**
   New work runs *before* it, never instead of it. A diff that touches a line
   inside `_shrink` fails this round regardless of what else it does.
3. **One local commit for this ticket alone.** Never `git push`. No
   opportunistic refactors bundled in.
4. **Ships a test.** New behaviour → `tests/`; a fix to something that used to
   be wrong → `tests_bugfix/`. If you add a `tests/test_*.py`, run
   `python3 scripts/sync_test_tiers.py` so the `.smoke_tests/` mirror exists
   (a pre-commit hook enforces it).
5. **Run the suite as four separate invocations** — combining the roots in one
   `pytest` call produces ~362 false errors from a conftest collision:
   ```bash
   for d in tests tests_bugfix .smoke_tests .regression_tests; do python3 -m pytest "$d" -q --timeout=180; done
   ```
   `python` is not on PATH — use `python3`.
6. **Fail open.** A broken artifact, a malformed config key or an absent model
   degrades to "no collect data" and never raises into a run. Everything added
   here inherits that.
7. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled. `is_safe()` keeps reading only `guarded_accesses` /
   `FAIL_OPEN_REGISTRY` / `CONTRACTS`.
8. **Never run anything against a live provider config.** Copy
   `agents_128k.ini` to a scratch path and point `base_url` at a stub before any
   measurement run.
"""


def slug(text, limit=48):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "ticket"


def carve(path, heading_re):
    """Every (id, title, body) in one source file, body = up to the next heading."""
    full = os.path.join(HERE, path)
    if not os.path.exists(full):
        print(f"missing source: {path}", file=sys.stderr)
        return {}
    text = open(full, encoding="utf-8").read()
    hits = list(heading_re.finditer(text))
    out = {}
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        body = text[m.end():end].strip()
        body = re.sub(r"\n-{3,}\s*$", "", body).strip()
        out[m.group(1)] = {"title": m.group(2).strip(), "body": body, "src": path}
    return out


def meta(body):
    """`**Priority:** High · **Size:** S · **Files:** `a.py`, `b.py`` — may wrap."""
    head = body[:600]
    prio = re.search(r"\*\*Priority:\*\*\s*([A-Za-z]+)", head)
    size = re.search(r"\*\*Size:\*\*\s*([A-Za-z0-9()\- ]+?)\s*(?:·|\*\*|\n)", head)
    files = re.search(r"\*\*Files?:\*\*\s*(.+?)(?:\n\s*\n|\*\*Depends|\n\*\()", head, re.S)
    deps = re.search(r"\*\*Depends on:\*\*\s*(.+?)\s*(?:\n|\*\()", head)
    paths = re.findall(r"`([^`]+)`", files.group(1)) if files else []
    paths = [p for p in paths if "/" in p or p.endswith(".py") or p.endswith(".md")]
    return {
        "severity": SEVERITY.get((prio.group(1) if prio else "").lower(), "MEDIUM"),
        "size": (size.group(1).strip() if size else "—"),
        "files": paths or ["—"],
        "depends": (deps.group(1).strip() if deps else ""),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="epic-tasks", help="folder to write NN-*.md into")
    ap.add_argument("--order", choices=("full", "short"), default="full",
                    help="full = every ticket; short = PLAN-v2 §6's short path")
    a = ap.parse_args()

    tickets = {}
    for path, rx in SOURCES:
        tickets.update(carve(path, rx))
    if not tickets:
        print("no tickets parsed — are the source files where they should be?", file=sys.stderr)
        return 1

    order = SHORT_PATH if a.order == "short" else ROUND_ORDER
    missing = [t for t in order if t not in tickets]
    if missing:
        print(f"these ids are in the order but not in any source: {', '.join(missing)}",
              file=sys.stderr)
        return 1
    extra = sorted(set(tickets) - set(order))

    out = os.path.join(HERE, a.out)
    os.makedirs(out, exist_ok=True)

    index = [f"# Epic round — {len(order)} tickets, in order", "",
             "One ticket per round. Every agent does the same ticket against the same",
             "tree; you merge the winner; the next round starts from the merged tree.",
             "The loop and the scorecard: `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`.",
             "",
             "| # | id | severity | size | ticket | primary file |",
             "|---|---|---|---|---|---|"]

    for i, tid in enumerate(order, 1):
        t = tickets[tid]
        m = meta(t["body"])
        name = f"{i:02d}-{tid.lower()}-{slug(t['title'])}.md"
        doc = [f"# {tid} — {t['title']}\n",
               f"**Severity:** {m['severity']}  ",
               f"**File:** `{m['files'][0]}`  ",
               f"**Symbol:** `—`  ",
               f"**Round:** {i} of {len(order)}  ",
               f"**Size:** {m['size']}  ",
               f"**Source:** `{t['src']}` § {tid}  "]
        if m["depends"]:
            doc.append(f"**Depends on:** {m['depends']}  ")
        if len(m["files"]) > 1:
            doc.append("**Also touches:** " + ", ".join(f"`{p}`" for p in m["files"][1:]) + "  ")
        doc += ["", "---", "", t["body"], "", "---", "", GROUND_RULES]

        open(os.path.join(out, name), "w", encoding="utf-8").write("\n".join(doc))
        index.append(f"| {i} | `{tid}` | {m['severity']} | {m['size']} | "
                     f"[{name}]({name}) | `{m['files'][0]}` |")
        print(f"  {name}")

    if extra:
        index += ["", f"**Not in this round:** {', '.join('`'+e+'`' for e in extra)} — "
                  "present in the source epics but left out of the order above."]
    index += ["", "## Working these", "",
              "Ground rules are restated inside every ticket. Two carry the round:",
              "`CollectBridge._shrink` stays byte-identical, and nothing runs against a",
              "live provider config.", ""]
    open(os.path.join(out, "INDEX.md"), "w", encoding="utf-8").write("\n".join(index))
    print(f"\n{len(order)} ticket(s) -> {a.out}/  (INDEX.md written)"
          + (f"; {len(extra)} not scheduled: {', '.join(extra)}" if extra else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
