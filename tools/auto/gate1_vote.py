"""CC-10 — claim check in Gate 1: three voters with the code and a quote, instead of one reader
(docs/claim-check/tickets/292-cc-10-claim-check-in-gate1.md, epic-tasks/292-…).

Gate 1's Stage B asks one model, shown a code block, whether the problem a task names is
present. CC-6 already does the harder version of that job: voters of several families, each
answering with a verbatim quote of the evidence, the quote checked by code, a verdict accepted
only when the committed voters agree. This module asks that question for one candidate:

    "`<symbol>` in `<file>` has the problem: <the instruction>"

and says what the vote decided:

* unanimous ``TRUE`` (three voters, three families, quotes verified)  -> ``confirmed`` with
  ``vote_confirm = on`` (the default); with ``off`` the vote only vetoes and the single reader
  decides, as for no decision;
* unanimous ``FALSE``                                                  -> ``rejected``, with the
  voters' quotes as the reason — unless the single reader (``vote_veto_by_reader = on``, the
  default) says ``confirmed``, which is counted as ``votes_overridden``;
* anything else (``SPLIT``, ``UNSURE``, fewer than three committed, fewer than three
  families, a voter with no vote under ``vote_accept = all``, a cited file that is not in the
  tree, an empty instruction, a claim with no code anchor, the cap reached, any error)
  -> no decision, and **Stage B as it is today decides**.

What that does to Gate 1, both ways (measured by the first live run of layer 2,
`GATE1-VOTE-LAYER2.md` §6, which is why ``vote_confirm = off`` exists):

* a ``TRUE`` it confirms was never asked of the reader, so the reader's own grounds to reject (a
  docstring that says the code is a deliberate example, an intentional design, a test helper) are
  not applied: with ``vote_confirm = on`` the vote can be **less strict** than the reader. On the
  first known-answer set 8 of 10 such confirmations were wrong;
* a unanimous ``FALSE`` stands when the reader did not say ``confirmed``, and a reader that gave no
  answer at all did not say it (ticket question 2): there the vote is **stricter** than the reader
  under ``presence_unknown = keep``, which would have kept the candidate. All eight unanimous
  ``FALSE`` of that run were right.

A voter is a profile section (of the ini Gate 1 runs with, else of ``contest.ini`` /
``contest.local.ini``, where ``claim_vote.py --add-profiles`` writes them) or Kilo's ``provider/model``;
its family is read off the **model** the section names, never off the section's name. A voter that
cannot be resolved (checked once, before the first vote) is named in the log and left out; one that gives
no reply twice in a row (nothing by the vote's bound, its request failed after its retries, or it raised) is dropped
for the rest of the run, and the vote stops when what is left could no longer decide. The vote waits as
long as the claim_vote pacer's queue needs (voters of one host start two at a time, four seconds apart),
the requests of the other votes still in flight on that host included (``presence_workers`` > 1: the
requests reach the pacer in the order their votes queued them), and each request has 2 x
vote_timeout_sec once it has its slot; a voter given up on stops retrying, and its thread never holds
the process at exit.

Nothing here raises: a voter that raises, a pack that cannot be built, an ini that is wrong are
each a "no decision" and a line in the log. ``[gate1] claim_vote = off`` (the default) builds no
``Gate1Vote`` at all, so Gate 1 is byte for byte what it was.

``[gate1]`` keys (read by `VoteConfig.from_config`)::

    claim_vote          = off | on
    vote_profiles       = <ref>, <ref>, <ref>    ; a profile section name, or provider/model
    vote_max_per_run    = 200                    ; candidates that may be voted on in one run
    vote_timeout_sec    = 60                     ; one HTTP request of one voter (seconds, a fraction kept)
    vote_confirm        = on                     ; off: the vote only vetoes (a unanimous TRUE is the reader's)
    vote_skip_tests     = off                    ; on: a candidate citing a test file is not put to the voters
                                                 ; (tools/collect/test_paths, test dirs, a test_*.py that holds a test)
    vote_veto_by_reader = on                     ; a unanimous FALSE also needs the reader not
                                                 ; to say confirmed
    vote_accept         = all | committed        ; CC-11: with five or more voters, `committed`
                                                  ; lets an abstainer stop blocking (default all)
    vote_quorum         = 3                      ; not a knob: any other value is refused (CC-10 Q5)
    vote_collect        = off                    ; CC-13: auto = the vote's pack carries the collect
                                                  ; model's facts when the artifact of the run's base
                                                  ; directory is fresh (or stale and staleness = warn)
"""

from __future__ import annotations

import configparser
import logging
import math
import re
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tools.claimcheck import anchors as cc_anchors
from tools.claimcheck import truth as cc_truth
from tools.collect import test_paths as _test_paths
from tools.claimcheck.pack import PackBudget, build_pack

logger = logging.getLogger(__name__)

