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
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable, Optional

# Grounding check: the cited file must be the target file, since the model judges only the file it is shown (AUTO-H2-6).

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
            source = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        # An extensionless file is not treated as Python; its block is read with the language-neutral search (FIX-2 #13).
        file_ext = Path(tf).suffix

        block = ""
        for symbol in ([cited_symbol] if cited_symbol else []) + _instruction_symbol_candidates(instruction):
            # Symbols to look up are the cited symbol and the ones named in the instruction (same audit fix as above).
            try:
                block = extract_block(source, symbol, file_ext)
            except Exception:
                continue
            if block:
                break
        if not block:
            lines = source.splitlines()
            block = "\n".join(lines[:40])
            # With no symbol resolved, skip the module docstring and start at the first def or class, so Stage B sees code.
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


# Instruction names a file the citation never mentions: the note adds it, so a legal but off-topic citation is caught (AUTO-H2-6b).
_FILENAME_TOKEN_RE = re.compile(r"\b[\w][\w./-]*\.[A-Za-z][A-Za-z0-9]{0,8}\b")


def instruction_file_context(
    instruction: str,
    cited_file: str,
    base_dir: Path,
    *,
    already_noted: bool = False,
    max_chars: int = 800,
) -> Optional[str]:
    """Return context from a real file the instruction names, when the
    citation points somewhere else and never names the cited file at all.

    Deliberately narrower than ``target_file_context`` so the two stay
    additive instead of overlapping:

      * ``already_noted`` — whether ``target_file_context`` already
        returned a note for this candidate. When it did, this is a no-op;
        the two checks are meant to cover disjoint cases, not double up
        on the same one.
      * Only fires when ``cited_file``'s own name is nowhere in the
        instruction. A candidate that legitimately names a second file
        for context while correctly citing the first (e.g. "match the
        pattern main.py already uses in utils.py" while citing main.py)
        is left alone — the cited file IS under discussion, so this isn't
        the AUTO-H2-6b pattern.
      * Only matches tokens that resolve to a real file under *base_dir*.
        Prose like "e.g." or a version number never resolves to an actual
        path, so it is silently ignored rather than misfiring.

    Same non-rejecting, evidence-injection pattern as the rest of this
    module: this never overrides Stage A, it only gives Stage B a chance
    to judge the file the instruction actually meant.
    """
    if already_noted or not instruction or not cited_file:
        return None

    cited_name = Path(cited_file).name
    if re.search(r"\b" + re.escape(cited_name) + r"\b", instruction):
        return None  # the cited file IS named in the instruction — not this bug

    seen: set[str] = set()
    for m in _FILENAME_TOKEN_RE.finditer(instruction):
        token = m.group(0)
        if token in seen or token == cited_file:
            continue
        seen.add(token)

        path = base_dir / token
        # A filename from the instruction is contained in base_dir before it is read (BUGFIX).
        try:
            path.resolve().relative_to(base_dir.resolve())
        except ValueError:
            continue
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue

        block = "\n".join(source.splitlines()[:40])
        snippet = block if len(block) <= max_chars else block[:max_chars] + " …(truncated)"
        return (
            f"NOTE (automated): the cited evidence above is from `{cited_file}`, but "
            f"this candidate's own instruction is entirely about `{token}` — "
            f"`{cited_file}` is never named in the instruction at all. This is the "
            f"AUTO-H2-6b mismatch pattern (cited_location and target_files agree "
            f"with each other and are both wrong), which the target-file check "
            f"above does not catch. Content of `{token}` so the claim can be "
            f"judged against the file it actually describes:\n"
            f"```\n{snippet}\n```\n"
            f"If the claimed problem is not actually present in `{token}` either, "
            f"reject."
        )
    return None


# ── AUTO-H2-1: config.getX(..., fallback=...) already-safe check ──────────────

_SECTION_ERROR_CUES = re.compile(
    r"NoSectionError|NoOptionError|missing \[?\w+\]? section|no \[?\w+\]? section|"
    r"missing section|section (does not|doesn't) exist",
    re.IGNORECASE,
)

