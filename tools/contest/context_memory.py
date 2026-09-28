"""tools/contest/context_memory.py — KC-67: a context overflow is remembered for
7 days, and the next session compacts at 80 % of it.

Kilo compacts a session on its own only when it knows the model's context size.
For the models it does not know — the provider declares no limit and neither
does the Kilo config — the session grows until the provider rejects it with a
context overflow. Round 74, 112 and 113 overflowed like that 12 times on one
machine, the same model at the same size in every round: sensenova-6.7 at
exactly 262 144 tokens each time.

The provider's answer already carries the size. Sometimes it names the limit —
``This model's maximum context length is 262144 tokens. However, you requested
32000 output tokens and your prompt contains at least 262514 input tokens`` —
and sometimes it does not (kenary's ``exceeds the maximum context length``), in
which case the last reply that still went through is the best number available.
This module is the one place that keeps either of them:

  * ``parse_overflow`` reads the limit and the prompt out of the provider's
    message, ``None`` for either when the message does not name it;
  * ``add`` appends one record to the file shared by every round, atomically,
    dropping the records older than ``context_memory_days`` on the way;
  * ``load`` reads that file back with the same age cut, ``[]`` for a file that
    is missing, unreadable, not a JSON list or not a record — no memory is never
    a failed round;
  * ``smallest_size`` answers, per provider and model, with the smallest size
    any record remembers: the named limit when a record has one, else its
    ``last_ok``. kenary's ``last_ok`` varies a lot for one model (hy3 104 065 –
    149 359 in 8 rounds), because one step can add tens of thousands of tokens,
    so the smallest one is the safe choice — it compacts earlier than needed,
    never later.

Every function here is fail-open in both directions: an absent memory, a value
that is not a number and a write that cannot be made all come back as "no
collect data", never an exception. The runner reads this module once before a
prompt and once per overflow, and nothing in it knows about a round.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_COMPACT_AT_PERCENT",
    "DEFAULT_DAYS",
    "DEFAULT_FILENAME",
    "DEFAULT_OUTPUT_RESERVE",
    "DEFAULT_SUMMARY_AT_PERCENT",
    "KILO_COMPACT_RESERVE",
    "OverflowRecord",
    "add",
    "compact_at_percent",
    "summary_at_percent",
    "summary_file_name",
    "days_of",
    "kilo_limit",
    "load",
    "memory_path",
    "parse_output",
    "parse_overflow",
    "plan_lines",
    "remembered",
    "size_of",
    "smallest_size",
]

_LOG = logging.getLogger(__name__)

#: The default age a record is kept (KC-67), in days.
DEFAULT_DAYS = 7.0
#: The default memory file, next to the rounds' output.
DEFAULT_FILENAME = "context-memory.json"
#: KC-69: the output Kilo reserves when the provider named none — Kilo's own
#: default maximum output, and the 32 000 sensenova's overflow says was asked.
DEFAULT_OUTPUT_RESERVE = 32000
#: KC-69: the tokens Kilo 7.6.2 keeps free under ``limit.input`` when the config
#: sets no ``compaction.reserved`` — ``min(20 000, the model's output)``.
KILO_COMPACT_RESERVE = 20000
#: The default fill, as a percent of the remembered size, at which the runner
#: compacts a session that has no limit of its own. 0 turns the compact off.
DEFAULT_COMPACT_AT_PERCENT = 80.0
#: KC-40: the default fill, as a percent of a known size, at which the runner
#: asks a session that holds an uncommitted diff for its own account of it,
#: before a compact shrinks the history that explains the diff. Kept above
#: `DEFAULT_COMPACT_AT_PERCENT` on purpose: at the default numbers the ask fires
#: at 90 %, and a fill there has already earned the compact at 80 % — so the
#: summary is what goes out first of the two. Set it below the compact's percent
#: to ask earlier, of a session the compact has not reached yet. 0 turns the
#: summary off, and a value at or above 100 turns it off too — a summary is
#: asked of a session that still has room to answer it.
DEFAULT_SUMMARY_AT_PERCENT = 90.0
_DAY = 86400.0

#: The limit the provider names for itself. The sensenova wording is the one in
#: the ticket's table, kenary's carries no number at all, and OpenRouter's
#: ``context_length_exceeded`` puts the two numbers after a colon.
_LIMIT_RE = re.compile(
    r"(?:maximum\s+context\s+length|context\s+length|context\s+window|context\s+limit|"
    r"context_length_exceeded|limit)\s*(?:of|is|:|=)?\s*(\d[\d,]*)",
    re.IGNORECASE,
)
#: The prompt the provider names, when it names one: the input it counted, not
#: the output it was asked for.
_PROMPT_RE = re.compile(
    r"prompt\s+contains(?:\s+at\s+least)?\s+(\d[\d,]*)\s*input\s*tokens?", re.IGNORECASE
)


#: KC-69: the output the provider reserved on top of the prompt — ``you requested
#: 32000 output tokens`` — which the prompt budget does not get to use.
_OUTPUT_RE = re.compile(r"requested\s+(\d[\d,]*)\s*output\s*tokens?", re.IGNORECASE)


def _number(value) -> int | None:
    """A token count from a message or a file: a positive int, else ``None``.

    Thousands separators are dropped, and everything that is not a number —
    ``None``, ``""``, ``"262,14"`` — is ``None``: a size the runner cannot
    trust is no size at all, and no size is what a fresh session gets today.
    ``bool`` is not a number here either, where ``True`` would be 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        raw = value.replace(",", "").strip()
    elif isinstance(value, (int, float)):
        raw = str(int(value))
    else:
        return None
    try:
        out = int(raw)
    except ValueError:
        return None
    return out if out > 0 else None


