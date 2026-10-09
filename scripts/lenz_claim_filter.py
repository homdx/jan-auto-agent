#!/usr/bin/env python3
"""Check the outside-world claims of a model-written report with Lenz.

Reads one report (a reviewer's bug list, a round summary), keeps only the
claims that are about a *public* technical fact (pytest, git, POSIX, stdlib),
drops everything that talks about this repo (Lenz cannot know our code), sends
the survivors in ONE /assess batch, and escalates the single most doubtful row
to /verify. Prints a human text and writes a JSON for later automation.

Credits are scarce, so every answer is cached on disk by claim text and a
repeated claim never costs a second credit.  The key comes from LENZ_API_KEY,
else it is asked for on the console (never written to disk), and only when a
request must actually go out: a fully cached run needs no key.

    python3 scripts/lenz_claim_filter.py kc-bug-report.md --json out.json
    python3 scripts/lenz_claim_filter.py report.md --dry-run   # no network
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://lenz.io/api/v1"
ENV_KEY = "LENZ_API_KEY"
DEFAULT_CACHE = Path.home() / ".cache" / "lenz" / "claims.json"
MAX_BATCH = 20          # /assess takes at most 20 claims per call
DEFAULT_MAX = 12        # our own cap: one credit per claim
VERIFY_CREDITS = 10     # what one /verify costs, for the console estimate

# A sentence is *internal* when it names something that only exists in this
# repo: a path, a ticket id, a backticked identifier with _ or a dot-suffix.
_TICKET = re.compile(r"\b(?:KC|FL|AR|AUTO|SLOW|GATE1)-[\w-]*\d")
_BACKTICK = re.compile(r"`([^`]+)`")
_PATHLIKE = re.compile(r"(?:\w+/)+\w|\b\w+/(?![\w/])|\.(?:py|md|ini|json|csv|sh)\b")
# Ordinary words that carry a slash ("read/write", "TCP/IP") and plain
# fractions ("24/7") are not paths; they are cut out before _PATHLIKE looks,
# or a public claim holding one is never sent and is voted CODE-CHECK.
_SLASH_WORDS = re.compile(
    r"(?<![\w/.])(?:and/or|either/or|read/write|r/w|input/output|i/o|tcp/ip|"
    r"client/server|yes/no|true/false|on/off|pass/fail|success/failure|"
    r"enable/disable|enabled/disabled|start/stop|open/close|get/set|up/down|"
    r"left/right|min/max|he/she|s/he|his/her|km/h|w/o|w/)(?![\w/])"
    r"|(?<![\w/.])\d+/\d+(?![\w/])", re.I)
_LABEL = re.compile(r"^([A-Z][\w' ]{1,20}):\s+")
_NOT_CLAIMS = {"fix", "where", "proof", "severity", "impact"}
_SYMBOL = re.compile(r"^[_a-zA-Z]\w*(?:\(\))?$")
_SNAKE = re.compile(r"\b[_a-zA-Z]\w*_\w+\b")    # a bare foo_bar outside backticks

# Words that mark a claim about the outside world; used only to rank.
_HINTS = re.compile(
    r"exit(?:s)? with code|\brc ?\d|unrecognized|plugin|\broot\b|chmod|"
    r"git (?:status|diff|show|rev-list|checkout)|numstat|rename|\$HOME|"
    r"expandvars|rglob|splitlines|pytest|posix|xdist|re\.M\b",
    re.I)


def _repo_symbols(root: Path) -> set[str]:
    """Names defined in the repo's own code: def/class names and file stems."""
    names: set[str] = set()
    # -z: NUL-separated and unquoted, so "my module.py" stays one file and a
    # non-ASCII name is not C-quoted; surrogateescape keeps any bytes readable.
    try:
        raw = subprocess.run(["git", "ls-files", "-z", "*.py"], cwd=root,
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raw = b""
    files = [f for f in raw.decode("utf-8", "surrogateescape").split("\0") if f]
    for rel in files:
        names.add(Path(rel).stem)
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        names.update(re.findall(r"^\s*(?:def|class)\s+(\w+)", text, re.M))
    return {n for n in names if len(n) > 5 and not n.startswith("__")}


def split_sentences(text: str) -> list[str]:
    """Report text -> candidate sentences (markup stripped, tables dropped)."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("|", "#", "```")):
            continue
        # one real list marker ("- ", "* ", "+ ", "1. ", "12) ", "1.2. ") and
        # only that: "404 is ...", "3.5 seconds ...", "-1 is ..." keep their words
        line = re.sub(r"^(?:[-*+]|\d+(?:\.\d+)*[.)])\s+", "", line, count=1)
        line = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"\1", line)   # bold, not "2 ** 10"
        label = _LABEL.match(line)
        if label:                      # "Fix: ..." is advice, "Where: ..." a pointer
            if label.group(1).lower() in _NOT_CLAIMS:
                continue
            line = line[label.end():]
        for part in re.split(r"(?<=[.!?])\s+(?=[A-Z`])", line):
            part = part.strip()
            if len(part) > 30:
                out.append(part)
    return out


_OURS = re.compile(r"\bcommit [0-9a-f]{7}\b|\b(?:ticket|operator|this repo|the gate|"
                   r"the harvest|the runner|contest|worktree|scorecard)\b", re.I)
_CODEISH = re.compile(r"[(]|::|_\w|\w_|\.\w+\b")


def is_internal(sentence: str, symbols: set[str]) -> bool:
    """True when the sentence is about this repo, not about the world."""
    if (_TICKET.search(sentence) or _PATHLIKE.search(_SLASH_WORDS.sub(" ", sentence))
            or _OURS.search(sentence)):
        return True
    for tok in _BACKTICK.findall(sentence):
        if tok in symbols or _CODEISH.search(tok):
            return True
    # a repo name written bare; only snake_case ones, a plain word ("render")
    # is too often an English word as well
    return any(tok in symbols for tok in _SNAKE.findall(sentence))


def pick_claims(text: str, root: Path, limit: int) -> tuple[list[str], int]:
    """Public-looking sentences, hint-bearing first, deduped, capped."""
    symbols = _repo_symbols(root)
    seen: set[str] = set()
    external: list[str] = []
    for s in split_sentences(text):
        key = normalize(s)
        if key in seen:
            continue
        seen.add(key)
        if _HINTS.search(s) and not is_internal(s, symbols):
            external.append(s)
    external.sort(key=lambda s: -len(_HINTS.findall(s)))
    return external[:limit], len(seen)


def normalize(claim: str) -> str:
    return re.sub(r"\s+", " ", claim.strip().lower())


class Cache:
    """claim+endpoint -> answer, on disk; the guard against paying twice."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        # valid JSON that is not an object ([] or null) is an empty cache too;
        # the next put rewrites the file as an object
        self.data: dict = data if isinstance(data, dict) else {}

    @staticmethod
    def key(endpoint: str, claim: str) -> str:
        return endpoint + ":" + hashlib.sha256(normalize(claim).encode()).hexdigest()

    def get(self, endpoint: str, claim: str):
        return self.data.get(self.key(endpoint, claim))

    def put(self, endpoint: str, claim: str, value) -> None:
        self.data[self.key(endpoint, claim)] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)           # atomic: a crash never half-writes it