# Matches a config.getint call with a key, so the fallback can be checked in that same call.
_CONFIG_GET_RE = re.compile(
    r"""\.get(?:int|boolean|float)?\(\s*
        ["'](?P<section>[^"']+)["']\s*,\s*
        ["'](?P<key>[^"']+)["']
        (?P<rest>[^)]*)\)
    """,
    re.VERBOSE,
)

# One-hop wrapper resolution: a self._read_int call hides its fallback, so the check follows the wrapper (AUTO-H2-7).
_WRAPPER_CALL_RE = re.compile(r"\bself\.([a-z_][a-z0-9_]*)\(")


# A wrapper definition is found in the class the call site lives in, via the AST, so the same helper name in another class is not used (FIX-2 #12).

# The first def in an extracted block is the method the call site is in.
_BLOCK_DEF_RE = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)


def _direct_method(cls: ast.ClassDef, name: str):
    """``cls``'s own ``name`` method, ignoring anything it inherits."""
    for stmt in cls.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name:
            return stmt
    return None


def _resolve_method(cls: ast.ClassDef, name: str, by_name: dict, _seen=None):
    """``cls.name``, following base classes that are defined in this file.

    A wrapper inherited from a base class in the same module is genuinely
    reachable from the call site, so it stays resolvable -- the whole-file
    search used to find it by accident, and narrowing to the exact class
    without this would lose a case that used to work. Bases from other
    modules are unknown here and simply end the walk; ``_seen`` guards
    against a cyclic or self-referential base list in malformed source.
    """
    if _seen is None:
        _seen = set()
    if id(cls) in _seen:
        return None
    _seen.add(id(cls))

    found = _direct_method(cls, name)
    if found is not None:
        return found

    for base in cls.bases:
        base_name = None
        if isinstance(base, ast.Name):
            base_name = base.id
        elif isinstance(base, ast.Attribute):
            base_name = base.attr
        parent = by_name.get(base_name)
        if parent is not None:
            found = _resolve_method(parent, name, by_name, _seen)
            if found is not None:
                return found
    return None


def _calling_class(full_source: str, code_block: str):
    """(class owning *code_block*'s method, {class name: node}), or (None, None).

    Returns ``None`` -- meaning "emit no note" -- whenever the scope cannot
    be pinned down: unparseable source, a block with no ``def`` line, a
    method name no class defines, or several classes defining it that
    position cannot separate. Failing closed is the right direction for this
    check: a missing counter-fact costs one extra LLM judgement, while a
    counter-fact quoted from the wrong class actively argues a real finding
    away.
    """
    try:
        tree = ast.parse(full_source)
    except (SyntaxError, ValueError):
        return None, None

    block_def = _BLOCK_DEF_RE.search(code_block)
    if block_def is None:
        return None, None
    caller = block_def.group(1)

    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if not classes:
        return None, None
    by_name = {c.name: c for c in classes}

    owners = [c for c in classes if _direct_method(c, caller) is not None]
    if len(owners) > 1:
        # The same method name can repeat in several classes, so the file position tells them apart.
        idx = full_source.find(code_block)
        if idx >= 0:
            line = full_source.count("\n", 0, idx) + 1
            owners = [
                c for c in owners
                if c.lineno <= line <= (getattr(c, "end_lineno", None) or c.lineno)
            ]
    if len(owners) != 1:
        return None, None
    return owners[0], by_name


def _method_body_source(full_source: str, fn) -> str:
    """Source of *fn*'s body, excluding its signature line(s)."""
    if not fn.body:
        return ""
    lines = full_source.splitlines(keepends=True)
    start = fn.body[0].lineno - 1
    end = getattr(fn, "end_lineno", None) or fn.body[-1].lineno
    return "".join(lines[start:end])


def _wrapper_fallback_note(instruction: str, code_block: str, full_source: str) -> Optional[str]:
    if not full_source:
        return None
    scope, by_name = _calling_class(full_source, code_block)
    if scope is None:
        return None
    for m in _WRAPPER_CALL_RE.finditer(code_block):
        name = m.group(1)
        wrapper = _resolve_method(scope, name, by_name)
        if wrapper is None:
            continue
        body = _method_body_source(full_source, wrapper)
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
        # A partial excerpt may not parse, so names are found with a light textual scan instead.
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


