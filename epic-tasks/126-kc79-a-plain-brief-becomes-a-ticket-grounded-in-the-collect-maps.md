# KC-79 — A plain brief becomes a ticket grounded in the collect maps

**Status:** landed `551ac20` + `e6628da` (indented Acceptance) + `cfb712b` (contest.ini drafter) on 2026-09-30 — round 126, winner 25/26 on contest-bench/kc79 taken as-is, 26/26 with the fix
**Severity:** HIGH
**File:** `tools/contest/draft.py`
**Symbol:** `draft_ticket`, `lint_ticket`
**Round:** 126
**Size:** M
**Source:** 2legs rounds on `ext-demo-repo`: every ticket was written by hand;
`2legs/prepare_task.sh` only pastes the brief into a fixed template
**Depends on:** KC-76 (round 123, `--target`)
**Also touches:** `tools/contest/cli.py`, `tests/test_contest_draft.py`

---

## Why

An operator says in one line what they want — "speed up `pytest tests`",
"raise coverage of `calc.py`", "fix the docs", "find bugs in the parser" — and
today someone has to turn it into a ticket by hand: which files, which symbols,
which tests, what counts as done. The facts for that already exist: `--collect`
writes `MODULE_MAP.md`, `TEST_MAP.md`, `RISK_INDEX.md` and `artifact.json` under
`<repo>/.collect/`. Nothing feeds them to a model that writes the ticket.

## What to build

A new subcommand:

    python3 -m tools.contest draft --target REPO "brief" [--round NN] [--out FILE]

1. **Collect.** Call `tools.collect.cli.action_collect(root, llm_call=None)` on
   REPO. A fresh `.collect/` costs nothing; no Pass B (no LLM) is run here.
2. **Draft.** One LLM call. The prompt carries: the brief verbatim; the text of
   `MODULE_MAP.md`, `TEST_MAP.md`, `RISK_INDEX.md` (each cut to a budget, the
   budget from config, never a hard-coded model window); the ticket format —
   the header fields of this very ticket (`Status`, `Severity`, `File`,
   `Symbol`, `Round`, `Size`, `Also touches`) and the sections `## Why`,
   `## What to build`, `## Acceptance`, `## Rules`. The model is the one named
   by `[contest] draft_llm_profile` in the contest config, resolved the same way
   as `gate_llm_profile` (roster.py); no model name or URL in code. Unset →
   a clear error naming the key.
3. **Lint** (`lint_ticket(text, artifact) -> list[str]`, pure, no LLM). Each
   problem is one string. It checks:
   - every header field above is present, `Status` is `open`;
   - every path in `File` / `Also touches` either exists in REPO or is a new
     file under a directory that exists (a new test file is normal);
   - every name in `Symbol` is found in `artifact.json` for that file, unless
     the file is new;
   - `## Acceptance` exists and holds at least one command in a code block;
   - `Round` is NN and no `epic-tasks/NN-*.md` exists yet in REPO.
   On problems the draft is sent back once with the list appended; still
   failing → exit 2, the problems printed, the last draft saved next to the
   output with `.rejected.md`.
4. **Write.** A clean ticket goes to `REPO/epic-tasks/NN-<slug>.md` (NN: next
   free number when `--round` is not given; slug from the title). No commit,
   no branch change in this ticket — KC-80 does that. Print the path.

The brief is never rewritten into a different task: speed, coverage, docs,
tests, bug hunt are all the same code path; what differs is what the model
reads in the maps. A brief about test speed gets an acceptance line that
measures before and after (`pytest --durations=20`, wall time), since collect
holds no timings.

## Acceptance

    python3 -m pytest tests/test_contest_draft.py -q

The tests use a fake LLM (a callable returning canned text) and a tiny repo
built in `tmp_path` with a real `action_collect` run over it:
- a good canned ticket lands at `epic-tasks/NN-*.md` and lint returns `[]`;
- a ticket naming a missing file / a symbol not in the artifact / no
  `## Acceptance` / a taken NN yields one lint message each;
- a bad first draft and a good second one → written, the fake saw the lint list;
- two bad drafts → exit 2 and `.rejected.md` saved, nothing in `epic-tasks/`;
- no `draft_llm_profile` → error naming the key, no LLM call;
- the maps are cut to the configured budget (a huge `TEST_MAP.md` does not
  reach the prompt whole).

## Rules

- No real provider in tests; no network.
- No model name, URL or key in code or tests' asserted strings.
- `2legs/prepare_task.sh` is not changed in this ticket.
- The full suite stays green: `python3 -m pytest .smoke_tests/ .regression_tests/`.
