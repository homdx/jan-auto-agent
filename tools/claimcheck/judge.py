"""CC-6 — evidence-bound voting: prompt v2, the vote parser v2, and the quote check
(epic-tasks/280-cc-6-evidence-bound-voting.md, docs/claim-check/EPIC-CC.md §4.4).

With a pack in the prompt a voter *can* judge a claim about the code. It can also pretend
to: say `TRUE` because the claim sounds right and cite nothing. The one defence that does
not trust the model is to make it quote, and to check the quote by code:

* `build_prompt_v2` — `CLAIM-1`'s prompt (three openers, a shuffled claim order, `fixed`
  as the control) with each claim's rendered pack under it and the evidence rules;
* `parse_votes_v2` — the reply, tolerant of fences, prose and a trailing comma, as
  `{claim index: Vote}`;
* `verify_quotes` — a `TRUE`/`FALSE` on a claim with a pack stands only when the named
  chunk is in that claim's pack and holds the quote (`Pack.find_all`, the one rule of
  `pack.py`); otherwise it becomes `UNSURE` and is listed in `rejected` with its reason;
* `ask_with_packs` — one voter, one run, over the claims in batches (world claims
  `batch` at a time, code and mixed claims `code_batch` at a time), through a
  `completion_fn(prompt) -> text` the caller owns: `scripts/claim_vote.py` hands it the
  HTTP call, the tests a fake.

A verdict the evidence does not support costs a pending-list entry, never a wrong answer.
Nothing here touches the network; nothing here raises on a model's reply.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from typing import Callable, Optional

VERDICTS = ("TRUE", "FALSE", "UNSURE")
COMMITTED = ("TRUE", "FALSE")
MAX_QUOTE = 200          # what the rules ask for; a longer verbatim quote is still a quote

#: why a committed vote was downgraded to UNSURE
CHUNK_NOT_IN_PACK = "chunk_not_in_pack"   # the named id is not a chunk of the claim's pack
QUOTE_NOT_IN_PACK = "quote_not_in_pack"   # the named chunk does not hold the quote
REASONS = (CHUNK_NOT_IN_PACK, QUOTE_NOT_IN_PACK)

SYSTEM = "You are a careful technical fact checker."

# Same job, three wordings (as `CLAIM-1`): the first sentence swaps its words around, so
# the prompts differ from the very first token and no prefix cache can match.
OPENERS = (
    "Judge each of the following claims as TRUE, FALSE or UNSURE.",
    "Each of the following claims must be judged by you: TRUE, FALSE or UNSURE.",
    "TRUE, FALSE or UNSURE: that is the judgment you give to each claim below.",
)

#: the evidence rules, verbatim from the ticket (a test pins this text)
RULES = (
    "For a claim about this repository, the evidence under it is your only source: do not use "
    "what you remember about similar code. For a fact about the world you may use what you "
    "know. If the evidence does not decide the claim, answer UNSURE — that is a correct answer. "
    "For TRUE or FALSE, name the chunk ([[id]]) and copy, character for character, up to 200 "
    "characters from that chunk that decide the claim.")

REPLY_FORMAT = (
    'Reply with JSON only: a list of {"id": <n>, "verdict": "TRUE|FALSE|UNSURE", '
    '"chunk": "<id>", "quote": "<text>"} and nothing else, no explanations. '
    'For a claim marked WORLD, "chunk" and "quote" may be empty.')

#: what stands under a world claim instead of a pack
WORLD_BLOCK = "WORLD: a fact about the world; no evidence is given and none is needed."

_CLAIM_HEAD = "=== CLAIM {n} ==="


@dataclass(frozen=True)
class Vote:
    """One voter's answer on one claim: the verdict and the evidence it cites."""

    verdict: str
    chunk: str = ""
    quote: str = ""


# ------------------------------------------------------------------ the prompt