# The definition search follows a fixed preference (cited file, its package, then the tracked repo in sorted order), so runs agree (FL-9).

def _resolve_cited(base_dir: Path, cited_file: Optional[str]) -> Optional[Path]:
    """*cited_file* as an absolute path inside *base_dir*, else ``None``.

    ``None`` covers an empty citation, a missing file's directory, a
    traversal out of *base_dir* and a non-path *base_dir* alike. Every
    caller treats ``None`` as "no citation to anchor on" and falls back to
    the unanchored walk rather than raising.
    """
    if not cited_file:
        return None
    try:
        root_abs = Path(base_dir).resolve()
        cited_abs = (Path(base_dir) / cited_file).resolve()
        cited_abs.relative_to(root_abs)
    except (ValueError, OSError, RuntimeError, TypeError):
        return None
    return cited_abs


def _same_file_as_citation(found_path: Path, cited_file: str, base_dir: Path) -> bool:
    """True when *found_path* IS the cited file.

    Comparing identities, not basenames. The old ``found_path.name ==
    Path(cited_file).name`` test also swallowed a genuinely different module
    that merely shares a filename (``tools/x.py`` vs
    ``tests/fixtures/copy/tools/x.py``), which is the copy confusion this
    search exists to steer around.
    """
    cited_abs = _resolve_cited(base_dir, cited_file)
    if cited_abs is None:
        return False
    try:
        return found_path.resolve() == cited_abs
    except (OSError, RuntimeError):
        return False


def _display_path(found_path: Path, base_dir: Path) -> Path:
    """*found_path* relative to *base_dir* for the prompt.

    ``_find_def_in_repo`` only returns paths under *base_dir*, so this is
    normally a plain relative_to. The resolve covers *base_dir* reached
    through a symlink, and the last resort keeps a stray absolute path out
    of Stage B's prompt rather than printing one.
    """
    try:
        return found_path.relative_to(base_dir)
    except (ValueError, TypeError):
        pass
    try:
        return found_path.resolve().relative_to(Path(base_dir).resolve())
    except (ValueError, OSError, RuntimeError, TypeError):
        return Path(found_path.name)    # AUDIT3-C15: the annotated type, not a str


#: Tracked-file lookups are not cached across calls, since the tree changes between Gate 1 passes; the list is read once per call.
_TRACKED_FILES_TIMEOUT_SECONDS = 10


#: Directories never searched: the run's own state and vendored output, tracked or not (FIX-2 #11).
_EXCLUDED_SEGMENTS: tuple[str, ...] = (".agent", "node_modules")


def _path_parts(p: object) -> list[str]:
    """*p*'s path components, for a ``Path`` or a bare string stand-in.

    ``tests_bugfix/test_bugfix_fix2_11_find_def_budget.py`` drives the
    search with a stub whose ``rglob`` yields relative ``Path`` objects, so
    a ``"/.agent/"``-style substring test would silently stop matching a
    path that merely *starts* with an excluded name. Comparing whole
    segments handles absolute, relative and plain-string inputs alike.
    """
    try:
        return list(p.parts)  # type: ignore[union-attr,attr-defined]
    except AttributeError:
        return str(p).replace(os.sep, "/").split("/")


def _has_segment(p: object, segments: tuple[str, ...]) -> bool:
    """True when any component of *p* is one of *segments*."""
    return any(part in segments for part in _path_parts(p))


def _is_excluded_path(p: Path) -> bool:
    """True when *p* is inside one of ``_EXCLUDED_SEGMENTS``."""
    return _has_segment(p, _EXCLUDED_SEGMENTS)


