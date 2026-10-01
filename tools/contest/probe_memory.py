"""tools/contest/probe_memory.py — KC-70: a named variant that answered is
remembered for 24 hours, and the next intake asks it nothing.

KC-61 made intake ask every named variant (``--variant high``, ``@max``,
``@xhigh`` and whatever tag a model lists) to ``say: hello`` before the round,
so a dry key is seen at intake instead of fifteen minutes into the round. That
ask is a real model call: round 87 spent 70 s of its start on six of them, one
after the other, ~17 s each — and the next ``--fresh`` of the same roster asked
the same six again a minute later. KC-11's ``contest-probe.json`` caches
``highest`` only; a named variant was never remembered.

This module is that memory, the shape of KC-67's ``context-memory.json``: a JSON
list of records in a file shared by every round of the machine, each record one
``provider/model@variant`` that answered, with the time it answered.

  * ``load`` reads the records still inside ``probe_memory_hours`` of now, ``[]``
    for a file that is missing, unreadable, not a JSON list or not a record —
    no memory is a probe, never a failed round;
  * ``answered`` says whether a fresh record exists for one
    ``provider/model@variant`` — the only question intake asks;
  * ``update`` writes the new successes and drops the keys a live probe just saw
    fail, in one atomic write that also evicts every record past the age cut
    and every older duplicate of a key — so the file holds at most one record
    per ``provider/model@variant``, none older than the cut.

Only a success is remembered. A refusal, a quota or a timeout is asked again
next time: the round must never start on a variant whose last word was a
failure, and a quota resets on its own clock, not on this one.

Every function here is fail-open: an absent memory, a value that is not a
number and a write that cannot be made all come back as "nothing remembered",
never an exception into intake.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_FILENAME",
    "DEFAULT_HOURS",
    "FUTURE_SLACK_SEC",
    "ProbeRecord",
    "answered",
    "hours_of",
    "load",
    "memory_path",
    "update",
]

_LOG = logging.getLogger(__name__)

#: The default age a success is trusted (KC-70), in hours.
DEFAULT_HOURS = 24.0
#: The default memory file, next to the rounds' output and KC-67's memory.
DEFAULT_FILENAME = "probe-memory.json"
#: A record stamped further than this into the future is not trusted: a clock
#: that was set back would otherwise keep it fresh for as long as the skew.
FUTURE_SLACK_SEC = 300.0
_HOUR = 3600.0


@dataclass(frozen=True)
class ProbeRecord:
    """One ``provider/model@variant`` that answered ``say: hello`` at ``at``."""

    at: float
    provider: str
    model: str
    variant: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.variant)

    def to_dict(self) -> dict:
        """The shape it has in the file, keys in order for a stable diff."""
        return {"at": self.at, "provider": self.provider, "model": self.model,
                "variant": self.variant}

    @classmethod
    def from_dict(cls, data) -> "ProbeRecord | None":
        """One entry of the file, or ``None`` when it is not a record.

        A record needs a finite time and three non-empty strings: without any
        of them it can be matched against nothing, and keeping it would only
        make the file grow.
        """
        if not isinstance(data, dict):
            return None
        raw_at = data.get("at")
        if isinstance(raw_at, bool):
            return None
        try:
            at = float(raw_at)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(at):
            return None
        fields = []
        for name in ("provider", "model", "variant"):
            value = data.get(name)
            if not isinstance(value, str) or not value.strip():
                return None
            fields.append(value)
        return cls(at, *fields)


def _window(hours, now) -> tuple[float, float] | None:
    """``(oldest, newest)`` times a record may carry, or ``None`` for no memory.

    *hours* at or below zero, or not a number of hours, is no memory at all —
    a record that cannot be aged is one nobody may trust.
    """
    try:
        span = float(hours)
        stamp = time.time() if now is None else float(now)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(span) or not math.isfinite(stamp) or span <= 0:
        return None
    return stamp - span * _HOUR, stamp + FUTURE_SLACK_SEC


def _read(path) -> list:
    """The file's raw JSON list, ``[]`` for anything else."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _fresh(entries, window) -> list[ProbeRecord]:
    """The records inside *window*, one per key — the newest one — in file order."""
    oldest, newest = window
    latest: dict = {}
    for entry in entries:
        record = entry if isinstance(entry, ProbeRecord) else ProbeRecord.from_dict(entry)
        if record is None or not oldest <= record.at <= newest:
            continue
        held = latest.get(record.key)
        if held is None or record.at >= held.at:
            latest[record.key] = record
    return list(latest.values())


