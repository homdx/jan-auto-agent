#!/usr/bin/env python3
"""Quick Python model test: one task through `kilo run`, 15 checks on the answer.

    python3 scripts/py_model_test.py bynara/ling-3.0-flash-fin-free bynara/ling-3.0-flash-sante-free
    python3 scripts/py_model_test.py --kilo /path/to/kilo --timeout 300 kenary/hy3:free

Reads no keys: the request goes through kilo with kilo's own config.
The model's code runs in a separate process with a timeout, in a temp dir,
with no stdin. When there is no answer, the reason is printed: timeout, kilo's
exit code and the tail of its stderr (401, 404, "Database is busy",
"Failed to execute statement", "Failed query:", ...).

Every `kilo run` writes the user's own Kilo store, so a check next to a live
round (`kilo serve`) can be what makes the round's writes fail: `-j` above four
is capped to four with a warning, and `--force` keeps the count.

Exit: 0 — every model gave code and got a score; 1 — at least one has no
score (no answer, no code, code crashed).

Direct mode — bypass kilo, hit any OpenAI-compatible endpoint:

    python3 scripts/py_model_test.py --base-url https://api.zyloai.net/v1 --api-key zk_... deepseek-v4

First pass — find a provider's free models (sends nothing to any model):

    python3 scripts/py_model_test.py --find-free vercel_8080 openrouter2 kilo

    # single direct provider via --base-url/--api-key
    python3 scripts/py_model_test.py --find-free --base-url https://api.zyloai.net/v1 --api-key zk_... zyloai

    # multiple direct providers in one run via --provider NAME URL KEY (repeatable)
    python3 scripts/py_model_test.py --find-free \\
        --provider teamorouter https://api.teamorouter.com/v1 sk-teamo-... \\
        --provider orca https://api.orcarouter.ai/v1 sk-orca-... \\
        --provider pollinations https://gen.pollinations.ai sk_LL...

Prints the text models with tools and price 0, marks which ones kilo already
has under that provider, and the model list for the second pass (a normal run).
Prices come from the provider's public API, no key needed: a provider named
vercel* — ai-gateway.vercel.sh, every endpoint of every model; openrouter* —
openrouter.ai/api/v1/models. A provider named zyloai* (or any name when
--base-url is given) — hits that base URL's /models endpoint directly (needs
--api-key). Anything else — `kilo models <p> --verbose`, where 0 can also
mean an unknown price: such models are printed as "maybe paid", as are models
that have a paid endpoint next to the free one.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

PROMPT = (
    "Write a Python 3 function `merge_intervals(intervals: list[tuple[int,int]]) -> "
    "list[tuple[int,int]]` that merges overlapping or touching closed intervals "
    "(e.g. (1,3),(3,5) -> (1,5)), ignores intervals where start>end by swapping them, "
    "returns sorted result, and does not mutate input. Also `parse_duration(s: str) -> int` "
    'returning seconds for strings like "1h30m", "45s", "2h", "1h2m3s" (units h, m, s, '
    "each at most once, in that order); raise ValueError "
    'on empty or invalid input like "1x", "h" or "1h1h". Output ONLY one ```python code block, '
    "no explanation, do not create files."
)

# Runs in a separate process: argv[1] is the file with the model's code.
CHECKER = r'''
import sys
ns = {}
exec(open(sys.argv[1]).read(), ns)
mi, pd = ns["merge_intervals"], ns["parse_duration"]
ok = tot = 0
def t(name, f):
    global ok, tot
    tot += 1
    try:
        r = bool(f())
    except Exception as e:
        r = False
        name += f" ({type(e).__name__}: {e})"
    ok += r
    if not r:
        print("  FAIL", name)
inp = [(3, 5), (1, 3)]
t("merge touching", lambda: mi(inp) == [(1, 5)])
t("input not mutated", lambda: inp == [(3, 5), (1, 3)])
t("swap reversed", lambda: mi([(5, 1), (6, 8)]) == [(1, 5), (6, 8)])
t("empty list", lambda: mi([]) == [])
t("nested", lambda: mi([(1, 10), (2, 3)]) == [(1, 10)])
t("disjoint", lambda: mi([(1, 2), (4, 5)]) == [(1, 2), (4, 5)])
t("1h30m", lambda: pd("1h30m") == 5400)
t("45s", lambda: pd("45s") == 45)
t("2h", lambda: pd("2h") == 7200)
t("1h2m3s", lambda: pd("1h2m3s") == 3723)
for bad in ["", "1x", "h", "abc", "1h1h"]:
    def f(b=bad):
        try:
            pd(b)
            return False
        except ValueError:
            return True
    t(f"ValueError on {bad!r}", f)
print(f"SCORE {ok}/{tot}")
'''

# ```python, ```py, ```python3 or a bare ```; \r\n too.
FENCE_RE = re.compile(r"```[ \t]*(?:python3?|py)?[ \t]*\r?\n(.*?)```", re.S | re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def find_kilo(explicit: str | None) -> str:
    if explicit:
        return explicit
    found = shutil.which("kilo")
    if found:
        return found
    hits = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo")))
    if hits:
        return hits[-1]
    sys.exit("kilo not found: pass --kilo /path/to/kilo")


#: KC-62: the parallelism this check is capped to next to a live round. Twelve
#: `kilo run`s against the store a round is writing is what emptied round 107.
JOBS_NEXT_TO_A_ROUND = 4

#: The box the process scan runs on. A test points it at a fake `/proc`.
_PROC_ROOT = "/proc"

#: KC-62: the line the cap prints, in the box's own language.
LIVE_ROUND_NOTE = (
    "живой раунд (kilo serve pid {pid}): -j снижен до {jobs}, --force чтобы оставить"
)


def _proc_cmdline(proc_root: str, pid: str) -> list:
    """The argv of one `/proc/<pid>`, `[]` when it has none or cannot be read.

    A kernel thread has no cmdline at all, and so does a process that exited
    between the scan and the read: both are an empty argv, not an error.
    """
    try:
        with open(f"{proc_root}/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _proc_uid(proc_root: str, pid: str):
    """The `Uid:` of `/proc/<pid>/status`, `None` when it cannot be read."""
    try:
        with open(f"{proc_root}/{pid}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    field = line.split(None, 1)[1].split()
                    return int(field[0]) if field and field[0].isdigit() else None
    except (OSError, ValueError):
        return None
    return None


def _kilo_procs(proc_root: str = "/proc") -> list:
    """`[(pid, argv)]` of this user's `kilo` processes, `[]` when there are none.

    KC-62: a process counts when `argv[0]`'s basename is `kilo` and its `Uid:`
    is ours — another user's Kilo writes another store. `[]` when there is
    nothing to count, or when `/proc` is not readable at all (not Linux, no
    perms): an unreadable tree is an empty one, never an error.
    """
    try:
        entries = os.listdir(proc_root)
        ours = os.getuid()
    except (OSError, AttributeError):
        return []
    procs = []
    for entry in entries:
        if not entry.isdigit():
            continue
        cmdline = _proc_cmdline(proc_root, entry)
        if not cmdline or os.path.basename(cmdline[0]) != "kilo":
            continue
        uid = _proc_uid(proc_root, entry)
        if uid is not None and uid != ours:
            continue
        procs.append((int(entry), cmdline))
    return procs


def live_kilo_serve(proc_root: str = "/proc") -> int | None:
    """The pid of a live `kilo serve` of this user, or `None`.

    KC-62: a model check that outruns a round makes the round's own store
    refuse writes. `serve` among a Kilo process's args is the round's server.
    `None` when nothing is running, or when `/proc` is not readable at all.
    """
    for pid, cmdline in _kilo_procs(proc_root):
        if "serve" in cmdline:
            return pid
    return None


def store_note(proc_root: str = "/proc") -> str | None:
    """KC-62: the one line about the store, or `None` when there is nothing to say.

    Every `kilo run` — and `--find-free`'s own second pass — writes this user's
    own Kilo store, so a check next to a live round can be what makes the
    round's writes fail. The count is the Kilo processes of this user and the
    pid is the `kilo serve` when there is one; nothing is counted and nothing
    is printed when there is none.
    """
    procs = _kilo_procs(proc_root)
    if not procs:
        return None
    if len(procs) == 1:
        note = "kilo: 1 Kilo process of this user writes the same store"
    else:
        note = f"kilo: {len(procs)} Kilo processes of this user write the same store"
    serve = live_kilo_serve(proc_root)
    if serve is not None:
        note += f" (kilo serve pid {serve})"
    return note


def cap_jobs(args, proc_root: str = "/proc") -> None:
    """KC-62: hold `-j` to four next to a live round, unless `--force`.

    Warns and caps when this user already has a `kilo serve` alive and the
    caller asked for more than `JOBS_NEXT_TO_A_ROUND` models in parallel;
    `--force` leaves the count alone. *args* is changed in place and nothing is
    returned, so `main` keeps one parse and one place where the number is set.
    """
    if args.force or args.parallel <= JOBS_NEXT_TO_A_ROUND:
        return
    pid = live_kilo_serve(proc_root)
    if pid is None:
        return
    print(LIVE_ROUND_NOTE.format(pid=pid, jobs=JOBS_NEXT_TO_A_ROUND), file=sys.stderr)
    args.parallel = JOBS_NEXT_TO_A_ROUND


def run(cmd: list, cwd: str, timeout: int) -> tuple[str, str, int | None]:
    """stdout, stderr, exit code (None = timeout). On timeout the whole process
    group is killed: `kilo run` starts its own server, which must not be left behind."""
    p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return out, err, p.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        return out, err, None


def pick_code(raw: str) -> str:
    """The block with both functions, else the first block, else empty."""
    blocks = FENCE_RE.findall(raw)
    for b in blocks:
        if "def merge_intervals" in b and "def parse_duration" in b:
            return b
    return blocks[0] if blocks else ""


def tail(text: str, n: int = 3) -> str:
    lines = [l.strip() for l in ANSI_RE.sub("", text).splitlines() if l.strip()]
    return " | ".join(lines[-n:])[:300]


# --- first pass: --find-free --------------------------------------------

FREE = "free"
MAYBE = "maybe paid"
VERCEL_API = "https://ai-gateway.vercel.sh/v1/models"
OPENROUTER_API = "https://openrouter.ai/api/v1/models"
# in the model name: ":free", "-free", "/free" — the provider itself calls it free
FREE_NAME_RE = re.compile(r"[:/-]free\b", re.I)


def get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "py_model_test"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def is_zero(v) -> bool:
    """Price 0. A nested structure (video tiers etc.) is not zero."""
    if v is None or v == "":
        return True
    if isinstance(v, (list, dict)):
        return not v
    try:
        return float(v) == 0
    except (TypeError, ValueError):
        return False


def paid_fields(pricing: dict) -> dict:
    return {k: v for k, v in (pricing or {}).items()
            if k not in ("discount", "varies_by_provider") and not is_zero(v)}


def kilo_models(kilo: str, provider: str, verbose: bool) -> dict:
    """{model id without provider: metadata or {}} from `kilo models <p>`."""
    cmd = [kilo, "models", provider, "--pure"] + (["--verbose"] if verbose else [])
    out, err, rc = run(cmd, os.getcwd(), 120)
    if rc != 0:
        print(f"  kilo models {provider}: exit {rc} {tail(err)}", file=sys.stderr)
        return {}
    out = ANSI_RE.sub("", out)
    found = {}
    if verbose:
        for m in re.finditer(r"^(\S+?)/(\S+)\n(\{.*?\n\})", out, re.M | re.S):
            try:
                found[m.group(2)] = json.loads(m.group(3))
            except ValueError:
                pass
    else:
        for line in out.splitlines():
            if line.startswith(provider + "/"):
                found[line[len(provider) + 1:].strip()] = {}
    return found


def free_from_vercel() -> list:
    """(id, status, context, note). Every endpoint of every text model."""
    models = [m["id"] for m in get_json(VERCEL_API)["data"] if m.get("type") == "language"]

    def endpoints(mid):
        try:
            url = f"{VERCEL_API}/{urllib.parse.quote(mid)}/endpoints"
            return mid, get_json(url)["data"].get("endpoints") or [], None
        except Exception as e:  # noqa: BLE001 — one model does not fail the search
            return mid, [], str(e)[:80]

    rows, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(8) as ex:
        for mid, eps, err in ex.map(endpoints, models):
            if err:
                errors.append(f"{mid}: {err}")
                continue
            free = [e for e in eps if e.get("pricing") and not paid_fields(e["pricing"])
                    and "tools" in (e.get("supported_parameters") or [])]
            if not free:
                continue
            paid = [e["provider_name"] for e in eps if e not in free
                    and paid_fields(e.get("pricing"))]
            ctx = max(e.get("context_length") or 0 for e in free)
            if paid:
                rows.append((mid, MAYBE, ctx, "has a paid endpoint: " + ", ".join(paid)))
            else:
                rows.append((mid, FREE, ctx, ""))
    for e in errors:
        print(f"  vercel: could not read {e}", file=sys.stderr)
    print(f"  vercel: checked {len(models)} text models, {len(errors)} errors")
    return rows


def free_from_openrouter() -> list:
    rows = []
    data = get_json(OPENROUTER_API)["data"]
    for m in data:
        arch = m.get("architecture") or {}
        if "text" not in (arch.get("output_modalities") or ["text"]):
            continue
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        if paid_fields(m.get("pricing")):
            continue
        ok = bool(m.get("pricing")) and (FREE_NAME_RE.search(m["id"]) or m["id"].endswith(":free"))
        rows.append((m["id"], FREE if ok else MAYBE, m.get("context_length") or 0,
                     "" if ok else "price 0 but no :free in the name (a router?)"))
    print(f"  openrouter: checked {len(data)} models")
    return rows


def free_from_direct_api(base_url: str, api_key: str) -> list:
    """(id, status, context, note) from an OpenAI-compatible /models endpoint.

    Handles several response shapes:
      - zyloai: {"text": [{id, pricing, capabilities, context_window, min_plan}]}
      - standard: {"data": [{id, ...}]}
      - bare list: [{id, ...}] or ["model-id", ...]
    When capabilities are listed and neither 'tool-use' nor 'tools' appears, the
    model is skipped. When capabilities are absent, the model is included (unknown).
    Models with non-zero pricing are excluded; no pricing info → MAYBE.
    """
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "py_model_test"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if isinstance(data, list):
        models = data
    else:
        models = data.get("text") or data.get("data") or data.get("models") or []
    rows = []
    for m in models:
        if isinstance(m, str):
            m = {"id": m}
        mid = m.get("id") or m.get("name") or ""
        if not mid:
            continue
        caps = m.get("capabilities") or []
        # If capabilities are present and neither tool-use nor tools is listed, skip
        if caps and "tool-use" not in caps and "tools" not in caps:
            continue
        pricing = m.get("pricing") or {}
        if paid_fields(pricing):
            continue
        ctx = m.get("context_window") or m.get("context_length") or 0
        plan = (m.get("min_plan") or "").upper()
        if not pricing:
            status, note = MAYBE, "no pricing info"
        elif plan and plan not in ("BASIC", "FREE", ""):
            status, note = MAYBE, f"min_plan={plan}"
        else:
            status, note = FREE, ""
        rows.append((mid, status, ctx, note))
    print(f"  direct api: checked {len(models)} models")
    return rows


def free_from_kilo(meta: dict) -> list:
    rows = []
    for mid, d in meta.items():
        cost = d.get("cost") or {}
        caps = d.get("capabilities") or {}
        if not caps.get("toolcall"):
            continue
        if not ((caps.get("input") or {}).get("text") and (caps.get("output") or {}).get("text")):
            continue
        if not (is_zero(cost.get("input")) and is_zero(cost.get("output"))
                and all(is_zero(v) for v in (cost.get("cache") or {}).values())):
            continue
        named = bool(FREE_NAME_RE.search(mid))
        rows.append((mid, FREE if named else MAYBE, (d.get("limit") or {}).get("context") or 0,
                     "per kilo" if named else "kilo also shows 0 for an unknown price"))
    return rows


def find_free(kilo: str, provider_specs: list) -> int:
    """provider_specs: list of (name, url_or_None, key_or_None).
    url/key are set for direct-API providers; None for vercel/openrouter/kilo."""
    for provider, purl, pkey in provider_specs:
        print(f"== {provider}", flush=True)
        low = provider.lower()
        direct = bool(purl and pkey)
        try:
            if direct or low.startswith("zyloai"):
                url = purl or f"https://api.{low}.net/v1"
                if not pkey:
                    print("  --api-key required for direct API provider")
                    continue
                rows, source = free_from_direct_api(url, pkey), f"direct api {url}"
            elif low.startswith("vercel"):
                rows, source = free_from_vercel(), "api vercel"
            elif low.startswith("openrouter"):
                rows, source = free_from_openrouter(), "api openrouter"
            else:
                rows, source = free_from_kilo(kilo_models(kilo, provider, True)), "kilo models"
        except Exception as e:  # noqa: BLE001
            print(f"  could not get the list: {e}")
            continue
        rows.sort(key=lambda r: (r[1] != FREE, r[0]))
        print(f"  price source: {source}; found {len(rows)}\n")
        if not rows:
            print()
            continue
        w = max([len(r[0]) for r in rows] + [6]) + 2
        if direct or low.startswith("zyloai"):
            # no kilo cross-check in direct mode — just list models + second-pass command
            print("model".ljust(w), "status".ljust(12), " context", " note")
            for mid, status, ctx, note in rows:
                print(mid.ljust(w), status.ljust(12), f"{ctx // 1000:>7}k", f"  {note}")
            free_ids = [r[0] for r in rows if r[1] == FREE]
            if free_ids:
                cmd = (f"python3 scripts/py_model_test.py"
                       f" --base-url {purl} --api-key {pkey}"
                       f" " + " ".join(free_ids))
                print(f"\n  second pass (free only):\n  {cmd}")
        else:
            in_kilo = kilo_models(kilo, provider, False)
            print("model".ljust(w), "status".ljust(12), " context", " in kilo", " note")
            for mid, status, ctx, note in rows:
                mark = "yes" if mid in in_kilo else "no"
                print(mid.ljust(w), status.ljust(12), f"{ctx // 1000:>7}k", f" {mark:<7}", note)
            ready = [f"{provider}/{r[0]}" for r in rows if r[0] in in_kilo]
            missing = [r[0] for r in rows if r[0] not in in_kilo]
            if ready:
                print("\n  second pass:\n  python3 scripts/py_model_test.py " + " ".join(ready))
            if missing:
                print(f"\n  not in kilo under {provider} (add to kilo.jsonc to test them): "
                      + ", ".join(missing))
        print()
    return 0


def _score_raw(raw: str, checker: str, mdir: str) -> tuple[str, str]:
    """Run the checker on extracted code. Returns (score_str, why_fail)."""
    code = pick_code(raw)
    if not code:
        why = "empty answer" if not raw.strip() else "no ```python block"
        return "-", why
    src = os.path.join(mdir, "answer.py")
    with open(src, "w", encoding="utf-8") as f:
        f.write(code)
    cout, cerr, crc = run([sys.executable, checker, src], mdir, 30)
    check = cout + cerr
    score_m = re.search(r"SCORE (\d+/\d+)", check)
    if crc is None:
        return "crash", "code hung"
    if score_m:
        detail = check.replace(score_m.group(0), "").rstrip()
        return score_m.group(1), detail
    return "crash", f"code crashed: {tail(check, 4)}"


def ask_direct(base_url: str, api_key: str, model: str, prompt: str, timeout: int) -> str:
    """Send one chat completion request directly to an OpenAI-compatible API.
    Returns the assistant's text content, or raises on HTTP error."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2048,
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"] or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("models", nargs="*",
                    help="provider/model as in kilo; with --find-free, provider names")
    ap.add_argument("--kilo", help="path to the kilo binary")
    ap.add_argument("--timeout", type=int, default=240, help="seconds for the model to answer")
    ap.add_argument("--keep", action="store_true",
                    help="keep answers and stderr in ./py_model_test_out/")
    ap.add_argument("--find-free", action="store_true",
                    help="first pass: find the providers' free models, no test")
    ap.add_argument("--base-url", metavar="URL",
                    help="direct mode: OpenAI-compatible base URL (e.g. https://api.zyloai.net/v1)")
    ap.add_argument("--api-key", metavar="KEY",
                    help="direct mode: API key for --base-url")
    ap.add_argument("--provider", nargs=3, action="append",
                    metavar=("NAME", "URL", "KEY"),
                    help="add a direct-API provider for --find-free (repeatable)")
    ap.add_argument("--parallel", "-j", metavar="N", type=int, default=1,
                    help="run N models in parallel (default: 1 = sequential)")
    ap.add_argument("--force", action="store_true",
                    help=f"ignore a live round: do not cap -j to {JOBS_NEXT_TO_A_ROUND} "
                         "next to a running kilo serve")
    args = ap.parse_args()
    cap_jobs(args, _PROC_ROOT)

    if args.find_free:
        # Build unified (name, url, key) spec list
        provider_specs: list[tuple[str, str | None, str | None]] = []
        for name in (args.models or []):
            if args.base_url and args.api_key:
                provider_specs.append((name, args.base_url, args.api_key))
            else:
                provider_specs.append((name, None, None))
        for name, url, key in (args.provider or []):
            provider_specs.append((name, url, key))
        if not provider_specs:
            ap.error("--find-free requires provider names or --provider NAME URL KEY")
        # need kilo only if any non-direct providers
        kilo = find_kilo(args.kilo) if any(u is None for _, u, _ in provider_specs) else ""
        # KC-62: find-free sends nothing to a model, but it is the step right
        # before the parallel second pass: name the store before it is crowded.
        note = store_note(_PROC_ROOT)
        if note:
            print(note, file=sys.stderr)
        return find_free(kilo, provider_specs)

    if not args.models:
        ap.error("at least one model is required")
    direct = bool(args.base_url and not args.find_free)
    if direct and not args.api_key:
        sys.exit("--api-key is required with --base-url")
    # the second pass runs `kilo run` unless it asks the API directly
    kilo = "" if direct else find_kilo(args.kilo)

    import threading
    print_lock = threading.Lock()

    def run_one(model: str, work: str, checker: str) -> tuple[str, str, float, str]:
        """Run one model; return (model, score, took, why). Thread-safe stdout."""
        mdir = tempfile.mkdtemp(prefix="m-", dir=work)
        start = time.time()
        lines: list[str] = []

        if direct:
            try:
                raw = ask_direct(args.base_url, args.api_key, model, PROMPT, args.timeout)
                err_msg = ""
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode(errors="replace")
                raw, err_msg = "", f"HTTP {e.code}: {body}"
            except Exception as e:  # noqa: BLE001
                raw, err_msg = "", str(e)[:200]
            took = time.time() - start
            if args.keep:
                os.makedirs("py_model_test_out", exist_ok=True)
                safe = model.replace("/", "_").replace(":", "_")
                with open(f"py_model_test_out/{safe}.txt", "w", encoding="utf-8") as f:
                    f.write(raw)
            if err_msg:
                lines.append(f"  {err_msg} ({took:.0f}s)")
                with print_lock:
                    print(f"== {model}", flush=True)
                    print("\n".join(lines), flush=True)
                return model, "-", took, err_msg
        else:
            out, err, rc = run([kilo, "run", "--pure", "-m", model, PROMPT], mdir, args.timeout)
            took = time.time() - start
            raw = ANSI_RE.sub("", out)
            if args.keep:
                os.makedirs("py_model_test_out", exist_ok=True)
                safe = model.replace("/", "_").replace(":", "_")
                with open(f"py_model_test_out/{safe}.txt", "w", encoding="utf-8") as f:
                    f.write(raw)
                with open(f"py_model_test_out/{safe}.stderr.txt", "w", encoding="utf-8") as f:
                    f.write(err)
            err_msg = ""
            if not pick_code(raw):
                if rc is None:
                    err_msg = f"timeout {args.timeout}s"
                elif rc != 0:
                    err_msg = f"kilo exit {rc}"
                elif not raw.strip():
                    err_msg = "empty answer"
                else:
                    err_msg = "no ```python block"
            if err_msg:
                lines.append(f"  {err_msg} ({took:.0f}s)")
                if raw.strip():
                    lines.append(f"  stdout: {raw.strip()[:200]!r}")
                if tail(err):
                    lines.append(f"  stderr: {tail(err)}")
                with print_lock:
                    print(f"== {model}", flush=True)
                    print("\n".join(lines), flush=True)
                return model, "-", took, err_msg

        score, detail = _score_raw(raw, checker, mdir)
        if score == "crash" and detail == "code hung":
            lines.append("  FAIL timeout (model code hung)")
        elif score == "-":
            lines.append(f"  {detail} ({took:.0f}s)")
            if raw.strip() and not err_msg:
                lines.append(f"  stdout: {raw.strip()[:200]!r}")
        elif score.startswith("crash"):
            lines.append(f"  {detail}")
        else:
            lines.append(detail or "  all checks passed")
        with print_lock:
            print(f"== {model}", flush=True)
            print("\n".join(lines), flush=True)
        why = detail if score in ("-", "crash") else ""
        return model, score, took, why

    results: list[tuple[str, str, float, str]] = []
    with tempfile.TemporaryDirectory(prefix="pymodeltest-") as work:
        checker = os.path.join(work, "checker.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write(CHECKER)
        workers = max(1, args.parallel)
        if workers == 1:
            for model in args.models:
                results.append(run_one(model, work, checker))
        else:
            with concurrent.futures.ThreadPoolExecutor(workers) as ex:
                futs = {ex.submit(run_one, m, work, checker): m for m in args.models}
                for fut in concurrent.futures.as_completed(futs):
                    results.append(fut.result())
            # restore input order for the summary
            order = {m: i for i, m in enumerate(args.models)}
            results.sort(key=lambda r: order.get(r[0], 0))

    print()
    print("model".ljust(44), "score", " time", " reason")
    for model, score, took, why in results:
        print(model.ljust(44), score.ljust(5), f"{took:4.0f}s", "", why)
    return 0 if all(s not in ("-", "crash") for _, s, _, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
