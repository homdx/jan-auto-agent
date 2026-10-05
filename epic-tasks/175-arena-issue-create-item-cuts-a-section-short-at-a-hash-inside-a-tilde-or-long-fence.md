# 175 — `arena issue create --item` cuts a section short at a `#` inside a `~~~` or ```` fence

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 169
**Severity:** MEDIUM
**Area:** `tools/arena/tickets.py` — `_headings`, `_FENCE_RE` (used by `_cut_item` / `build_brief`)

## Problem

`arena issue create --file EPIC.md --item AR-7` cuts the `### AR-7` section out
of the epic and sends it to the drafter. `_headings` skipped headings inside a
code block, but it only knew the ``` fence and toggled on any line starting with
three backticks:

- A `~~~` fence was not a fence. A `# comment` line in a `~~~python` block
  matched `_HEADING_RE` as a level-1 heading, which ends every section.
- A ``` line inside a ```` (four-backtick) block, the usual way to show a
  Markdown example, closed the block early. The `#` heading in the example
  then ended the real section.

Either way the brief held half the section with no warning. A ticket was then
drafted against half a spec, which `build_brief`'s docstring says must never
happen ("Nothing here is written and nothing is cut").

Repro:

```
### AR-1 — first
Text.
~~~python
# set up
x = 1
~~~
More AR-1 text.
### AR-2 — next
```

`_cut_item(..., "AR-1", ...)` returned `"### AR-1 — first\nText.\n~~~python"`.

## Fix

The fence follows CommonMark. An opening fence is 3+ backticks or tildes, up to
3 spaces in; a backtick fence's info string may not contain a backtick. Only a
run of the same character, at least as long and with nothing after it, closes
the block.

## Tests

`tests_bugfix/test_arena_cut_item_fences_175.py`:

- A `# comment` in a `~~~` fence does not end the section.
- A ``` line inside a ```` fence does not close it.
- A `~~~` line does not close a ``` fence.
- A plain ``` fence still works, and the next item still ends the section.

The first two fail on the old code.