SECTION = "gate1"
QUORUM = 3
DEFAULT_MAX_PER_RUN = 200
DEFAULT_TIMEOUT_SEC = 60.0
#: a one-line instruction is a claim; a longer one is cut here (the claim is judged, not the essay)
MAX_INSTRUCTION = 400
#: times in one run a voter may miss the vote's time bound before it is dropped for the rest of it
MAX_MISSES = 2
#: the vote waits this past the end of its last request (`Gate1Vote._bound`): the request stops there itself
VOTE_SLACK_SEC = 5.0
#: where ``contest.ini`` / ``contest.local.ini`` live (the project root); a test points it elsewhere
LOCAL_ROOT = Path(__file__).resolve().parents[2]
LOCAL_FILES = ("contest.ini", "contest.local.ini")

#: [gate1] vote_confirm when the ini does not set it (on, as CC-10 landed; off = the vote only vetoes)
DEFAULT_CONFIRM = True
#: CC-13: `[gate1] vote_collect` — `off` (the default, every pack is what it is) or `auto`
#: (the pack carries the collect model's facts when the artifact of the run's base directory is
#: fresh, or stale and `[collect] staleness = warn` allows a stale one)
DEFAULT_VOTE_COLLECT = "off"
VOTE_COLLECT_MODES = ("off", "auto")

#: The vote's own counters of the collect model (CC-13): `collect_chunks` is how many
#: chunks of kind `collect` the run's packs carried, `collect_state` is what the artifact
#: said (`off`, `absent`, `stale`, `fresh`) — a result then says what it had.
COUNTER_COLLECT_CHUNKS = "collect_chunks"
COUNTER_COLLECT_STATE = "collect_state"

_ON = {"on", "true", "yes", "1"}
_OFF = {"off", "false", "no", "0", ""}


def voter_ini(config, profiles) -> configparser.ConfigParser:
    """*config*'s sections, plus each of *profiles* that only ``contest.ini`` / ``contest.local.ini``
    (in `LOCAL_ROOT`) define: the RUNBOOK names both places for a voter's section, and Gate 1 read
    only the first, so a ``claim_vote_llm.*`` profile of ``--add-profiles`` was a voter that never
    started. The ini Gate 1 runs with wins; nothing of the local files but the named sections is taken."""
    parser = configparser.ConfigParser(interpolation=None)
    if config is not None:
        try:
            for name in config.sections():
                parser.add_section(name)
                for key, value in config.items(name, raw=True):
                    parser.set(name, key, value)
        except (configparser.Error, AttributeError):
            pass
    missing = [p for p in profiles if not parser.has_section(p)]
    if missing:
        local = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
        for name in LOCAL_FILES:
            try:
                local.read(LOCAL_ROOT / name, encoding="utf-8-sig")   # a BOM (Notepad), as claim_vote reads it
            except (configparser.Error, OSError, UnicodeError) as exc:
                logger.warning("Gate1[vote] %s cannot be read (%s) — its profiles are not used", name, exc)
        for ref in missing:
            if local.has_section(ref):
                parser.add_section(ref)
                for key, value in local.items(ref, raw=True):
                    parser.set(ref, key, value)
    return parser


def model_of(ref: str, parser: configparser.ConfigParser) -> str:
    """The model a voter ref names: a section's ``model``, else the ref itself (``provider/model``)."""
    if parser.has_section(ref):
        return (parser.get(ref, "model", fallback="") or "").strip() or ref
    return ref


