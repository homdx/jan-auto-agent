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
cannot be resolved (checked once, before the first vote) is named in the log and left out; one that misses the vote's time bound twice in
a run is dropped for the rest of it, and the vote stops when what is left could no longer decide.
The voters' requests carry the vote's deadline, so one that is given up on stops retrying, and its
thread never holds the process at exit.

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
    vote_veto_by_reader = on                     ; a unanimous FALSE also needs the reader not
                                                 ; to say confirmed
    vote_accept         = all | committed        ; CC-11: with five or more voters, `committed`
                                                 ; lets an abstainer stop blocking (default all)
    vote_quorum         = 3                      ; not a knob: any other value is refused (CC-10 Q5)
"""

from __future__ import annotations

import configparser
import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tools.claimcheck import anchors as cc_anchors
from tools.claimcheck import truth as cc_truth
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
#: the vote waits 2 x vote_timeout_sec + this for its voters (their requests stop at 2 x the timeout)
VOTE_SLACK_SEC = 5.0
#: where ``contest.ini`` / ``contest.local.ini`` live (the project root); a test points it elsewhere
LOCAL_ROOT = Path(__file__).resolve().parents[2]
LOCAL_FILES = ("contest.ini", "contest.local.ini")

#: [gate1] vote_confirm when the ini does not set it (on, as CC-10 landed; off = the vote only vetoes)
DEFAULT_CONFIRM = True

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
            if value is None or value < low:
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
        return cls(True, profiles,
                   number("vote_max_per_run", DEFAULT_MAX_PER_RUN, int, 0),
                   number("vote_timeout_sec", DEFAULT_TIMEOUT_SEC, float, 1.0),
                   switch("vote_veto_by_reader", "on"), accept,
                   switch("vote_confirm", "on" if DEFAULT_CONFIRM else "off"), switch("vote_skip_tests", "off"),
                   "claim_vote = on", models)


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


def _is_test_path(path: str) -> bool:
    """A cited file that is test code: under tests/ or tests_bugfix/ (at any depth) or named ``test_*``/``*_test.*``."""
    parts = str(path or "").replace("\\", "/").split("/")
    name = parts[-1]
    return (any(p in ("tests", "tests_bugfix", "test", ".smoke_tests", ".regression_tests") for p in parts[:-1])
            or name.startswith("test_") or name.rsplit(".", 1)[0].endswith("_test"))


def _load_claim_vote():
    """``scripts/claim_vote.py`` (the voters' HTTP call, the pacer and the tally), imported the way
    it imports its own neighbours: the scripts directory is not a package."""
    scripts = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import claim_vote
    return claim_vote


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
        self._resolve_lock = threading.Lock()   # held for the whole resolution: a second presence
                                                # worker waits for it instead of voting with all refs
        # votes_skipped: candidates that reached the vote and were not voted on (nothing in the tree
        # to quote, the cap, the vote stopped) — votes_asked alone hid them
        self.counters = {"votes_asked": 0, "votes_decided": 0, "votes_overridden": 0,
                         "votes_requests": 0, "votes_skipped": 0}

    # -- the voters ----------------------------------------------------------------------------

    def _ask(self, ref: str, items: list, timeout: float, deadline: Optional[float] = None) -> dict:
        if self._ask_fn is not None:
            return self._ask_fn(ref, items, 0, 0, timeout)
        cv = _load_claim_vote()
        parser = self._voter_parser(cv)
        # code_batch = 1: a Gate 1 candidate is one claim, so a voter costs one request. The timeout
        # is passed as it was set (1.9 was cut to 1), and the deadline stops the request's retries
        # once the vote no longer waits for it
        return cv.ask_v2(ref, items, 0, 0, parser, timeout, batch=1, code_batch=1, deadline=deadline)

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
        bound = timeout * 2 + VOTE_SLACK_SEC  # the bound on the whole vote
        deadline = time.monotonic() + timeout * 2
        rows: dict = {}
        lock, done = threading.Lock(), threading.Event()
        left = [len(refs)]

        def one(ref: str) -> None:
            try:
                row = self._ask(ref, items, timeout, deadline)
                got = row if isinstance(row, dict) else {}
            except Exception as exc:   # noqa: BLE001 — a dead voter is a result, not a crash
                got = {"error": f"raised {type(exc).__name__}: {str(exc)[:100]}"}
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
        """The voter's error, logged once a run (it was dropped); a missed bound counted, and a
        voter that misses it `MAX_MISSES` times in a row is not asked again this run."""
        if row is not None and row.get("error") and ref not in self._warned:
            self._warned.add(ref)
            logger.warning("Gate1[vote] voter %s: %s (logged once a run)", ref, str(row["error"])[:160])
        with self._lock:
            if row is not None:
                self._misses[ref] = 0
                return
            self._misses[ref] = self._misses.get(ref, 0) + 1
            dropped = self._misses[ref] >= MAX_MISSES and ref in self._voters
            if dropped:
                self._voters.remove(ref)
        logger.warning("Gate1[vote] voter %s did not answer within the vote's bound (%d in a row)%s",
                       ref, self._misses[ref], " — it is not asked again this run" if dropped else "")
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
        if self.cfg.skip_tests and _is_test_path(candidate.cited_location.file):
            return self._skip()       # a claim about a test is "the test lacks X": true and no reason for a task
        where, instruction = claim_parts(candidate)
        if not instruction:
            # a hand-edited plan.json reaches Gate 1 through --validate-plan with no instruction:
            # "`f` in `x.py` has the problem: " was three requests about nothing
            return self._skip()
        claim = claim_of(candidate)
        resolved = cc_anchors.resolve_anchors(cc_anchors.extract_anchors(claim), view)
        kind = cc_anchors.classify(claim, resolved)
        # dangling is judged on the cited file and symbol only: an instruction that names the file
        # it will create ("add a test in tests/test_new.py") made every such candidate "dangling",
        # and the vote skipped it without a word
        cited = [r for r in resolved if r.anchor.start < len(where)]
        if kind == "world" or cc_anchors.is_dangling(cited):
            return self._skip()       # nothing to quote: the single reader as today
        pack = build_pack(claim, resolved, view, budget=self._budget)
        if not pack.chunks:
            return self._skip()
        if not self._admit():
            return self._skip()       # past [gate1] vote_max_per_run: the single reader answers
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