def parse_overflow(message: object) -> tuple[int | None, int | None]:
    """The two numbers an overflow message carries: ``(limit, prompt)``.

    *message* is the text ``_is_overflow`` already matched — ``data.message``, a
    top-level ``message`` or the payload's ``name`` — and either half is
    ``None`` when the provider does not name it: kenary's
    ``exceeds the maximum context length`` names neither, so a record with only
    its ``last_ok`` is the right shape for it. Anything that is not a string is
    ``(None, None)``.
    """
    text = message if isinstance(message, str) else ""
    limit = prompt = None
    limit_match = _LIMIT_RE.search(text)
    if limit_match is not None:
        limit = _number(limit_match.group(1))
    prompt_match = _PROMPT_RE.search(text)
    if prompt_match is not None:
        prompt = _number(prompt_match.group(1))
    return limit, prompt


def parse_output(message: object) -> int | None:
    """KC-69: the output tokens the provider says were requested, else ``None``.

    sensenova's ``you requested 32000 output tokens`` is the part of its named
    limit the prompt never gets; kenary names nothing, so ``None`` there.
    """
    match = _OUTPUT_RE.search(message) if isinstance(message, str) else None
    return _number(match.group(1)) if match is not None else None


@dataclass(frozen=True)
class OverflowRecord:
    """One overflow, one line of the shared file.

    ``at`` is the time it happened, ``round`` the round that saw it and
    ``agent`` the model that produced it; ``provider`` and ``model`` are the
    two halves the runner already splits, so the file and the roster speak the
    same id. ``limit`` is the size the provider named, ``last_ok`` the context
    of the last reply that still went through, ``prompt`` the input the
    provider counted — any of the three ``None`` when it is not known.
    ``output`` (KC-69) is the output the provider reserved on top of the
    prompt, ``None`` for a record that did not name one — every record written
    before KC-69. ``grew`` (KC-73) is what the tool results of that last reply
    added before the request that overflowed, in tokens estimated from their
    text; ``None`` when it was not measured — every record written before it.
    """

    at: float
    round: str
    agent: str
    provider: str
    model: str
    limit: int | None = None
    last_ok: int | None = None
    prompt: int | None = None
    output: int | None = None
    grew: int | None = None

    def to_dict(self) -> dict:
        """The shape it has in the file, keys in order for a stable diff."""
        return {
            "at": self.at,
            "round": self.round,
            "agent": self.agent,
            "provider": self.provider,
            "model": self.model,
            "limit": self.limit,
            "last_ok": self.last_ok,
            "prompt": self.prompt,
            "output": self.output,
            "grew": self.grew,
        }

    @classmethod
    def from_dict(cls, data) -> "OverflowRecord | None":
        """One entry of the file, or ``None`` when it is not a record.

        ``None`` is the answer for an entry without a usable time, or without
        the provider and model the runner looks the size up by: such a line
        cannot be matched anyway, and keeping it would only make the file grow.
        The three sizes are normalised, so a stray ``""`` or a zero never
        reaches a comparison.
        """
        if not isinstance(data, dict):
            return None
        try:
            at = float(data.get("at"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(at):
            return None
        provider = data.get("provider")
        model = data.get("model")
        if not isinstance(provider, str) or not provider.strip():
            return None
        if not isinstance(model, str) or not model.strip():
            return None
        rnd = data.get("round")
        agent = data.get("agent")
        return cls(
            at=at,
            round=rnd if isinstance(rnd, str) else "",
            agent=agent if isinstance(agent, str) else "",
            provider=provider,
            model=model,
            limit=_number(data.get("limit")),
            last_ok=_number(data.get("last_ok")),
            prompt=_number(data.get("prompt")),
            output=_number(data.get("output")),
            grew=_number(data.get("grew")),
        )


#: KC-73: a ``last_ok`` sizes a model only when the request that overflowed
#: grew less than this share on top of it (`size_of`).
LOOSE_FLOOR_SHARE = 0.25


def size_of(record: OverflowRecord) -> int | None:
    """The size one record remembers: the limit the provider named, else its
    ``last_ok`` — the last reply that still went through, which is the only
    number a provider that names no limit ever gave.

    KC-69: a limit named together with the output reserved on top of it is
    ``limit - output`` — sensenova's 262 144 with 32 000 requested is a prompt
    budget of 230 144, and a prompt past that overflows. A record without the
    output, or with one that would leave nothing, keeps the limit as before.

    KC-73: a ``last_ok`` the overflow jumped far past is no size. The window
    lies between ``last_ok`` and ``last_ok + grew``; when ``grew`` is more than
    `LOOSE_FLOOR_SHARE` of ``last_ok`` that range says nothing a session can
    compact by. Live, agnes-2-0-flash: 17 382 OK, then one step of reads added
    ~269 000 and overflowed a ~250 000 window; glm-4-7-flash: 14 179 OK plus
    ~134 000 past a ~120 000 one. Sized at 80 % of those floors the runner
    compacted every turn and the agents gave up. Such a record sizes nothing;
    the next overflow that grows into the window step by step will.
    """
    if record.limit and record.output and record.limit > record.output:
        return record.limit - record.output
    if record.limit:
        return record.limit
    if (record.last_ok and record.grew is not None
            and record.grew > record.last_ok * LOOSE_FLOOR_SHARE):
        return None
    return record.last_ok


def _pick(records, provider: str, model: str):
    """``(size, output)`` for ``provider`` / ``model`` out of *records*, else ``None``.

    A record that names a limit is a wall: the smallest of those wins, as it
    always did. A record that names none holds only its ``last_ok`` — the last
    reply that still went through, which proves the window is *at least* that
    big and says nothing about how much bigger. So of those, the largest wins:
    one overflow whose last step read a pile of files at once (live, KC-73's
    agnes-2-0-flash: 17 382 OK, then one step past the window) must not size a
    model that another session proved at 254 613 — at 80 % of 17 382 the runner
    compacted every turn and the agent gave up. The two are then combined as
    before, the smaller wins: hy3's 104 065 over a named 262 144 still stands.
    """
    if not isinstance(records, (list, tuple)):
        return None
    wall = floor = None
    for entry in records:
        record = entry if isinstance(entry, OverflowRecord) else OverflowRecord.from_dict(entry)
        if record is None or record.provider != provider or record.model != model:
            continue
        size = size_of(record)
        if size is None:
            continue
        if record.limit:
            if wall is None or size < wall[0]:
                wall = (size, record.output)
        elif floor is None or size > floor[0]:
            floor = (size, record.output)
    if wall is not None and floor is not None:
        return wall if wall[0] <= floor[0] else floor
    return wall or floor


def remembered(records, provider: str, model: str) -> tuple[int | None, int | None]:
    """KC-69: ``(size, output)`` of the record that sizes ``provider`` /
    ``model`` — :func:`smallest_size` plus the output that record reserved,
    ``None`` when it named none. ``(None, None)`` for nothing remembered.
    """
    best = _pick(records, provider, model)
    return best if best is not None else (None, None)


def kilo_limit(size, output, percent: float = DEFAULT_COMPACT_AT_PERCENT) -> dict | None:
    """KC-69: the ``limit`` a remembered size becomes in Kilo's config, else ``None``.

    *size* is the prompt budget (`size_of`). Kilo 7.6.2 compacts inside a turn
    after every step whose context reaches ``limit.input - reserved`` —
    ``reserved`` is ``min(20 000, output)`` unless the config sets it, and the
    preflight ``compaction.threshold_percent`` is skipped mid-turn (live, laguna:
    one compact at a turn's start, then 53 % inside the turn). So ``input`` is
    *percent* of the budget plus Kilo's reserve: the step past *percent* of the
    budget compacts, the same share the runner's own gate uses.

    ``context`` stays the model's real window, ``size + output`` — Kilo treats it
    as a hard wall, and a cut one ends turns in ``Compaction exhausted`` (live,
    sensenova-6.7 at a wall cut to 30 %). ``output`` is always in the dict —
    ``DEFAULT_OUTPUT_RESERVE`` when the provider named none: a ``limit`` without
    it is ``ConfigInvalidError``, and Kilo skips the WHOLE ``KILO_CONFIG_CONTENT``
    (live, laguna). *percent* 0 or out of range is ``input = size``, Kilo's own
    compact at the full budget.
    """
    size = _number(size)
    if size is None:
        return None
    reserve = _number(output) or DEFAULT_OUTPUT_RESERVE
    try:
        share = float(percent)
    except (TypeError, ValueError):
        share = 0.0
    if math.isfinite(share) and 0 < share < 100:
        at = min(size, int(size * share / 100) + min(KILO_COMPACT_RESERVE, reserve))
    else:
        at = size
    return {"context": size + reserve, "input": at, "output": reserve}


def smallest_size(records, provider: str, model: str) -> int | None:
    """The size remembered for ``provider`` / ``model``, else ``None``.

    The smallest named limit, or the largest ``last_ok`` of the records that
    name none, whichever is smaller (:func:`_pick`). Only records of that exact
    provider and model count — one model's overflow says nothing about its
    neighbour's — and a record without either a limit or a ``last_ok`` counts
    for nothing at all. Anything that is not a list of records is ``None``: no
    size, the way today's session is treated.
    """
    best = _pick(records, provider, model)
    return best[0] if best is not None else None


def load(path, *, days: float = DEFAULT_DAYS, now: float | None = None) -> list[OverflowRecord]:
    """The records of *path* still inside *days* of *now*, in file order.

    A missing file, an unreadable one, a file that is not JSON, a JSON value
    that is not a list, an entry that is not a record and a *days* at or below
    zero all come back as ``[]`` — never an exception, because the only other
    answer is a round that refused to start for want of a memory file. Records
    past the age cut are ignored here as well as dropped by :func:`add`, so a
    file an earlier version of the runner left behind ages out on its own.
    """
    try:
        span = float(days)
        keep = span * _DAY if math.isfinite(span) else 0.0
        stamp = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        # a *days* that is not a number of days is no age cut at all: a record
        # that cannot be aged is one that must not be trusted by anybody
        return []
    if not keep > 0 or not math.isfinite(stamp):
        return []
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for entry in data:
        record = OverflowRecord.from_dict(entry)
        if record is None or record.at < stamp - keep:
            continue
        out.append(record)
    return out


def add(path, record: OverflowRecord, *, days: float = DEFAULT_DAYS,
        now: float | None = None) -> bool:
    """One overflow into *path*: the records still inside *days*, plus this one.

    Written atomically — a temp file in the same directory, then
    ``os.replace`` — so a crash never leaves a half-written memory, which is
    exactly the file the next round would read. A parent that does not exist is
    made on the way, and ``True`` back.

    ``False`` rather than an exception for every failure: a read-only parent, a
    path that is a directory, a record that is not a record. The overflow that
    asked for the line still ends the turn the way KC-54 does; the line is for
    the next round, not for this one.
    """
    if not isinstance(record, OverflowRecord):
        return False
    try:
        at = float(record.at)
        if not math.isfinite(at):
            return False
        # the age cut is *now*, not the record's own time: a record written with
        # a back-dated `at` must not prune its neighbours to its own age
        stamp = time.time() if now is None else float(now)
        span = float(days)
        keep = span * _DAY if math.isfinite(span) else 0.0
        # no age cut is no memory at all (context_memory_days = 0): a file that
        # can never be pruned grows one line per overflow, forever
        if not keep > 0 or not math.isfinite(stamp):
            return False
        target = Path(path)
        existing = [entry.to_dict() for entry in load(path, days=days, now=stamp)]
        existing.append(record.to_dict())
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                   dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(existing, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, str(target))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:  # noqa: BLE001 — a failed write is a warning, never a round
        _LOG.warning("context memory %s: %s: %s", path, type(exc).__name__, exc)
        return False


def memory_path(config, out_dir) -> Path:
    """The shared file: ``[contest] context_memory_file`` when it names a path,
    else ``<out_dir>/../context-memory.json`` — next to the rounds' output, so
    every round on a machine shares the one file.

    A config without the key, with an empty one, or that cannot be read at all
    is the default. *out_dir* is the round's own directory,
    ``contest-out/<NN>``: its parent is where the rounds land, and that is the
    file every round of the round's machine reads and writes.
    """
    raw = ""
    if config is not None:
        try:
            raw = str(getattr(config, "context_memory_file", "") or "")
        except Exception:  # noqa: BLE001 — a config that cannot be read is the default
            raw = ""
    raw = raw.strip()
    if raw:
        return Path(raw).expanduser()
    try:
        return Path(out_dir).resolve().parent / DEFAULT_FILENAME
    except (OSError, TypeError, ValueError):
        return Path(DEFAULT_FILENAME)


def days_of(config) -> float:
    """``[contest] context_memory_days``: the age a record is kept, in days.

    The default when the key is absent; ``0.0`` when it is present but not a
    number of days above zero, because "no memory" is the safe answer for both
    — a record that cannot be aged is one that must not be trusted. Anything
    here is ``0.0``, never an exception.
    """
    if config is None:
        return float(DEFAULT_DAYS)
    try:
        days = float(getattr(config, "context_memory_days", DEFAULT_DAYS))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(days) or days <= 0:
        return 0.0
    return days


def compact_at_percent(config) -> float:
    """``[contest] compact_at_percent``: the fill, as a percent of the
    remembered size, at which the runner compacts before the next prompt.

    The default is 80. ``0`` is what it means: the mechanism off, which is how
    a round turns it down. A missing key, a value that is not a number, one
    below zero and one above 100 all fall back to the default — a typo must not
    silently stop the compact, and a percent above 100 would never fire.
    """
    if config is None:
        return float(DEFAULT_COMPACT_AT_PERCENT)
    try:
        value = float(getattr(config, "compact_at_percent", DEFAULT_COMPACT_AT_PERCENT))
    except (TypeError, ValueError):
        return float(DEFAULT_COMPACT_AT_PERCENT)
    if not math.isfinite(value) or value < 0 or value > 100:
        return float(DEFAULT_COMPACT_AT_PERCENT)
    return value


def summary_at_percent(config) -> float:
    """KC-40: ``[contest] summary_at_percent`` — the fill, as a percent of a
    known size, at which a session that already holds an uncommitted diff is
    asked for its own summary, before anything is compacted away.

    The default is 90. ``0`` is what it means: the mechanism off, which is how
    a round turns it down. A missing key, a value that is not a number, one
    below zero and one above 100 all fall back to the default — a typo must not
    silently stop the summary, and a percent above 100 could never fire. Nothing
    here is an exception.
    """
    if config is None:
        return float(DEFAULT_SUMMARY_AT_PERCENT)
    try:
        value = float(getattr(config, "summary_at_percent", DEFAULT_SUMMARY_AT_PERCENT))
    except (TypeError, ValueError):
        return float(DEFAULT_SUMMARY_AT_PERCENT)
    if value == 0:
        return 0.0
    if not math.isfinite(value) or value < 0 or value > 100:
        return float(DEFAULT_SUMMARY_AT_PERCENT)
    return value


def summary_file_name(agent) -> str:
    """KC-40: the summary's file name for *agent* — ``<agent>.summary.md``.

    The operator opens one file per agent to read the chain of summaries the
    round asked for, whatever the server did to the sessions that wrote them.
    An agent name that is not a string, or one that would name another path
    component, gets the ``agent`` prefix and nothing else — the file always
    lands flat in the round's own directory.
    """
    name = agent if isinstance(agent, str) else ""
    safe = "".join(c for c in name if c.isalnum() or c in "_-.")
    return f"{safe or 'agent'}.summary.md"


def plan_lines(records, agents, *, percent: float = DEFAULT_COMPACT_AT_PERCENT) -> list[str]:
    """One line per model that has a remembered size, for the round's plan.

    *agents* is the round's roster, in its own order; a model with nothing
    remembered gets no line, because a line for it would say nothing. ``[]`` for
    an empty memory and for anything that is not a roster — the plan prints
    what it already prints, and no more.
    """
    if not isinstance(agents, (list, tuple)):
        return []
    lines = []
    for spec in agents:
        provider = getattr(spec, "provider_id", "")
        model = getattr(spec, "model_id", "")
        if not isinstance(provider, str) or not isinstance(model, str):
            continue
        size = smallest_size(records, provider, model)
        if size is None:
            continue
        lines.append(f"context memory: {provider}/{model} = {size} "
                     f"(compact at {percent:g} %)")
    return lines