def _tracked_python_files(base_dir: Path) -> Optional[list[Path]]:
    """Paths of the ``*.py`` files git tracks under *base_dir*, or ``None``.

    ``None`` means "git could not tell us" — not inside a work tree, no
    ``git`` on PATH, an unreadable index, a hang, anything at all. Every
    failure mode degrades to the plain walk, which is the pre-FL-9
    behaviour; nothing here may raise. An empty list is a real answer and
    is returned as such — see the note before the sort. The list comes back
    sorted, so the caller's order is the same on every run and every
    machine.
    """
    if not isinstance(base_dir, (Path, str, os.PathLike)):
        return None

    def _git(args: list[str], *, timeout: int = 5) -> Optional[bytes]:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=str(base_dir),
                capture_output=True, timeout=timeout,
            )
        except Exception:  # noqa: BLE001 — no git, no cwd, timeout: all "no git"
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    top = _git(["rev-parse", "--show-toplevel"])
    if top is None or not top.strip():
        return None
    # The git listing is relative to the work tree root, so it is requested that way and joined correctly.
    raw = _git(
        ["ls-files", "-z", "--full-name", "*.py"],
        timeout=_TRACKED_FILES_TIMEOUT_SECONDS,
    )
    if raw is None:
        return None

    try:
        base_abs = Path(base_dir).resolve()
        root_abs = Path(top.strip().decode("utf-8", "replace")).resolve()
    except (OSError, ValueError):
        return None

    # Both ends are resolved before the containment test, so symlinks and out-of-tree paths are handled.
    out: list[Path] = []
    for rel in raw.split(b"\x00"):
        rel_s = rel.decode("utf-8", "replace").strip()
        if not rel_s:
            continue
        candidate = root_abs / rel_s
        # A plain relative_to settles containment; resolve() is only needed when base_dir is reached through a symlink.
        try:
            candidate.relative_to(base_abs)
        except ValueError:
            try:
                candidate.resolve().relative_to(base_abs)
            except (ValueError, OSError, RuntimeError):
                continue
        if candidate.suffix == ".py" and candidate.exists():
            out.append(candidate)

    # An empty tracked list is a real answer under the work tree; below it, git knows nothing, so the directory is walked.
    if not out and base_abs != root_abs:
        return None
    out.sort(key=lambda p: str(p).replace(os.sep, "/"))
    return out


def _ordered_python_files(base_dir: Path, tracked: Optional[list[Path]]) -> list[Path]:
    """Every candidate file, preferred first.

    1. git-tracked files under *base_dir*, sorted — the real repo, nothing
       else. Nested work trees, ignored build output and untracked copies
       fall out, and max_files then counts only files that are really
       part of the tree being judged.
    2. the old ``rglob`` walk, sorted, with the FIX-2 #11 exclusions. Used
       when there is no git at all, and never raises.
    """
    if tracked is not None:
        return tracked
    try:
        found = [
            p for p in base_dir.rglob("*.py")
            if p.suffix == ".py" and not _has_segment(p, _EXCLUDED_SEGMENTS)
        ]
    except OSError:
        return []
    return sorted(found, key=lambda p: str(p).replace(os.sep, "/"))


def _scan_for_def(
    paths: "Iterable[Path]",
    pattern: "re.Pattern[str]",
    budget: list[int],
    seen: Optional[set[Path]] = None,
) -> Optional[tuple[Path, str]]:
    """Read *paths* in order and return the first containing *pattern*.

    ``budget`` is a one-element list of remaining max_files, shared across
    the cited file, its package and the repo walk, so the cap still bounds
    the total work instead of applying per stage. ``seen`` holds the paths
    already read earlier in the same search — the cited file's own package
    is scanned shallowly and then appears again in the repo walk, and
    re-reading it would spend budget on work already done.

    Excluded trees, vanished files and already-read files are skipped
    BEFORE the decrement. FIX-2 #11 pinned this ordering: with the
    exclusion below the increment every file the walk was about to throw
    away still consumed max_files budget — and .agent/ holds the run's own
    state while node_modules/ holds thousands of vendored files, so
    whenever either came first the search hit the cap having read nothing
    but files it was discarding anyway and reported "not found" for a
    definition sitting right there. Never read, never counted.
    """
    for p in paths:
        if seen is not None and p in seen:
            continue
        if _is_excluded_path(p) or not p.is_file():
            continue
        budget[0] -= 1
        if budget[0] < 0:
            return None
        if seen is not None:
            seen.add(p)
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            return p, text
    return None


