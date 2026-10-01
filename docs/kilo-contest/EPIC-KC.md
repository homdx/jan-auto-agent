# EPIC KC — the Kilo contest: N models, one ticket, N patches side by side

**Prepared:** 2026-09-17
**Component:** `tools/contest/` (new) driving the Kilo Code headless server; reuses `scripts/next_task.py`, `scripts/append_task.py`, `scripts/judge_epic_round.py`, `contest-bench/`
**Branch:** `tickets`
**Basis:** every mechanism below was exercised live by `scripts/kilo_hello.py` on 2026-09-17 — see [`PROBE.md`](PROBE.md). Nothing in this plan rests on an API that has not been seen working.

---

## 1. Goal

One ticket in, N ready-to-apply patches out, no human in the loop between.

```
ticket (epic-tasks/NN-*.md)  +  roster (contest.ini)  +  base commit
        │
        ▼
  intake ─► workspaces ─► one Kilo session per model ─► wait ─► harvest ─► rework? ─► export
                                                          ▲                   │
                                                          └───── same session ┘
        │
        ▼
contest-out/<NN>/{<agent>.patch …, entrants.json, SUMMARY.md, state.json}   → contest-bench / judge_epic_round.py
```

The operator's job shrinks to three commands: reset, run, score. The scoring
side already exists (`judge_epic_round.py`, `contest-bench/`); this epic
builds the production side and stops exactly where the scoring side starts.

## 2. What the probe settled (read `PROBE.md` for the payloads)

| question | answer | consequence |
|---|---|---|
| can we pick provider + model per session over HTTP? | yes — `POST /session` with `{"providerID","id"}`, `prompt_async` with `{"providerID","modelID"}` | roster is a list of `provider/model` strings |
| how do we know the model is done? | `session.idle` for that `sessionID`, consumed from a cursor **after** the prompt | one wait primitive, reused for every turn |
| can we send it back for rework? | yes — a second `prompt_async` into the same session; context is kept | rework = message into the same session, no re-creation |
| where is the boundary? | the session `directory`; inside is free, outside raises `permission.asked` | session directory = the agent's worktree |
| can we answer over HTTP? | `POST /permission/{id}/reply` `once|reject`; the model sees our message as a tool error | our policy is the approver; the reason text reaches the model |
| does `external_directory` see command arguments? | yes — `patterns: ["/tmp/*"]` **and** `metadata.command` | no bash parsing needed to know the directory; the command is available for the LLM gate |
| do models route around a rejection? | mistral retried with `workdir: /tmp` | judge every event, never only the first |

## 3. Architecture

### Modules (`tools/contest/`, nothing in `tools/auto/` changes)

| module | role | ticket |
|---|---|---|
| `kilo_client.py` | server spawn/attach, session create, prompt, event tap with cursor, permission/question replies, messages, diff, abort | KC-1 |
| `roster.py` | `contest.ini`: `[contest]` limits, `[contest.agent.<name>]` models, `[contest_gate_llm]` profile | KC-2 |
| `policy.py` | the approver: server rules → mechanical → **LLM safety gate on a different model** → fail-closed | KC-3 |
| `workspace.py` + `scripts/contest_reset.sh` | one worktree (or an existing clone) per agent at the base commit, idempotent | KC-4 |
| `gates.py` + `harvest.py` | `judge_epic_round.py`'s core made importable; READY / REWORK(reasons) / GAVE_UP | KC-5 |
| `runner.py` | per-agent state machine: prompt → wait → harvest → rework in the same session; parallel pool; `state.json` | KC-6 |
| `export.py` + `cli.py` | patches, `entrants.json`, `SUMMARY.md`, session exports; `python3 -m tools.contest run` with intake, `--dry-run`, `--resume` | KC-7 |
| `docs/kilo-contest/RUN-THE-KILO-CONTEST.md` | the operator's page | KC-8 |

### Data on disk

```
contest.ini                          roster + limits + gate profile (committed, no keys — keys via env or a git-ignored override)
contest-out/<NN>/                    one folder per round (git-ignored)
  state.json                         every agent's state after every transition — `--resume` reads it
  <agent>/events.jsonl               every SSE event of that session, wall-clocked (the probe's format)
  <agent>/decisions.jsonl            every permission decision: layer, verdict, reason, elapsed, gate model
  <agent>/turns.jsonl                prompt sent, idle reached, harvest verdict, per turn
  <agent>.patch                      git format-patch base..branch (only for READY and GAVE_UP-with-commit)
  <agent>.session.json               GET /session/{id}/message dump
  entrants.json                      contest-bench input, names = agents
  SUMMARY.md                         the table
../rounds/<NN>-<agent>/              worktrees (outside the repo tree), branch contest/<NN>/<agent>
  runs/<agent>/PROGRESS.csv          written by the agent via append_task.py (git-ignored, as today)
```

### The session

Created with `directory = <worktree>`, the roster's model, and these rules:

```
* allow · external_directory ask · doom_loop ask · bash ask for each `ask_commands` pattern · bash deny for each `deny_commands` pattern
```

