# Running the Kilo Contest on an External Repository

How to run a contest round on a git repository other than jan-auto-agent, and how
to split one task into parts. The worked, tested example is `2legs/`
(`HOW-WE-RUN-2LEGS.md`, `run_2legs.sh`); this page is the general version.

## What the target repo needs (committed at the base)

| what | why |
|---|---|
| `scripts/next_task.py`, `scripts/append_task.py` | the agents' prompt runs them in their clone — copy from jan-auto-agent |
| `epic-tasks/NN-<slug>.md` | the ticket; an agent reads it from its own clone, so it must be committed |
| `git status --porcelain -- epic-tasks` empty | intake refuses a dirty or untracked `epic-tasks/` |

Intake (`--target`, KC-76) names whatever is missing and stops.

Ticket rules the code enforces: first line `# <ID> — <title>`; `**Status:** open`;
``**File:** `a.py`, `tests/test_a.py` `` — backticks, one span per path, else the whole
line is one path; `**Size:**` XS/S/M/L (picks the legs, below). The agent adds a test
under `tests/` and makes one local commit.

## A round

```bash
cd <jan-auto-agent>
python3 -m tools.contest run --ticket NN --target <repo> --roster <jan>/contest.ini --models $MODELS
```

A relative `--roster` resolves against the CWD (where you stand), not the target.
Output: `<repo>/contest-out/NN/` — `state.json`, `<agent>.patch`, per-agent logs;
checkouts under `rounds_dir` (default `../rounds`). `run` does not score: you pick.

Land a winner and close the ticket in the base, or the next ticket is refused:

```bash
cd <repo>
git am contest-out/NN/<winner>.patch
sed -i 's/^\*\*Status:\*\* open/**Status:** landed/' epic-tasks/NN-*.md
git add epic-tasks && git commit -m "NN landed"
```

## Splitting a task

**A. Several tickets, one round each** — the proven way (KC-77: 11/11 on two machines).
Ticket 02 says what 01 already did and what not to touch; intake refuses 02 while 01
is open, so the order is enforced.

**B. One ticket, several legs** (KC-43/KC-44). `--legs N`, or the ticket's `**Size:**`
through `[contest] legs_by_size`. Each leg is a whole turn in a fresh session on the same
worktree; only agents that ended `GAVE_UP` or `STALLED` go on; the next leg gets
`contest-out/NN.<leg>/<agent>.leg.md` (diff, tests, last word, what is left). Output is
`contest-out/NN.1/ … NN.N/`. Not yet run live on an external repo.

## State

```python
s = json.load(open("contest-out/NN/state.json"))
for a in s["agents"]:
    print(a["agent"]["name"], a["state"], a.get("commit"), a.get("last_reason"))
print(s["base_sha"])
```

## Troubleshooting

See the table at the end of `2legs/HOW-WE-RUN-2LEGS.md`.