#: Default for tracked files: read the index; a caller looking up several names reads it once and passes it in.
_READ_TRACKED = object()


def _find_def_in_repo(
    name: str,
    base_dir: Path,
    max_files: int = 4000,
    cited_file: Optional[str] = None,
    *,
    tracked: "Optional[list[Path]] | object" = _READ_TRACKED,
) -> Optional[tuple[Path, str]]:
    """Best-effort repo-wide search for a top-level ``def name(`` and return
    (file, source). Deliberately shallow — this is context for an LLM
    prompt, not a resolver that needs to be exact; a wrong-but-plausible
    match is caught by the LLM having full instruction context, an
    exception here must never break Stage B.

    FL-9: the search is ordered, not first-come. When *cited_file* names a
    real Python file under *base_dir* that start is authoritative — the
    citation resolved there in Stage A, so a same-named copy anywhere else
    cannot displace it. Next comes that file's own package: its directory,
    then each parent directory up to *base_dir*, shallowest first. Only
    then is the rest of the tree walked. Each directory is scanned for the
    ``def`` without descending into its subpackages, so this stays a
    constant-cost local search no matter how deep the citation sits.
    """
    pattern = re.compile(rf"^\s*def {re.escape(name)}\s*\(", re.MULTILINE)

    # Depth 0 = one named file, depth 1 = one directory scanned shallowly.
    queue: list[tuple[Path, int]] = []
    cited_abs = _resolve_cited(base_dir, cited_file)
    if cited_abs is not None and cited_abs.suffix == ".py":
        # base_dir was resolved already, so it cannot fail a second time here.
        root_abs = Path(base_dir).resolve()
        queue.append((cited_abs, 0))
        current = cited_abs.parent
        while current != root_abs and current != current.parent:
            queue.append((current, 1))
            current = current.parent
        if current == root_abs:
            queue.append((root_abs, 1))

    budget = [max_files]
    seen: set[Path] = set()
    try:
        if tracked is _READ_TRACKED:
            tracked = _tracked_python_files(base_dir)
        for path, depth in queue:
            if depth == 0:
                candidates = [path] if path.is_file() else []
            elif tracked is not None:
                # Package candidates come from the index too, so an untracked scratch copy cannot outrank the tracked module.
                candidates = [c for c in tracked if c.parent == path]
            else:
                candidates = sorted(
                    (c for c in path.iterdir() if c.is_file() and c.suffix == ".py"),
                    key=lambda p: p.name,
                )
            hit = _scan_for_def(candidates, pattern, budget, seen)
            if hit is not None:
                return hit
        return _scan_for_def(_ordered_python_files(base_dir, tracked), pattern, budget, seen)
    # Any failure while scanning is non-fatal: Stage B loses one optional note, not the run.
    except (OSError, TypeError, AttributeError, ValueError, RuntimeError):
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

    tracked = _READ_TRACKED  # read on the first name that needs it, then shared
    for name in _referenced_call_names(code_block):
        if len(name) < 3 or name.startswith("__"):
            continue
        if tracked is _READ_TRACKED:
            tracked = _tracked_python_files(base_dir)
        found = _find_def_in_repo(name, base_dir, cited_file=cited_file, tracked=tracked)
        if found is None:
            continue
        found_path, found_source = found
        if _same_file_as_citation(found_path, cited_file, base_dir):
            continue  # same file as the citation — Stage B already sees this
        from tools.block_extractor import extract_block
        body = extract_block(found_source, name, ".py")
        if not body:
            continue
        rel = _display_path(found_path, base_dir)
        snippet = body if len(body) <= max_chars else body[:max_chars] + " …(truncated)"
        return (
            f"Downstream context (automated, one call-hop from the code above): "
            f"the claim references `{name}(...)`, defined in {rel}:\n```\n{snippet}\n```"
        )
    return None


