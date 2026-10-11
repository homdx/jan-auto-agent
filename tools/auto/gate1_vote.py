"""CC-10 — claim check in Gate 1: three voters with the code and a quote, instead of one reader
(docs/claim-check/tickets/292-cc-10-claim-check-in-gate1.md, epic-tasks/292-…).

Gate 1's Stage B asks one model, shown a code block, whether the problem a task names is
present. CC-6 already does the harder version of that job: voters of several families, each
answering with a verbatim quote of the evidence, the quote checked by code, a verdict accepted
only when the committed voters agree. This module asks that question for one candidate:

    "`<symbol>` in `<file>` has the problem: <the instruction>"

and says what the vote decided:

* unanimous ``TRUE`` (three voters, three families, quotes verified)  -> ``confirmed``;
* unanimous ``FALSE``                                                  -> ``rejected``, with the
  voters' quotes as the reason — unless the single reader (``vote_veto_by_reader = on``, the
  default) says ``confirmed``, which is counted as ``votes_overridden``;
* anything else (``SPLIT``, ``UNSURE``, fewer than three committed, fewer than three
  families, a dead voter, a claim with no code anchor, the cap reached, any error)
  -> no decision, and **Stage B as it is today decides**.

So the vote can only shortcut a decision it is sure of: it never makes Gate 1 less strict than
the single reader (a ``TRUE`` it confirms was never asked of the reader, but needed three
families and three quotes the code found in the pack; a ``FALSE`` needs the reader's consent).

Nothing here raises: a voter that raises, a pack that cannot be built, an ini that is wrong are
each a "no decision" and a line in the log. ``[gate1] claim_vote = off`` (the default) builds no
``Gate1Vote`` at all, so Gate 1 is byte for byte what it was.

``[gate1]`` keys (read by `VoteConfig.from_config`)::

    claim_vote          = off | on
    vote_profiles       = <ref>, <ref>, <ref>    ; a profile section name, or provider/model
    vote_max_per_run    = 200                    ; candidates that may be voted on in one run
    vote_timeout_sec    = 60                     ; one HTTP request of one voter
    vote_veto_by_reader = on                     ; a unanimous FALSE also needs the reader not
                                                 ; to say confirmed
    vote_quorum         = 3                      ; not a knob: any other value is refused (CC-10 Q5)
"""

from __future__ import annotations

import configparser
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait
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

_ON = {"on", "true", "yes", "1"}
_OFF = {"off", "false", "no", "0", ""}


@dataclass(frozen=True)
class VoteConfig:
    """The ``[gate1]`` keys of the vote. ``enabled`` is False for every reason the vote cannot
    run (off, fewer than three profiles, fewer than three families, a refused quorum); ``why``
    says which, for the log."""

    enabled: bool = False
    profiles: tuple = ()
    max_per_run: int = DEFAULT_MAX_PER_RUN
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    veto_by_reader: bool = True
    why: str = "claim_vote = off"

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
        if len({cc_truth.family_of(p) for p in profiles}) < QUORUM:
            logger.warning("config [gate1] vote_profiles come from fewer than %d families — the vote stays off",
                           QUORUM)
            return cls(profiles=profiles, why=f"vote_profiles: fewer than {QUORUM} families")

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

        veto = get("vote_veto_by_reader", "on").lower()
        if veto not in _ON | _OFF:
            logger.warning("config [gate1] vote_veto_by_reader = %r is not on/off — using on", veto)
            veto = "on"
        return cls(True, profiles,
                   number("vote_max_per_run", DEFAULT_MAX_PER_RUN, int, 0),
                   number("vote_timeout_sec", DEFAULT_TIMEOUT_SEC, float, 1.0),
                   veto in _ON or veto == "", "claim_vote = on")


@dataclass(frozen=True)
class VoteOutcome:
    """What the vote said about one candidate. ``verdict`` is ``"confirmed"``, ``"rejected"`` or
    None (no decision: the single reader answers). ``reason`` is the line Gate 1 records;
    ``requests`` the voters asked (0 when the claim had nothing to vote on)."""

    verdict: Optional[str] = None
    reason: str = ""
    requests: int = 0
    quotes: tuple = ()