def build_prompt_v2(items: list, run: int, seed: int, fixed: bool = False) -> tuple:
    """The user message for run *run*, and the original index of each shown claim.

    *items* is a list of `(claim, pack_or_None)`; `None` is a world claim. Claim `n` of the
    prompt (1-based, in the order shown) is `items[order[n - 1]]`. *fixed* sends run 0's
    wording and order every run: the control that tells a model's own randomness from the
    effect of rewording."""
    if fixed:
        run = 0
    order = list(range(len(items)))
    random.Random(seed * 31 + run).shuffle(order)
    blocks = []
    for n, i in enumerate(order, 1):
        claim, pack = items[i]
        evidence = WORLD_BLOCK if pack is None else pack.render()
        blocks.append(f"{_CLAIM_HEAD.format(n=n)}\n{claim}\n{evidence}")
    head = OPENERS[run % len(OPENERS)] + " " + RULES + "\n" + REPLY_FORMAT
    return head + "\n\n" + "\n\n".join(blocks), order


# ------------------------------------------------------------------ the parser

_ID = re.compile(r"^\s*(?:claim[\s\-_#]*)?(\d+)\s*$", re.I)


def _drop_trailing_commas(text: str) -> str:
    """*text* with every `,` that stands before a `]` or `}` (whitespace between) dropped,
    a comma inside a string literal left alone."""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "]}":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _row_id(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        m = _ID.match(value)
        return int(m.group(1)) if m else None
    return None


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _rows_to_votes(rows, order: list) -> dict:
    votes: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        n = _row_id(row.get("id"))
        verdict = row.get("verdict")
        verdict = verdict.strip().upper() if isinstance(verdict, str) else ""
        if n is None or not 1 <= n <= len(order) or verdict not in VERDICTS:
            continue
        votes.setdefault(order[n - 1], Vote(verdict, _text(row.get("chunk")).strip(),
                                            _text(row.get("quote"))))
    return votes


def _lists(text: str):
    """Every JSON list in *text*, in order of where it starts."""
    # strict=False: a model copying a code line into "quote" writes the line break as a bare
    # newline, which strict JSON forbids; the whole reply was lost to it, not just the quote
    decoder = json.JSONDecoder(strict=False)
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            rows, _end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(rows, list):
            yield rows


def parse_votes_v2(text: str, order: list) -> dict:
    """Model reply -> {original claim index: Vote}.

    The first JSON list in the reply that holds at least one usable row wins; code fences
    and prose around it do not matter, nor does a trailing comma, the case of `verdict`, or
    an `id` written as a string (`"3"`, `"CLAIM-3"`). A row with an unknown verdict or an
    `id` out of range is dropped; garbage gives {} (the voter cast no votes)."""
    text = text if isinstance(text, str) else ""
    for candidate in (text, _drop_trailing_commas(text)):
        for rows in _lists(candidate):
            votes = _rows_to_votes(rows, order)
            if votes:
                return votes
    return {}


# ------------------------------------------------------------------ the quote check

_LABEL_TAIL = re.compile(r"\s+\((?:source|collect|git|ticket|note)\)\s*$")


def normalise_chunk_id(chunk: str) -> str:
    """The id a voter named, the way the pack writes it: brackets (`[[id]]`, `[id]`),
    backticks and quotes around it, and a copied `  (kind)` label tail dropped."""
    if not isinstance(chunk, str):
        return ""
    cid = _LABEL_TAIL.sub("", chunk.strip())
    while True:
        stripped = cid.strip().strip("`'\"").strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            stripped = stripped[1:-1]
        if stripped == cid:
            return cid
        cid = stripped


def _pack_of(packs, i: int):
    if isinstance(packs, dict):
        return packs.get(i)
    try:
        return packs[i]
    except (IndexError, TypeError, KeyError):
        return None


def check_vote(vote: Vote, pack) -> Optional[str]:
    """Why *vote* does not stand on *pack*, or None when it does: a world claim (no pack),
    an UNSURE, or a TRUE/FALSE whose named chunk is in the pack and holds the quote."""
    if pack is None or vote.verdict not in COMMITTED:
        return None
    cid = normalise_chunk_id(vote.chunk)
    if not cid or cid not in {c.id for c in pack.chunks}:
        return CHUNK_NOT_IN_PACK
    if cid not in pack.find_all(vote.quote):
        return QUOTE_NOT_IN_PACK
    return None


def verify_quotes(votes: dict, packs) -> tuple:
    """`(votes, rejected)`: *votes* with every committed vote the evidence does not support
    turned into `UNSURE`, and one row per downgrade.

    *packs* maps a claim index to its `Pack`, or `None` for a world claim (a dict, or a
    list by index; an index it does not hold is a world claim). A vote that stands keeps its
    chunk id in the pack's own spelling. A rejected row is `{"claim": i, "verdict": …,
    "chunk": …, "quote": …, "reason": "chunk_not_in_pack"|"quote_not_in_pack"}`."""
    out: dict = {}
    rejected: list = []
    for i, vote in votes.items():
        pack = _pack_of(packs, i)
        reason = check_vote(vote, pack)
        if reason is None:
            if pack is not None and vote.verdict in COMMITTED:
                vote = Vote(vote.verdict, normalise_chunk_id(vote.chunk), vote.quote)
            out[i] = vote
            continue
        out[i] = Vote("UNSURE", vote.chunk, vote.quote)
        rejected.append({"claim": i, "verdict": vote.verdict, "chunk": vote.chunk,
                         "quote": vote.quote[:MAX_QUOTE * 2], "reason": reason})
    return out, rejected


def rejected_counts(rejected: list) -> dict:
    """`{reason: n}` of *rejected* rows, only the reasons that occur."""
    counts: dict = {}
    for row in rejected:
        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
    return counts


# ------------------------------------------------------------------ one voter, one run

def batches(items: list, batch: int = 10, code_batch: int = 2) -> list:
    """The index lists one run sends: world claims (pack `None`) *batch* at a time, then the
    claims with a pack *code_batch* at a time, each in the claims' own order."""
    world = [i for i, (_c, p) in enumerate(items) if p is None]
    code = [i for i, (_c, p) in enumerate(items) if p is not None]
    out = []
    for group, size in ((world, max(1, batch)), (code, max(1, code_batch))):
        out += [group[s:s + size] for s in range(0, len(group), size)]
    return out


def ask_with_packs(items: list, run: int, seed: int,
                   completion_fn: Callable[[str], str], *, batch: int = 10,
                   code_batch: int = 2, fixed: bool = False) -> dict:
    """One voter's run over *items* (`(claim, pack_or_None)`), its votes checked.

    `{"votes": {i: verdict}, "accepted": {i: {"chunk", "quote"}}, "rejected": [...],
    "errors": [...], "raw_head": str}`: *votes* are the verdicts after `verify_quotes`,
    *accepted* the evidence of each committed vote on a claim with a pack. A batch whose
    call raises is an entry in *errors* and no votes, never an exception: a dead model is a
    result. *raw_head* is the head of the first reply nothing could be read from."""
    votes: dict = {}
    accepted: dict = {}
    rejected: list = []
    errors: list = []
    raw_head = ""
    for group in batches(items, batch, code_batch):
        shown = [items[i] for i in group]
        prompt, order = build_prompt_v2(shown, run, seed + group[0], fixed)
        try:
            text = completion_fn(prompt)
        except Exception as exc:   # noqa: BLE001 — a dead model is a result, not a crash
            errors.append(str(exc)[:120] or type(exc).__name__)
            continue
        got = parse_votes_v2(text, order)
        if not got and not raw_head:
            raw_head = (text if isinstance(text, str) else "")[:200]
        checked, bad = verify_quotes(got, {k: p for k, (_c, p) in enumerate(shown)})
        for k, vote in checked.items():
            i = group[k]
            votes[i] = vote.verdict
            if vote.verdict in COMMITTED and shown[k][1] is not None:
                accepted[i] = {"chunk": vote.chunk, "quote": vote.quote}
        rejected += [{**row, "claim": group[row["claim"]]} for row in bad]
    return {"votes": votes, "accepted": accepted, "rejected": rejected,
            "errors": errors, "raw_head": raw_head}
