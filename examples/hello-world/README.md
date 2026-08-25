# hello-world

A deliberately tiny Python project used to exercise jan-auto-agent flows
end to end. `main.py` prints `Hello world` and nothing else.

Kept minimal on purpose: with one function and no dependencies, the
Architect has exactly one place to propose work, so a run is short enough
to read the whole trace by hand and see which gates fired.

## Running the skill flows

`RUNBOOK.md` has the three validated commands — one per mechanical base —
with what to expect from each, the sandbox reset step, and how to tell
whether Gate-3 actually ran. the runbook test in the parent repository (tests/test_hello_world_runbook.py) parses it
and fails if any documented command stops being valid.

## Changelog

Narrative entries live in `CHANGELOG.md`, newest first. They are prose, not
bullet lists, and are written by the `hello-creative` skill.
