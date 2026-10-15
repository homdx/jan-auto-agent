"""tools/auto/utils.py — Shared micro-utilities for the autonomous mode."""
from __future__ import annotations

import configparser
import hashlib
import logging
import os
import re
import shlex
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Longest file-name component: keeps room for suffixes and temp-file names on common filesystems.
_MAX_FILENAME_COMPONENT = 200


def _ts() -> str:
    """Return the current UTC time as an ISO-8601 string (YYYY-MM-DDTHH:MM:SSZ).

    FIX-1 #8: this used to call ``datetime.now()`` — the *local* system
    clock — while ``ticket_store.py``'s schema (lines 16-17) documents
    every ``created_at`` / ``updated_at`` as "ISO-8601 UTC", and every
    caller that supplies its own literal timestamp already uses the
    trailing "Z" (see ``tests/test_auto_d1.py``), matching
    ``tools/collect/manifest.py::_utc_iso_now()``. Recording local time
    under a UTC label is invisible on one machine, but the moment a
    ticket written on a UTC+5 box is read on a UTC box the chronology is
    wrong, and the old string gave no hint it ever was local. UTC plus
    an explicit "Z" suffix matches the rest of the codebase's contract.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_filename_component(value: str) -> str:
    """Return a filesystem-safe version of *value* for use as a file/dir name.

    Strips path separators, leading dots, and anything else that could let a
    crafted id (e.g. ``"../../evil"``) escape the intended directory when
    interpolated into a path. Keeps only alphanumerics, hyphens, and
    underscores. Shared by StateStore (task directories) and TicketStore
    (ticket files) so both apply the identical rule to ids that flow between
    them (a ticket id is typically derived from a task id).
    """
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", value)
    safe = safe.strip("_") or "task"

    # Over-long names are truncated with a digest of the full value, so they stay unique and deterministic (bugfix).
    if len(safe) > _MAX_FILENAME_COMPONENT:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:_MAX_FILENAME_COMPONENT - len(digest) - 1]}_{digest}"
    return safe


# Shared count of finished feedback rounds, so the executor and the controller agree on exhaustion (BLOCKED reset).
_FEEDBACK_ROUND_RE = re.compile(r"feedback_round_(\d+)\.md$")


def highest_completed_round(task_dir: "str | Path") -> int:
    """Return the highest ``feedback_round_<N>.md`` round number under *task_dir*.

    Returns 0 if the directory doesn't exist or contains no feedback files
    (i.e. the task has never failed a round yet).
    """
    best = 0
    for p in Path(task_dir).glob("feedback_round_*.md"):
        # Only real files count as rounds; a directory with a round-like name would park a task forever (B3).
        if not p.is_file():
            continue
        m = _FEEDBACK_ROUND_RE.search(p.name)
        if m:
            best = max(best, int(m.group(1)))
    return best


def file_set_fingerprint(base_dir: "str | Path", files: "list[str]") -> str:
    """Short, content-aware hash of a set of files (relative path + size + mtime).

    Any change to the file SET (added/removed/renamed) or to a file's
    CONTENT (which changes its size and/or mtime) yields a different
    fingerprint. Used anywhere a cache needs to answer "have these files
    actually changed?" — a plain list-of-paths hash answers a different,
    weaker question (did the *set* of paths change) and silently goes stale
    the moment a file already in the set is edited in place.

    Missing/unreadable files hash as a sentinel rather than raising, so a
    file that was deleted (or briefly inaccessible) still changes the
    fingerprint instead of crashing the caller.
    """
    base = Path(base_dir)
    h = hashlib.sha1()
    for rel in sorted(files):
        h.update(rel.encode("utf-8", "replace"))
        try:
            st = (base / rel).stat()
            h.update(f"|{st.st_size}|{st.st_mtime_ns}|".encode("utf-8"))
        except OSError:
            h.update(b"|?|")
    return h.hexdigest()[:12]


def fsync_directory(directory: "str | Path") -> None:
    """Best-effort ``fsync`` of *directory* itself.

    ``os.replace`` is atomic, but on Linux the rename is only guaranteed
    DURABLE once the containing directory has been fsynced -- fsyncing the
    file alone is not enough, and a crash right after the rename can make the
    "atomically written" file disappear entirely.

    This is a hint, not a guarantee, and it is called only after the write and
    the rename have already succeeded, so a failure here must never turn a
    successful write into a raised error:

    * ``os.O_DIRECTORY`` does not exist as an attribute on Windows at all (not
      merely unsupported at the OS level), so referencing it directly would
      raise ``AttributeError``. ``getattr`` turns that into a clean skip.
    * some filesystems reject fsync on a directory; that ``OSError`` is
      logged at DEBUG and swallowed.
    """
    o_directory = getattr(os, "O_DIRECTORY", None)
    if o_directory is None:
        return
    try:
        dir_fd = os.open(str(directory), o_directory)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        logger.debug(
            "fsync_directory: fsync on %s failed (best-effort durability "
            "step, the write itself already succeeded): %s", directory, exc,
        )


def _read_umask_once() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


#: the umask at import, for a system without /proc: set and put back once, before any thread
_UMASK_AT_IMPORT = _read_umask_once()


def _umask() -> int:
    """The process umask, read without changing it (os.umask is process-wide: a file another thread made
    in between got none); /proc/self/status on Linux, else the value read at import."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
            for line in fh:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return _UMASK_AT_IMPORT