@dataclass(frozen=True)
class VoteConfig:
    """The ``[gate1]`` keys of the vote. ``enabled`` is False for every reason the vote cannot
    run (off, fewer than three profiles, fewer than three families, a refused quorum); ``why``
    says which, for the log. ``models`` pairs each profile with the model it names."""

    enabled: bool = False
    profiles: tuple = ()
    max_per_run: int = DEFAULT_MAX_PER_RUN
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    veto_by_reader: bool = True
    accept: str = "all"
    confirm: bool = DEFAULT_CONFIRM   # False: the vote only vetoes, a unanimous TRUE is the reader's to decide
    skip_tests: bool = False          # True: a candidate that cites a test file is not put to the voters
    vote_collect: str = DEFAULT_VOTE_COLLECT   # CC-13: auto = the pack may carry collect chunks
    why: str = "claim_vote = off"
    models: tuple = ()

    @property
    def model_map(self) -> dict:
        return dict(self.models)

    @classmethod
    def from_config(cls, config) -> "VoteConfig":
        """The vote's settings of *config* (a ``ConfigParser``). Never raises; a malformed value
        is a warning and its default, and a setting that makes the vote unsafe turns it off."""
        def get(key: str, fallback: str = "") -> str:
            try:
                return (config.get(SECTION, key, fallback=fallback) or "").strip()
            except (configparser.Error, AttributeError):
                return fallback

        raw = get("claim_vote", "off").lower()
        if raw in _OFF:
            return cls()
        if raw not in _ON:
            logger.warning("config [gate1] claim_vote = %r is not on/off — the vote stays off", raw)
            return cls(why=f"claim_vote = {raw!r} is not on/off")
        profiles = tuple(dict.fromkeys(p.strip() for p in get("vote_profiles").split(",") if p.strip()))
        quorum = get("vote_quorum", str(QUORUM))
        if quorum != str(QUORUM):
            # CC-10 Q5: one voter is not this ticket's; a lower quorum is never the default
            logger.warning("config [gate1] vote_quorum = %r: only %d is supported — the vote stays off",
                           quorum, QUORUM)
            return cls(profiles=profiles, why=f"vote_quorum = {quorum!r}: only {QUORUM}")
        if len(profiles) < QUORUM:
            logger.warning("config [gate1] vote_profiles names %d voter(s), the vote needs %d — it stays off",
                           len(profiles), QUORUM)
            return cls(profiles=profiles, why=f"vote_profiles names {len(profiles)} voter(s), needs {QUORUM}")
        # the family is the model's: three sections vote_a/b/c that all name agnes models were three
        # "families" and confirmed a candidate on one model's opinion; llm1/2/3 of three vendors were one
        ini = voter_ini(config, profiles)
        models = tuple((p, model_of(p, ini)) for p in profiles)
        families = {cc_truth.family_of(p, m) for p, m in models}
        if len(families) < QUORUM:
            logger.warning("config [gate1] vote_profiles come from fewer than %d families (%s) — the vote stays off",
                           QUORUM, ", ".join(sorted(families)))
            return cls(profiles=profiles, models=models, why=f"vote_profiles: fewer than {QUORUM} families")

        def number(key: str, default, cast, low):
            text = get(key)
            if not text:
                return default
            try:
                value = cast(text)
            except ValueError:
                value = None
            # float() takes "inf" and "nan", which int() refused: inf overflowed the vote's wait on every
            # candidate, and nan made it return at once, every voter "missing" it
            if value is None or not math.isfinite(value) or value < low:
                logger.warning("config [gate1] %s = %r is malformed — using %s", key, text, default)
                return default
            return value

        def switch(key: str, default: str) -> bool:
            # an empty value is the default, as for claim_vote and the numbers above
            value = get(key, default).lower() or default
            if value not in _ON | _OFF:
                logger.warning("config [gate1] %s = %r is not on/off — using %s", key, value, default)
                value = default
            return value in _ON

        accept = get("vote_accept", "all").lower() or "all"
        if accept not in ("all", "committed"):
            logger.warning("config [gate1] vote_accept = %r is not all/committed — using all", accept)
            accept = "all"
        # CC-13: a malformed value is `off`, never an error — a wrong key must degrade to
        # today's pack rather than keep the vote from running
        vote_collect = get("vote_collect", DEFAULT_VOTE_COLLECT).lower() or DEFAULT_VOTE_COLLECT
        if vote_collect not in VOTE_COLLECT_MODES:
            logger.warning("config [gate1] vote_collect = %r is not off/auto — using off", vote_collect)
            vote_collect = DEFAULT_VOTE_COLLECT
        return cls(True, profiles,
                   number("vote_max_per_run", DEFAULT_MAX_PER_RUN, int, 0),
                   number("vote_timeout_sec", DEFAULT_TIMEOUT_SEC, float, 1.0),
                   switch("vote_veto_by_reader", "on"), accept,
                   switch("vote_confirm", "on" if DEFAULT_CONFIRM else "off"), switch("vote_skip_tests", "off"),
                   vote_collect, "claim_vote = on", models)


@dataclass(frozen=True)
class VoteOutcome:
    """What the vote said about one candidate. ``verdict`` is ``"confirmed"``, ``"rejected"`` or
    None (no decision: the single reader answers). ``reason`` is the line Gate 1 records;
    ``requests`` the voters asked (0 when the claim had nothing to vote on)."""

    verdict: Optional[str] = None
    reason: str = ""
    requests: int = 0
    quotes: tuple = ()


def claim_parts(candidate) -> tuple:
    """(where, instruction) of a candidate's claim: "`symbol` in `file`" and the one-line instruction."""
    loc = candidate.cited_location
    instruction = " ".join(str(candidate.instruction or "").split())[:MAX_INSTRUCTION]
    where = f"`{loc.symbol}` in `{loc.file}`" if loc.symbol else f"`{loc.file}`"
    return where, instruction


def claim_of(candidate) -> str:
    """The claim a candidate makes: "`symbol` in `file` has the problem: <instruction>"."""
    where, instruction = claim_parts(candidate)
    return f"{where} has the problem: {instruction}"


#: directories whose files are test code at any depth (pytest's tests/, Java's src/test/, Jest's __tests__/)
_TEST_DIRS = frozenset(("tests", "test", "__tests__", "tests_bugfix", "tests_slow", ".smoke_tests",
                        ".regression_tests", "spec", "specs"))
