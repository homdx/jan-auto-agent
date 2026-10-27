# cc-epic-docs → main: Lie detector v2 — the jury gets to read the code

## Short (the elevator pitch)

In this repo, models write the tickets. Models write the code. Models review the code. And every one of them is
absolutely sure it is the best model in the building. The ticket writer thinks its ticket is flawless, the coder
thinks its patch is perfect, and the reviewer thinks its review is the truth.

We decided that nobody gets to be the best *on their own word*. If you say something about the code, **show us the
line.** The line is checked by code, not by another model.

```bash
scripts/run_claim_check.sh report.md --target . --ref HEAD     # the jury, with the code on the table
python3 scripts/claim_diff.py claims.json --target . --base main --head HEAD --profiles a b c
                                                                # "did the fix fix it?" in one word
```

Before this branch, the jury settled **0 of 50** claims about code. After it, the jury settles **56 to 63 of 80**, with
**0 wrong**. 218 commits, 0 behind main, a clean merge, all green.

## Full (the stand-up)

**The cast, so you can follow along:**
- **The Operator**: a human with a terminal, and a growing collection of reports that all say "fixed".
- **The Box**: the same 8-core machine. It still has opinions about load average.
- **The Jury**: three free models from three different families. They never meet, and they never copy each other.
- **The Bouncer**: Gate 1, the reader that decides which improvement ideas get into the backlog.
- **The Contestants**: the free models that wrote most of this branch in rounds. **step-5-preview** won 12 rounds and
  the **sensenova** family won 22 between its four editions, so it is a dynasty now.

### Previously, on this contest…
Last season ended on a cliffhanger. We built a lie detector: three free models from three families vote on every
claim in a round report, and a verdict counts only if they agree. On public facts it worked well: 0.4 % wrong. On our
own round reports it was nearly useless, because **128 of 157 claims got no committed vote from anybody.** The claims
were about our code, and the models had never seen our code. So we made a firm rule that a vote on a claim about our
code is never accepted, and we promised: *"Next episode: give the voters the file the claim names."*

This is that episode.

### Season 3: "Have you heard of `run_step`?"
First we measured the patient properly. Three voters, three runs each, fifty claims about code. Settled: **zero**.
Seventy answers of "UNSURE" on the fixture. We had been asking about a function as if it were a celebrity: "Have you
heard of `run_step`?" "...I think I saw it at an airport once."

So the jury now gets an **evidence pack** for every claim: the cited lines, the definition the claim names, the git
history, the diff and the ticket. Then comes the rule that changes everything: **every verdict has to come with a
quote from that pack.** The quote is checked by code, with `in`. No other model takes part.

If you want to say TRUE, show the line. If the quote you made up is not in the pack, that is not a verdict, buddy, it
is fan fiction. Your vote is downgraded to UNSURE and the room moves on.

| | no evidence (season 2) | evidence + quote (this branch) |
|---|---|---|
| fixture, 80 claims | 1 settled | **56 to 63 settled, 0 wrong** |
| real claims about this repo, 30 | 0 settled | **10 to 16 settled, 0 wrong** |
| a quote that is not in the pack | the model could make one up | caught every time (a deterministic test) |

From zero to sixty-three. That is not a glow-up, that is a resurrection.

### "Fixes #42". Does it, though?
Every pull request says "fixes #42". I have never in my life seen one that says "makes #42 slightly more interesting".

`claim_diff.py` judges the same claim **twice**: once at the commit before, once at the commit after, with the diff
on the table. Then it says one word: `FIXED`, `STILL`, `NEW`, `GONE` or `UNCLEAR`. My favourite rule is this one: a
claim that was false before *and* false after is **not** `FIXED`. It is `UNCLEAR`, and the docs say calling it fixed
"would be a lie". We wrote a rule against lying *into the scoring script*. I have worked at companies that couldn't
get that into the code of conduct.

The live run on our own fixes: **28 right, 6 unclear, 0 wrong.** "Unclear" is an honest answer, and the costly error
(a claim still true reported as `FIXED`) is pinned at zero by the scorer.

### Democracy, or: the group chat picking a restaurant
"How many judges do you need?" More is better, surely. Democracy!

It turned out to be wrong. When everybody must agree, **more voters settle fewer claims**: 129 of 150 with three
voters, 125 with five, 115 with seven. Every extra voter is one more person who can't make it on Thursday.

So the branch adds `--accept committed`. If three voters actually *committed* to an answer and they agree, that is a
verdict, and the voter who said "I'm not sure" doesn't get a veto. A real dissent still blocks. With five or seven
good voters: **146 of 150 settled, 0 wrong.** The trick to democracy turned out to be simple: the guy who didn't read
the menu doesn't get to block dinner.

### The Bouncer gets a jury (and we tell you the truth about it)
Then we gave the jury the hardest job in the building. Gate 1 is the bouncer that decides which improvement ideas
reach the backlog. An Architect model proposes "there's a bug in X", and a single reader model says yes or no. We
put the three-family jury next to it, with the code, the quotes and the whole courtroom.

We measured it over seven live runs. Whenever all three said **"FALSE, this problem doesn't exist"**, they were right
**14 out of 14** times. Frame it.

Whenever all three said **"TRUE, this is a real problem"**, they were wrong **75 to 88 percent** of the time.

*(pause)*

Three models from different families, each quoting real lines of real code, all agreeing, and all wrong. That's not
a jury, that's a podcast. The docs say it better than I can: unanimity measures *agreement*, not *truth*.

So we did the most senior thing an engineer can do: the feature ships **behind a flag, and the flag is off**.
`[gate1] claim_vote = off`. It works, the measurement is honest and published, and it waits until it earns the job.
Your pipeline today behaves exactly like yesterday. That's the whole migration guide.

