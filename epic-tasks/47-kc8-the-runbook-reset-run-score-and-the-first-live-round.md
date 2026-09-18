# KC-8 — `docs/kilo-contest/RUN-THE-KILO-CONTEST.md`: reset, run, score — and the first live round on record

**Status:** queued — after KC-7 (round 46). Not a contest: size S, one author, landed by hand.  
**Severity:** MEDIUM  
**File:** `docs/kilo-contest/RUN-THE-KILO-CONTEST.md` (new)  
**Symbol:** —  
**Round:** 47  
**Size:** S  
**Source:** `docs/RUN-THE-COMPETITION.md` and `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md` are the house style: one page, a map table (stage / who / command / what you take away), the prompt verbatim, the hard gates, what is deliberately out. The contest replaces their stages 1–2 and hands over to 3–5 unchanged.  
**Depends on:** KC-7.  
**Also touches:** `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md` (one paragraph at the top of Stage 1 pointing here), `docs/kilo-contest/PROBE.md` (link), `AGENTS.md`, `docs/PIPELINE-DOCS-INDEX.md` (one row)

---

## What must change

1. The page, in this order: **the map** (reset → run → score → judge →
   merge, with the two new commands and the three old ones); **what you
   need once** (`contest.ini`, `contest.local.ini` with the gate key, the
   Kilo extension installed, `kilo models` to pick ids); **stage R —
   reset** (`scripts/contest_reset.sh`, clones, what "fresh/reset/clone"
   means); **stage RUN** (`--dry-run` first, then `run`; what the console
   shows; `status`; `--resume` after a Ctrl-C or a provider outage; where
   every file lands, from `EPIC-KC.md` §3); **the gate** (what the second
   model sees, how to read `decisions.jsonl` and the "Decisions worth a
   look" section, what to do when the gate blocked something the ticket
   needed — add a `tmp_roots` entry or rerun, never `always`); **hand-over**
   (the three commands `SUMMARY.md` prints; a pointer to stages 3–5 of the
   epic runbook and to `contest-bench/README.md`); **known limits** (no
   port discovery for the extension's server; serial rounds; mechanical
   harvest only).

2. **The first live round, recorded.** Run one small open ticket with the
   default roster (the three `kenary` models) and a fourth model as the
   gate; paste the resulting `SUMMARY.md` at the bottom of the page under
   "First round on record", with the decisions list and one paragraph on
   what the gate did. This is the acceptance test of the epic; the
   numbers are the baseline the next roster is compared against.

## Acceptance

- [ ] The page exists with the sections above; every command in it was
      executed once while writing it (`scripts/check_runbook.py` if it
      applies to this page's shape, otherwise state it in the commit).
- [ ] `RUN-THE-EPIC-COMPETITION.md` Stage 1 points here in one paragraph;
      `PIPELINE-DOCS-INDEX.md` lists the page.
- [ ] "First round on record" holds a real `SUMMARY.md` with ≥ 1 READY.

## Out of scope

- Anything the code does not do yet.

## Ground rules

- Do not edit `epic-tasks/`.
- One commit, no push.