`bash` stays `allow` (the probe showed `bash: ask` turns every `pytest` and
`git` into a round trip). The `deny_commands` patterns (`git push*`,
`sudo *`, `rm -rf /*`, …) are refused by Kilo itself, no round trip, no
LLM. The `ask_commands` patterns catch shell writes that do not name a path
argument — redirects, `tee`, `cp`/`mv`/`ln`/`rsync`/`dd` destinations and
installer commands — and raise `bash` instead of falling through to an
invisible `external_directory` that cannot parse redirect targets.
Everything else that leaves the worktree arrives as `external_directory` and
goes through the policy.

## 4. The approver — three layers, the third is a second model

Every `permission.asked` / `permission.v2.asked` event of a session is
decided by `policy.decide(event, ctx)`; the reply (`once` or `reject`) and
the reason go back to Kilo and the model reads the reason.

| layer | decides when | verdict | LLM call |
|---|---|---|---|
| 0 server rules | command matches `deny_commands` | Kilo denies | none |
| 1 mechanical | every path in `patterns` / `metadata.directories` is inside the worktree or inside a `tmp_roots` glob → allow; any path under a hard denylist (`/`, `$HOME` itself, `~/.ssh`, `~/.config`, the repo's `.git/hooks`, any other worktree of the round) → reject; `permission == doom_loop` → reject; `permission == bash` with no path outside the worktree/tmp_roots (a redirect, `2>&1`, a relative write, or a bare command like `pytest -q`) → allow | final | none |
| 2 LLM safety gate | everything layer 1 could not settle (a path outside both lists, a `bash` ask with an outside path) | `allow` or `reject` with a one-line reason | **one call to the gate model** |

The gate model is **not** the agent's model. It is a named profile
(`[contest] gate_llm_profile = contest_gate_llm`) resolved with
`tools.auto.llm_profile.resolve_llm_profile` — the same mechanism Gate 1
uses for its presence check — and called through
`tools.llm_stream.request_completion` with `response_format` on. It receives:
the permission (`permission`, `patterns`, `metadata.command`,
`metadata.description`, `metadata.directories`), the worktree path,
`tmp_roots`, the ticket's title and `**File:**` list, and the last five
`tool` parts of the session (what the agent was doing). It answers one JSON
object: `{"verdict": "allow"|"reject", "reason": "<one line>"}`.

Fail-closed, always: transport error, timeout, non-JSON, a verdict outside
the enum, the per-session budget exhausted (`gate_max_calls_per_session`,
default 20) → `reject`, reason `gate unavailable: <why>`. A gate that cannot
be reached never turns into an allow.

Every decision, all three layers, is one line in `decisions.jsonl`. The
round's `SUMMARY.md` counts them per agent: asked / allowed / rejected /
gated / gate-failed. A round where the gate rejected something the ticket
needed is visible there, not lost.

## 5. The loop — stop, check, continue

```
PROMPTED ──wait──► IDLE ──harvest──► READY ─────────────────────► export
   ▲                 │                  │
   │                 │                  ├─ REWORK(reasons), attempt < max_rework ─► prompt_async(same session, reasons) ─┐
   │                 │                  │                                                                                 │
   └─────────────────┼──────────────────┴─ REWORK, attempts exhausted ─► GAVE_UP (patch still taken if a commit exists)   │
                     │                                                                                                    │
                     ├─ session.error ─► ERROR                                                                            │
                     ├─ no event for idle_event_timeout_sec, or turn_timeout_sec ─► abort ─► STALLED                     │
                     └─ 3 questions in one turn ─► abort ─► STALLED                                                       │
                                                                                                                          │
   ◄──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

`wait` is the probe's `wait_idle`: a cursor over the event tap, every event
consumed once, permissions answered by the policy on the way, questions
rejected and counted. `harvest` is mechanical and runs the same checks the
round is scored on (`gates.py`): a `DONE` row in `runs/<agent>/PROGRESS.csv`
with a commit, exactly one commit over the base, nothing pushed, a test file
in the diff, `CollectBridge._shrink` byte-identical, no file outside the
ticket's list without a reason. Each failed check becomes one line of the
rework message; the agent fixes it in the same session and records again.

## 6. Outputs and the hand-over

The run stops when every agent is in a terminal state (READY, GAVE_UP,
STALLED, ERROR). `export.py` writes the folder in §3. `entrants.json` is
exactly what `contest-bench/harness/setup_worktrees.py` reads, and the
worktrees are exactly what `scripts/judge_epic_round.py --worktree` scores.
Nothing is merged, nothing is pushed, nothing is judged — that is the
operator's stage 3–5 in `RUN-THE-EPIC-COMPETITION.md`, unchanged.

Exit status of `run`: 0 when at least one agent is READY, 2 when none is,
1 on an operator error (intake failed, server did not start).

## 7. Tickets

Eight, in dependency order. Each is a normal round of the epic
competition (`epic-tasks/40-*.md` … `47-*.md`); every agent implements the
same ticket against the same tree; the winner is merged and becomes the
next round's base. Every ticket ships its tests against a **fake Kilo
server** (`tests/_kilo_fake.py`, built in KC-1 from the probe's recorded
events) and a **stubbed gate model**; no ticket's tests touch a live
provider or a live `kilo`.

| round | id | what lands | size |
|---|---|---|---|
| 40 | KC-1 | `kilo_client.py` + the fake server | M |
| 41 | KC-2 | `roster.py` + `contest.ini` | S |
| 42 | KC-3 | `policy.py` — three layers, LLM gate on a second model, fail-closed | M |
| 43 | KC-4 | `workspace.py` + `scripts/contest_reset.sh` | M |
| 44 | KC-5 | `gates.py` (extracted from `judge_epic_round.py`) + `harvest.py` | M |
| 45 | KC-6 | `runner.py` — the loop, the pool, `state.json` | M |
| 46 | KC-7 | `export.py` + `cli.py` — intake, `--dry-run`, `--resume`, the output folder | M |
| 47 | KC-8 | the runbook | S |

The first live run (the operator's, after KC-7) uses the roster the probe
used — `kenary/laguna-s-2-1:free`, `kenary/mistral-medium-3-5:free`,
`kenary/hy3:free` — on a small open ticket, with the gate on a fourth
model.

## 8. Not in this epic

- Merging, judging, cherry-picking — stages 3–5 of the existing runbook.
- Attaching to the VS Code extension's own `kilo serve` automatically
  (`--attach URL` is supported; port discovery is not).
- An LLM review of the *patch* before READY — harvest is mechanical; the
  scoring side reads code.
- Parallel rounds — rounds stay serial for the reason `RUN-THE-EPIC-COMPETITION.md` gives.
- Any change under `tools/auto/`; `CollectBridge._shrink` stays byte-identical.

## 9. Added 2026-09-17 (second pass) — three more rounds

| round | id | what lands | size |
|---|---|---|---|
| 48 | KC-9 | an idle that is a cut-off, not a finish, gets `continue` into the same session (bounded by `max_continues`) | S |
| 49 | KC-10 | context fill from the server's token counts; `POST /session/{id}/summarize` at 80 %, and once on `ContextOverflowError` | S |
| 50 | KC-11 | each roster model probed once at the highest thinking `variant` that answers `say: hello`; cached in `contest-probe.json`; models without reasoning skip it | M |

Order of implementation as requested: KC-9, then KC-10, then KC-11.

## 10. Added 2026-09-20 — operator comfort, last in the queue

| round | id | what lands | size |
|---|---|---|---|
| 66 | KC-27 | the heartbeat shows each agent's phase, a files-count progress bar against the pack's median (60 % working, 70 % committed, 80 % tests, 100 % READY) and how long the judge's tests took; `turn["harvest"]["elapsed"]` persisted | S |
| 67 | KC-28 | `/dev/null`, `/dev/stdout`, `/dev/stderr`, `/dev/tty` are dropped by `_extract_paths`, so a `> /dev/null` is a no-path bash ask (KC-15's `once`/`mechanical`) and not a gate call out of the session's twenty | S |
| 68 | KC-29 | a stall the runner asked for (the questions cap) keeps its `STALLED` and exports its patch — only a session that died on its own is promoted to `READY` | S |
| 69 | KC-30 | `harvest` names the branch's one commit when no `PROGRESS.csv` row does, so `export_patches` has a sha and no caller re-derives one | S |
| 70 | KC-31 | a `STALLED`/`ERROR` worktree with edits and no commit is exported as `<agent>.STALLED.diff` | S |
| 71 | KC-32 | the contest tests stop failing on the operator's own box: `contest.local.ini` is not the committed roster, and no test bounds wall-clock under a round's load | S |

## 11. Added 2026-09-23 — FL-1 and its postmortem

Round 84 (FL-1, `epic-tasks/84-…`) is the hardest ticket the epic has
produced: the full test suite is not reproducibly green under load, and the
causes are **layered**, so a candidate with real progress still sees an
identical red suite. Five agents ran it and none finished; Opus 5 needed two
sittings and twelve further stress runs after the first fix already looked
done.

| file | what it is |
|---|---|
| `POSTMORTEM-FL-1.md` (repo root; identical copy `docs/kilo-contest/POSTMORTEM-FL-1.md`) | the full account — ten causes, six rounds, the four-shape taxonomy of time-dependent test failure (§8), verification by regression injection (§9) and the diagnostic toolkit (Appendix B) |
| `contest-bench/fl1/RUNBOOK.md` | how to rerun FL-1 as a benchmark: the base, the stress command, what the round is scoring, and the acceptance bar |

Four of the ten causes are production bugs (a lock held across `fsync`;
`float(MagicMock()) == 1.0`; `EventTap` flushing per event on the reader
thread and starving the silence clock it feeds; `.git/index.lock` treated as
a hard failure). Two of those were in code nobody suspected, inside tests
that had already been written off as flaky — which is the reason the
postmortem exists as a document rather than a commit message.