### Meanwhile, the review found... things
Fifty rounds of review fixes and twenty-seven HARDEN tickets came out of this branch, and most of those tickets were
fixed by the contestants themselves, in rounds. Some highlights from the crime scene:

- **The verdict reader spoke only sarcasm.** It read **"Yes."** (with a period) as *no*. It read "Nope, but there's a
  problem on line 12" as an **approval**, and "Approved — but the date is wrong" as a *confident* approval.
- **The secret mask got a little too excited.** It masked a **CSS class** because the class name started with `sk-`.
  Your password was safe, and your button styling was classified.
- **"Not found" for a React button.** The block extractor said "not found" for `const Button = React.memo(...)`.
  It was right there, it was a *button*. Gate 1 read "not found" as "the model hallucinated", so a perfectly real
  component was being called imaginary. Now it finds React wrappers, `module.exports.f = () => …`, C# `=> w * h;`,
  Dart getters and Scala one-liners.
- **Five minutes of vibes.** The collector once scanned **32,539 Python files** of old contest output: five minutes at
  96 % CPU with not a single line printed. Now there's `[collect] skip_dirs`, and the collector says which files it
  skipped and why.
- **The agent that wouldn't stop.** When the runner told an agent "stop", it politely refused that one tool call and
  kept going for up to `max_steps` **paid** model calls. When the runner sent SIGTERM, the agent died on the spot,
  and its dev server, in a process group of its own, kept on living. It was like a party where the host leaves and
  nobody tells the DJ. Now an abort ends the turn, and SIGTERM takes the whole group down, even when it lands in the
  half-millisecond while the command is being born.
- **The request that hid inside its neighbour.** Two threads printed progress at the same moment and produced
  `… request 2 0s[progress] …`. A log reader counted fourteen requests out of fifteen. The request was never lost.
  It was hiding inside the line next to it.

Each of these comes with a test that fails on the old code.

### Proof, because we're still not animals

```bash
python3 -m pytest .smoke_tests/ .regression_tests/ -q -p no:cacheprovider   # 10723 passed, 2 skipped
python3 -m pytest tests_bugfix -n 4 -q -p no:cacheprovider                 # 5356 passed
python3 scripts/sync_test_tiers.py --check                                  # smoke=352, regression=40
```

The tests stayed green under load: three parallel checkouts on one Box, and the two tests that had ever flaked were
soaked 30 times each, with zero failures. Every new setting is written in `agents.ini` / `contest.ini` with its
default, and **every default is "behaves like before"**.

## Conclusion

Here is what this branch really changes. **A model's word is no longer evidence. A quoted line is.**

- **Review reports:** a claim about our code is no longer "go read the file yourself". The jury reads the file,
  quotes it, and settles up to four claims in five on the fixture and up to half of the real ones. When it
  can't, it says `UNSURE` instead of guessing.
- **"Fixed"** became something you can check: one command and one word per claim, at two commits.
- **Gate 1 doesn't change, on purpose.** The jury is very good at saying *"this bug isn't real"* (14 of 14) and bad at
  saying *"this bug is real"*. Turning it on today would trade one confident reader for three confident readers. The
  flag waits for CC-12 (callers in the evidence pack), the next attempt to teach three models that agreeing is not the
  same as being right.
- **Everything the review touched is sturdier**: the verdict parser, the secret mask, the extractor, process cleanup
  and the collector.

Season 2 ended with "the models can't see our code". Season 3 ends with "the models can see it, they have to prove
it, and we know exactly how far to trust them". It is a smaller promise than "AI reviews your code", and it is one we
can keep.

### Credits scene
- **CC-12 (294)** stays open. It is the sequel and doesn't block this merge.
- **The jury** is free models only. Nobody was paid, and some of them clearly were not paid enough.
- **The Bouncer** keeps its job for now. The jury is in the next room, taking notes.
- No models were harmed. Several were cross-examined, one dynasty was founded, and one podcast was cancelled.

Thank you, you've been a great audience. Tip your reviewers: they found fifty rounds of bugs, and they're all on free tiers.

*(drops the mic; `_run_bash_group` catches it and kills its process group)*

---

### Sources

| Line in the set | Where it was measured |
|---|---|
| 128 of 157 claims with no vote; 0.4 % wrong on public facts | the previous merge, `d7ff804`; `docs/claim-check/REPORT.md` |
| 0 of 50 code claims, 70 UNSURE | `contest-bench/cc/BASELINE.md` |
| 56–63 of 80, 10–16 of 30, 0 wrong | `docs/claim-check/RUNBOOK.md` (round 280) |
| 28 right, 6 unclear, 0 wrong (before/after) | CC-7 judging note; `docs/claim-check/BEFORE-AFTER.md` |
| 129 / 125 / 115 of 150; 146 of 150 committed | `docs/claim-check/RUNBOOK.md`, `GATE1-VOTE-LAYER2.md` (CC-11) |
| FALSE right 14 of 14, TRUE wrong 75–88 % | `docs/claim-check/RUNBOOK.md`, `GATE1-VOTE-LAYER2.md` (CC-10) |
| step-5-preview 12, sensenova 22 rounds won | the `@round-NN.contest` co-authors in `git log main..cc-epic-docs` |
| 32,539 files, five minutes, 96 % CPU | `agents.ini` `[collect] skip_dirs` (CC-13) |
| The reviews | `epic-tasks/298-…` … `325-…` (HARDEN), the "CC review fixes" commits, `docs/claim-check/EPIC-CC.md` |