# ── GATE1-CTX-1: collect-model contract note ────────────────────────────────

def collect_contract_note(collect_bridge, symbol: "str | None") -> Optional[str]:
    """GATE1-CTX-1: when `collect_bridge` (a
    `tools.auto.collect_bridge.CollectBridge`, or `None`) knows a static
    `collect`-derived contract for the cited symbol — e.g. a seeded
    "fail-open by design" contract — surface it as evidence.

    Purely additive and read-only, same non-rejecting pattern as every
    other note in this module (`# Every piece here is evidence for the LLM
    to weigh, never a decision by itself` — see gate1_filter.py's
    `_build_grounding_notes` docstring). Returns `None` when there's no
    bridge, no symbol, or nothing to say.
    """
    if collect_bridge is None or not symbol:
        return None
    try:
        contracts = collect_bridge.contracts_for_symbol(symbol)
    except Exception:  # noqa: BLE001 — best-effort context, never fatal
        return None
    if not contracts:
        return None
    lines = [
        f"Static analysis contract(s) already on record for `{symbol}` "
        f"(from `collect`, independent of the code excerpt above — "
        f"treat as strong evidence, not proof, since contracts can be "
        f"stale or scoped differently than the claim):"
    ]
    for c in sorted(contracts, key=lambda c: c.name):
        lines.append(f"  - {c.name}: {c.description}")
    return "\n".join(lines)


# ── GATE1-CTX-2: existing-test-coverage note ────────────────────────────────

def existing_test_coverage_note(collect_bridge, cited_file: str) -> Optional[str]:
    """GATE1-CTX-2: when `collect_bridge` knows which test file(s) already
    import the cited module (`CollectModel.test_map`, module-level
    granularity — COLLECT's own coverage pass), surface that list so a
    "no test exists for X" claim can be weighed against what's already
    there, even when the citation itself is the SOURCE file (not the test
    file), which today's citation-only code_block would otherwise never
    reveal.

    Returns `None` when there's no bridge, no cited file, or the module
    has zero covering tests on record (nothing extra to say — a genuine
    "no test exists" claim then has no counter-evidence, exactly as
    today).
    """
    if collect_bridge is None or not cited_file:
        return None
    try:
        tests = collect_bridge.tests_covering(cited_file)
    except Exception:  # noqa: BLE001
        return None
    if not tests:
        return None
    listing = ", ".join(f"`{t}`" for t in sorted(tests))
    return (
        f"Existing test coverage on record (from `collect`) for `{cited_file}`: "
        f"{listing}. If the claim is about MISSING tests, check whether one of "
        f"these files already covers the specific behavior described before "
        f"confirming — module-level coverage existing doesn't by itself prove "
        f"the exact claim is covered, but it's a strong hint to look closely."
    )


# ── GATE1-CTX-3: truncation-safety note ─────────────────────────────────────

# Same truncation marker as gate1_filter; kept local so this module does not import gate1_filter.
_TRUNCATION_MARKER = "... [truncated"


def truncation_safety_note(code_block: str) -> Optional[str]:
    """GATE1-CTX-3: when `code_block` was clipped by gate1_filter.py's own
    `_truncate()` (visible in-band as a `"... [truncated — N more chars]"`
    marker at the tail), name that fact explicitly as background — a
    truncated block can hide error handling that exists just past the cut,
    so "I didn't see it" is weaker evidence of absence than for an
    untruncated block. This is deliberately NEUTRAL, not a push toward
    rejecting: most truncated blocks are still perfectly judgeable from
    what's shown (a large legitimate function truncated at a generous
    budget is the common case, not the exception), and steering the model
    to reject merely because a block was truncated would trade false
    positives for false negatives on exactly the well-grounded, legitimate
    tasks this module exists to protect — see `_build_grounding_notes`'s
    own "evidence to weigh, never a decision by itself" policy.

    Returns `None` when the block wasn't truncated (the common case).
    """
    if _TRUNCATION_MARKER not in code_block:
        return None
    return (
        "Background: the code block above was truncated to fit the context "
        "budget, so it may not show the full function/class. This alone is "
        "not a reason to reject or confirm — judge from what IS shown as "
        "usual — but if the claim specifically hinges on code you cannot "
        "see (e.g. whether a later line in the same block handles the "
        "error), weigh that uncertainty into your reason rather than "
        "assuming either way."
    )