#: pytest's file shapes: a test only when the file holds one (see `_is_test_path`)
_PY_TEST_NAME = re.compile(r"(?:^test_.*|_test)\.py$")
_PY_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test\w*\s*\(|^class\s+Test\w*", re.M)
#: other languages' test files, unambiguous by name (Go requires the suffix; Jest and friends the infix)
_OTHER_TEST_NAME = re.compile(r"_test\.go$|\.(?:test|spec)\.[cm]?[jt]sx?$")


def _is_test_path(path: str, view=None) -> bool:
    """A cited file that is test code. The project's own rule first (`tools/collect/test_paths`:
    its test roots, a ``conftest.py`` anywhere), then a test directory at any depth, the other
    languages' test names, Django's ``tests.py`` — and a pytest-shaped name (``test_*.py``,
    ``*_test.py``) outside those only when the file holds a test (with *view*, a ``RepoView``):
    `tools/collect/test_map.py` is shipped code and lost its vote, while `conftest.py`,
    ``*.spec.ts`` and ``__tests__/`` were voted on."""
    p = str(path or "").replace("\\", "/")
    if not p:
        return False
    parts = p.split("/")
    name = parts[-1]
    if _test_paths.is_test_path(p) or any(d in _TEST_DIRS for d in parts[:-1]):
        return True
    if name in ("conftest.py", "tests.py") or _OTHER_TEST_NAME.search(name):
        return True
    if _PY_TEST_NAME.search(name):
        if view is None:
            return True
        try:
            return bool(_PY_TEST_DEF.search(view.read(p)))
        except Exception:   # noqa: BLE001 — unreadable: the name decides, as before
            return True
    return False


def _load_claim_vote():
    """``scripts/claim_vote.py`` (the voters' HTTP call, the pacer and the tally), imported the way
    it imports its own neighbours: the scripts directory is not a package."""
    scripts = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import claim_vote
    return claim_vote


#: CC-13: the collect model read once per tree, shared by the vote and by Gate 1's own line
#: about it. The read is a full scan and hash of the tree, so it is cached here rather than
#: re-done per candidate or per consumer; `main.py` is one process per run, so nothing of one
#: run leaks into the next.
_COLLECT_READS: dict = {}
_COLLECT_READS_LOCK = threading.Lock()


def _tree_key(base_dir) -> Optional[str]:
    """*base_dir* as the key of a tree, or None when it names nothing to read."""
    try:
        root = Path(base_dir)
        if not root.exists():
            return None
        return str(root.resolve())
    except Exception:   # noqa: BLE001 — a weird base_dir is "no collect data"
        return None


def _tree_root_of(view) -> Optional[str]:
    """The tree a *view* reads from (`PathRepoView.root`, a git view's `base_dir`), or None:
    the collect model must be the one for the tree the claim is judged against."""
    try:
        for name in ("root", "base_dir", "worktree", "directory", "dir"):
            value = getattr(view, name, None)
            if isinstance(value, (str, Path)) and str(value):
                return str(value)
    except Exception:   # noqa: BLE001 — a weird view is "no collect data"
        return None
    return None


def _collect_loader():
    """`tools.collect.loader`, imported lazily: a collect package that cannot be imported is
    "no collect data", never a run that dies before Stage B."""
    try:
        from tools.collect import loader as cl
        return cl
    except Exception:   # noqa: BLE001 — the vote runs without collect
        return None


def read_collect_model(base_dir, config=None):
    """CC-13: the read of the collect artifact of *base_dir*, or None when there is none.

    The same freshness test `main.py --collect --check` reports (the manifest's hashes against
    the tracked files, the current `collector_version`) and the same `[collect] staleness`
    policy, except that it never writes: `ignore` gives no model, `warn` gives the stale model
    and says how many files moved, `refresh` is not honoured here (the vote is not the
    collector and will not pay an hour of Pass B in the middle of Stage B). An absent,
    unreadable or malformed artifact is "no collect data" — the pack is today's — and nothing
    here raises. Read once per tree: the second call is a dict lookup."""
    key = _tree_key(base_dir)
    if key is None:
        return None
    with _COLLECT_READS_LOCK:
        if key in _COLLECT_READS:
            return _COLLECT_READS[key]
        read = None
        try:
            cl = _collect_loader()
            if cl is not None:
                read = cl.load_readonly(base_dir, config=config)
        except Exception as exc:   # noqa: BLE001 — a broken artifact is no artifact
            logger.warning("Gate1[vote] the collect artifact could not be read (%s: %s) — "
                           "no collect data", type(exc).__name__, str(exc)[:100])
            read = None
        _COLLECT_READS[key] = read
        return read


def clear_collect_reads() -> None:
    """Drop the cached reads of `read_collect_model` (a test of one tree then another)."""
    with _COLLECT_READS_LOCK:
        _COLLECT_READS.clear()


class _Turns:
    """The places of this run's requests in each host's queue, in the order their votes queued them
    (`Gate1Vote._run_voters`): a request asks the pacer for a slot only after every earlier one has its
    slot or has given up. The pacer's semaphore served whoever asked first, and with presence_workers
    > 1 a vote's requests could reach it after those of a vote queued later, or lose a freed slot to a
    newcomer, and wait past the bound that had counted them in order ("no free slot before the deadline")."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._issued: Counter = Counter()
        self._next: Counter = Counter()
        self._gone: dict = {}              # host -> places given up before their turn: skipped when it comes

    def take(self, host: str) -> int:
        with self._cond:
            place = self._issued[host]
            self._issued[host] += 1
            return place

    def wait(self, host: str, place: int, deadline: Optional[float]) -> bool:
        with self._cond:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            if self._cond.wait_for(lambda: self._next[host] == place, left):
                return True
            self._gone.setdefault(host, set()).add(place)
            return False

    def done(self, host: str, place: int) -> None:
        """*place* has its slot, gave up, or never asked: the next place's turn."""
        with self._cond:
            if self._next[host] == place:
                nxt, gone = place + 1, self._gone.get(host, set())
                while nxt in gone:
                    gone.discard(nxt)
                    nxt += 1
                self._next[host] = nxt
                self._cond.notify_all()
            elif self._next[host] < place:
                self._gone.setdefault(host, set()).add(place)


class _Turn:
    """One request's place (`_Turns`), as the pacer `claim_vote.ask_v2` waits on (``pacer=``)."""

    def __init__(self, turns: _Turns, host: str, place: int, pacer) -> None:
        self._turns, self._host, self._place, self._pacer = turns, host, place, pacer
        self._used = False

    def wait(self, provider: str, deadline: Optional[float] = None):
        if self._used:                     # a second request of the same voter: no place of its own
            return self._pacer.wait(provider, deadline)
        self._used = True
        try:
            if not self._turns.wait(self._host, self._place, deadline):
                raise TimeoutError(f"no turn for {provider} before the deadline")
            return self._pacer.wait(provider, deadline)
        finally:
            self._turns.done(self._host, self._place)

    def close(self) -> None:
        """The request never asked for its slot (it failed before): its place is passed on."""
        if not self._used:
            self._used = True
            self._turns.done(self._host, self._place)


class Gate1Vote:
    """The vote of one Gate 1 run. *ask_fn* ``(ref, items, run, seed, timeout) -> row`` is one
    voter's run over ``(claim, pack)`` items (the row of `claim_vote.ask_v2`); the default is
    `claim_vote.ask_v2` over *config*, and a test hands a fake. *tally_fn* is `claim_vote.tally`."""

    def __init__(self, cfg: VoteConfig, config=None, *, ask_fn: Optional[Callable] = None,
                 tally_fn: Optional[Callable] = None, budget: Optional[PackBudget] = None) -> None:
        self.cfg = cfg
        self._config = config
        self._ask_fn = ask_fn
        self._tally_fn = tally_fn
        self._budget = budget or PackBudget()
        self._lock = threading.Lock()
        self._voted = 0          # candidates voted on so far (the cap)
        self._voters = list(cfg.profiles)   # who is still asked: resolved, not dropped
        self._misses: dict = {}             # ref -> bounds missed in a row
        self._warned: set = set()           # refs whose error was logged once
        self.stopped = ""                   # why the vote ended for the rest of the run
        self._resolved = False              # resolve_voters ran (once, before the first vote)
        self._hosts: dict = {}              # ref -> the host its requests queue on (the pacer's key)
        self._inflight: Counter = Counter()  # host -> requests of this run's votes not returned yet
        self._turns = _Turns()               # their order in each host's queue
        self._resolve_lock = threading.Lock()   # held for the whole resolution: a second presence
                                                # worker waits for it instead of voting with all refs
        # votes_skipped: candidates that reached the vote and were not voted on (nothing in the tree
        # to quote, the cap, the vote stopped) — votes_asked alone hid them. CC-13 adds the collect
        # side: collect_chunks counts the chunks of kind `collect` the run's packs carried, and
        # collect_state says what the artifact was (off = the switch, absent/stale/fresh = the read),
        # so a result says what its judges had.
        self.counters = {"votes_asked": 0, "votes_decided": 0, "votes_overridden": 0,
                         "votes_requests": 0, "votes_skipped": 0,
                         COUNTER_COLLECT_CHUNKS: 0, COUNTER_COLLECT_STATE: DEFAULT_VOTE_COLLECT}

    # -- the collect model (CC-13) -------------------------------------------------------------

    def collect_read(self, base_dir):
        """The read of *base_dir*'s collect artifact for this vote, or None.

        ``[gate1] vote_collect = off`` (the default) reads nothing and leaves the counter at
        ``off``: every pack is exactly what it is today. ``auto`` reads the artifact of the run's
        base directory once — the same freshness test `main.py --collect --check` uses, and never
        a write — and records what it said in `collect_state`. Nothing raises: an absent tree, a
        malformed ``[collect]`` key or a broken artifact is no model, and the pack is today's."""
        if self.cfg.vote_collect != "auto":
            return None
        read = read_collect_model(base_dir, config=self._config)
        self._set_state(getattr(read, "status", None) or "absent")
        return read

    def collect_model(self, view):
        """The collect model for one vote's pack, or None (the pack is today's). Only a read that
        has a model to give one is passed on: `staleness = ignore` and the ``refresh`` case the
        vote does not honour both read as no model."""
        try:
            read = self.collect_read(_tree_root_of(view))
            return read.model if read is not None and getattr(read, "used", False) else None
        except Exception:   # noqa: BLE001 — the pack is built without collect, never a crash
            return None

    def _set_state(self, state: str) -> None:
        with self._lock:
            self.counters[COUNTER_COLLECT_STATE] = str(state or "absent")

    def collect_state(self) -> str:
        """``off`` (the switch is off), or what the artifact said: ``absent``, ``stale``, ``fresh``."""
        with self._lock:
            return self.counters.get(COUNTER_COLLECT_STATE, DEFAULT_VOTE_COLLECT)

    # -- the voters ----------------------------------------------------------------------------

    def _ask(self, ref: str, items: list, timeout: float, deadline: Optional[float] = None,
             turn: Optional[_Turn] = None) -> dict:
        if self._ask_fn is not None:
            return self._ask_fn(ref, items, 0, 0, timeout)
        cv = _load_claim_vote()
        parser = self._voter_parser(cv)
        # code_batch = 1: a Gate 1 candidate is one claim, so a voter costs one request. The timeout
        # is passed as it was set (1.9 was cut to 1). *deadline* bounds the wait for a pacer slot,
        # and once the request has its slot it has 2 x timeout of its own (budget), retries included;
        # *turn* is its place in the host's queue (passed only when set: a stub of ask_v2 still fits)
        return cv.ask_v2(ref, items, 0, 0, parser, timeout, batch=1, code_batch=1, deadline=deadline,
                         budget=timeout * 2, **({"pacer": turn} if turn is not None else {}))

    def _host(self, ref: str) -> str:
        return self._hosts.get(ref) or f"#{ref}"

    def _bound(self, refs: list, queued: Optional[Counter] = None) -> float:
        """Seconds the vote waits for *refs*: on one host the claim_vote pacer starts `width`
        requests at a time and `interval` apart, and each has 2 x timeout once it starts. A flat
        2 x timeout + 5 cut the tail of the queue — nine voters on one provider lost the last few
        every candidate, recorded as dead (and, two in a row, dropped for the run).

        *queued*: host -> requests already in flight there (`_inflight`), which the pacer serves
        first. With ``presence_workers`` > 1 several candidates are voted on at once on one
        pacer, and a bound of the vote's own requests alone cut most of them: eight workers, three
        voters on one host, 5 of 16 votes decided."""
        per_request = self.cfg.timeout_sec * 2
        if self._ask_fn is not None:
            width, interval = 1, 0.0                 # a test's fake: no pacer, no queue
        else:
            pacer = _load_claim_vote().PACER
            width, interval = max(1, int(getattr(pacer, "_width", 2))), float(getattr(pacer, "interval", 4.0))
        hosts = Counter(self._host(r) for r in refs)
        queued = queued or Counter()
        worst = max((math.ceil((k + queued[h]) / width) * per_request + (k + queued[h] - 1) * interval
                     for h, k in hosts.items()), default=0.0)
        return worst + VOTE_SLACK_SEC

    def _voter_parser(self, cv):
        """*config* with a ``[claim_vote]`` section and the voters' sections (`voter_ini`):
        `voter_settings` and the retry budget read it. Built once; the operator's own
        ``[claim_vote]`` (retries, pacing) is kept when there is one."""
        parser = getattr(self, "_parser", None)
        if parser is None:
            parser = voter_ini(self._config, self.cfg.profiles)
            if not parser.has_section(cv.SECTION):
                parser.add_section(cv.SECTION)
            self._parser = parser
        return parser

    def resolve_voters(self) -> str:
        """The real path only (no *ask_fn*): each voter resolved once, before the first vote. One
        that cannot be (no section, no key, Kilo has no such model) is named in the log and left
        out — it used to be asked for every candidate, silently, with its "request" counted.
        Returns why the vote cannot run with who is left, or ``""`` (and stops the vote then)."""
        with self._resolve_lock:
            if not self._resolved:
                why = self._resolve()
                if why:
                    self._stop(why)
                self._resolved = True
            return self.stopped

    def _resolve(self) -> str:
        if self._ask_fn is not None:
            return ""
        cv = _load_claim_vote()
        parser = self._voter_parser(cv)
        models = self.cfg.model_map
        for ref in list(self._voters):
            try:
                st = cv.voter_settings(ref, parser)
                if not (st.base_url and st.model):
                    raise ValueError("no base_url or model")
                models[ref] = st.model
                self._hosts[ref] = st.base_url.split("//")[-1].split("/")[0]
            except (ValueError, OSError, KeyError, configparser.Error) as exc:
                logger.warning("Gate1[vote] voter %s cannot be used (%s) — it is left out", ref, str(exc)[:120])
                self._voters.remove(ref)
        self.cfg = VoteConfig(**{**self.cfg.__dict__, "models": tuple(models.items())})
        return self._viable("a voter could not be resolved")

    def _viable(self, what: str) -> str:
        """Why the voters left can no longer decide, or ``""``. Under ``vote_accept = all`` every
        configured voter must answer, so one left out ends the vote; otherwise three voters of three
        families must be left."""
        gone = [r for r in self.cfg.profiles if r not in self._voters]
        if gone and self.cfg.accept == "all":
            return f"{what} ({', '.join(gone)}) and vote_accept = all needs every voter"
        models = self.cfg.model_map
        if len(self._voters) < QUORUM or \
                len({cc_truth.family_of(r, models.get(r)) for r in self._voters}) < QUORUM:
            return f"{what} ({', '.join(gone)}): fewer than {QUORUM} voters of {QUORUM} families are left"
        return ""

    def _stop(self, why: str) -> None:
        with self._lock:
            if self.stopped:
                return
            self.stopped = why
        logger.warning("Gate1[vote] the vote stops for the rest of this run: %s — the single reader answers", why)

    def _run_voters(self, refs: list, items: list) -> list:
        """One row per voter; a voter that raises or does not answer in time is a row with no votes.

        Daemon threads, not a pool: a voter given up on kept a pool thread (and its pacer slot)
        for its whole retry budget, and the interpreter waited for it at exit — a plan finished,
        and the process ran on for twenty minutes. The deadline goes into the request itself."""
        timeout = self.cfg.timeout_sec
        pacer = None if self._ask_fn is not None else _load_claim_vote().PACER
        turns: dict = {}
        with self._lock:
            # the bound on the whole vote, the pacer's queue included: the requests of the other votes
            # still in flight on the same hosts are served first (`_Turns` keeps that order), and this
            # vote's join them
            bound = self._bound(refs, self._inflight)
            for ref in refs:
                host = self._host(ref)
                self._inflight[host] += 1
                if pacer is not None:
                    turns[ref] = _Turn(self._turns, host, self._turns.take(host), pacer)
        deadline = time.monotonic() + bound - VOTE_SLACK_SEC
        rows: dict = {}
        lock, done = threading.Lock(), threading.Event()
        left = [len(refs)]

        def one(ref: str) -> None:
            turn = turns.get(ref)
            try:
                row = self._ask(ref, items, timeout, deadline, turn)
                got = row if isinstance(row, dict) else {}
            except Exception as exc:   # noqa: BLE001 — a dead voter is a result, not a crash
                got = {"error": f"raised {type(exc).__name__}: {str(exc)[:100]}"}
            finally:
                if turn is not None:
                    turn.close()
                with self._lock:      # out of the queue when it returns, given up on or not
                    self._inflight[self._host(ref)] -= 1
            with lock:
                rows[ref] = got
                left[0] -= 1
                if not left[0]:
                    done.set()

        for ref in refs:
            threading.Thread(target=one, args=(ref,), name=f"gate1-vote-{ref}", daemon=True).start()
        done.wait(bound)
        with lock:
            got = dict(rows)
        out = []
        for ref in refs:
            row = got.get(ref)
            self._note_voter(ref, row)
            row = row or {}
            out.append({"model": ref, "run": 0, "votes": row.get("votes") or {},
                        "accepted": row.get("accepted") or {}, "rejected": row.get("rejected") or []})
        return out

    def _note_voter(self, ref: str, row: Optional[dict]) -> None:
        """The voter's error, logged once a run (it was dropped); a vote with no reply of the voter
        counted, and a voter with `MAX_MISSES` of them in a row is not asked again this run.

        No reply is no row by the bound, or a row of `claim_vote.ask_v2` that had a slot and got no
        reply (``requests`` > 0, ``replies`` 0: a timeout, a refused connection, an HTTP error, each
        after its retries). Its request ends at its own deadline, before the bound, so a provider
        that never answers was "answered" every time: never dropped, and each vote waited out its
        2 x timeout. An error row with no ``requests`` at all (the voter raised, or its settings
        failed) is no reply either: it reset the count, and a voter that raised on every vote was
        asked on every one, the vote never deciding under ``vote_accept = all``. A request that never
        got its slot (``requests`` 0) is the queue's, not the voter's: neither counted nor a reply."""
        if row is not None and row.get("error") and ref not in self._warned:
            self._warned.add(ref)
            logger.warning("Gate1[vote] voter %s: %s (logged once a run)", ref, str(row["error"])[:160])
        voted = row is not None and bool(row.get("votes"))
        made = row.get("requests") if row is not None else None
        failed = row is not None and not voted and (
            (bool(made) and not row.get("replies")) or (made is None and bool(row.get("error"))))
        with self._lock:
            if row is not None and not failed:
                if voted or made is None or made:
                    self._misses[ref] = 0
                return
            self._misses[ref] = self._misses.get(ref, 0) + 1
            dropped = self._misses[ref] >= MAX_MISSES and ref in self._voters
            if dropped:
                self._voters.remove(ref)
        logger.warning("Gate1[vote] voter %s %s (%d in a row)%s", ref,
                       "gave no reply" if failed else "did not answer within the vote's bound",
                       self._misses[ref], " — it is not asked again this run" if dropped else "")
        if dropped:
            why = self._viable("a voter was dropped")
            if why:
                self._stop(why)

    # -- the decision --------------------------------------------------------------------------

    def _admit(self) -> bool:
        with self._lock:
            if self._voted >= self.cfg.max_per_run:
                return False
            self._voted += 1
            return True

    def _bump(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] += n

    def presence_by_vote(self, candidate, view) -> VoteOutcome:
        """The vote on *candidate* against *view* (a ``RepoView`` of the candidate's target).
        Never raises."""
        try:
            return self._presence_by_vote(candidate, view)
        except Exception as exc:   # noqa: BLE001 — Gate 1 goes on with the single reader
            logger.warning("Gate1[vote] %r: no decision (%s: %s)", getattr(candidate, "title", "?"),
                           type(exc).__name__, str(exc)[:120])
            return VoteOutcome()

    def _skip(self) -> VoteOutcome:
        self._bump("votes_skipped")
        return VoteOutcome()

    def _presence_by_vote(self, candidate, view) -> VoteOutcome:
        if not self.cfg.enabled:
            return VoteOutcome()
        if not self._resolved:
            self.resolve_voters()
        if self.stopped:
            return self._skip()
        if self.cfg.skip_tests and _is_test_path(candidate.cited_location.file, view):
            return self._skip()       # a claim about a test is "the test lacks X": true and no reason for a task
        where, instruction = claim_parts(candidate)
        if not instruction:
            # a hand-edited plan.json reaches Gate 1 through --validate-plan with no instruction:
            # "`f` in `x.py` has the problem: " was three requests about nothing
            return self._skip()
        claim = claim_of(candidate)
        resolved = cc_anchors.resolve_claim(claim, view)
        kind = cc_anchors.classify(claim, resolved)
        # dangling is judged on the cited file and symbol only: an instruction that names the file
        # it will create ("add a test in tests/test_new.py") made every such candidate "dangling",
        # and the vote skipped it without a word
        cited = [r for r in resolved if r.anchor.start < len(where)]
        if kind == "world" or cc_anchors.is_dangling(cited):
            return self._skip()       # nothing to quote: the single reader as today
        # CC-13: `vote_collect = auto` and a fresh (or `staleness = warn` stale) artifact add the
        # collect facts about the cited module to the pack the voters read; `off` and every failure
        # give None and the pack is byte for byte what it was
        pack = build_pack(claim, resolved, view, model=self.collect_model(view), budget=self._budget)
        if not pack.chunks:
            return self._skip()
        if not self._admit():
            return self._skip()       # past [gate1] vote_max_per_run: the single reader answers
        # the pack's own record of what the voters read: counted once it is put to them, so a pack past the
        # cap (never sent) does not count, and a collect chunk of zero is "no collect data"
        self._bump(COUNTER_COLLECT_CHUNKS, sum(1 for c in pack.chunks if c.kind == "collect"))
        with self._lock:
            refs = list(self._voters)
        self._bump("votes_asked")
        items = [(claim, pack)]
        results = self._run_voters(refs, items)
        requests = len(results)
        self._bump("votes_requests", requests)
        if self.cfg.accept == "all" and any(not r["votes"] for r in results):
            # `tally` leaves a voter with no vote out of the voters (CC-6: a model dead for the whole
            # run is not one), and a Gate 1 tally is one candidate: with four profiles a voter that
            # timed out on this one did not block three TRUEs. Under `all` every voter answers
            return VoteOutcome(requests=requests)
        cv = self._tally_fn_module()
        row = cv.tally([{"claim": claim}], results, quorum=QUORUM,
                       meta=[{"kind": kind, "dangling": False, "sha": pack.sha}],
                       accept=self.cfg.accept)[0]
        # truth.settled: unanimous, TRUE/FALSE, and three families behind it (CC-8) — of the models
        # the profiles name, not of the profile names
        if cc_truth.settled(row, self.cfg.model_map) is None:
            return VoteOutcome(requests=requests)
        quotes = tuple(row.get("quotes") or ())
        by = ", ".join(sorted(m for m, v in row["by_model"].items() if v == row["verdict"]))
        shown = "; ".join(f"“{q[:100]}”" for q in quotes[:2])
        if row["verdict"] == "TRUE" and not self.cfg.confirm:
            # veto only: the reader decides what the vote would confirm; the line is what layer 2
            # reads back to score the vote's own TRUEs
            logger.info("Gate1[vote] %r: claim vote: TRUE by %s (%s) — vote_confirm = off, the single "
                        "reader decides", getattr(candidate, "title", "?"), by, shown)
            return VoteOutcome(requests=requests)
        if row["verdict"] == "TRUE":
            return VoteOutcome("confirmed", f"claim vote: confirmed by {by} ({shown})", requests, quotes)
        return VoteOutcome("rejected", f"claim vote: rejected by {by} ({shown})", requests, quotes)

    def _tally_fn_module(self):
        """An object with ``.tally``: the injected function wrapped, or `claim_vote` itself."""
        if self._tally_fn is not None:
            fn = self._tally_fn

            class _T:
                tally = staticmethod(fn)
            return _T
        return _load_claim_vote()

    def note_decided(self) -> None:
        self._bump("votes_decided")

    def note_overridden(self) -> None:
        self._bump("votes_overridden")


def build(config, *, ask_fn=None, tally_fn=None) -> Optional[Gate1Vote]:
    """A `Gate1Vote` for *config*, or None when ``[gate1] claim_vote`` is off (or cannot run):
    None is what leaves Gate 1 as it was."""
    cfg = VoteConfig.from_config(config)
    if not cfg.enabled:
        return None
    return Gate1Vote(cfg, config, ask_fn=ask_fn, tally_fn=tally_fn,
                     budget=PackBudget.from_config(config) if config is not None else None)
