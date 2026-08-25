# Running a standard SKILL.md

## The idea

A standard skill is a `SKILL.md`: YAML frontmatter (`name`, `description`)
plus a markdown body of instructions. It is **prompt material**, not a
description of machinery. So running one needs no new runtime — it is a
config overlay applied once at startup:

```
SKILL.md  +  skills/<name>.skill.ini   ──▶  overlay  ──▶  agents.ini (in memory)
 (unchanged)   adapter, ~15 lines           (no LLM)       everything downstream
                                                           runs unmodified
```

Nothing generates a skill. You bring an existing `SKILL.md` and write a
short adapter beside it.

## Why `base` is the whole trick

`task_mode` used to conflate two different things: the **mechanics** (how
the coder's reply is parsed, whether acceptance criteria are required,
whether Gate 1's presence check runs) and the **identity** (which prompts,
which gates, which token caps).

The adapter's `base` key separates them. It picks one of the three
mechanical modes; everything else about the skill lives in config:

| `base` | Reply parsing | Acceptance criteria | Gate 1 Stage B |
|---|---|---|---|
| `code` | fenced blocks | required | active |
| `docs` | fenced blocks | required | active |
| `creative` | prose | optional | skipped |

That is why this costs zero changes to the 20 modules that branch on
`task_mode` — they keep reading exactly what they always read.

## The adapter format

```ini
[skill]
name        = hello-creative
source      = hello-creative/SKILL.md   ; relative to the adapter
base        = creative                  ; code | docs | creative
min_num_ctx = 16384
budget_fraction = 0.25                  ; share of num_ctx the body may use
on_overflow = error                     ; error | sections | truncate

[skill.inject]                          ; who gets the SKILL.md body
architect = body
coder     = body
validator_agent = none
gate1_filter    = none

[skill.overlay]                         ; any agents.ini key, as section.key
auto.max_tasks_per_run = 2
coder.max_tokens       = 3000

[gates]                                 ; Gate-3 order for this skill
creative = canon, continuity
```

## The context budget

**This is not optional.** Real skill bodies run from ~2 000 to ~20 000
characters. The `algorithmic-art` skill shipped with Claude is 19 327
characters — about 4 832 tokens — injected into the system prompt of
**every agent call**. Against `agents.ini`'s `num_ctx = 8192` that consumes
more than half the window before the task text is added, and the run then
fails in a way that looks like model weakness rather than misconfiguration.
That is the worst kind of failure: it sends you debugging the wrong thing.

So the loader refuses rather than overspending:

```
$ python3 main.py --auto "..." --config agents.ini --skill hello-code
Error: --skill 'hello-code': skill 'hello-code' requires num_ctx >= 16384,
but the active profile has 8192. Switch to a larger profile
(agents_32k.ini or above), or lower [skill] min_num_ctx if you have
measured that this skill fits.
```

**Use `agents_32k.ini` or larger for skill runs.** 16K is the floor, and
the shipped adapters set `min_num_ctx = 16384` to enforce it.

Token estimation adapts to script: Latin text is counted at ~4 characters
per token, Cyrillic and other non-ASCII at ~2.5. A single 4.0 divisor would
underestimate a Russian body by roughly half — and that error points the
wrong way, letting an oversized skill through the guard.

### When a skill genuinely is too big

Two escape hatches, both explicit:

```ini
on_overflow = sections
[skill.sections]
keep = Overview, Constraints      ; names of `##` headings, exactly
```

Keeps only the listed sections and logs what was dropped. A misspelled
heading is an error, not a silent no-op. Note that `###` sub-headings are
not selectable — only `##`.

```ini
on_overflow = truncate
```

Cuts at a section boundary until the body fits. Never mid-sentence.

## Trying it on hello-world

`examples/hello-world/` is a deliberately tiny project: `main.py` prints
`Hello world` and nothing else. With one function there is exactly one
place for the Architect to propose work, so a run is short enough to read
the whole trace by hand.

```bash
python3 main.py --list-skills
# hello-code
# hello-creative
# hello-docs
```

### Flow 1 — code hardening (`base = code`)

```bash
python3 main.py --auto "Harden main.py: docstrings, type hints, a pytest test" \
  --base examples/hello-world \
  --config agents_32k.ini \
  --skill hello-code
```

Full code mechanics. Gate 1 Stage B is active, so a task claiming a problem
that isn't in the source gets rejected before the coder runs. Acceptance
criteria are required — the skill body tells the Architect to state a
runnable check.

**Watch for:** Gate 1 rejections with a grounding reason; the Architect
citing `main.py` line numbers; `python -m pytest -q` as the acceptance
check.

### Flow 2 — documentation (`base = docs`)

```bash
python3 main.py --auto "Write user documentation for this project" \
  --base examples/hello-world \
  --config agents_32k.ini \
  --skill hello-docs
```

Prose output, still grounded. The skill body forbids documenting behaviour
that isn't in the source, so this is the flow where an invented `--verbose`
flag should get caught.

**Watch for:** whether Gate 1 rejects a task about a nonexistent feature;
whether the coder writes only `.md` files.

### Flow 3 — creative changelog (`base = creative`)

```bash
python3 main.py --auto "Write a narrative changelog entry for the greeting" \
  --base examples/hello-world \
  --config agents_32k.ini \
  --skill hello-creative
```

This is the interesting one for gates. Prose parsing, no acceptance criteria,
Gate 1 Stage B skipped — and the Gate-3 set is **trimmed to two**:

```ini
[gates]
creative = canon, continuity
```

`canon` guards the fact that the program prints `Hello world`, `continuity`
guards against entries contradicting each other. `theme` and `prosody` are
dropped: this is a changelog, not a poem, and each gate is a full LLM round
trip on every attempt.

**Watch for:** in the run trace, exactly two Gate-3 stages should appear
(`canon`, `continuity`) and never `theme` or `prosody`. That is the
clearest single signal that the `[gates]` overlay took effect.

## Reading the results

```bash
python3 view_trace.py            # per-stage decisions
python3 analyze_logs.py          # aggregate
```

The startup log line names the resolved skill:

```
skill 'hello-creative' loaded — base=creative, ~279 tokens injected into architect, coder
```

## Known limitation

You get the mechanics of one of three bases, not an arbitrary mix. If a
skill ever needs, say, prose parsing *with* mandatory acceptance criteria,
that combination does not exist today and would need capability flags
splitting the ~28 `task_mode == "creative"` branches into named switches.
Worth doing when such a skill actually shows up — not before, on
speculation.

## Adding your own skill

1. Drop the skill folder in `skills/` (or point `source` at it anywhere).
2. Write `skills/<name>.skill.ini` — start from `hello-creative.skill.ini`.
3. `python3 main.py --list-skills` to confirm it is found.
4. Run against a 32K-or-larger profile.

If the loader refuses on budget, read the number it prints before raising
`budget_fraction` — a skill that only fits by taking 60% of the window will
technically start and then behave badly.
