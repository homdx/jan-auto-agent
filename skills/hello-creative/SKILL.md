---
name: hello-creative
description: Write a short narrative changelog entry about a program, in prose, with a consistent voice across entries.
---

# Narrative Changelog

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

- Prose only. No code blocks, no bullet lists inside an entry.
- Never contradict an earlier entry in the same file.
- One entry per task.
