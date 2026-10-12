#!/usr/bin/env python3
"""Let several free models vote on claims, several runs each, and tally the votes.

The idea: Lenz's /extract (free, 1000 a day) cuts a report into atomic claims;
instead of paying for Lenz verdicts, N models of different families judge each
claim, and a claim stands when the models agree.

The models are ini profiles, resolved the way every other caller in the tree
resolves its own: ``tools.auto.llm_profile.resolve_llm_profile`` over
``contest.ini`` + ``contest.local.ini``, called through ``build_chat_request`` /
``request_completion`` with the retry budget of ``[claim_vote]``
(``retry_kwargs_from_config``).  A voter is a section name (``--profiles a b c``)
or ``[claim_vote] llm_profiles``; ``provider/model`` (``--models``) reads Kilo's
own files instead, for a provider that has no profile yet, and ``--add-profiles
provider/model ...`` writes such profiles into ``contest.local.ini`` (gitignored;
the key is copied, never printed, and never world-readable).

Each model is asked ``--runs`` times with the same claims but a reworded first
sentence and a shuffled claim order, so a provider-side cache of the prompt
cannot hand back one answer N times (``--fixed-prompt`` is the control).

    python3 scripts/claim_vote.py claims.json --profiles vote_sn68 vote_agnes vote_nemo --runs 3

``claims.json`` is a JSON list of strings, or of {"claim": ..., "truth": true|false|null}.

CC-6: with ``--target REPO`` (and ``--ref``, ``--base``, ``--fetch``) the claims are
judged against that repository at a pinned commit (``tools.claimcheck.target.Target``):
each claim's anchors are resolved and classified (CC-1), a ``code`` or ``mixed`` claim is
sent with its evidence pack (CC-5) in batches of ``[claim_vote] code_batch``, and a
TRUE/FALSE stands only when it names a chunk of that pack and quotes it verbatim
(``tools.claimcheck.judge.verify_quotes``); one that does not is UNSURE and counted in
``rejected``. Without ``--target`` nothing of this runs: the script is ``CLAIM-1``'s.

    python3 scripts/claim_vote.py claims.json --target . --ref origin/main --profiles a b c

CC-11: ``--accept all|committed`` (``[claim_vote] accept``, default ``all``) is the acceptance
rule of `tally`: ``all`` needs every voter of the run to commit; ``committed`` needs three
committed voters that agree, and a voter that abstained on a claim does not block it (the rows
name it in ``abstained``). Five voters or more: ``--accept committed``. ``votes.json`` records it.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import configparser
import copy
import json
import os
import random
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.auto.llm_profile import LlmSettings, resolve_llm_profile  # noqa: E402
from tools.contest import roster  # noqa: E402
from tools.llm_stream import (  # noqa: E402
    build_chat_request, request_completion, retry_kwargs_from_config, strip_think)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import lenz_claim_filter as lf  # noqa: E402  (is_internal: the repo-claim test)
from tools.claimcheck import anchors as cc_anchors  # noqa: E402  (CC-1: the repo-claim test with a repository)
from tools.claimcheck import judge as cc_judge  # noqa: E402  (CC-6: prompt v2, the quote check)
from tools.claimcheck.pack import PackBudget, build_pack  # noqa: E402  (CC-5: the evidence pack)
from tools.claimcheck.target import Target, TargetError, parse_report_ref  # noqa: E402  (CC-2)

KILO_CONFIG = Path.home() / ".config" / "kilo" / "kilo.jsonc"
KILO_AUTH = Path.home() / ".local" / "share" / "kilo" / "auth.json"
VERDICTS = ("TRUE", "FALSE", "UNSURE")

# Same job, three wordings: the first sentence swaps its words around, so the
# prompts differ from the very first token and no prefix cache can match.
_OPENERS = (
    "Judge each of the following claims as TRUE, FALSE or UNSURE.",
    "Each of the following claims must be judged by you: TRUE, FALSE or UNSURE.",
    "TRUE, FALSE or UNSURE: that is the judgment you give to each claim below.",
)
_RULES = (
    "Answer UNSURE when the claim depends on code or data you cannot see, "
    "instead of guessing. Reply with JSON only: a list of "
    '{"id": <number>, "verdict": "TRUE|FALSE|UNSURE"} and nothing else, no explanations.')


class Pacer:
    """Per-provider politeness: at most *width* calls in flight and *interval*
    seconds between two starts.  A free tier answers a burst with 429 ("rpm
    exhausted") and the answers of those calls are lost, so pace before, not after."""

    def __init__(self, width: int = 2, interval: float = 4.0) -> None:
        self.interval = interval
        self._slots: dict[str, threading.Semaphore] = {}
        self._next: dict[str, float] = {}
        self._width = width
        self._lock = threading.Lock()

    def wait(self, provider: str, deadline: "float | None" = None) -> threading.Semaphore:
        """A slot of *provider*'s, held until the caller releases it. *deadline* (monotonic):
        a caller that no longer waits for the answer (Gate 1's vote gave up) does not queue for
        a slot past it — `TimeoutError` instead."""
        with self._lock:
            sem = self._slots.setdefault(provider, threading.Semaphore(self._width))
        if deadline is None:
            sem.acquire()
        elif not sem.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise TimeoutError(f"no free slot for {provider} before the deadline")
        with self._lock:
            start = max(time.time(), self._next.get(provider, 0.0))
            self._next[provider] = start + self.interval
        time.sleep(max(0.0, start - time.time()))
        return sem


PACER = Pacer()


SECTION = "claim_vote"
# A header-less urllib request says "Python-urllib/3.x", which Cloudflare in front of some
# free providers (publicai: HTTP 403, error code 1010) refuses before the model is reached.
USER_AGENT = "jan-auto-agent"


def read_ini(root: Path) -> configparser.ConfigParser:
    """``contest.ini`` then ``contest.local.ini`` (the real keys), as roster reads them."""
    parser = roster._new_parser()
    for name in ("contest.ini", roster.LOCAL_FILENAME):
        if (root / name).is_file():
            parser.read(root / name, encoding="utf-8-sig")   # a BOM (Notepad), as roster reads it
    if not parser.has_section(SECTION):
        parser.add_section(SECTION)
    return parser


def strip_jsonc_comments(text: str) -> str:
    """*text* with its JSONC comments gone, every string literal left as it was.

    Bug 210/48: ``kilo.jsonc`` is JSONC, and the old reader dropped only a line
    that was *nothing but* a ``//`` comment. A comment after a value
    (``"baseURL": "https://x", // note``) or a ``/* … */`` block reached
    ``json.loads`` and ``--add-profiles`` / ``--models`` died with a traceback.
    This walks the text once and knows when it is inside a string, so a ``//``
    in ``"https://…"`` is the string's, not a comment. A ``//`` comment ends at
    the line's end (the newline is kept, so a parse error still names the right
    line); a ``/* … */`` block becomes one space, so it never glues two tokens
    together; an unterminated block simply runs to the end of the text and the
    JSON that is left decides whether it parses.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            # a string literal, copied whole: a backslash escapes the next
            # character (``\"`` does not end it), and nothing in it is a comment
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            out.append(" ")
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _kilo_settings(ref: str) -> LlmSettings:
    """``provider/model`` -> settings from Kilo's own files; the key is never printed.

    Bug 210/48: ``kilo.jsonc`` is read as JSONC (``strip_jsonc_comments``), and a file
    that still does not parse is one ``ValueError`` line naming the file and
    the place — never the text itself, which may carry a key — so ``ask``
    records it as the voter's error and ``--add-profiles`` refuses with it. A
    config that parses but has no such provider is the same one-line error.
    """
    provider, model = ref.split("/", 1)
    try:
        config = json.loads(strip_jsonc_comments(KILO_CONFIG.read_text(encoding="utf-8")))
    except json.JSONDecodeError as err:
        raise ValueError(f"{KILO_CONFIG}: not valid JSONC "
                         f"(line {err.lineno}, column {err.colno}: {err.msg})") from None
    try:
        base = config["provider"][provider]["options"]["baseURL"]
    except (KeyError, TypeError, IndexError):
        # `[]`, `{"provider": {}}` or a provider without options: one line too, a
        # TypeError was the traceback `--add-profiles` still printed
        raise ValueError(f"{KILO_CONFIG}: no provider {provider!r} with "
                         "options.baseURL") from None
    if not isinstance(base, str):
        raise ValueError(f"{KILO_CONFIG}: provider {provider!r} options.baseURL is not a string")
    # a plain-string entry ({"openrouter": "sk-…"}) was a TypeError traceback past --add-profiles'
    # one-line refusal; the key itself is never named
    try:
        auth = json.loads(KILO_AUTH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise ValueError(f"{KILO_AUTH}: not valid JSON (line {err.lineno}, column {err.colno})") from None
    entry = auth.get(provider) if isinstance(auth, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if not isinstance(key, str) or not key:
        raise ValueError(f"{KILO_AUTH}: no key for provider {provider!r}")
    return LlmSettings(base_url=base.rstrip("/"), api_key=key, model=model,
                       temperature=0.3, max_tokens=4000)


def voter_settings(ref: str, parser: configparser.ConfigParser) -> LlmSettings:
    """A voter is an ini section name, or ``provider/model`` from Kilo's files."""
    if "/" in ref and not parser.has_section(ref):
        return _kilo_settings(ref)
    local = copy.deepcopy(parser)
    local.set(SECTION, "_pick", ref)
    return resolve_llm_profile(
        local, SECTION, "_pick",
        defaults=LlmSettings(base_url="", api_key="", model="", temperature=0.3,
                             max_tokens=4000))[0]


def voter_models(voters, parser: configparser.ConfigParser) -> dict:
    """``{ref: model}`` of the voters that resolve: votes.json records it (``models``), so the truth
    step reads each voter's family off its model wherever it runs. It read ``contest.ini`` of the
    current directory: the same votes.json gave no row from the project and a REAL row of three
    agnes sections ("three families") from anywhere else. A ref that does not resolve is left out."""
    out = {}
    for ref in dict.fromkeys(voters):
        try:
            model = voter_settings(ref, parser).model
        except Exception:   # noqa: BLE001 — a dead voter is reported by the vote itself
            continue
        if model:
            out[ref] = model
    return out


def add_profiles(refs: list[str], root: Path) -> list[str]:
    """Append ``[claim_vote_llm.<slug>]`` sections for Kilo ``provider/model`` refs
    to ``contest.local.ini``; returns the section names.  Existing ones are kept.
    A freshly-created file is mode 0600 — the key is copied, never printed, and
    never world-readable."""
    path = root / roster.LOCAL_FILENAME
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    names, add, owner = [], "", {}
    for ref in refs:
        name = "claim_vote_llm." + re.sub(r"[^a-z0-9]+", "_", ref.lower()).strip("_")
        # the check read only the file as it was before this call: one ref twice (or gpt-4.1
        # and gpt-4-1) wrote the section twice, and read_ini then failed every later run
        if owner.setdefault(name, ref) != ref:
            raise ValueError(f"{owner[name]!r} and {ref!r} both map to [{name}]; pass one of them")
        if name in names:
            continue
        names.append(name)
        if f"[{name}]" in existing:
            continue
        st = _kilo_settings(ref)
        add += (f"\n[{name}]\nbase_url        = {st.base_url}\napi_key         = {st.api_key}\n"
                f"model           = {st.model}\napi_format      = openai\n"
                f"temperature     = 0.3\nmax_tokens      = 4000\n")
    if add:
        content = existing.rstrip("\n") + "\n" + add
        if path.is_file():
            path.write_text(content, encoding="utf-8")
        else:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
    return names


def build_prompt(claims: list[str], run: int, seed: int,
                 fixed: bool = False) -> tuple[str, list[int]]:
    """The user message for run *run*, and the original index of each shown claim.

    *fixed* sends the byte-identical prompt every run (run 0's wording and
    order): the control that tells a model's own randomness from the effect of
    rewording."""
    if fixed:
        run = 0
    order = list(range(len(claims)))
    random.Random(seed * 31 + run).shuffle(order)
    lines = [f"{n}. {claims[i]}" for n, i in enumerate(order, 1)]
    return _OPENERS[run % len(_OPENERS)] + " " + _RULES + "\n\n" + "\n".join(lines), order


def parse_votes(text: str, order: list[int]) -> dict[int, str]:
    """Model reply -> {original claim index: verdict}; garbage gives {}."""
    text = text or ""
    # judge's bounded tries (the head and the tail of the reply, a work budget): the CLAIM-1 path
    # had judge.py's RecursionError too, and lost an answer past its first 400 `[`
    for rows in cc_judge.json_values(text, "[", strict=True):
        if not isinstance(rows, list):
            continue
        votes: dict[int, str] = {}
        for row in rows:
            try:
                n, verdict = int(row["id"]), str(row["verdict"]).upper().strip()
            except (KeyError, TypeError, ValueError):
                continue
            if verdict in VERDICTS and 1 <= n <= len(order):
                votes[order[n - 1]] = verdict
        if votes:
            return votes
    return {}


def ask(ref: str, claims: list[str], run: int, seed: int, parser: configparser.ConfigParser,
        timeout: int, batch: int = 10, fixed: bool = False) -> dict:
    """One model, one run: the claims go in batches (a long list of claims
    exhausts a reasoning model's budget before it writes a single verdict).

    A voter that cannot be resolved returns a result row with errors set and
    no votes — a dead model is a result, not a crash.
    """
    try:
        st = voter_settings(ref, parser)
        retry = retry_kwargs_from_config(parser, SECTION)
        host = st.base_url.split("//")[-1].split("/")[0]
    except (ValueError, OSError, KeyError) as exc:
        return {"model": ref, "run": run, "votes": {}, "error": str(exc)[:120]}
    votes: dict[int, str] = {}
    errors: list[str] = []
    raw_head = ""
    for start in range(0, len(claims), batch):
        chunk = claims[start:start + batch]
        prompt, order = build_prompt(chunk, run, seed + start, fixed)
        url, headers, payload = build_chat_request(
            base_url=st.base_url, api_key=st.api_key, model=st.model,
            api_format=st.api_format, temperature=st.temperature,
            max_tokens=st.max_tokens, system="You are a careful technical fact checker.",
            user_msg=prompt, num_ctx=st.num_ctx, think=st.think)
        headers.setdefault("User-Agent", USER_AGENT)
        sem = PACER.wait(host)
        try:
            text = strip_think(request_completion(url, headers, payload, timeout,
                                                  api_format=st.api_format, **retry))
        except Exception as exc:              # a dead model is a result, not a crash
            errors.append(str(exc)[:120])
            continue
        finally:
            sem.release()
        got = parse_votes(text, order)
        if not got and not raw_head:
            raw_head = (text or "")[:200]
        votes.update({start + i: v for i, v in got.items()})
    out = {"model": ref, "run": run, "votes": votes}
    if errors:
        out["error"] = errors[0]
    if raw_head:
        out["raw_head"] = raw_head
    return out


def ask_v2(ref: str, items: list, run: int, seed: int, parser: configparser.ConfigParser,
           timeout: float, batch: int = 10, code_batch: int = 2, fixed: bool = False,
           deadline: "float | None" = None) -> dict:
    """CC-6's `ask`: one model, one run over ``(claim, pack_or_None)`` items, prompt v2,
    every committed vote on a claim with a pack checked against its quote.

    The HTTP call, the pacer and the retry budget are `ask`'s; the batching, the prompt,
    the parser and the check are ``tools.claimcheck.judge.ask_with_packs``. The row adds
    ``accepted`` (the evidence of each vote that stood) and ``rejected`` (each downgrade,
    with its reason) to `ask`'s. *deadline* (``time.monotonic()``, Gate 1's vote): no slot is
    waited for and no retry is made past it (`request_completion`'s ``deadline``)."""
    try:
        st = voter_settings(ref, parser)
        retry = retry_kwargs_from_config(parser, SECTION)
        host = st.base_url.split("//")[-1].split("/")[0]
    except (ValueError, OSError, KeyError) as exc:
        return {"model": ref, "run": run, "votes": {}, "accepted": {}, "rejected": [],
                "error": str(exc)[:120]}

    def complete(prompt: str) -> str:
        url, headers, payload = build_chat_request(
            base_url=st.base_url, api_key=st.api_key, model=st.model,
            api_format=st.api_format, temperature=st.temperature,
            max_tokens=st.max_tokens, system=cc_judge.SYSTEM,
            user_msg=prompt, num_ctx=st.num_ctx, think=st.think)
        headers.setdefault("User-Agent", USER_AGENT)
        sem = PACER.wait(host, deadline)
        started = time.monotonic()
        # passed only when set: a stub of request_completion with the old signature still fits
        until = {"deadline": deadline} if deadline is not None else {}
        try:
            return strip_think(request_completion(url, headers, payload, timeout,
                                                  api_format=st.api_format, **retry, **until))
        finally:
            sem.release()
            calls[0] += 1
            # a live run is minutes long and the report comes at the end: one line a request,
            # so the log shows which voter is alive and how far it has got
            print(f"[progress] {ref} run{run} request {calls[0]} "
                  f"{time.monotonic() - started:.0f}s", file=sys.stderr, flush=True)

    calls = [0]
    got = cc_judge.ask_with_packs(items, run, seed, complete, batch=batch,
                                  code_batch=code_batch, fixed=fixed)
    out = {"model": ref, "run": run, "votes": got["votes"], "accepted": got["accepted"],
           "rejected": got["rejected"]}
    if got["errors"]:
        out["error"] = got["errors"][0]
    if got["raw_head"]:
        out["raw_head"] = got["raw_head"]
    return out


MIN_COMMITTED = 3   # models that must commit (TRUE/FALSE) before a verdict stands
#: CC-11: when a verdict is accepted (``unanimous``). ``all``: every voter of the run committed,
#: to one verdict (the rule until CC-10's Gate 1 set says otherwise). ``committed``: at least the
#: quorum committed and the committed agree; an abstainer (UNSURE, no vote on this claim, a quote
#: the code rejected) does not block. A dissent among the committed blocks under both.
ACCEPT_RULES = ("all", "committed")
DEFAULT_ACCEPT = "all"


def _strict_plurality(counts: Counter) -> str:
    """The model's own verdict is its strict plurality; a tie is UNSURE (an
    abstention), so a contradictory model is visible rather than masked by the
    first run in insertion order."""
    most = counts.most_common(1)[0][1]
    if sum(1 for _, c in counts.most_common() if c == most) > 1:
        return "UNSURE"
    return counts.most_common(1)[0][0]


def claim_kind(claim: str, symbols: "set[str] | None" = None, view=None) -> tuple:
    """(kind, dangling) of one claim: ``code``, ``world`` or ``mixed``.

    With a ``RepoView`` the claim's anchors are looked up in the repository
    (CC-1, ``tools/claimcheck/anchors.py``); without one the regex rule
    ``lenz_claim_filter.is_internal`` decides, as before, and nothing dangles."""
    if view is not None:
        resolved = cc_anchors.resolve_anchors(cc_anchors.extract_anchors(claim), view)
        return cc_anchors.classify(claim, resolved), cc_anchors.is_dangling(resolved)
    return ("code" if lf.is_internal(claim, symbols or set()) else "world"), False


def _evidence_of(results: list[dict], i: int, verdict: "str | None" = None) -> tuple:
    """(chunk ids, quotes, {reason: n}) of claim *i* over every result: what the votes
    that stood cited, first seen first, and what the quote check rejected. With a TRUE/FALSE
    *verdict* only the votes for it count: a FALSE run's quote led the `how` of a REAL row."""
    chunks: list = []
    quotes: list = []
    rejected: Counter = Counter()
    for r in results:
        got = r.get("accepted", {}).get(i)
        if got and verdict in ("TRUE", "FALSE") and got.get("verdict", verdict) != verdict:
            got = None     # a vote for the other side (an entry without a verdict is an old file's)
        if got:
            if got.get("chunk") and got["chunk"] not in chunks:
                chunks.append(got["chunk"])
            if got.get("quote") and got["quote"] not in quotes:
                quotes.append(got["quote"])
        rejected.update(row["reason"] for row in r.get("rejected", ()) if row.get("claim") == i)
    return chunks, quotes, dict(rejected)


def tally(claims: list[dict], results: list[dict],
          symbols: "set[str] | None" = None, quorum: int = MIN_COMMITTED,
          view=None, meta: "list[dict] | None" = None, accept: str = DEFAULT_ACCEPT) -> list[dict]:
    """Per claim: every vote, each model's own majority, and the cross-model one.

    UNSURE is an abstention, not a vote: the verdict is decided among TRUE and
    FALSE, ``SPLIT`` on a tie, ``UNSURE`` when nobody committed.  A claim about
    this repo's own code (``needs_code``) is never accepted from votes: the
    models cannot see the code, so a TRUE there is a plausible guess.  With a
    ``view`` (a ``RepoView``) the test is the anchor classifier, and each row
    also says ``kind`` (code|world|mixed) and ``dangling``.

    CC-6: *meta* (one ``{kind, dangling, sha}`` per claim, from ``--target``) means the
    votes were cast on the evidence and already passed ``verify_quotes``: a code claim's
    verdict then stands like a world claim's (no ``CODE-CHECK``), under the same rule, and
    each row gains ``id`` (when the claim has one), ``sha``, ``evidence``, ``quotes``,
    ``rejected`` ({reason: n}) and ``downgrades`` (their total).

    CC-11: *accept* is the acceptance rule (`ACCEPT_RULES`). Under ``committed`` each row also
    says ``abstained``: the voters of the run that did not commit on this claim. Under ``all``
    the rows are what they were (no ``abstained``: an accepted row has none by the rule).
    """
    if accept not in ACCEPT_RULES:
        raise ValueError(f"accept = {accept!r}: one of {', '.join(ACCEPT_RULES)}")
    out = []
    # a result read back from a saved JSON has string keys; a live one has ints
    results = [{**r, "votes": {int(k): v for k, v in r["votes"].items()},
                **({"accepted": {int(k): v for k, v in r["accepted"].items()}}
                   if "accepted" in r else {})} for r in results]
    # a model with no vote at all in any run (dead, out of its free plan, a 404) is not a voter:
    # counted, it made "every voter committed" false for every claim, and a run with one dead
    # model decided 0 of 80 (round 280, run B). A lost batch or a silent run of a live model
    # still counts against unanimity: that model did vote elsewhere.
    voters = {r["model"] for r in results if r["votes"]} or {r["model"] for r in results}
    for i, c in enumerate(claims):
        per_model: dict[str, list[str]] = {}
        for r in results:
            if i in r["votes"]:
                per_model.setdefault(r["model"], []).append(r["votes"][i])
        model_major = {m: _strict_plurality(Counter(v)) for m, v in per_model.items()}
        total = Counter(v for vs in per_model.values() for v in vs)
        decided = Counter(v for v in model_major.values() if v != "UNSURE").most_common()
        committed = sum(n for _, n in decided)
        if committed < quorum:         # one brave vote among abstainers is a guess
            verdict = "UNSURE"
        elif len(decided) > 1 and decided[0][1] == decided[1][1]:
            verdict = "SPLIT"
        else:
            verdict = decided[0][0]
        # the acceptance rule of the runbook: every voter committed, all to one verdict.
        # ``voters`` is every model that produced any result; a voter with no vote
        # on this claim (silent, lost batch, unreadable reply) makes unanimous False.
        # CC-11 `committed`: that last clause dropped — with five voters one UNSURE blocked the
        # claim for all, so more voters settled fewer claims (132 of 150 at five, 91 at nine;
        # 146 at both under this rule, 0 wrong). A dissent still blocks: len(decided) == 1.
        unanimous = (len(decided) == 1 and committed >= quorum
                     and (accept == "committed" or committed == len(voters)))
        if meta is not None:
            kind, dangling = meta[i]["kind"], meta[i]["dangling"]
        else:
            kind, dangling = claim_kind(c["claim"], symbols, view)
        needs_code = kind != "world"
        blind = needs_code and meta is None     # a code claim the voters could not see
        if blind and verdict in ("TRUE", "FALSE", "SPLIT"):
            verdict = "CODE-CHECK"
        row = {"claim": c["claim"], "truth": c.get("truth"), "verdict": verdict,
               "needs_code": needs_code, "kind": kind, "dangling": dangling,
               "unanimous": unanimous and not blind,
               "by_model": model_major,
               "all_votes": dict(total)}
        if accept == "committed":
            # who was asked and did not commit here: a row accepted over abstainers says so
            row["abstained"] = sorted(m for m in voters if model_major.get(m, "UNSURE") == "UNSURE")
        if meta is not None:
            chunks, quotes, rejected = _evidence_of(results, i, verdict)
            if c.get("id") is not None:
                row = {"id": c["id"], **row}
            row.update(sha=meta[i].get("sha", ""), evidence=chunks, quotes=quotes,
                       rejected=rejected, downgrades=sum(rejected.values()))
        out.append(row)
    return out


def report_sha(path: "Path | None") -> "str | None":
    """The first sha a report names about itself (``origin/kc @ afa53f1``), or None."""
    if path is None:
        return None
    for hint in parse_report_ref(path.read_text(encoding="utf-8", errors="replace")):
        if hint.sha:
            return hint.sha
    return None


def prepare_target(claims: list[dict], target: Target, budget: PackBudget,
                   base: "str | None" = None, collect: bool = True) -> tuple:
    """(items, meta) for the claims at *target*: each claim's anchors resolved and
    classified there (CC-1); a ``code`` or ``mixed`` claim gets its pack (CC-5, with
    ``base``..the target's sha when *base* is given, and the collect facts of the target when
    *collect*), a ``world`` claim ``None``."""
    view = target.view()
    items, meta = [], []
    model = [None, False]    # built once, on the first claim that needs a pack
    for c in claims:
        text = c["claim"]
        resolved = cc_anchors.resolve_anchors(cc_anchors.extract_anchors(text), view)
        kind = cc_anchors.classify(text, resolved)
        pack = None
        if kind != "world":
            if collect and not model[1]:
                # CC-3's collect facts (callers, contracts, risk) were never built on the voter's
                # path: build_pack takes a model, nothing passed one. Target.collect never raises
                # (a repository collect cannot model is a warning and an absent model).
                model[:] = [target.collect(), True]
            pack = build_pack(text, resolved, view, model=model[0], base=base,
                              head=target.sha if base else None, budget=budget)
        items.append((text, pack))
        meta.append({"kind": kind, "dangling": cc_anchors.is_dangling(resolved),
                     "sha": target.sha})
    return items, meta


#: claim_vote.py options that only mean something with --target
TARGET_ONLY = ("ref", "base", "fetch", "pack_chars", "code_batch", "report", "expect_sha", "scratch",
               "no_collect")


def given_or(value, fallback):
    """*value* when the option was given, else *fallback*: `args.x or default` turned an explicit
    `--interval 0` (no pacing) into the ini's 4 s."""
    return fallback if value is None else value


def check_counts(args) -> str:
    """Why a count option is out of range, or ``""``: a batch, a per-host limit or a pack size
    is at least 1, an interval at least 0 (both shared with claim_diff.py)."""
    for name in ("batch", "code_batch", "per_provider", "pack_chars", "parallel", "runs"):
        value = getattr(args, name, None)
        if value is not None and value < 1:
            return f"--{name.replace('_', '-')} {value}: must be 1 or more"
    interval = getattr(args, "interval", None)
    if interval is not None and interval < 0:
        return f"--interval {interval}: must be 0 or more"
    return ""


def accept_rule(given: "str | None", parser: configparser.ConfigParser) -> tuple:
    """(rule, why it is refused or ``""``): ``--accept`` beats ``[claim_vote] accept``, which
    defaults to `DEFAULT_ACCEPT` (shared with claim_diff.py)."""
    rule = given if given is not None else parser.get(SECTION, "accept", fallback=DEFAULT_ACCEPT)
    rule = rule.strip().lower()
    if rule not in ACCEPT_RULES:
        where = "--accept" if given is not None else f"[{SECTION}] accept ="
        return rule, f"{where} {rule}: must be {' or '.join(ACCEPT_RULES)}"
    return rule, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("claims", type=Path, nargs="?")
    ap.add_argument("--profiles", nargs="+", help="ini section names (contest.local.ini)")
    ap.add_argument("--models", nargs="+", help="provider/model from Kilo's files, no profile needed")
    ap.add_argument("--add-profiles", nargs="+", metavar="PROVIDER/MODEL",
                    help="write Kilo provider/model refs into contest.local.ini as profiles, then stop")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--fixed-prompt", action="store_true", help="the same prompt every run (a control)")
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--batch", type=int, help="claims per request ([claim_vote] batch)")
    ap.add_argument("--per-provider", type=int, help="calls in flight per host ([claim_vote] per_provider)")
    ap.add_argument("--interval", type=float, help="seconds between call starts per host ([claim_vote] interval_sec)")
    ap.add_argument("--accept", metavar="all|committed",
                    help="the acceptance rule ([claim_vote] accept, default all): all = every voter committed; "
                         "committed = at least 3 committed and they agree, an abstainer does not block (CC-11)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--repo-root", type=Path, default=Path.cwd(), help="where contest.ini / contest.local.ini live")
    ap.add_argument("--symbols-root", type=Path, help="the repo whose code the claims are about (default: --repo-root)")
    # CC-6: judge the claims on the evidence of a repository at a pinned commit
    ap.add_argument("--target", metavar="REPO", help="a repository (path or URL): code claims are judged on its evidence")
    ap.add_argument("--ref", help="the commit of --target to judge at (default HEAD)")
    ap.add_argument("--base", metavar="REF", help="also give each pack the git evidence of BASE..the --ref commit")
    ap.add_argument("--fetch", action="store_true", help="git fetch --target before resolving --ref")
    ap.add_argument("--pack-chars", type=int, help="rendered characters of one claim's pack ([claim_vote] pack_chars)")
    ap.add_argument("--code-batch", type=int, help="code and mixed claims per request ([claim_vote] code_batch)")
    ap.add_argument("--report", type=Path, help="the report the claims come from: the sha it names is checked against --ref")
    ap.add_argument("--expect-sha", help="the sha the report says it is about (instead of --report)")
    ap.add_argument("--scratch", type=Path, help="where --target's worktree goes (default: <repo-root>/claim-check-out/scratch)")
    ap.add_argument("--no-collect", action="store_true",
                    help="packs without the target's collect facts (a collect of a large tree takes seconds to minutes)")
    args = ap.parse_args(argv)

    if args.add_profiles:
        # Bug 210/48: Kilo's files unreadable, unparsable or without the
        # provider is one refusal line on stderr and exit 2, not a traceback.
        try:
            names = add_profiles(args.add_profiles, args.repo_root)
        except (ValueError, OSError, KeyError) as exc:
            detail = f"no {exc} entry" if isinstance(exc, KeyError) else str(exc)
            print(f"claim_vote: --add-profiles refused: {detail}", file=sys.stderr)
            return 2
        for name in names:
            print(f"[{name}] in {roster.LOCAL_FILENAME}")
        return 0
    # options that only mean something with --target were accepted and ignored without it;
    # --expect-sha is a guard, so it silently guarded nothing
    if not args.target:
        given = [f"--{k.replace('_', '-')}" for k in TARGET_ONLY if getattr(args, k) not in (None, False)]
        if given:
            ap.error(f"{', '.join(given)} need --target")
    bad = check_counts(args)
    if bad:
        ap.error(bad)
    parser = read_ini(args.repo_root)
    get = lambda key, cast, fallback: cast(parser.get(SECTION, key, fallback=str(fallback)))  # noqa: E731
    batch = given_or(args.batch, get("batch", int, 10))
    per_provider = given_or(args.per_provider, get("per_provider", int, 2))
    interval = given_or(args.interval, get("interval_sec", float, 4.0))
    # check_counts saw the options only: `batch = 0` in the ini was range()'s ValueError on the
    # CLAIM-1 path (the --target path clamps it), per_provider = 0 a pacer nothing passes
    for key, value, least in (("batch", batch, 1), ("per_provider", per_provider, 1),
                              ("interval_sec", interval, 0)):
        if value < least:
            ap.error(f"[{SECTION}] {key} = {value:g}: must be {least} or more")
    # one line and exit 2, before any request: a typo here was a run under a rule nobody chose
    args.accept, why = accept_rule(args.accept, parser)
    if why:
        print(f"claim_vote: {why}", file=sys.stderr)
        return 2
    PACER.__init__(per_provider, interval)
    voters = (args.profiles or []) + (args.models or []) or [
        n.strip() for n in parser.get(SECTION, "llm_profiles", fallback="").split(",") if n.strip()]
    if not args.claims or not voters:
        ap.error("need claims.json and voters: --profiles, --models, or [claim_vote] llm_profiles")
    if len(voters) < MIN_COMMITTED:
        # Bug 210/46: below the quorum no claim can ever be accepted — `tally`
        # needs MIN_COMMITTED committed models — and the table was all UNSURE
        # with no word why. One line says so before the run; the run goes on
        # (its votes are still worth reading) and the exit code is unchanged.
        print(f"claim_vote: {len(voters)} voters, quorum is {MIN_COMMITTED}: "
              "no claim can be accepted", file=sys.stderr)

    raw = json.loads(args.claims.read_text(encoding="utf-8"))
    # the object form ({"claims": [...]}, claim_diff's and the benches') was iterated as a dict:
    # its keys `set`, `base_sha`, `note`, `claims` were voted on as claims, exit 0
    if isinstance(raw, dict) and isinstance(raw.get("claims"), list):
        raw = raw["claims"]
    if not isinstance(raw, list) or not all(
            isinstance(c, str) or (isinstance(c, dict) and isinstance(c.get("claim"), str)) for c in raw):
        ap.error(f"{args.claims}: not a claims file (a list of claims, or {{\"claims\": [...]}}, "
                 "each a string or an object with a \"claim\")")
    claims = [c if isinstance(c, dict) else {"claim": c, "truth": None} for c in raw]
    texts = [c["claim"] for c in claims]
    if args.target:
        return main_target(args, parser, claims, voters, batch)
    jobs = [(m, r) for m in voters for r in range(args.runs)]
    with concurrent.futures.ThreadPoolExecutor(args.parallel) as pool:
        results = list(pool.map(lambda j: ask(j[0], texts, j[1], args.seed, parser,
                                              args.timeout, batch, args.fixed_prompt), jobs))
    # CC-1: --symbols-root names the repo the claims are about, so its anchors are
    # resolved there; without it the old regex rule decides needs_code.
    view = cc_anchors.PathRepoView(args.symbols_root) if args.symbols_root else None
    table = tally(claims, results, lf._repo_symbols(args.symbols_root or args.repo_root), view=view,
                  accept=args.accept)
    report = {"accept": args.accept, "models": voter_models(voters, parser), "results": results, "claims": table}
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in results:
        print(f"{r['model']:48} run{r['run']} votes={len(r['votes'])}/{len(texts)}"
              + (f" ERROR {r['error']}" if r.get("error") else ""))
    dead = sorted({r["model"] for r in results if not r["votes"]})
    print("verdicts:", dict(Counter(t["verdict"] for t in table)),
          "| unanimous (*, accept):", sum(t["unanimous"] for t in table), f"(rule {args.accept})",
          "| models with no votes at all:", dead or "none")
    for t in table:
        print(f"[{t['verdict']:10}]{'*' if t['unanimous'] else ' '}truth={t['truth']} {t['all_votes']} {t['claim'][:90]}")
    if claims and results and all(r.get("error") and not r["votes"] for r in results):
        # every voter failed (a wrong key, no network, a dead endpoint): votes.json says so, and
        # the exit says so too — it was 0, and run_claim_check.sh went on to an all-UNSURE table.
        # A voter that answered and said nothing usable is a result, not a failed run.
        print(f"claim_vote: no voter returned a vote ({len(results)} runs, every one an ERROR)",
              file=sys.stderr)
        return 1
    return 0


def main_target(args, parser: configparser.ConfigParser, claims: list[dict],
                voters: list[str], batch: int) -> int:
    """CC-6: the vote on the evidence of ``--target`` at ``--ref``."""
    get = lambda key, cast, fallback: cast(parser.get(SECTION, key, fallback=str(fallback)))  # noqa: E731
    code_batch = given_or(args.code_batch, get("code_batch", int, 2))
    budget = PackBudget.from_config(parser)
    if args.pack_chars:
        budget = PackBudget(chars=args.pack_chars, chunks=budget.chunks, per_chunk=budget.per_chunk)
    try:
        expect = args.expect_sha or report_sha(args.report)
    except OSError as exc:
        print(f"claim_vote: --report {args.report}: {exc.strerror or exc}", file=sys.stderr)
        return 2
    scratch = args.scratch or args.repo_root / "claim-check-out" / "scratch"
    try:
        target = Target.open(args.target, args.ref or "HEAD", scratch=scratch, fetch=args.fetch,
                             expect_sha=expect)
    except TargetError as exc:
        print(f"claim_vote: --target refused: {exc}", file=sys.stderr)
        return 2
    with target:
        for warning in target.warnings:
            print(f"claim_vote: warning: {warning}", file=sys.stderr)
        # --base is resolved once, here, and the sha is what votes.json records: a ref name
        # re-resolved by claimcheck_truth.py after the branch moved rebuilt another diff
        base_sha = None
        if args.base:
            try:
                base_sha = target.view().git("rev-parse", "--verify", f"{args.base}^{{commit}}").strip()
            except TargetError as exc:
                print(f"claim_vote: --base {args.base}: does not resolve ({exc})", file=sys.stderr)
                return 2
        items, meta = prepare_target(claims, target, budget, base_sha, collect=not args.no_collect)
        jobs = [(m, r) for m in voters for r in range(args.runs)]
        with concurrent.futures.ThreadPoolExecutor(args.parallel) as pool:
            results = list(pool.map(lambda j: ask_v2(j[0], items, j[1], args.seed, parser,
                                                     args.timeout, batch, code_batch,
                                                     args.fixed_prompt), jobs))
        table = tally(claims, results, meta=meta, accept=args.accept)
        warnings = list(target.warnings)
        report = {"target": {"repo": target.repo, "ref": target.ref, "sha": target.sha,
                             "base": args.base, "base_sha": base_sha, "warnings": warnings,
                             "collect": not args.no_collect,
                             # the budget the packs were cut to (--pack-chars may differ from the ini)
                             "budget": {"chars": budget.chars, "chunks": budget.chunks,
                                        "per_chunk": budget.per_chunk}},
                  "accept": args.accept, "models": voter_models(voters, parser),
                  "results": results, "claims": table}
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in results:
        print(f"{r['model']:48} run{r['run']} votes={len(r['votes'])}/{len(claims)}"
              f" rejected={len(r.get('rejected', ()))}"
              + (f" ERROR {r['error']}" if r.get("error") else ""))
    dead = sorted({r["model"] for r in results if not r["votes"]})
    print("verdicts:", dict(Counter(t["verdict"] for t in table)),
          "| unanimous (*, accept):", sum(t["unanimous"] for t in table), f"(rule {args.accept})",
          "| models with no votes at all:", dead or "none",
          "| rejected quotes:", sum(len(r.get("rejected", ())) for r in results),
          "| warnings:", "; ".join(warnings) or "none",
          f"| at {report['target']['sha'][:7]}")
    for t in table:
        print(f"[{t['verdict']:10}]{'*' if t['unanimous'] else ' '}{t['kind']:5} truth={t['truth']} "
              f"{t['all_votes']} {t['claim'][:90]}")
    if claims and results and all(r.get("error") and not r["votes"] for r in results):
        # every voter failed (a wrong key, no network, a dead endpoint): votes.json says so, and
        # the exit says so too — it was 0, and run_claim_check.sh went on to an all-UNSURE table.
        # A voter that answered and said nothing usable is a result, not a failed run.
        print(f"claim_vote: no voter returned a vote ({len(results)} runs, every one an ERROR)",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
