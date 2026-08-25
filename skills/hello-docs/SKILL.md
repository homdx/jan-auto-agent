---
name: hello-docs
description: Write end-user documentation for a small Python script — usage, examples, and exit codes — in prose, not code.
---

# Small-Project Documentation

## Overview

You are writing documentation a stranger will read before running the
project. Assume they have Python installed and nothing else. Write prose,
not bullet-point fragments.

## Structure

Produce, in this order:

1. A one-sentence statement of what the program does.
2. A `## Usage` section with a copy-pasteable command and its exact output.
3. A `## Exit codes` table, if the script has any.
4. A `## Development` section covering how to run the tests.

## Voice

Second person, present tense. "Run `python main.py` and it prints
`Hello world`." Never "the user should be able to run".

State what the code actually does. If a behaviour is not present in the
source you were shown, do not document it — omit the section instead of
guessing. An invented flag is worse than a missing one.

## Constraints

- Write to `.md` files only. Never modify `.py` files.
- No badges, no shields, no emoji.
- Do not document features that do not exist yet.