def api_key() -> str:
    key = os.environ.get(ENV_KEY, "").strip()
    if key:
        return key
    if not sys.stdin.isatty():
        sys.exit(f"{ENV_KEY} is not set and there is no console to ask on")
    return getpass.getpass("Lenz API key (not saved): ").strip()


def call(method: str, path: str, key: str, body: dict | None = None,
         timeout: int = 120) -> dict:
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json", "User-Agent": "jan-auto-agent"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"Lenz {method} {path}: HTTP {exc.code} {exc.read()[:300]!r}")


def split_sections(text: str) -> list[str]:
    """Report -> sections at ##/### headings; a section keeps its own heading,
    so /extract gets the context ("Bug 1: ...") the bare sentences lacked."""
    parts = [p.strip() for p in re.split(r"(?m)^(?=#{2,3} )", text) if len(p.strip()) > 120]
    if len(parts) >= 2:
        return parts
    # no headings (a plain-text review): pack paragraphs into ~1800-char chunks
    chunks: list[str] = []
    cur = ""
    for para in re.split(r"\n\s*\n", text):
        if cur and len(cur) + len(para) > 1800:
            chunks.append(cur.strip())
            cur = ""
        cur += para + "\n\n"
    chunks.append(cur.strip())
    return [c for c in chunks if len(c) > 120]


def extract_claims(section: str, key: str, cache: Cache) -> list[str]:
    """Free (1000/day): a section -> self-contained claims.  Cached anyway."""
    hit = cache.get("extract", section)
    if hit is None:
        resp = call("POST", "/extract", key, {"text": section})
        found = [resp.get("claim")] + list(resp.get("identified_claims") or [])
        hit = [c if isinstance(c, str) else (c or {}).get("claim") for c in found]
        hit = [c for c in hit if c]
        cache.put("extract", section, hit)
    return hit


def pick_extracted(claims: list[str], limit: int,
                   symbols: set[str] | None = None) -> list[str]:
    """Public-looking extracted claims, hint-bearing first, deduped, capped.
    A claim about this repo (is_internal, as in pick_claims) is never sent."""
    symbols = symbols or set()
    seen: set[str] = set()
    keep: list[str] = []
    for c in claims:
        if normalize(c) in seen or not _HINTS.search(c) or is_internal(c, symbols):
            continue
        seen.add(normalize(c))
        keep.append(c)
    keep.sort(key=lambda c: -len(_HINTS.findall(c)))
    return keep[:limit]