def atomic_write_text(path: "str | Path", content: str, *, exclusive: bool = False,
                      newline: "str | None" = None, encoding: str = "utf-8") -> None:
    """Write *content* to *path* atomically (temp file + ``os.replace``).

    ``os.replace`` is a single filesystem rename, which POSIX and Windows both
    guarantee is atomic within the same directory/filesystem: *path* always
    ends up holding either the old complete content or the new complete
    content, never a partial write. Plain ``Path.write_text`` gives no such
    guarantee — a process killed mid-write (SIGKILL, OOM-kill, power loss)
    can leave a truncated file behind that later fails ``json.loads``.

    Used for state that must survive an interrupted run (plan.json,
    progress.json, tickets) so a mid-write kill can never corrupt it.

    ``exclusive=True`` makes it a create-only write: the temp file is
    hard-linked into place (``os.link`` fails if *path* exists), so of two
    racing writers exactly one wins and the other gets ``FileExistsError``
    instead of silently replacing the winner's file. A filesystem without
    hard links creates *path* with ``O_EXCL`` and copies the content into it (create-only still holds;
    a reader may see the file before it is complete).

    The file keeps the mode it had, and a new one gets what ``open()`` would
    give it (0666 less the umask): mkstemp's temporary file is 0600, and the
    rename made a 0644 IMPROVEMENTS.md and a 0755 script 0600. *newline* is
    ``open()``'s: ``""`` writes *content*'s line ends as they are.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        try:
            os.chmod(tmp_name, (path.stat().st_mode & 0o7777) if path.exists() else 0o666 & ~_umask())
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if exclusive:
            try:
                os.link(tmp_name, path)
            except FileExistsError:
                raise
            except OSError:
                # No hard links: O_EXCL is the atomic "fail if it exists" (check-then-replace would let two writers win).
                import shutil
                out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, os.stat(tmp_name).st_mode & 0o7777)
                with os.fdopen(out, "wb") as dst, open(tmp_name, "rb") as src:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
            os.unlink(tmp_name)
        else:
            os.replace(tmp_name, path)
        fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def in_style_of(dest: Path, content: str) -> str:
    """*content* with *dest*'s line ends (CRLF when most of its lines end so) and its BOM. The model writes
    LF and no BOM, and a one-line edit of a CRLF file was a diff of every line (12 changed of 6)."""
    try:
        raw = dest.read_bytes()
    except OSError:
        return content
    text = content.replace("\r\n", "\n")
    if raw.count(b"\r\n") * 2 > raw.count(b"\n"):
        text = text.replace("\n", "\r\n")
    if raw.startswith(b"\xef\xbb\xbf") and not text.startswith("\ufeff"):
        text = "\ufeff" + text
    return text


def _cfg_mode(
    config: configparser.ConfigParser,
    section: str,
    key: str,
    task_mode: str,
    fallback: Any = None,
) -> Any:
    """Read a mode-specific config key with graceful fallback (AUTO-CR-3).

    Priority (highest -> lowest):

    1. ``{key}_{task_mode}`` in *section*  -- e.g. ``max_tokens_creative``
    2. ``{key}``            in *section*  -- e.g. ``max_tokens``
    3. *fallback*

    Works for any ``task_mode`` value; when ``task_mode`` is ``"code"`` (the
    default) step 1 is effectively skipped because ``max_tokens_code`` is
    unlikely to be set, so it behaves identically to a plain ``config.get``.

    Parameters
    ----------
    config:
        Parsed ``agents.ini``.
    section:
        INI section name (e.g. ``"coder"``).
    key:
        Base key name (e.g. ``"max_tokens"``).
    task_mode:
        Current task mode (e.g. ``"creative"``).
    fallback:
        Value returned when neither key variant is present.

    Returns
    -------
    str | None
        The raw string value, or *fallback* when not found.  Callers are
        responsible for casting to ``int`` / ``float`` as required.
    """
    mode_key = f"{key}_{task_mode}"
    val = config.get(section, mode_key, fallback=None)
    if val is not None and str(val).strip():
        return val
    val = config.get(section, key, fallback=None)
    if val is not None and str(val).strip():
        return val
    return fallback


# ── AUTO-CR-9: language consistency for creative mode ────────────────────────

# ── Input-side token budgeting: chars-per-token varies by script ────────────

# Default characters per token for Latin-script prose; the input budget math uses it.
_CHARS_PER_TOKEN_DEFAULT = 4.0

# Cyrillic is much denser per token, so the ratio is measured for it, or the budget silently overflows the context (chars_per_token).
_CHARS_PER_TOKEN_CYRILLIC = 2.2

# Share of Cyrillic letters above which the Cyrillic ratio is used (matches detect_language's threshold).
_CYRILLIC_RATIO_THRESHOLD = 0.30


def chars_per_token(text: str) -> float:
    """Estimate characters-per-token for *text*, for sizing input budgets.

    Returns ``_CHARS_PER_TOKEN_CYRILLIC`` (2.2) when at least 30% of the
    text's alphabetic characters are Cyrillic, else the Latin/English-safe
    default of 4.0. Empty or non-alphabetic text falls back to the default.

    This is the input-side counterpart to the project's Cyrillic-aware
    output truncation (``max_tokens_creative``) — without it, budgets
    computed as ``tokens * 4`` let ~2x too much Cyrillic text into a prompt,
    silently overflowing the model's context window.
    """
    if not text:
        return _CHARS_PER_TOKEN_DEFAULT
    cyrillic = sum(1 for ch in text if 0x0400 <= ord(ch) <= 0x04FF)
    total_alpha = sum(1 for ch in text if ch.isalpha())
    if total_alpha == 0:
        return _CHARS_PER_TOKEN_DEFAULT
    if (cyrillic / total_alpha) >= _CYRILLIC_RATIO_THRESHOLD:
        return _CHARS_PER_TOKEN_CYRILLIC
    return _CHARS_PER_TOKEN_DEFAULT


def detect_language(text: str) -> "str | None":
    """Best-effort detection of the dominant script/language of *text*.

    Returns a human-readable language NAME suitable for dropping into an LLM
    instruction (e.g. ``"Russian"``, ``"English"``), or ``None`` when the text
    is too short / ambiguous to decide.

    Intentionally lightweight (no external deps): it distinguishes the scripts
    that matter for this project by counting characters. Cyrillic vs Latin is
    the common case (Russian source, English drift); a few other scripts are
    recognised so the instruction is correct if the source is in them.
    """
    if not text:
        return None
    counts = {
        "Russian": 0,    # Cyrillic
        "English": 0,    # Latin
        "Greek": 0,
        "Arabic": 0,
        "Hebrew": 0,
        "Chinese": 0,
        "Japanese": 0,
    }
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF:
            counts["Russian"] += 1
        elif (0x41 <= o <= 0x5A) or (0x61 <= o <= 0x7A):
            counts["English"] += 1
        elif 0x0370 <= o <= 0x03FF:
            counts["Greek"] += 1
        elif 0x0600 <= o <= 0x06FF:
            counts["Arabic"] += 1
        elif 0x0590 <= o <= 0x05FF:
            counts["Hebrew"] += 1
        elif 0x4E00 <= o <= 0x9FFF:
            counts["Chinese"] += 1
        elif (0x3040 <= o <= 0x30FF):
            counts["Japanese"] += 1

    total = sum(counts.values())
    if total < 8:           # too little signal to be sure
        return None
    max_n = max(counts.values())
    if max_n == 0 or (max_n / total) < 0.30:
        return None
    # An exact tie in language counts is ambiguous, so it returns None rather than an arbitrary winner (bug 44).
    tied = [name for name, n in counts.items() if n == max_n]
    if len(tied) > 1:
        return None
    return tied[0]


def resolve_creative_language(
    config: "configparser.ConfigParser | None",
    context_text: str = "",
    task_mode: str = "creative",
) -> "str | None":
    """Resolve the language a creative chapter must be written in.

    Priority:
      1. Explicit ``[coder] creative_language`` (or ``language_creative``) in
         config — lets the author force a language regardless of detection.
      2. Auto-detection from *context_text* (the story so far).
      3. ``None`` — caller omits the language instruction (model decides).
    """
    if config is not None:
        for key in ("creative_language", "language_creative"):
            if config.has_option("coder", key):
                val = (config.get("coder", key) or "").strip()
                if val and val.lower() not in ("auto", "detect", ""):
                    return val
    return detect_language(context_text)


def language_instruction(language: "str | None") -> str:
    """Return a strong, prompt-ready language-lock instruction, or ``""``.

    Used by the creative coder/summary/canon prompts so a small model
    (e.g. llama3.1:8b) does not drift into English when the source is Russian.
    """
    if not language:
        return ""
    return (
        f"LANGUAGE: Write entirely in {language}. The story so far is in "
        f"{language}; you MUST continue in the SAME language. Do not switch to "
        f"another language, do not translate, do not add text in any other "
        f"language. Output {language} only."
    )


# ── AUTO-CR-10: task_mode normalisation (typo-tolerant) ──────────────────────

_KNOWN_TASK_MODES = ("code", "docs", "creative")


def normalize_task_mode(value: "str | None") -> tuple[str, "str | None"]:
    """Normalise a configured ``task_mode`` to a known mode.

    Returns ``(mode, warning)``. ``warning`` is ``None`` when *value* was an
    exact known mode, otherwise a human-readable message describing the
    correction (so the controller can log it loudly instead of silently
    degrading — e.g. ``creativy`` → ``creative``).

    Mapping:
      * exact match (case/space-insensitive) → that mode
      * starts with ``creat`` → ``creative``     (catches ``creativy``, ``creativ``)
      * starts with ``doc``   → ``docs``
      * starts with ``cod``   → ``code``
      * anything else         → ``code`` (with a warning)
    """
    raw = (value or "").strip()
    low = raw.lower()
    if low in _KNOWN_TASK_MODES:
        return low, None
    if not low:
        return "code", None  # empty/missing → default, no warning

    if low.startswith("creat"):
        guess = "creative"
    elif low.startswith("doc"):
        guess = "docs"
    elif low.startswith("cod"):
        guess = "code"
    else:
        return (
            "code",
            f"unknown task_mode {raw!r} — expected one of "
            f"{_KNOWN_TASK_MODES}; falling back to 'code'.",
        )
    return (
        guess,
        f"task_mode {raw!r} is not exact — interpreting as {guess!r}. "
        f"Set [auto] task_mode = {guess} in agents.ini to silence this.",
    )


def human_duration(seconds: float) -> str:
    """Format *seconds* as a compact human-readable duration.

    Examples: 0.4 -> '0.4s', 42 -> '42s', 125 -> '2m 5s', 3725 -> '1h 2m 5s',
    90061 -> '1d 1h 1m 1s'. Negative values get a leading '-'. A value that
    is not a finite number has no duration to spell out, so it is echoed as
    'nan', 'inf' or '-inf' (``int()`` of those raises) — a log line that
    formats a bad clock reading must not itself crash.
    """
    if seconds != seconds or abs(seconds) == float("inf"):
        return str(float(seconds))
    sign = "-" if seconds < 0 else ""
    s = abs(float(seconds))
    if s < 1:
        return f"{sign}{s:.1f}s"
    s = int(s)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= size:
            parts.append(f"{s // size}{unit}")
            s %= size
    if s or not parts:
        parts.append(f"{s}s")
    return sign + " ".join(parts)


# Pytest detection: shared by the executor and the failure detail, matched by command position, never by substring (RUN-3).

PYTEST_TOKENS = ("pytest", "py.test")

# Separators that end one command: a pytest invocation is recognised after any of them (RUN-3).
_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", "{", "!"})

#: A shell variable set for one command: ``PYTHONPATH=. pytest tests``.
ENV_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")

#: Wrapper commands that run the rest of the line; one list for the pytest check and the contest policy.
COMMAND_WRAPPERS = frozenset({"timeout", "nohup", "env", "nice", "time", "exec", "command",
                              "stdbuf", "ionice"})

#: The options of those wrappers that take a separate value.
WRAPPER_VALUE_FLAGS = ("-n", "-s", "-k", "--signal", "--kill-after", "-o", "-e",
                       "-i", "-c", "-u", "--unset")

#: Project runners: ``uv run pytest``, ``poetry run pytest`` — ``run`` is theirs.
_RUNNERS = frozenset({"uv", "poetry", "pipenv", "pdm", "hatch", "rye"})

# Xdist flags in every spelling; anchored, so long options that only begin with -n do not match.
_XDIST_FLAG_RE = re.compile(r"^(?:-n(?:\d+|auto|logical)?|--numprocesses(?:=.*)?|--dist(?:=.*)?)$")


def is_xdist_flag(token: str) -> bool:
    """True when *token* is an xdist worker-count / ``--dist`` flag (RUN-3).

    Answers "does this pytest invocation (or this ``addopts``) ask xdist for
    a worker pool" — in an acceptance check and in a config file alike.
    """
    return bool(_XDIST_FLAG_RE.match(token.strip().strip("\"'")))


def _basename(token: str) -> str:
    return token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _command_word(seg: list[str]) -> int:
    """Index of the real command word of one command, past ``VAR=value``, the
    ``COMMAND_WRAPPERS`` with their options, ``xvfb-run`` and ``uv run``-style runners."""
    i = 0
    while i < len(seg):
        word = seg[i]
        if ENV_ASSIGN_RE.fullmatch(word):
            i += 1
            continue
        name = _basename(word)
        if name in _RUNNERS and i + 1 < len(seg) and seg[i + 1] == "run":
            i += 2
            continue
        if name in COMMAND_WRAPPERS or name == "xvfb-run":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                flag = seg[i]
                i += 1
                if flag in WRAPPER_VALUE_FLAGS and i < len(seg):
                    i += 1
            if name == "timeout" and i < len(seg):
                i += 1                  # the duration
            continue
        break
    return i


def pytest_argv_start(seg: list[str]) -> int | None:
    """Index of the first pytest argument of one command, or ``None`` when the
    command is not a pytest run (206: the contest's suite slot reads the same
    answer, so the two recognisers cannot drift again).

    ``pytest tests`` → 1, ``python3 -u -m pytest tests`` → 4, ``uv run pytest``
    → 3; the index may equal ``len(seg)`` when pytest is given no argument.
    """
    i = _command_word(seg)
    if i >= len(seg):
        return None
    base = _basename(seg[i])
    if base in PYTEST_TOKENS or base in ("pytest.exe", "py.test.exe"):
        return i + 1
    # python -m pytest is recognised across interpreter names and options before -m (RUN-3).
    if base == "py" or base.startswith("python"):
        k = i + 1
        while k < len(seg):
            tok = seg[k]
            if tok == "-m":
                if k + 1 < len(seg) and _basename(seg[k + 1]) in PYTEST_TOKENS:
                    return k + 2
                return None
            if tok in ("-X", "-W"):
                k += 2
            elif tok.startswith("-") and tok != "-c":
                k += 1
            else:
                break
    return None


def _segment_runs_pytest(seg: list[str]) -> bool:
    return pytest_argv_start(seg) is not None


def pytest_segments(parts: list[str]) -> list[list[str]]:
    """*parts* (a ``split_command`` result) cut into its commands at the
    ``_COMMAND_SEPARATORS``: ``cd sub && pytest`` is ``[cd, sub]`` and ``[pytest]``."""
    segs: list[list[str]] = []
    seg: list[str] = []
    for part in [*parts, ";"]:
        if part in _COMMAND_SEPARATORS:
            if seg:
                segs.append(seg)
            seg = []
        else:
            seg.append(part)
    return segs


def _tokens_run_pytest(parts: list[str]) -> bool:
    return any(_segment_runs_pytest(seg) for seg in pytest_segments(parts))


def split_command(cmd: str, posix: bool) -> list[str]:
    """``shlex`` split that also cuts a glued ``&&`` / ``;`` / ``|`` into its own token."""
    lex = shlex.shlex(cmd, posix=posix, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def pytest_piece_spans(command: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every pytest run in *command*, as character offsets.

    A piece is the text between the unquoted ``|``, ``&``, ``;`` and newline of
    a command line, so ``pytest tests | tail -5`` is two pieces and only the
    first is a run; the ``&`` of a redirect (``2>&1``, ``&>log``) is not a cut.
    ``Executor._serialise_pytest`` adds its flag at the end of each span, not at
    the end of the line — the end of the line may be somebody else's argument
    list (``tail -5 -n 0`` prints nothing; ``tee run.log -n 0`` writes two files).
    A piece whose quoting does not parse is not a run.
    """
    cuts: list[tuple[int, int]] = []
    start, quote, i, n = 0, "", 0, len(command)
    while i < n:
        ch = command[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = ""
        elif ch == "\\":
            i += 1
        elif ch in "\"'":
            quote = ch
        elif ch in "|;\n" or (ch == "&" and not _is_redirect_amp(command, i)):
            cuts.append((start, i))
            start = i + 1
        i += 1
    cuts.append((start, n))
    spans = []
    for lo, hi in cuts:
        piece = command[lo:hi]
        if not piece.strip():
            continue
        try:
            parts = split_command(piece, os.name != "nt")
        except ValueError:
            continue
        if parts and _segment_runs_pytest(parts):
            spans.append((lo, lo + len(piece.rstrip())))
    return spans


def _is_redirect_amp(command: str, i: int) -> bool:
    """Whether the ``&`` at *i* belongs to a redirect: ``>&``, ``<&`` or ``&>``."""
    before = command[:i].rstrip()
    return before.endswith((">", "<")) or command[i + 1:i + 2] == ">"


def is_pytest_command(command: str) -> bool:
    """Return True when *command* runs pytest (RUN-3).

    Recognised forms::

        pytest …                                  pytest tests/test_x.py -q
        python -m pytest …                        /venv/bin/python3.11 -m pytest …
        C:\\venv\\Scripts\\python.exe -m pytest …    cd sub && pytest …

    Splitting is tried POSIX-style and Windows-style (POSIX shlex eats the
    backslashes of a Windows path; ``posix=False`` leaves quotes attached to
    a POSIX argument), and the command is a pytest run when either reading
    says so. Un-parseable quoting falls back to a substring test — a false
    negative would cost the coder the tail of the output.
    """
    cmd = (command or "").strip()
    if not cmd:
        return False
    parsed = False
    for posix in (os.name != "nt", os.name == "nt"):
        try:
            parts = split_command(cmd, posix)
        except ValueError:
            continue
        parsed = True
        if parts and _tokens_run_pytest(parts):
            return True
    if not parsed:
        return "pytest" in cmd or "py.test" in cmd
    return False
