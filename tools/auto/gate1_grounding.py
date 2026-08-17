"""tools/auto/gate1_grounding.py — AUTO-H2-1 / AUTO-H2-3: deterministic
pre-checks that run between Gate 1's Stage A (existence) and Stage B (LLM
problem-presence), plus one-hop callee context extraction for Stage B.

Why this exists
----------------
Gate 1's Stage B shows the LLM exactly one thing: the extracted body of the
cited symbol (see ``block_extractor.extract_block``), plus the candidate's
own instruction text. Nothing else. That is sufficient for genuinely fuzzy
judgment calls ("is this error handling adequate?") but insufficient for
claims that are actually mechanically checkable from information Stage B
never sees:

  * "config.getX(section, key, ...) can raise NoSectionError" — false
    whenever that exact call already passes ``fallback=``. Checkable with
    zero LLM calls by looking at the code block Stage A already extracted.
  * "calling X() may crash because Y doesn't validate its input" — X's
    definition (and whether *it* already guards against the input) usually
    lives in a different function, sometimes a different file, and
    ``extract_block`` never follows that reference.

This module does not reject candidates on its own. A regex/AST heuristic
can misfire, and auto-rejecting on a heuristic just trades one class of
false positive (a bad task entering the plan) for another (a real bug
silently dropped because a pattern matched by coincidence). Instead, every
check here returns evidence that gets *injected into Stage B's prompt* as
an explicit counter-fact the LLM must address before confirming — the LLM
remains the final arbiter, it just stops being blind to facts that were
sitting in the same code block it was already shown.

Two known false-positive classes found in manual review (documented in
JIRA epic AUTO-H2) motivated this module:

  AUTO-T1/T2/T3 (this session): candidates claimed ``NoSectionError`` risk
  on calls that already had ``fallback=``. Confirmed false by direct
  inspection — this is exactly what ``config_fallback_note`` catches.

  AUTO-T11 (this session): a candidate claimed ``suppress()`` could crash
  because ``model.is_safe()`` doesn't validate its input, when the crash
  is actually prevented two call-frames down, inside
  ``AlreadySafeIndex.query()``. ``callee_context`` exists so Stage B can at
  least see the callee's own body when it's resolvable in the repo, instead
  of reasoning about a function it has never seen.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Optional

# ── AUTO-H2-6: cited-location / target-file mismatch ───────────────────────────
#
# Confirmed in production, not hypothesized: across two real folders (31 and
# 35 candidates), every single candidate whose Location field named a
# different file than its own target_files was rejected — 26 out of 26.
# The mechanism: plan_emitter's "Location:" field is a direct rendering of
# ``cited_location.file`` (see tools/auto/backlog_prioritiser.py:
# ``loc_str = loc.file``), and that is the exact field
# ``Gate1Filter._check_existence`` reads and extracts a code block from —
# never ``target_files``. When they diverge, Stage B is shown the cited
# evidence file (sometimes a cluster-seed config file, a test file, or an
# unrelated module) while being asked whether a problem exists in a
# completely different file — the one that would actually get edited. Every
# rejection reason observed says some version of "the code shown is X, not
# Y, so the claimed problem is not present" — the LLM was entirely correct
# given what it saw; what it saw just wasn't the target.

_INSTRUCTION_SYMBOL_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\.([a-z_][A-Za-z0-9_]*)\b|\b([a-z_][a-z0-9_]{3,})\("
)


def _instruction_symbol_candidates(instruction: str) -> list[str]:
    """Best-effort list of function/method names mentioned in *instruction*,
    most-specific first (dotted Class.method's method part, then bare
    snake_case names used as calls). Used to find a more targeted block in
    the target file than "just the first N lines" when possible."""
    names: list[str] = []
    for m in _INSTRUCTION_SYMBOL_RE.finditer(instruction):
        name = m.group(1) or m.group(2)
        if name and name not in names:
            names.append(name)
    return names


def target_file_context(
    target_files: list[str],
    cited_file: str,
    cited_symbol: "str | None",
    instruction: str,
    base_dir: Path,
    max_chars: int = 800,
) -> Optional[str]:
    """Return context from the candidate's actual target file when it
    differs from the cited location, or ``None`` when they already match
    (the common case — no extra cost for candidates that don't need it) or
    nothing resolves.

    Deliberately does not change Stage A's pass/fail existence logic — a
    mismatch here doesn't mean the citation was invalid (Stage A already
    checked THAT file/symbol resolves), it means Stage B was about to judge
    the wrong file. Same non-rejecting, evidence-injection pattern as the
    rest of this module.
    """
    if not target_files or cited_file in target_files:
        return None

    from tools.block_extractor import extract_block

    for tf in target_files:
        path = base_dir / tf
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_ext = Path(tf).suffix or ".py"

        block = ""
        for symbol in ([cited_symbol] if cited_symbol else []) + _instruction_symbol_candidates(instruction):
            block = extract_block(source, symbol, file_ext)
            if block:
                break
        if not block:
            lines = source.splitlines()
            block = "\n".join(lines[:40])
            # Nothing named in the instruction resolved to a real symbol —
            # a plain head-of-file slice on a well-documented module is
            # often just its module docstring, never reaching actual code.
            # Skip ahead to the first def/class so Stage B sees something
            # concrete to compare the claim against, not just a description
            # of what the module is for.
            if "def " not in block and "class " not in block:
                for i, ln in enumerate(lines):
                    if ln.startswith(("def ", "class ")):
                        block = "\n".join(lines[i:i + 40])
                        break
        if not block:
            continue

        snippet = block if len(block) <= max_chars else block[:max_chars] + " …(truncated)"
        return (
            f"NOTE (automated): the cited evidence above is from `{cited_file}`, but "
            f"this candidate's actual target file — the one that would be edited — "
            f"is `{tf}`, a different file. Content of `{tf}` so the claim can be "
            f"judged against the file that matters, not just the citation:\n"
            f"```\n{snippet}\n```\n"
            f"If the claimed problem is not actually present in `{tf}` either, reject."
        )
    return None


# ── AUTO-H2-1: config.getX(..., fallback=...) already-safe check ──────────────

_SECTION_ERROR_CUES = re.compile(
    r"NoSectionError|NoOptionError|missing \[?\w+\]? section|no \[?\w+\]? section|"
    r"missing section|section (does not|doesn't) exist",
    re.IGNORECASE,
)

# Matches e.g. config.getint("auto", "max_rounds_per_task", fallback=X)
# Captures (section, key, rest-of-call) so we can check "fallback" is in
# *that specific call*, not just somewhere else in the block.
_CONFIG_GET_RE = re.compile(
    r"""\.get(?:int|boolean|float)?\(\s*
        ["'](?P<section>[^"']+)["']\s*,\s*
        ["'](?P<key>[^"']+)["']
        (?P<rest>[^)]*)\)
    """,
    re.VERBOSE,
)

# AUTO-H2-7: one-hop wrapper resolution for config_fallback_note. Confirmed
# in production: AUTO-T3's __init__ calls self._read_int('max_depth', 2) —
# a wrapper method — not config.getint(...) directly. The fallback= lives
# inside _read_int's own body, one call-hop away from what's extracted for
# __init__, so the direct check below never sees it. Same class of gap as
# callee_context's documented one-hop limit, just for this check instead.
_WRAPPER_CALL_RE = re.compile(r"\bself\.([a-z_][a-z0-9_]*)\(")


def _wrapper_fallback_note(instruction: str, code_block: str, full_source: str) -> Optional[str]:
    if not full_source:
        return None
    for m in _WRAPPER_CALL_RE.finditer(code_block):
        name = m.group(1)
        body_match = re.search(
            rf"def\s+{re.escape(name)}\s*\(([^)]*)\)(?:\s*->\s*[^:]+)?:(.*?)(?=\n    def |\nclass |\Z)",
            full_source, re.S,
        )
        if not body_match:
            continue
        body = body_match.group(2)
        get_match = _CONFIG_GET_RE.search(body)
        if get_match and "fallback" in get_match.group("rest"):
            return (
                f"NOTE (automated, not from the candidate's author): the code above "
                f"calls the wrapper method `{name}(...)`, defined elsewhere in this "
                f"same file. That wrapper's own body already calls .get(...) for "
                f"[{get_match.group('section')}] '{get_match.group('key')}' with "
                f"fallback= — per configparser's docs, this never raises "
                f"NoSectionError/NoOptionError regardless of whether the section/"
                f"option exists. Does the claimed crash still hold given this? If "
                f"not, reject."
            )
    return None


def config_fallback_note(instruction: str, code_block: str, full_source: str = "") -> Optional[str]:
    """Return a counter-fact string if *instruction* warns of a config
    section/option crash that *code_block* already guards against with
    ``fallback=``, else ``None``.

    This only fires when the instruction names (or the code block only
    has) one plausibly-relevant call — with several unrelated config calls
    in the same block, silently picking the "wrong" one and injecting a
    misleading counter-fact would be worse than saying nothing, so multiple
    ambiguous matches are skipped rather than guessed at.

    *full_source* (AUTO-H2-7, optional): when the direct check finds
    nothing, and *code_block* calls a same-file wrapper method
    (``self._read_int(...)`` etc.) whose own body does the real
    ``.get(..., fallback=...)`` call, resolve one hop through it. Omit to
    keep the old direct-only behavior.
    """
    if not _SECTION_ERROR_CUES.search(instruction):
        return None

    matches = list(_CONFIG_GET_RE.finditer(code_block))
    if not matches:
        return _wrapper_fallback_note(instruction, code_block, full_source)

    relevant = [
        m for m in matches
        if m.group("section") in instruction or m.group("key") in instruction
    ]
    candidates = relevant or (matches if len(matches) == 1 else [])
    if not candidates:
        return _wrapper_fallback_note(instruction, code_block, full_source)

    for m in candidates:
        if "fallback" in m.group("rest"):
            return (
                f"NOTE (automated, not from the candidate's author): the call to "
                f".get(...) for [{m.group('section')}] '{m.group('key')}' shown "
                f"above already passes fallback=. Per Python's configparser "
                f"documentation, getX(section, key, fallback=X) never raises "
                f"NoSectionError or NoOptionError regardless of whether the "
                f"section/option exists — it returns X. Does the claimed crash "
                f"still hold given this? If not, reject."
            )
    return _wrapper_fallback_note(instruction, code_block, full_source)


# ── AUTO-H2-3: one-hop callee context for call-chain-dependent claims ─────────

_CRASH_CLAIM_CUES = re.compile(
    r"\bmay crash\b|\bcould crash\b|\bcan crash\b|\bmight crash\b|\bwill crash\b",
    re.IGNORECASE,
)


def _referenced_call_names(code_block: str) -> list[str]:
    """Names directly called within *code_block* (best-effort — parse
    failures degrade to an empty list, never raise)."""
    try:
        tree = ast.parse(code_block)
    except SyntaxError:
        # code_block is often a partial excerpt (e.g. one method, dedented
        # oddly, or a line-range slice) — not always independently parseable.
        # Fall back to a light textual scan for `name(` / `obj.name(`.
        return list(dict.fromkeys(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", code_block)))

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                names.append(f.attr)
            elif isinstance(f, ast.Name):
                names.append(f.id)
    return list(dict.fromkeys(names))


def _find_def_in_repo(name: str, base_dir: Path, max_files: int = 4000) -> Optional[tuple[Path, str]]:
    """Best-effort repo-wide search for a top-level ``def name(`` and return
    (file, source). Deliberately shallow — this is context for an LLM
    prompt, not a resolver that needs to be exact; a wrong-but-plausible
    match is caught by the LLM having full instruction context, an
    exception here must never break Stage B."""
    pattern = re.compile(rf"^\s*def {re.escape(name)}\s*\(", re.MULTILINE)
    count = 0
    try:
        for p in base_dir.rglob("*.py"):
            count += 1
            if count > max_files:
                break
            if "/.agent/" in str(p) or "/node_modules/" in str(p):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pattern.search(text):
                return p, text
    except OSError:
        return None
    return None


def callee_context(
    instruction: str,
    code_block: str,
    cited_file: str,
    base_dir: Path,
    max_chars: int = 600,
) -> Optional[str]:
    """When *instruction* claims a downstream crash, try to resolve one
    directly-called name from *code_block* to its own definition elsewhere
    in the repo and return a short excerpt, so Stage B isn't reasoning
    about a function it has never seen.

    Returns ``None`` whenever nothing resolves — this is best-effort
    context enrichment, not a requirement; absence of a result must never
    block or alter Stage B's normal flow.
    """
    if not _CRASH_CLAIM_CUES.search(instruction):
        return None

    for name in _referenced_call_names(code_block):
        if len(name) < 3 or name.startswith("__"):
            continue
        found = _find_def_in_repo(name, base_dir)
        if found is None:
            continue
        found_path, found_source = found
        if str(found_path.name) == Path(cited_file).name:
            continue  # same file as the citation — Stage B already sees this
        from tools.block_extractor import extract_block
        body = extract_block(found_source, name, ".py")
        if not body:
            continue
        rel = found_path
        try:
            rel = found_path.relative_to(base_dir)
        except ValueError:
            pass
        snippet = body if len(body) <= max_chars else body[:max_chars] + " …(truncated)"
        return (
            f"Downstream context (automated, one call-hop from the code above): "
            f"the claim references `{name}(...)`, defined in {rel}:\n```\n{snippet}\n```"
        )
    return None