# Intentional-design cue: a comment saying a shape is deliberate is flagged for Stage B; negated phrases are not (AUTO-H3-1).
_INTENTIONAL_DESIGN_CUES = re.compile(
    r"\b(deliberate(?:ly)?|intentional(?:ly)?|on purpose|noqa|hardening|"
    r"negative case|toy module|not meant to run as real code|control case)\b",
    re.IGNORECASE,
)

_NEGATION_RE = re.compile(
    r"\b(not|isn't|isnt|wasn't|wasnt|no longer|never|un)\W{0,3}$",
    re.IGNORECASE,
)

_NEGATION_WINDOW_CHARS = 20


def intentional_design_note(code_block: str) -> Optional[str]:
    """AUTO-H3-1. Return a counter-note when *code_block* itself (comments
    or docstring inside the cited symbol, not just the module docstring)
    contains language asserting its current shape is deliberate.

    Distinct from the module-docstring note in ``_build_grounding_notes``:
    that one only fires for fixture files whose docstring lives at the top
    of the module. This one catches the same intent when it's stated
    inline, right next to the code being judged.

    Skips matches where the cue is itself negated ("not intentional",
    "unintentional side effect") — that phrasing disclaims design intent
    rather than asserting it, and is common in comments explaining a KNOWN
    bug, which is exactly the case this note must not suppress.
    """
    for m in _INTENTIONAL_DESIGN_CUES.finditer(code_block):
        window_start = max(0, m.start() - _NEGATION_WINDOW_CHARS)
        preceding = code_block[window_start:m.start()]
        # "un" attaches directly to the word, so it is checked as a tight prefix (e.g. unintentional).
        word = m.group(0)
        if word.lower().startswith("intentional") and code_block[
            max(0, m.start() - 2):m.start()
        ].lower() == "un":
            continue
        if _NEGATION_RE.search(preceding):
            continue
        return (
            "NOTE (automated): the code above contains its own comment/docstring "
            f"language ({word!r}) suggesting the current behavior is "
            "deliberate, not an oversight. If the claimed problem is exactly the "
            "behavior that comment defends, treat that as strong evidence to "
            "reject — read the full sentence around it before confirming."
        )
    return None


# Test-helper detection (Python only): a private helper in a test file is not flagged for try/except, since its failures should stay loud (AUTO-H3-2).
_TEST_PATH_RE = re.compile(r"(^|/)tests?/")
_PYTHON_FILE_RE = re.compile(r"\.pyi?$")


def test_helper_note(cited_file: str, cited_symbol: "str | None") -> Optional[str]:
    """AUTO-H3-2. Return a counter-note when the citation is a private
    helper function living inside a Python test file, rather than a
    ``def test_*`` case itself or production code.

    Restricted to ``.py``/``.pyi`` files — see module comment above for
    why this convention doesn't generalise to other languages.
    """
    if not cited_symbol or not cited_file:
        return None
    if not _PYTHON_FILE_RE.search(cited_file):
        return None
    if not _TEST_PATH_RE.search(cited_file):
        return None
    leaf = cited_symbol.rsplit(".", 1)[-1]
    if leaf.startswith("test_") or not leaf.startswith("_"):
        # A test case or a public helper is outside this heuristic, so nothing is guessed.
        return None
    return (
        "NOTE (automated): this is a private helper function inside a Python "
        f"test file (`{cited_file}`), not production code under test. Test "
        "helpers conventionally fail loudly — an unhandled exception here "
        "points precisely at the setup step that broke. Wrapping it in "
        "try/except or adding input validation usually makes test failures "
        "harder to diagnose, not easier, even though it reads like a "
        "generic improvement. Only confirm if the instruction shows the "
        "current loud failure is actually misleading (e.g. it masks the "
        "real cause), not just that a failure is theoretically possible."
    )