def load(path, *, hours: float = DEFAULT_HOURS, now: float | None = None) -> list[ProbeRecord]:
    """The records of *path* still inside *hours* of *now*.

    Past the age cut, stamped in the future beyond :data:`FUTURE_SLACK_SEC`,
    malformed, or a duplicate older than another record of the same key: none
    of those comes back. A missing or unreadable file, or *hours* at or below
    zero, is ``[]``.
    """
    window = _window(hours, now)
    if window is None:
        return []
    return _fresh(_read(path), window)


def answered(records, provider: str, model: str, variant: str) -> bool:
    """True when *records* hold a success for ``provider/model@variant``.

    *records* is what :func:`load` returned — already cut to the age — so this
    is a lookup, not a second clock.
    """
    if not isinstance(records, (list, tuple)):
        return False
    key = (provider, model, variant)
    return any(isinstance(r, ProbeRecord) and r.key == key for r in records)


def update(path, successes=(), failures=(), *, hours: float = DEFAULT_HOURS,
           now: float | None = None) -> bool:
    """Write *successes* in, take *failures* out, evict the rest by age.

    *successes* are ``(provider, model, variant)`` keys that answered just now
    and are stamped *now*; *failures* are keys a live probe just saw fail, and
    every record of them is dropped — a success an earlier run remembered must
    not outlive the probe that contradicts it. Everything else in the file is
    kept only while inside the age cut, one record per key.

    Nothing is written when the kept list equals what the file holds, so an
    intake that asked nothing and found nothing to evict leaves the file alone;
    nor when there is nothing to keep and no file yet.
    A parent that does not exist is made. The write is atomic — a temp file in
    the same directory, then ``os.replace``. ``True`` when the file on disk now
    holds the answer; ``False`` for every failure, never an exception.
    """
    try:
        window = _window(hours, now)
        if window is None:
            return False
        stamp = time.time() if now is None else float(now)
        drop = {tuple(key) for key in failures}
        raw = _read(path)
        records = [r for r in _fresh(raw, window) if r.key not in drop]
        fresh = {tuple(key) for key in successes} - drop
        records = [r for r in records if r.key not in fresh]
        records.extend(ProbeRecord(stamp, *key) for key in sorted(fresh))
        out = [record.to_dict() for record in records]
        target = Path(path)
        # nothing to keep and no file yet: an intake that remembered nothing
        # leaves no empty file behind
        if (out == raw and target.is_file()) or (not out and not target.exists()):
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                   dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(out, fh, ensure_ascii=False, indent=2)
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
        _LOG.warning("probe memory %s: %s: %s", path, type(exc).__name__, exc)
        return False


def memory_path(config, repo) -> Path:
    """``[contest] probe_memory_file`` when it names a path, else
    ``<repo>/<out_dir>/probe-memory.json`` — next to KC-67's
    ``context-memory.json`` when ``--out`` is not given, shared by every round.

    A relative ``probe_memory_file`` is relative to *repo*, as ``out_dir`` is.
    """
    raw = ""
    out_dir = "contest-out"
    if config is not None:
        try:
            raw = str(getattr(config, "probe_memory_file", "") or "").strip()
            out_dir = str(getattr(config, "out_dir", "") or "contest-out")
        except Exception:  # noqa: BLE001 — a config that cannot be read is the default
            raw = ""
    base = Path(repo) if repo is not None else Path.cwd()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else base / path
    return base / out_dir / DEFAULT_FILENAME


def hours_of(config) -> float:
    """``[contest] probe_memory_hours``: how long a success is trusted.

    The default when the key is absent; ``0.0`` — no memory, every intake asks
    — when it is present but not a number of hours above zero.
    """
    if config is None:
        return float(DEFAULT_HOURS)
    try:
        hours = float(getattr(config, "probe_memory_hours", DEFAULT_HOURS))
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(hours) or hours <= 0:
        return 0.0
    return hours