def claim_of(candidate) -> str:
    """The claim a candidate makes: "`symbol` in `file` has the problem: <instruction>"."""
    loc = candidate.cited_location
    instruction = " ".join(str(candidate.instruction or "").split())[:MAX_INSTRUCTION]
    where = f"`{loc.symbol}` in `{loc.file}`" if loc.symbol else f"`{loc.file}`"
    return f"{where} has the problem: {instruction}"


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
        self.counters = {"votes_asked": 0, "votes_decided": 0, "votes_overridden": 0,
                         "votes_requests": 0}

    # -- the voters ----------------------------------------------------------------------------

    def _ask(self, ref: str, items: list, timeout: float) -> dict:
        if self._ask_fn is not None:
            return self._ask_fn(ref, items, 0, 0, timeout)
        cv = _load_claim_vote()
        parser = self._voter_parser(cv)
        # code_batch = 1: a Gate 1 candidate is one claim, so a voter costs one request
        return cv.ask_v2(ref, items, 0, 0, parser, int(timeout), batch=1, code_batch=1)

    def _voter_parser(self, cv):
        """*config* with a ``[claim_vote]`` section: `voter_settings` and the retry budget read it.
        Built once; the operator's own ``[claim_vote]`` (retries, pacing) is kept when there is one."""
        parser = getattr(self, "_parser", None)
        if parser is None:
            parser = configparser.ConfigParser(interpolation=None)
            if self._config is not None:
                for name in self._config.sections():
                    parser.add_section(name)
                    for key, value in self._config.items(name, raw=True):
                        parser.set(name, key, value)
            if not parser.has_section(cv.SECTION):
                parser.add_section(cv.SECTION)
            self._parser = parser
        return parser

    def _run_voters(self, items: list) -> list:
        """One row per voter; a voter that raises or does not answer in time is a row with no votes."""
        refs = list(self.cfg.profiles)
        rows = {}
        pool = ThreadPoolExecutor(max_workers=len(refs), thread_name_prefix="gate1-vote")
        futures = {pool.submit(self._ask, ref, items, self.cfg.timeout_sec): ref for ref in refs}
        # a request has its own timeout and retry budget; this is the bound on the whole vote
        done, _pending = wait(futures, timeout=self.cfg.timeout_sec * 2 + 5)
        for fut in done:
            ref = futures[fut]
            try:
                row = fut.result()
                rows[ref] = row if isinstance(row, dict) else {}
            except Exception as exc:   # noqa: BLE001 — a dead voter is a result, not a crash
                logger.warning("Gate1[vote] voter %s raised %s", ref, type(exc).__name__)
        pool.shutdown(wait=False, cancel_futures=True)
        out = []
        for ref in refs:
            row = rows.get(ref) or {}
            out.append({"model": ref, "run": 0, "votes": row.get("votes") or {},
                        "accepted": row.get("accepted") or {}, "rejected": row.get("rejected") or []})
        return out

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

    def _presence_by_vote(self, candidate, view) -> VoteOutcome:
        if not self.cfg.enabled:
            return VoteOutcome()
        claim = claim_of(candidate)
        resolved = cc_anchors.resolve_anchors(cc_anchors.extract_anchors(claim), view)
        kind = cc_anchors.classify(claim, resolved)
        if kind == "world" or cc_anchors.is_dangling(resolved):
            return VoteOutcome()      # nothing to quote: the single reader as today
        pack = build_pack(claim, resolved, view, budget=self._budget)
        if not pack.chunks:
            return VoteOutcome()
        if not self._admit():
            return VoteOutcome()      # past [gate1] vote_max_per_run: the single reader answers
        self._bump("votes_asked")
        items = [(claim, pack)]
        results = self._run_voters(items)
        self._bump("votes_requests", len(results))
        cv = self._tally_fn_module()
        row = cv.tally([{"claim": claim}], results, quorum=QUORUM,
                       meta=[{"kind": kind, "dangling": False, "sha": pack.sha}])[0]
        requests = len(results)
        # truth.settled: unanimous, TRUE/FALSE, and three families behind it (CC-8)
        if cc_truth.settled(row) is None:
            return VoteOutcome(requests=requests)
        quotes = tuple(row.get("quotes") or ())
        by = ", ".join(sorted(m for m, v in row["by_model"].items() if v == row["verdict"]))
        shown = "; ".join(f"“{q[:100]}”" for q in quotes[:2])
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
