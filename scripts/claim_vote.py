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

    def wait(self, provider: str) -> threading.Semaphore:
        with self._lock:
            sem = self._slots.setdefault(provider, threading.Semaphore(self._width))
        sem.acquire()
        with self._lock:
            start = max(time.time(), self._next.get(provider, 0.0))
            self._next[provider] = start + self.interval
        time.sleep(max(0.0, start - time.time()))
        return sem


PACER = Pacer()


SECTION = "claim_vote"


def read_ini(root: Path) -> configparser.ConfigParser:
    """``contest.ini`` then ``contest.local.ini`` (the real keys), as roster reads them."""
    parser = roster._new_parser()
    for name in ("contest.ini", roster.LOCAL_FILENAME):
        if (root / name).is_file():
            parser.read(root / name, encoding="utf-8")
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
    key = json.loads(KILO_AUTH.read_text(encoding="utf-8"))[provider]["key"]
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


def add_profiles(refs: list[str], root: Path) -> list[str]:
    """Append ``[claim_vote_llm.<slug>]`` sections for Kilo ``provider/model`` refs
    to ``contest.local.ini``; returns the section names.  Existing ones are kept.
    A freshly-created file is mode 0600 — the key is copied, never printed, and
    never world-readable."""
    path = root / roster.LOCAL_FILENAME
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    names, add = [], ""
    for ref in refs:
        name = "claim_vote_llm." + re.sub(r"[^a-z0-9]+", "_", ref.lower()).strip("_")
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
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "[":
            continue
        try:
            rows, _end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
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


MIN_COMMITTED = 3   # models that must commit (TRUE/FALSE) before a verdict stands


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


def tally(claims: list[dict], results: list[dict],
          symbols: "set[str] | None" = None, quorum: int = MIN_COMMITTED,
          view=None) -> list[dict]:
    """Per claim: every vote, each model's own majority, and the cross-model one.

    UNSURE is an abstention, not a vote: the verdict is decided among TRUE and
    FALSE, ``SPLIT`` on a tie, ``UNSURE`` when nobody committed.  A claim about
    this repo's own code (``needs_code``) is never accepted from votes: the
    models cannot see the code, so a TRUE there is a plausible guess.  With a
    ``view`` (a ``RepoView``) the test is the anchor classifier, and each row
    also says ``kind`` (code|world|mixed) and ``dangling``.
    """
    out = []
    # a result read back from a saved JSON has string keys; a live one has ints
    results = [{**r, "votes": {int(k): v for k, v in r["votes"].items()}} for r in results]
    voters = {r["model"] for r in results}
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
        unanimous = (len(decided) == 1 and committed >= quorum
                     and committed == len(voters))
        kind, dangling = claim_kind(c["claim"], symbols, view)
        needs_code = kind != "world"
        if needs_code and verdict in ("TRUE", "FALSE", "SPLIT"):
            verdict = "CODE-CHECK"
        out.append({"claim": c["claim"], "truth": c.get("truth"), "verdict": verdict,
                    "needs_code": needs_code, "kind": kind, "dangling": dangling,
                    "unanimous": unanimous and not needs_code,
                    "by_model": model_major,
                    "all_votes": dict(total)})
    return out


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
    ap.add_argument("--out", type=Path)
    ap.add_argument("--repo-root", type=Path, default=Path.cwd(), help="where contest.ini / contest.local.ini live")
    ap.add_argument("--symbols-root", type=Path, help="the repo whose code the claims are about (default: --repo-root)")
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
    parser = read_ini(args.repo_root)
    get = lambda key, cast, fallback: cast(parser.get(SECTION, key, fallback=str(fallback)))  # noqa: E731
    batch = args.batch or get("batch", int, 10)
    PACER.__init__(args.per_provider or get("per_provider", int, 2),
                   args.interval or get("interval_sec", float, 4.0))
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
    claims = [c if isinstance(c, dict) else {"claim": c, "truth": None} for c in raw]
    texts = [c["claim"] for c in claims]
    jobs = [(m, r) for m in voters for r in range(args.runs)]
    with concurrent.futures.ThreadPoolExecutor(args.parallel) as pool:
        results = list(pool.map(lambda j: ask(j[0], texts, j[1], args.seed, parser,
                                              args.timeout, batch, args.fixed_prompt), jobs))
    # CC-1: --symbols-root names the repo the claims are about, so its anchors are
    # resolved there; without it the old regex rule decides needs_code.
    view = cc_anchors.PathRepoView(args.symbols_root) if args.symbols_root else None
    table = tally(claims, results, lf._repo_symbols(args.symbols_root or args.repo_root), view=view)
    report = {"results": results, "claims": table}
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in results:
        print(f"{r['model']:48} run{r['run']} votes={len(r['votes'])}/{len(texts)}"
              + (f" ERROR {r['error']}" if r.get("error") else ""))
    dead = sorted({r["model"] for r in results if not r["votes"]})
    print("verdicts:", dict(Counter(t["verdict"] for t in table)),
          "| unanimous (*, accept):", sum(t["unanimous"] for t in table),
          "| models with no votes at all:", dead or "none")
    for t in table:
        print(f"[{t['verdict']:10}]{'*' if t['unanimous'] else ' '}truth={t['truth']} {t['all_votes']} {t['claim'][:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
