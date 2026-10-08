"""Ticket 212 — the cache gate one agent's permission handler calls.

``TestCacheGate.ask`` is asked about every ``bash`` permission the policy was
about to let through. When the command is pytest runs and nothing else with an
effect, and the round's cache already holds each of those runs on this
worktree's present fingerprint, it answers with the one-line cached result (the
caller turns that into a ``reject`` the model reads as the tool's error);
otherwise it answers ``None`` and the command runs exactly as without a cache.
``TestCacheGate.harvest`` reads the finished ``bash`` parts of the session and
records the single-run ones that ended with a pytest summary line.

Nothing here raises into a run: every failure is "no answer".
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

from tools.contest import testcache
from tools.contest.testcache_store import TestRunCache, tree_fingerprint

_log = logging.getLogger(__name__)

#: the first word of a command part that has no effect on the tree or the world
#: worth worrying about; a pytest call wrapped in these is still just pytest
_HARMLESS = frozenset({
    "cd", "echo", "printf", "time", "date", "tail", "head", "true", "false",
    "exit", "set", "export", "unset", "wc", "grep", "cat", "ls", ":",
})
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECT = re.compile(r"^\d*>+&?\d*(/dev/null)?$")
_SPLIT = re.compile(r"&&|\|\||;|\||\n")
#: text that makes a command worth asking the model about when the parser reads
#: no pytest in it
_MAYBE_TESTS = re.compile(r"test|check|suite|ci\b|verify", re.I)


def run_key(run: "testcache.PytestRun") -> str:
    """The opaque cache key of a parsed run: roots and flags, order-free."""
    return json.dumps([sorted(run.roots), sorted(run.flags)])


def only_pytest_and_harmless(command: str) -> bool:
    """True when every part of *command* is a pytest call or a harmless helper.

    A part is judged by its first word (after leading ``VAR=…`` assignments and
    group punctuation); a command with any other word — ``rm``, ``git``,
    ``make``, ``python3 other.py`` — is not answered from the cache, because the
    cache would hide that part's effect.
    """
    flat = command.replace("⏎", "\n")
    for part in _SPLIT.split(flat):
        words = [w for w in part.replace("(", " ").replace(")", " ")
                 .replace("{", " ").replace("}", " ").split()
                 if w and not _REDIRECT.match(w)]
        while words and _ASSIGN.match(words[0]):
            words.pop(0)
        if not words:
            continue
        head = words[0].rsplit("/", 1)[-1]
        if head in _HARMLESS:
            continue
        if head in ("pytest", "py.test") or "pytest" in words[:4]:
            continue
        if head in ("python", "python3") and "pytest" in words[:4]:
            continue
        if head in ("nice", "ionice", "timeout", "env", "stdbuf") or words[0] == "/usr/bin/time":
            continue
        return False
    return True


class TestCacheGate:
    """One agent's view of the round's test-run cache."""

    __test__ = False  # not a pytest class

    def __init__(self, path, *, agent: str, share: str = "round",
                 classify: Optional[Callable] = None,
                 now: Callable[[], float] = time.time):
        self.cache = TestRunCache(path)
        self.agent = agent
        self.share = share if share in ("round", "agent") else "round"
        self._classify = classify
        self._now = now
        self._pending: dict = {}   # callID -> (runs, fingerprint)
        self._done: set = set()

    # ── what the command is ───────────────────────────────────────────────

    def _runs_of(self, command: str) -> list:
        runs = testcache.parse_pytest(command)
        if runs or self._classify is None or not _MAYBE_TESTS.search(command):
            return runs
        known, parsed = self.cache.classification(command)
        if known:
            return [_run_from_key(parsed)] if parsed else []
        run = None
        try:
            run = self._classify(command)
        except Exception:  # noqa: BLE001 — a failing model is "run directly"
            return []
        if run is None:
            return []   # not remembered: an overloaded model is not "not tests"
        self.cache.record_classification(command, run_key(run))
        return [run]

    # ── the ask ───────────────────────────────────────────────────────────

    def ask(self, props: dict, worktree) -> Optional[str]:
        """The cached one-line answer for this ask, or None to run as usual."""
        try:
            command = ((props.get("metadata") or {}).get("command")) or ""
            if not command:
                return None
            parsed = testcache.parse_pytest(command)
            if parsed:
                if not only_pytest_and_harmless(command):
                    return None
                runs = parsed
            else:
                # the parser reads no pytest: only a single plain command (a
                # script, `make test`) may be put to the gate model — a chain
                # could hide an effect behind the one name it is asked about
                if _SPLIT.search(command.strip()):
                    return None
                runs = self._runs_of(command)
            if not runs:
                return None
            fp = tree_fingerprint(worktree)
            if fp is None:
                return None
            lines = []
            for run in runs:
                entry = self.cache.lookup(run_key(run), fp)
                if entry is None or (self.share == "agent" and entry.get("agent") != self.agent):
                    callid = (props.get("tool") or {}).get("callID")
                    if callid:
                        self._pending[callid] = (runs, fp)
                    return None
                summary = testcache.Summary(**{
                    k: v for k, v in (entry.get("summary") or {}).items()
                    if k in {f.name for f in dataclasses.fields(testcache.Summary)}})
                lines.append(testcache.progress_line(
                    summary, age_s=max(0.0, self._now() - float(entry.get("t") or 0)),
                    agent=str(entry.get("agent") or "?"), wall_s=entry.get("wall_s")))
            return "cached: " + " | ".join(lines)
        except Exception as exc:  # noqa: BLE001 — never into a run
            _log.warning("test cache ask failed: %s: %s", type(exc).__name__, exc)
            return None

    # ── the record ────────────────────────────────────────────────────────

    def harvest(self, parts) -> int:
        """Record the finished single-run pytest calls among *parts*; the count."""
        n = 0
        try:
            for part in parts or ():
                if not isinstance(part, dict) or part.get("tool") != "bash":
                    continue
                callid = part.get("callID")
                if callid not in self._pending or callid in self._done:
                    continue
                state = part.get("state") or {}
                if state.get("status") != "completed":
                    continue
                runs, fp = self._pending.pop(callid)
                self._done.add(callid)
                if len(runs) != 1:
                    continue
                summary = testcache.summarise(str(state.get("output") or ""), None)
                if summary is None:
                    continue
                t = state.get("time") or {}
                wall = (t["end"] - t["start"]) / 1000.0 if t.get("start") and t.get("end") else 0.0
                self.cache.record(run_key(runs[0]), fp, dataclasses.asdict(summary),
                                  None, wall, self.agent)
                n += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning("test cache harvest failed: %s: %s", type(exc).__name__, exc)
        return n


def _run_from_key(key: Optional[str]) -> "testcache.PytestRun":
    roots, flags = json.loads(key) if key else ([], [])
    return testcache.PytestRun(roots=frozenset(roots), flags=frozenset(flags))
