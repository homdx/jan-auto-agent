---
name: hello-creative
description: Write a short narrative changelog entry about a program, in prose, with a consistent voice across entries.
---

# Narrative Changelog

## Where to write

Every entry goes into `CHANGELOG.md`, and **only** into `CHANGELOG.md`.
Append the new entry directly beneath the `# Changelog` heading, above the
entries already there — newest first.

Never target a `.py` file. A task whose target file is source code is the
wrong task: propose one against `CHANGELOG.md` instead. This is not a style
preference. The validator judges the produced file against the request, so
writing prose into a source file (or code into the changelog) is rejected
on every attempt and the task cannot converge.

## Overview

You are writing a narrative changelog: each entry is a few paragraphs of
prose telling the story of one change, not a bullet list. The audience is a
developer who will read the whole file top to bottom in a year.

## Voice

Past tense, first person plural. "We taught main.py to take an argument."
Warm but never cute. No exclamation marks.

Keep the voice identical across entries. If a previous entry exists, match
its rhythm and sentence length before you match anything else.

## Structure

Each entry is:

- A `### ` heading naming the change in five words or fewer.
- Two to four paragraphs of prose.
- A closing sentence stating what a reader can now do that they could not before.

## Canon

The program prints `Hello world`. That is a fact about this project and it
does not change unless a task says it changed. Do not invent version
numbers, dates, contributors, or a project history that was not given to
you.

## Constraints

- Target `CHANGELOG.md`. Never modify `.py` files.
- Prose only. No code blocks, no bullet lists inside an entry.
- Never contradict an earlier entry in the same file.
- One entry per task.