def assess(claims: list[str], key: str, cache: Cache) -> list[dict]:
    """One row per claim, in order; cached rows are not sent again."""
    rows: dict[int, dict] = {}
    todo: list[int] = []
    for i, c in enumerate(claims):
        hit = cache.get("assess", c)
        if hit:
            rows[i] = {**hit, "cached": True}
        else:
            todo.append(i)
    for start in range(0, len(todo), MAX_BATCH):
        chunk = todo[start:start + MAX_BATCH]
        resp = call("POST", "/assess", key, {"claims": [claims[i] for i in chunk]})
        for i, row in zip(chunk, resp.get("claims", [])):
            rows[i] = {**row, "cached": False}
            if row.get("verdict") != "Error":
                cache.put("assess", claims[i], row)
    return [rows.get(i, {"claim": c, "verdict": "Error", "confidence": None})
            for i, c in enumerate(claims)]


# Doubt ranking for the one /verify: Mixed and low-confidence rows first.
_DOUBT = {"Mixed": 3, "Mostly False": 2, "Mostly True": 2, "False": 1, "True": 0}
_CONF = {"low": 2, "medium": 1, "high": 0, None: 0}


def most_doubtful(rows: list[dict]) -> int | None:
    scored = [(_DOUBT.get(r.get("verdict"), 0) + _CONF.get(r.get("confidence"), 0), i)
              for i, r in enumerate(rows) if r.get("verdict") != "Error"]
    scored = [s for s in scored if s[0] >= 2]
    return max(scored)[1] if scored else None


def verify(claim: str, key: str, cache: Cache, wait: int = 240) -> dict:
    hit = cache.get("verify", claim)
    if hit:
        return {**hit, "cached": True}
    task = call("POST", "/verify", key, {"text": claim})
    task_id = task.get("task_id")
    deadline = time.time() + wait
    while task_id and time.time() < deadline:
        time.sleep(10)
        st = call("GET", f"/verify/status/{task_id}", key)
        if st.get("status") in ("completed", "complete", "done", "ready", "failed"):
            if st.get("status") != "failed":
                cache.put("verify", claim, st)
            return {**st, "cached": False}
    return {"status": "timeout", "task_id": task_id, "cached": False}


def render(report: dict) -> str:
    lines = [f"Lenz check of {report['source']}: {report['sentences']} extracted claims, "
             f"{report['internal']} skipped, "
             f"{len(report['rows'])} sent"]
    for r in report["rows"]:
        tag = "cache" if r.get("cached") else "paid"
        lines.append(f"  [{r.get('verdict')}/{r.get('confidence')}] ({tag}) {r.get('claim')}")
        if r.get("rationale"):
            lines.append(f"      why: {r['rationale'][:300]}")
    v = report.get("verify")
    if v:
        lines.append(f"\n/verify on: {v.get('claim_text')}")
        lines.append("  " + json.dumps(v.get("result"), ensure_ascii=False)[:1500])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("report", type=Path)
    ap.add_argument("--max", type=int, default=DEFAULT_MAX, help="claims sent (1 credit each)")
    ap.add_argument("--verify", action="store_true", help="escalate the most doubtful row to /verify (10 credits)")
    ap.add_argument("--json", type=Path, help="write the machine-readable result here")
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--repo-root", type=Path, default=Path.cwd())
    ap.add_argument("--dry-run", action="store_true", help="show the picked claims, no network")
    args = ap.parse_args(argv)

    text = args.report.read_text(encoding="utf-8")
    cache = Cache(args.cache)
    sections = split_sections(text)
    if args.dry_run:                       # no network: show the sections only
        print(f"{len(sections)} sections would go to the free /extract")
        for sec in sections:
            print("  -", sec.splitlines()[0][:100])
        return 0

    # The key is asked for only when something must go out: a fully cached
    # run (every section, every claim) and an empty send need none.
    keys: list[str] = []

    def need_key() -> str:
        if not keys:
            keys.append(api_key())
        return keys[0]

    extracted: list[str] = []
    for sec in sections:
        key = need_key() if cache.get("extract", sec) is None else ""
        extracted += extract_claims(sec, key, cache)
    claims = pick_extracted(extracted, args.max, _repo_symbols(args.repo_root))
    fresh = sum(1 for c in claims if not cache.get("assess", c))
    print(f"{len(sections)} sections -> {len(extracted)} claims (free) -> "
          f"{len(claims)} public-looking; {fresh} will cost a credit each, "
          f"{len(claims) - fresh} are cached")
    if not claims:
        if args.json:                      # the next stage never reads a stale file
            args.json.write_text("[]\n", encoding="utf-8")
        return 0
    rows = assess(claims, need_key() if fresh else "", cache)
    report = {"source": str(args.report), "sentences": len(extracted),
              "internal": len(extracted) - len(claims), "rows": rows, "verify": None}
    worst = most_doubtful(rows) if args.verify else None
    if worst is not None:
        claim = rows[worst].get("claim") or claims[worst]
        key = "" if cache.get("verify", claim) else need_key()
        report["verify"] = {"claim_text": claim, "result": verify(claim, key, cache)}
    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
