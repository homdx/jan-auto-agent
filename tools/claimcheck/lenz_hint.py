"""CC-9: a Lenz comparison hint — the commands and their cost printed after a run, never run.

Lenz is the paid fact-checker we know: ``/extract`` is free, ``/assess`` costs one credit per
claim and ``/verify`` ten, from one pool of 100 a month + 400 extra
(`docs/claim-check/REPORT.md`). EPIC-CC §3 rule 7 says no run of the epic calls ``/assess`` or
``/verify``; this module is the other half of that rule: after a run the operator is *told* how
to spend credits on purpose, with the commands ready and the cost stated.

Only a claim about the world is worth a credit (Lenz cannot see the repository), and only one
the voters did not settle:

    kind ``world``, not dangling, and the verdict SPLIT, a TRUE/FALSE with a dissenter, or UNSURE

The ticket names SPLIT and UNSURE; a plurality with a dissenter is the same doubt (CC-8 sends
it to the pending list, never to a row), so it is picked too, between the two. A unanimous
TRUE/FALSE, a code, mixed or dangling claim is never picked.

What would be sent is written to a claims file (one ``###`` section per claim) that
`scripts/lenz_claim_filter.py` reads as its report, so its ``--dry-run`` lists exactly the
claims the block counts and ``--max N`` caps the send at them. Sending the whole report instead
would spend the credits on whatever the script picks from it, not on these claims.

The script has two filters of its own, and its filtering is not this ticket's to change, so the
block counts only what would pass them and names the rest by number:

  * `split_sections` drops a section of 120 characters or fewer (a very short claim);
  * `pick_extracted` sends only a claim with one of its public-fact words (``_HINTS``: pytest,
    POSIX, rename, ``git status`` …). Found by running the real script in the tests: of three
    world claims the voters split on, it sent none. ``_HINTS`` is read from the script's source.

It may still skip a claim it reads as about this repository (a path, a backticked name), and
``/extract`` may reword one, so the cost is stated as "at most".

Nothing here opens a connection, reads the key, or starts the script: the credit figures are
read from the script's source as literals (`lenz_constants`), never by importing it.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
import shlex
from pathlib import Path
from typing import Optional

from tools.claimcheck import truth as cc_truth

ROOT = Path(__file__).resolve().parent.parent.parent
FILTER_SCRIPT = ROOT / "scripts" / "lenz_claim_filter.py"
#: the script's own values when its source cannot be read (it holds them as module literals)
FALLBACK = {"DEFAULT_MAX": 12, "VERIFY_CREDITS": 10}
ASSESS_CREDITS = 1       # one credit per claim sent to /assess (REPORT.md; the script's --max help)
MONTHLY_CREDITS = 100    # REPORT.md, measured against the billing page
EXTRA_CREDITS = 400
SECTION_MIN = 120        # lenz_claim_filter.split_sections keeps a section only when longer than this
#: the order of the picked claims; every group is a world claim the voters left unsettled
LENZ_ORDER = ("SPLIT", "DISSENT", "UNSURE")
TITLE = "## If you want Lenz's opinion (costs credits — nothing below was run)"
MATCH_RATIO = 0.85       # a Lenz claim reworded by /extract still matches ours at this ratio


def _source(path: Path):
    try:
        return ast.parse(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError, ValueError):
        return None


def script_hints(path: Path = FILTER_SCRIPT) -> Optional[re.Pattern]:
    """The script's ``_HINTS`` (``re.compile("…", re.I)``) rebuilt from its source, or None when
    it cannot be read: then no claim is held back for it and the block says the count may be high."""
    tree = _source(path)
    for node in (tree.body if tree else []):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_HINTS" and isinstance(node.value, ast.Call) and node.value.args):
            try:
                pattern = ast.literal_eval(node.value.args[0])
            except ValueError:
                return None
            flags = re.I if any(isinstance(a, ast.Attribute) and a.attr in ("I", "IGNORECASE")
                                for a in ast.walk(node.value)) else 0
            try:
                return re.compile(pattern, flags) if isinstance(pattern, str) else None
            except re.error:
                return None
    return None


def lenz_constants(path: Path = FILTER_SCRIPT) -> dict:
    """``DEFAULT_MAX`` and ``VERIFY_CREDITS`` as `scripts/lenz_claim_filter.py` assigns them, read
    from its source (`ast.literal_eval`): importing the script would pull in its HTTP client."""
    out = dict(FALLBACK)
    tree = _source(path)
    for node in (tree.body if tree else []):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in out:
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:
                    continue
                if isinstance(value, int) and value > 0:
                    out[name] = value
    return out


# ------------------------------------------------------------------ which claims are worth a credit

def lenz_group(c: dict, models: Optional[dict] = None) -> Optional[str]:
    """The `LENZ_ORDER` group of one votes.json claim row, or None when it is not worth a credit.
    *models*: profile -> model, as the truth step reads the families (`truth.settled`)."""
    if c.get("kind") != "world" or c.get("dangling"):
        return None
    v = c.get("verdict")
    if v in ("SPLIT", "UNSURE"):
        return v
    if v in ("TRUE", "FALSE") and cc_truth.settled(c, models) is None:
        return "DISSENT"     # a plurality, or unanimous from too few families: not settled either way
    return None


def pick_for_lenz(claims: list, *, limit: Optional[int] = None, models: Optional[dict] = None) -> list:
    """The claim rows worth a credit (`lenz_group`), SPLIT first, then DISSENT, then UNSURE, each
    group in claim order; at most *limit* (default: the script's own cap, ``DEFAULT_MAX``)."""
    limit = lenz_constants()["DEFAULT_MAX"] if limit is None else limit
    ranked = sorted((LENZ_ORDER.index(g), i) for i, c in enumerate(claims or [])
                    if (g := lenz_group(c, models)) is not None)
    return [claims[i] for _, i in ranked[:max(0, limit)]]


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def section_of(claim: str) -> str:
    """One claim as a section of the claims file: the claim as the heading (what the script's
    ``--dry-run`` prints) and again as the body (what ``/extract`` reads).

    The body never starts with a fence (three backticks or tildes) or ``#``: a claim that began with
    a code fence opened a fence the script's splitter never saw closed (three claims read as one
    section), and one that began with ``## `` was a heading of its own (three read as four). A single
    backtick stays: `sorted()` keeps … went to Lenz as "sorted()` keeps …"."""
    line = _one_line(claim)
    body = line
    while True:
        cut = re.sub(r"^\s*(?:`{3,}|~{3,}|#+)\s*", "", body)
        if cut == body:
            break
        body = cut
    body = body or line.replace("#", "").replace("`", "").replace("~", "")
    return f"### {line}\n\n{body}\n"


def sendable(claim: str) -> bool:
    """True when `lenz_claim_filter.split_sections` keeps this claim's section (> 120 chars)."""
    return len(section_of(claim).strip()) > SECTION_MIN


def split_sendable(picked: list, hints: Optional[re.Pattern] = None) -> tuple:
    """(would be sent, too short for a section, without a public-fact word of the script's)."""
    send, short, no_word = [], [], []
    for c in picked:
        text = c.get("claim", "")
        if not sendable(text):
            short.append(c)
        elif hints is not None and not hints.search(_one_line(text)):
            no_word.append(c)
        else:
            send.append(c)
    return send, short, no_word


def claims_file_text(picked: list, hints: "Optional[re.Pattern] | bool" = True) -> str:
    """The claims file `lenz_claim_filter.py` reads as its report: one section per claim it would
    send, no other heading (a short top line would become a section of its own or be dropped).
    *hints*: True reads the script's ``_HINTS``; a pattern or None is used as it is."""
    hints = script_hints() if hints is True else hints
    return "\n".join(section_of(c["claim"]) for c in split_sendable(picked, hints)[0])


# ------------------------------------------------------------------ the block

def _ref(c: dict, numbers: dict) -> str:
    """``claim 5 (C12)``: the pending list's entry number, never the claim's text — pending.md
    names every unsettled claim exactly once (CC-8's acceptance and black box count it)."""
    n = numbers.get(id(c))
    who = f" ({c['id']})" if c.get("id") not in (None, "") else ""
    return f"claim {n}{who}" if n is not None else f"claim{who or ' ?'}"


def _ours(c: dict, models: Optional[dict] = None) -> str:
    v = c.get("verdict") or "?"
    if v in ("TRUE", "FALSE"):
        if c.get("unanimous") is not True:
            return f"{v} (with a dissenter)"
        return f"{v} (unanimous)" if cc_truth.settled(c, models) else f"{v} (unanimous, too few families)"
    return v


def _votes_cell(c: dict) -> str:
    by = c.get("by_model") or {}
    counts: dict = {}
    for v in by.values():
        counts[v] = counts.get(v, 0) + 1
    return ", ".join(f"{v} {n}" for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))) or "-"


def compare_table(claims: list, picked: list, *, lenz: Optional[dict] = None,
                  models: Optional[dict] = None) -> list:
    """The compare table's lines: every picked claim (and, with *lenz*, every claim a Lenz row
    matched) with our verdict and the voters' split; a ``lenz`` column only when *lenz* is given
    (``{claim index: "True/high"}``)."""
    numbers = {id(c): i + 1 for i, c in enumerate(claims)}
    rows = list(picked)
    if lenz:
        seen = {id(c) for c in rows}
        rows += [claims[i] for i in sorted(lenz) if id(claims[i]) not in seen]
    if not rows:
        return []
    head = "| claim | ours | votes |" + (" lenz |" if lenz is not None else "")
    lines = [head, "|---|---|---|" + ("---|" if lenz is not None else "")]
    index = {id(c): i for i, c in enumerate(claims)}
    for c in rows:
        line = f"| {_ref(c, numbers)} | {_ours(c, models)} | {_votes_cell(c)} |"
        if lenz is not None:
            line += f" {lenz.get(index.get(id(c)), '-')} |"
        lines.append(line)
    return lines


def lenz_hint_markdown(picked: list, report_path, *, claims: Optional[list] = None,
                       candidates: Optional[int] = None, json_path="lenz.json",
                       compare_cmd: Optional[str] = None, lenz: Optional[dict] = None,
                       script: str = "scripts/lenz_claim_filter.py", models: Optional[dict] = None) -> str:
    """The block appended to pending.md and printed on the console, text only.

    *report_path* is the file the commands hand to `lenz_claim_filter.py` (the claims file
    `claims_file_text` makes); *claims* is votes.json's claim list (for the entry numbers and the
    compare table); *candidates* how many qualified before the cap; *compare_cmd* command 3 as it
    must be typed (default: the ticket's short form); *lenz* the matched Lenz labels, which add the
    ``lenz`` column."""
    consts = lenz_constants()
    hints = script_hints()
    claims = claims if claims is not None else list(picked)
    numbers = {id(c): i + 1 for i, c in enumerate(claims)}
    send, short, no_word = split_sendable(picked, hints)
    lines = [TITLE, ""]
    if not picked:
        lines += ["Nothing worth sending to Lenz: no claim about the world was left unsettled by the voters "
                  "(Lenz cannot see this repository, so code claims are never worth a credit).", ""]
        table = compare_table(claims, [], lenz=lenz, models=models) if lenz else []
        return "\n".join(lines + ([*table, ""] if table else []))
    n = len(send)
    capped = (f" ({len(picked)} of {candidates}; the cap is DEFAULT_MAX = {consts['DEFAULT_MAX']})"
              if candidates is not None and candidates > len(picked) else "")
    q = shlex.quote
    lines.append("Lenz cannot see this repository, so only claims about the world are worth sending.")
    if n:
        lines += [f"Worth comparing (public facts the voters split on or were unsure about): {n} claim{'' if n == 1 else 's'}{capped}.",
                  f"Cost if you send them: /assess = at most {n * ASSESS_CREDITS} credits (of {MONTHLY_CREDITS} a "
                  f"month + {EXTRA_CREDITS} extra; this month: unknown to this tool — check the billing page). "
                  f"/verify on the single most doubtful row = {consts['VERIFY_CREDITS']} more "
                  "(add --verify to command 2).",
                  "At most: the script may still skip a claim it reads as about this repository, /extract may "
                  "reword one, and a claim already in its cache costs nothing.", ""]
    else:
        lines += [f"The voters left {len(picked)} world claims unsettled{capped}, but lenz_claim_filter.py as it is "
                  "would send none of them (below): no commands, nothing to spend.", ""]
    if hints is None:
        lines += ["(The script's public-fact words could not be read: the count may be higher than what it sends.)", ""]
    if no_word:
        lines += ["Not counted, the script would not send them (none of its public-fact words — pytest, POSIX, "
                  "rename, git status …; its filter is not changed here): "
                  + ", ".join(_ref(c, numbers) for c in no_word) + ".", ""]
    if short:
        lines += ["Not counted, too short for the script's sections (120 characters or fewer): "
                  + ", ".join(_ref(c, numbers) for c in short) + " — ask Lenz about them by hand if you must.", ""]
    if n:
        lines += ["  1. Look at what would be sent, no network, no credit (one line per claim):",
                  f"       python3 {q(script)} {q(str(report_path))} --dry-run",
                  "  2. Send them (asks for the Lenz API key on the console unless it is in the environment; "
                  "cached on disk, a repeat costs nothing):",
                  f"       python3 {q(script)} {q(str(report_path))} --json {q(str(json_path))} --max {n}",
                  "  3. Compare with our verdicts:",
                  "       " + (compare_cmd or f"python3 scripts/claimcheck_truth.py <votes.json> --lenz {q(str(json_path))}"),
                  ""]
    table = compare_table(claims, picked, lenz=lenz, models=models)
    if table:
        lines += ["The claims (their texts are in the pending entries above and in the claims file):", "", *table, ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ reading what lenz_claim_filter.py --json wrote

def read_lenz(path: Path) -> list:
    """The rows of a `lenz_claim_filter.py --json` file: ``[]`` (nothing was left to send) or its
    report object's ``rows`` (``claim``, ``verdict``, ``confidence``, …). Raises ValueError for any
    other shape, OSError for a file that cannot be read."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and isinstance(data.get("rows", []), list):
        rows = data.get("rows", [])
    else:
        raise ValueError("not a lenz_claim_filter.py --json file")
    return [r for r in rows if isinstance(r, dict) and r.get("claim")]


def lenz_label(row: dict) -> str:
    """``True/high``, ``Mixed/low``; ``Error`` (not charged) as it is."""
    v, conf = row.get("verdict") or "?", row.get("confidence")
    return f"{v}/{conf}" if conf else str(v)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower()).rstrip(" .;:!")


def match_lenz(claims: list, rows: list) -> tuple:
    """({claim index: label}, unmatched): each Lenz row onto our claim with the same text (case
    and spacing aside), else the one claim that contains it or is contained in it, else the one
    closest at `MATCH_RATIO` or more. A row that matches no claim, or two equally, is unmatched —
    a Lenz verdict on the wrong claim would be worse than none. A later row for the same claim wins."""
    norms = [_norm(c.get("claim", "")) for c in claims]
    out: dict = {}
    unmatched = 0
    for r in rows:
        t = _norm(r.get("claim", ""))
        hit = [i for i, n in enumerate(norms) if n == t]
        if not hit:
            hit = [i for i, n in enumerate(norms) if n and t and (t in n or n in t)]
        if not hit and t:
            scored = sorted(((difflib.SequenceMatcher(None, t, n).ratio(), i) for i, n in enumerate(norms) if n),
                            reverse=True)
            if scored and scored[0][0] >= MATCH_RATIO and (len(scored) == 1 or scored[1][0] < scored[0][0]):
                hit = [scored[0][1]]
        if len(hit) == 1:
            out[hit[0]] = lenz_label(r)
        else:
            unmatched += 1
    return out, unmatched
