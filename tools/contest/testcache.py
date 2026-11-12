"""Ticket 212 — the parsing half of the round's test-run cache.

A contest agent that runs the whole suite spends minutes on it, and models
write the same run many ways (``.smoke_tests/ .regression_tests/ tests_bugfix``,
``tests``, ``cd <worktree> && { time pytest … ; } 2>&1 | tail -30``). The cache
(``testcache_store.py``, another module) answers a repeat on an unchanged tree
from the earlier run; this module is everything that needs no disk and no git:

* :func:`parse_pytest` — every pytest invocation found in a shell command line,
  reduced to a :class:`PytestRun` (normalised roots + the flags that change
  *what runs or what is printed*), so two spellings of one run compare equal;
* :func:`summarise` — the pytest summary line out of a finished call's output;
* :func:`progress_line` — the one line the model reads instead of a re-run;
* :func:`classify_with_llm` — the fallback for a command the parser cannot read
  (``make test``, ``bash run_tests.sh``): one short question to the gate model.

Deterministic and dependency-free (stdlib plus the shared request builder in
``tools.llm_stream``); it imports neither the runner nor the policy. Nothing
here raises: an unreadable command is ``[]``, an unreadable output is ``None``.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

__all__ = [
    "PytestRun",
    "Summary",
    "classify_with_llm",
    "parse_pytest",
    "progress_line",
    "summarise",
]


# ── the run a command stands for ───────────────────────────────────────────


@dataclass(frozen=True)
class PytestRun:
    """One pytest invocation, normalised so equal runs compare equal.

    ``roots`` — what was asked to run (``.smoke_tests``, ``.regression_tests``
    and ``tests`` are three selections (HARDEN-28 B); empty when the command
    names none).
    ``flags`` — only the options that change what runs or what is printed,
    each as one string (``-x``, ``-k=slow``, ``--maxfail=3``, ``-vv``).
    ``cwd`` — the effective working directory a ``cd`` put the run in, as a
    normalised path (``""`` when the command names none, HARDEN-28 C).
    ``env`` — the ``VAR=value`` assignments that reach pytest, each as one
    string, excluding the variables that cannot change a test's outcome
    (HARDEN-29 F). ``frozenset()`` when the command names none.
    Frozen and hashable: it is a dictionary key in the store.
    """

    roots: frozenset = field(default_factory=frozenset)
    flags: frozenset = field(default_factory=frozenset)
    cwd: str = ""
    env: frozenset = field(default_factory=frozenset)


@dataclass
class Summary:
    """The numbers of pytest's final line, plus up to 10 failing test names."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    failed_names: list = field(default_factory=list)
    raw_line: str = ""


# ── tokenizer ──────────────────────────────────────────────────────────────

#: Separators between simple commands. ``( ) { }`` group, ``\n`` is ``;``.
_SEP = "SEP"

#: Group punctuation, kept so ``_parse`` can tell a ``cd`` inside a subshell
#: ``( … )`` from a top-level one — HARDEN-28 C: a ``cd`` the parser cannot
#: follow (one inside a subshell, ``cd -``, ``cd "$X"``) makes the command
#: not cacheable, and an untracked subshell ``cd`` would leak its directory
#: onto later segments.
_OPEN = "\x00OPEN"
_CLOSE = "\x00CLOSE"

#: The tool killed the call; whatever pytest printed before is not an answer.
_KILLED = "terminated command after exceeding timeout"


def _backtick_end(command: str, i: int) -> int:
    """Index of the backtick that closes the one at *i*; ``ValueError`` when none does.

    A backslash escapes the next character (``\\``` stays inside).
    """
    j, n = i + 1, len(command)
    while j < n:
        if command[j] == "\\":
            j += 2
            continue
        if command[j] == "`":
            return j
        j += 1
    raise ValueError("unclosed backtick")


def _dquote_end(command: str, i: int) -> int:
    """Index of the ``"`` that closes the double quote opened at *i*.

    The shell still reads ``$( … )`` and a backtick pair inside double quotes,
    so each is skipped as a unit and a ``"`` inside one does not end the quote.
    ``ValueError`` when the quote, or a substitution inside it, never closes.
    """
    j, n = i + 1, len(command)
    while j < n:
        ch = command[j]
        if ch == "\\":
            j += 2
            continue
        if ch == '"':
            return j
        if ch == "`":
            j = _backtick_end(command, j) + 1
            continue
        if ch == "$" and command[j + 1:j + 2] == "(":
            j = _subst_end(command, j) + 1
            continue
        j += 1
    raise ValueError("unclosed double quote")


def _subst_end(command: str, i: int) -> int:
    """Index of the ``)`` that closes the ``$(`` at *i*; ``ValueError`` when none does.

    Read as the shell reads it: a parenthesis inside quotes, inside a backtick
    pair, after a backslash or in a comment is not a parenthesis of the
    substitution, and a quote inside it must close. (``"$(echo ")"`` is an
    unclosed substitution: the ``)`` belongs to the quoted string.)
    """
    j, n = i + 2, len(command)
    depth = 1
    while j < n:
        ch = command[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "'":
            end = command.find("'", j + 1)
            if end < 0:
                raise ValueError("unclosed single quote")
            j = end + 1
            continue
        if ch == '"':
            j = _dquote_end(command, j) + 1
            continue
        if ch == "`":
            j = _backtick_end(command, j) + 1
            continue
        if ch == "$" and command[j + 1:j + 2] == "(":
            j = _subst_end(command, j) + 1
            continue
        if ch == "#" and (command[j - 1] in " \t\n;&|(" if j > i + 2 else True):
            end = command.find("\n", j)
            if end < 0:
                break               # the comment swallows the closing parenthesis
            j = end + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise ValueError("unclosed command substitution")


def _tokenize(command: str) -> list:
    """*command* as a flat list of words, with ``_SEP`` between simple commands.

    A small hand-written scanner rather than ``shlex`` alone: shlex has no
    notion of ``&&``/``|``/``;``/``(``/``)`` and chokes on an unbalanced quote.
    Quotes (single, double) and backslashes are honoured inside a word;
    ``$( … )`` and backticks are kept inside their word, balanced, and never
    executed; ``2>&1`` and ``&>`` keep their ``&``; ``{`` and ``}`` separate
    only as whole words (shell reserved words). Quotes are stripped from the
    words, so ``"tests"`` and ``tests`` are the same word. A quote, backtick or
    ``$(`` that is never closed is a syntax error in the shell, so it raises
    ``ValueError`` (``parse_pytest`` turns that into "no run": a command the shell
    refuses must not be answered from a cached green summary).
    """
    tokens: list = []
    word: list = []
    in_word = False
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal in_word
        if in_word:
            text = "".join(word)
            if text in ("{", "}"):
                tokens.append(_SEP)
            else:
                tokens.append(text)
        word.clear()
        in_word = False

    def sep() -> None:
        flush()
        if tokens and tokens[-1] != _SEP:
            tokens.append(_SEP)

    while i < n:
        ch = command[i]
        if ch == "\\" and i + 1 < n:
            if command[i + 1] != "\n":
                word.append(command[i + 1])
                in_word = True
            i += 2
            continue
        if ch == "'":
            end = command.find("'", i + 1)
            if end < 0:
                raise ValueError("unclosed single quote")
            word.append(command[i + 1:end])
            in_word = True
            i = end + 1
            continue
        if ch == '"':
            j = _dquote_end(command, i)
            word.append(command[i + 1:j].replace('\\"', '"'))
            in_word = True
            i = j + 1
            continue
        if ch == "`":
            end = _backtick_end(command, i)
            word.append(command[i:end + 1])
            in_word = True
            i = end + 1
            continue
        if ch == "$" and command[i + 1:i + 2] == "(":
            end = _subst_end(command, i)
            word.append(command[i:end + 1])
            in_word = True
            i = end + 1
            continue
        if ch in " \t\r":
            flush()
            i += 1
            continue
        if ch == "#" and not in_word:
            # a comment runs to the end of the line; a quote in it opens nothing (``# don't``)
            end = command.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch in "\n;":
            sep()
            i += 1
            continue
        if ch in "()":
            sep()
            tokens.append(_OPEN if ch == "(" else _CLOSE)
            i += 1
            continue
        if ch == "|":
            sep()
            i += 1
            continue
        if ch == "&":
            prev = word[-1][-1:] if word else ""
            if prev in (">", "<") or command[i + 1:i + 2] == ">" and not in_word:
                word.append(ch)             # 2>&1, >&2, &>file
                in_word = True
            else:
                sep()
            i += 1
            continue
        word.append(ch)
        in_word = True
        i += 1
    flush()
    # A subshell that never closes is a syntax error too (a stray ``)`` is left alone: a ``case`` pattern has one).
    if tokens.count(_OPEN) > tokens.count(_CLOSE):
        raise ValueError("unclosed parenthesis")
    return [t for t in tokens]


def is_well_formed(command: str) -> bool:
    """False when the shell would refuse *command* for an unclosed quote, backtick or ``$(``.

    Such a command ran nothing, so no answer for it may come from the cache or from a classifier.
    """
    try:
        _tokenize(command)
    except ValueError:
        return False
    visible = _shell_syntax_view(command)
    tail = visible.rstrip()
    if re.search(r"(?:&&|\|\||\|)$", tail):
        return False
    if re.search(r"(?:\d*>>?|<<?|&>)$", tail):
        return False
    # A right paren that closes no grouping is invalid, except for the reserved
    # `case pattern)` delimiter. The case bodies are intentionally not parsed.
    case_view = re.sub(r"\bcase\b[\s\S]*?\bin\b[ \t]+[^\s();|&]+\)", "case pattern in x ", visible, count=1)
    depth = 0
    for ch in case_view:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    if depth:
        return False
    return True


def _shell_syntax_view(command: str) -> str:
    """Mask quoted strings, substitutions and comments for trailing-operator checks."""
    out = list(command)
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "\\":
            out[i] = " "
            if i + 1 < n:
                out[i + 1] = " "
            i += 2
            continue
        if ch == "'":
            end = command.find("'", i + 1)
            end = n - 1 if end < 0 else end
            for j in range(i, end + 1):
                out[j] = " "
            i = end + 1
            continue
        if ch == '"':
            end = _dquote_end(command, i)
            for j in range(i, end + 1):
                out[j] = " "
            i = end + 1
            continue
        if ch == "`":
            end = _backtick_end(command, i)
            for j in range(i, end + 1):
                out[j] = " "
            i = end + 1
            continue
        if ch == "$" and command[i + 1:i + 2] == "(":
            end = _subst_end(command, i)
            for j in range(i, end + 1):
                out[j] = "x"
            i = end + 1
            continue
        if ch == "#" and (i == 0 or command[i - 1].isspace() or command[i - 1] in ";&|("):
            end = command.find("\n", i)
            end = n if end < 0 else end
            for j in range(i, end):
                out[j] = " "
            i = end
            continue
        i += 1
    return "".join(out)


def _segments(tokens: list) -> list:
    """The simple commands of *tokens* (lists of words), empties dropped."""
    out: list = []
    current: list = []
    for token in tokens:
        if token == _SEP:
            if current:
                out.append(current)
            current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    return out


# ── one simple command → a PytestRun ───────────────────────────────────────

_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
_PYTHON = re.compile(r"python(\d+(\.\d+)?)?")
_REDIRECT = re.compile(r"^(\d*|&)(>>?|<<?<?)(.*)$", re.S)

#: HARDEN-29 F: variables whose value cannot change a test's outcome, so an
#: assignment to one is not part of the cache key. Every other variable
#: counts.
_IGNORED_ENV_VARS = frozenset({
    "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "TERM", "COLUMNS",
})

#: HARDEN-29 F: single values of a counting variable that a plain run already
#: has. ``PYTHONPATH=.`` puts the checkout itself on the path, and any other
#: value counts.  ``PYTHONPATH=.`` and ``PYTHONPATH=./`` are now counted like
#: any other variable (they are no longer in the ignored list).
_IGNORED_ENV_VALUES = {}

#: ``env`` options that empty the environment pytest gets: such a run is not
#: cached at all, like ``env -u``.
_ENV_CLEARING = frozenset({"-i", "-", "--ignore-environment", "-u", "--unset"})


def _env_counts(var: str, value: str) -> bool:
    """True when ``var=value`` is part of the cache key (HARDEN-29 F)."""
    return var not in _IGNORED_ENV_VARS and value not in _IGNORED_ENV_VALUES.get(var, ())

#: Wrappers that run the rest of their line; the value-taking options of each.
_WRAPPERS = {
    "time": ("-f", "--format", "-o", "--output"),
    "nice": ("-n", "--adjustment"),
    "ionice": ("-c", "-n", "-p", "--class", "--classdata"),
    "nohup": (),
    "env": ("-u", "--unset", "-C", "--chdir", "-S"),
    "exec": (),
    "command": (),
    "stdbuf": ("-i", "-o", "-e"),
    "timeout": ("-s", "--signal", "-k", "--kill-after"),
    "xvfb-run": (),
}

#: AUDIT3-2: options that change which tests run or how they are judged, so two
#: runs that differ only in them must not share a cache entry. A cache miss
#: costs a re-run; a wrong hit serves another configuration's verdict.
#: Long spellings of the short keyed options, folded onto the short form.
_KEYED_LONG_ALIASES = {"--config-file": "-c", "--override-ini": "-o",
                       "--pythonwarnings": "-W"}
#: Long options whose value is part of the key.
_KEYED_LONG_VALUE = frozenset({"--ignore", "--ignore-glob", "--deselect", "--rootdir",
                               "--confcutdir", "--import-mode"})
#: Long switches that are part of the key.
_KEYED_LONG_FLAGS = frozenset({"--runxfail", "--strict-markers", "--strict-config",
                               "--strict", "--pyargs", "--noconftest"})
def _warning_filter_can_fail(value: str) -> bool:
    """A ``-W`` filter that turns warnings into errors (``error``, ``error::X``)."""
    return str(value).strip().lower().startswith("error")


#: ``-p`` values that only change speed or caching, never a verdict.
_NEUTRAL_PLUGIN_SWITCHES = frozenset({"no:cacheprovider", "no:randomly"})

#: pytest options that take a separate value (so the value is not a root).
_VALUE_OPTIONS = frozenset({
    "-k", "-m", "-n", "-p", "-W", "-c", "-o", "-r", "-C",
    "--maxfail", "--timeout", "--color", "--tb", "--durations",
    "--durations-min", "--rootdir", "--ignore", "--ignore-glob", "--deselect",
    "--junitxml", "--junit-xml", "--dist", "--basetemp", "--import-mode",
    "--log-level", "--confcutdir", "--override-ini", "--numprocesses",
    "--max-worker-restart", "--timeout-method", "--cov", "--cov-report",
    "--capture", "--show-capture", "--tx", "--cache-clear-dir",
    "--lfnf", "--last-failed-no-failures", "--pyargs-dir", "--log-file",
    "--config-file", "--pythonwarnings",
    "--log-cli-level", "--assert", "--maxprocesses", "--splits", "--group",
})

#: Short options of that set that glue a value on (``-nauto``, ``-k expr``).
_SHORT_VALUE = frozenset("kmnpWcorC")

#: --opt forms that take their value only with `=` when optional (--durations 10 is two words).

def _normalise_root(token: str) -> str:
    """``./tests/`` → ``tests``; a tier root keeps its own key (HARDEN-28 B);
    else the cleaned path."""
    root = token
    while root.startswith("./"):
        root = root[2:]
    root = root.rstrip("/") or root
    # HARDEN-28 B: `.smoke_tests`, `.regression_tests` and `tests` are three
    # selections — a cached green smoke run must not answer a later regression
    # or full-suite run. `tests/.smoke_tests` is the smoke tier spelled with a
    # `tests/` prefix; `./.smoke_tests/` already collapsed onto `.smoke_tests`
    # above, so one tier shares one key across its spellings.
    if root == "tests/.smoke_tests":
        return ".smoke_tests"
    if root == "tests/.regression_tests":
        return ".regression_tests"
    return root


def _skip_redirect(args: list, i: int) -> int | None:
    """If ``args[i]`` is a shell redirection, the index past it, else ``None``."""
    m = _REDIRECT.match(args[i])
    if not m or not (args[i][:1].isdigit() or args[i][:1] in "<>&"):
        return None
    if m.group(1) not in ("", "&") and not m.group(1).isdigit():
        return None
    target = m.group(3)
    if target == "":                        # `> file`: the file is the next word
        return i + 2
    return i + 1


def _env_word(word: str) -> "tuple[str, str] | None":
    """``(VAR, value)`` for a ``VAR=value`` word, else ``None``."""
    if not _ENV_ASSIGN.fullmatch(word):
        return None
    var, value = word.split("=", 1)
    return var, value


def _env_readable(value: str) -> bool:
    """False for a value the parser cannot read (``$(…)``, a backtick, ``$OTHER``)."""
    return "$" not in value and "`" not in value


def _pytest_run(args: list, cwd: str = "", env: "dict | None" = None) -> PytestRun:
    """Reduce the argv after ``pytest`` to roots and meaningful flags."""
    roots: set = set()
    flags: set = set()
    verbose = quiet = 0
    i = 0
    while i < len(args):
        token = args[i]
        skipped = _skip_redirect(args, i)
        if skipped is not None:
            i = skipped
            continue
        i += 1
        if token == "--":
            roots.update(_normalise_root(t) for t in args[i:] if t)
            break
        if not token.startswith("-") or token == "-":
            if token:
                roots.add(_normalise_root(token))
            continue
        if token.startswith("--"):
            name, eq, value = token.partition("=")
            if name in _VALUE_OPTIONS and not eq and i < len(args):
                nxt = args[i]
                # --durations, --tb and --maxfail take a word; -k and -m accept any word, even a flag-looking one.
                if not nxt.startswith("-") or name in ("-k", "-m"):
                    value = nxt
                    i += 1
            if name == "--co":
                name = "--collect-only"
            if name in ("--maxfail", "--durations", "--tb"):
                flags.add(f"{name}={value}" if value != "" else name)
            elif name in ("--lf", "--last-failed"):
                flags.add("--lf")
            elif name in ("--ff", "--failed-first"):
                flags.add("--ff")
            elif name in ("--collect-only", "--collect-q"):
                flags.add(name)
            elif name == "--verbose":
                verbose += 1
            elif name == "--quiet":
                quiet += 1
            elif name == "--exitfirst":
                flags.add("-x")
            elif name == "--pythonwarnings" and not _warning_filter_can_fail(value):
                pass                          # "ignore"/"default" can never fail a test
            elif name in _KEYED_LONG_ALIASES:
                # AUDIT3-2: a different ini, override or warning filter is a different run.
                flags.add(f"{_KEYED_LONG_ALIASES[name]}={value}")
            elif name in _KEYED_LONG_VALUE:
                flags.add(f"{name}={value}")
            elif name in _KEYED_LONG_FLAGS:
                flags.add(name)
            continue
        # a short-option cluster: -xvq, -n4, -k expr, -qq
        body = token[1:]
        j = 0
        while j < len(body):
            c = body[j]
            j += 1
            if c in _SHORT_VALUE:
                value = body[j:]
                if not value and i < len(args):
                    value = args[i]
                    i += 1
                if c in ("k", "m"):
                    flags.add(f"-{c}={value}")
                elif c in ("c", "o") or (c == "W" and _warning_filter_can_fail(value)) \
                        or (c == "p" and value not in _NEUTRAL_PLUGIN_SWITCHES):
                    # AUDIT3-2: these change what pytest runs or how it judges it.
                    flags.add(f"-{c}={value}")
                break                       # the rest of the cluster is the value
            if c == "x":
                flags.add("-x")
            elif c == "v":
                verbose += 1
            elif c == "q":
                quiet += 1
    if verbose:
        flags.add("-" + "v" * verbose)
    if quiet:
        flags.add("-" + "q" * quiet)
    env_set = frozenset(
        f"{k}={v}" for k, v in sorted((env or {}).items())
    )
    return PytestRun(frozenset(roots), frozenset(flags), cwd, env_set)


def _split_env_and_wrappers(words: list) -> "tuple[int, dict, bool]":
    """The command word of *words*, the env that feeds it, and ``unreadable``.

    HARDEN-29 F: skips the leading ``VAR=v`` prefixes and the wrappers
    (``time``, ``env``, ``nice``, …) exactly as ``_past_wrappers`` did, and
    collects every readable assignment that reaches the command. ``unreadable``
    is ``True`` when an assignment's value cannot be read statically
    (``$(…)``, a backtick, ``$OTHER``) or ``env -u``/``--unset`` removes a
    variable — such a command is not cached at all.
    """
    env: dict = {}
    unreadable = False
    i = 0
    while i < len(words):
        word = words[i]
        parsed = _env_word(word)
        if parsed is not None:
            var, value = parsed
            if not _env_readable(value):
                unreadable = True
            elif _env_counts(var, value):
                env[var] = value
            i += 1
            continue
        name = word.rsplit("/", 1)[-1]
        if name not in _WRAPPERS:
            return i, env, unreadable
        takes = _WRAPPERS[name]
        i += 1
        while i < len(words) and words[i].startswith("-") and words[i] != "--":
            flag = words[i]
            i += 1
            if flag in takes and i < len(words):
                i += 1
        if name == "env" and _ENV_CLEARING.intersection(words[:i]):
            unreadable = True
        if i < len(words) and words[i] == "--":
            i += 1
        if name == "timeout" and i < len(words):
            i += 1                          # the duration
    return i, env, unreadable


def _runs_in_segment(words: list, depth: int, cwd: str,
                     env: "dict | None" = None) -> "list | None":
    """The pytest runs one simple command stands for (usually 0 or 1).

    ``None`` when an environment assignment the parser cannot read reaches the
    command — the whole command is then not cached (HARDEN-29 F).
    """
    start, local_env, unreadable = _split_env_and_wrappers(words)
    if unreadable:
        return None
    if start >= len(words):
        return []
    merged = dict(env or {})
    merged.update(local_env)
    head = words[start].rsplit("/", 1)[-1]
    rest = words[start + 1:]
    if head in ("pytest", "py.test"):
        return [_pytest_run(rest, cwd, merged)]
    if _PYTHON.fullmatch(head):
        if rest[:2] == ["-m", "pytest"]:
            return [_pytest_run(rest[2:], cwd, merged)]
        return []
    if head in ("bash", "sh", "zsh", "dash") and depth < 3:
        # `bash -c "cd x && pytest tests"`: the string is a command line
        if "-c" in rest and rest.index("-c") + 1 < len(rest):
            return _parse(rest[rest.index("-c") + 1], depth + 1, cwd, env=merged)
    return []


def _cd_dest(words: list, cwd: str) -> "str | None":
    """The working directory a ``cd`` command leaves, or ``None`` when unknown.

    *words* is a ``cd`` segment: ``["cd", "DIR"]`` plus any ``-L``/``-P``
    options. ``None`` — the caller then reports the whole command as not
    cacheable (HARDEN-28 C) — for every target the parser cannot work out
    statically: no argument (``cd`` alone goes home), ``cd -`` (``$OLDPWD``),
    a ``~``/``$VAR``/backtick/``$(…)`` target, and any option it does not
    recognise. A literal target is combined with the current directory and
    normalised, so ``cd a && cd ..`` returns to the base (``""``).
    """
    target: "str | None" = None
    for w in words[1:]:
        if w.startswith("-") and w != "-":
            continue
        target = w
        break
    if target is None:
        return None
    if target == "-" or "~" in target or "$" in target or "`" in target:
        return None
    if target.startswith("/"):
        new = posixpath.normpath(target)
    else:
        new = posixpath.normpath(
            (cwd + "/" + target) if cwd else target
        )
    return "" if new == "." else new


def _parse(command: str, depth: int, cwd: str = "",
           env: "dict | None" = None) -> list:
    """Every pytest invocation in *command*, in order; ``[]`` when none, or
    when a ``cd``/``pushd`` cannot be resolved (then the command is not
    cached at all — HARDEN-28 C), or when an environment assignment the
    parser cannot read reaches a run (HARDEN-29 F)."""
    runs: list = []
    words: list = []
    cur = cwd
    cur_env = dict(env or {})
    paren = 0

    def flush() -> bool:
        """Reduce the accumulated segment; ``False`` = not cacheable."""
        nonlocal words, cur, cur_env
        if not words:
            return True
        head = words[0].rsplit("/", 1)[-1]
        if head == "cd":
            # A `cd` inside a subshell the parser cannot follow is not
            # cacheable: the tokenizer has flattened the grouping, so it
            # cannot know how far the directory change reaches.
            if paren > 0:
                return False
            nxt = _cd_dest(words, cur)
            if nxt is None:
                return False
            cur = nxt
            words = []
            return True
        if head == "pushd":
            return False
        # HARDEN-29 F: `unset` and `source`/`.` cannot be read statically, so
        # a command carrying one is not cached at all.
        if head in ("unset", "source", "."):
            return False
        if head == "export":
            for w in words[1:]:
                if "=" not in w:
                    return False          # `export VAR`: the value is not readable here
                var, value = w.split("=", 1)
                if not _env_readable(value):
                    return False
                if _env_counts(var, value):
                    cur_env[var] = value
            words = []
            return True
        # A bare assignment segment (`VAR=v;` / `VAR=v` then `&&`): it sets a
        # variable for the rest of the command line. An unreadable value is
        # tolerated and simply not tracked — a timing helper such as
        # `T0=$(date +%s)` must not make the whole run not cacheable — unless
        # the line already set that variable: `export X=1; X=$(date); pytest`
        # runs with the date, so keeping `X=1` in the key would be a wrong hit.
        if all(_ENV_ASSIGN.fullmatch(w) for w in words):
            for w in words:
                var, value = _env_word(w)
                if not _env_readable(value):
                    if var in cur_env:
                        return False
                elif _env_counts(var, value):
                    cur_env[var] = value
                else:
                    cur_env.pop(var, None)
            words = []
            return True
        res = _runs_in_segment(words, depth, cur, cur_env)
        if res is None:
            return False
        runs.extend(res)
        words = []
        return True

    for token in _tokenize(command):
        if token == _OPEN:
            paren += 1
        elif token == _CLOSE:
            paren = max(0, paren - 1)
        elif token == _SEP:
            if not flush():
                return []
        else:
            words.append(token)
    if not flush():
        return []
    return runs


def parse_pytest(command: str) -> list:
    """Every pytest invocation in *command*, in order; ``[]`` when none/unreadable.

    Sees through ``&&``, ``||``, ``;``, ``|``, newlines, ``( … )``, ``{ …; }``,
    ``time``, ``/usr/bin/time -v``, ``nice``, ``timeout N``, ``env X=Y``,
    ``VAR=v`` prefixes, ``cd DIR &&`` (a command that is not pytest is simply
    not a run), ``python3 -m pytest``, ``pytest``, ``py.test`` and a
    ``bash -c "…"`` string. Command substitutions are never executed or read.
    Never raises.
    """
    if not isinstance(command, str) or not command.strip() or "\x00" in command:
        return []
    try:
        return _parse(command.replace(" ⏎ ", "\n"), 0)
    except Exception:  # noqa: BLE001 — "cannot read" is [], never an exception
        return []


# ── the summary line ───────────────────────────────────────────────────────

_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")
_SUMMARY = re.compile(
    r"^[\s=_-]*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|"
    r"deselected|warnings?|rerun)(?:,\s*)?\s*)+)\s+in\s+([\d.]+)\s*s(?:econds)?\b.*$"
    r"|^[\s=_-]*no tests ran\s+in\s+([\d.]+)\s*s(?:econds)?\b.*$",
    re.M,
)
_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.M)


def summarise(output: str, rc: int | None) -> "Summary | None":
    """The :class:`Summary` of a pytest run's *output*, or ``None``.

    ``None`` when the output has no summary line (a ``| tail -5`` can cut it
    away, a crash prints none) and when the tool killed the call
    (``terminated command after exceeding timeout``): such a run is not
    recorded. The summary is looked for anywhere, and the last one wins (a
    chained ``a && b`` prints two). *rc* is accepted for the caller's symmetry
    and for a future use; the numbers come from the text alone.
    """
    try:
        if not isinstance(output, str) or not output:
            return None
        if _KILLED in output[-600:]:
            return None
        found = None
        for found in _SUMMARY.finditer(output):
            pass
        if found is None:
            return None
        counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        for number, word in _COUNT.findall(found.group(0)):
            key = "errors" if word.startswith("error") else word
            if key in counts:
                counts[key] += int(number)
        names: list = []
        for name in _FAILED_LINE.findall(output):
            if name not in names:
                names.append(name)
            if len(names) >= 10:
                break
        duration = found.group(2) or found.group(3) or "0"
        return Summary(
            passed=counts["passed"], failed=counts["failed"],
            errors=counts["errors"], skipped=counts["skipped"],
            duration_s=float(duration), failed_names=names,
            raw_line=found.group(0).strip(" =_-\t"),
        )
    except Exception:  # noqa: BLE001
        return None


# ── the line the model reads ───────────────────────────────────────────────


def progress_line(summary: Summary, *, age_s: float, agent: str,
                  wall_s: "float | None") -> str:
    """The single line served instead of a re-run, e.g.

    ``[##########] cached 100%  tests unchanged since 07:42 (run by agnes-2-5-flash,
    94 s): 7262 passed, 52 skipped — not re-run; edit a file and run again to get
    a fresh run``. Failures are served too, with at most 10 failing names. One
    physical line: newlines in any part are flattened.
    """
    parts = []
    for count, word in ((summary.passed, "passed"), (summary.failed, "failed"),
                        (summary.errors, "error" if summary.errors == 1 else "errors"),
                        (summary.skipped, "skipped")):
        if count:
            parts.append(f"{count} {word}")
    counts = ", ".join(parts) or "no tests ran"
    if summary.failed_names:
        counts += " [" + ", ".join(summary.failed_names[:10]) + "]"
    seconds = wall_s if wall_s is not None else summary.duration_s
    try:
        since = time.strftime("%H:%M", time.localtime(time.time() - float(age_s)))
    except (OverflowError, OSError, ValueError, TypeError):
        since = "earlier"
    who = (str(agent) or "an agent") + (f", {seconds:.0f} s" if seconds else "")
    line = (f"[##########] cached 100%  tests unchanged since {since} "
            f"(run by {who}): {counts} — not re-run; edit a file and run "
            f"again to get a fresh run")
    return " ".join(line.split())


# ── the gate-model fallback ────────────────────────────────────────────────

_CLASSIFY_SYSTEM = (
    "You read one shell command and say whether it runs the pytest test suite. "
    "Answer with JSON only, no prose: "
    '{"runs_tests": true|false, "roots": ["tests"], "flags": ["-x", "-k expr"]}. '
    "roots are the directories or files pytest is run on (empty when the "
    "command does not say); flags are only the pytest options that change which "
    "tests run or what is printed (-k, -m, -x, --maxfail, --lf, --ff, "
    "--collect-only, --durations, -v, -q, --tb). If unsure, runs_tests is false."
)


def _real_completion(settings) -> Callable:
    """A ``(system, user) -> str`` call through the gate's own ``LlmSettings``.

    Built the way ``Policy._gate_request`` builds the gate's request (the same
    ``build_chat_request`` arguments), with no retries: a 429 is "run directly".
    """
    from tools.llm_stream import (build_chat_request, make_unverified_context,
                                  request_completion)

    def call(system: str, user: str) -> str:
        api_format = str(getattr(settings, "api_format", "") or "openai")
        url, headers, payload = build_chat_request(
            base_url=settings.base_url, api_key=settings.api_key,
            model=settings.model, api_format=api_format,
            temperature=float(settings.temperature),
            max_tokens=int(settings.max_tokens),
            system=system, user_msg=user,
            num_ctx=int(settings.num_ctx or 0), think=bool(settings.think),
            response_format=bool(settings.response_format),
            think_effort=(settings.think_effort
                          if getattr(settings, "think_effort_enabled", False)
                          else None),
            stream=False,
        )
        ssl_context = (None if getattr(settings, "verify_ssl", True)
                       else make_unverified_context())
        return request_completion(
            url, headers, payload, 5.0, stream=False, api_format=api_format,
            ssl_context=ssl_context, error_retries=0, error_retry_wait_sec=0.0,
            max_retry_after_sec=0.0,
        )

    return call


def _run_from_answer(answer) -> "PytestRun | None":
    """Validate the model's JSON answer and turn it into a normalised run."""
    from tools.llm_stream import strip_json_fence, strip_think

    text = answer if isinstance(answer, str) else json.dumps(answer, default=str)
    text = strip_json_fence((strip_think(text) or "").strip()).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict) or data.get("runs_tests") is not True:
        return None
    roots = data.get("roots", [])
    flags = data.get("flags", [])
    if not isinstance(roots, list) or not isinstance(flags, list):
        return None
    argv: list = []
    for item in flags:
        if not isinstance(item, str):
            return None
        argv.extend(shlex.split(item))
    for root in roots:
        if not isinstance(root, str):
            return None
        argv.extend(["--", root] if root.startswith("-") else [root])
    return _pytest_run(argv)


def classify_with_llm(command: str, settings, *, budget_s: float = 5.0,
                      completion_fn: "Callable | None" = None) -> "PytestRun | None":
    """Ask the gate model whether *command* runs pytest, and with what.

    Only ever a *parse*: the caller still has to match fingerprint and stored
    run before serving anything. Any failure — no settings, an exception, a
    late answer (hard *budget_s*, the call is abandoned on a daemon thread),
    unreadable or negative JSON — is ``None``, i.e. "run the command directly".
    *completion_fn* ``(system, user) -> str`` replaces the HTTP call in tests.
    Never raises.
    """
    try:
        if not isinstance(command, str) or not command.strip():
            return None
        fn = completion_fn
        if fn is None:
            if settings is None:
                return None
            fn = _real_completion(settings)
        box: list = []

        def work() -> None:
            try:
                box.append(("ok", fn(_CLASSIFY_SYSTEM, "Command:\n" + command[:2000])))
            except BaseException as exc:  # noqa: BLE001
                box.append(("err", exc))

        thread = threading.Thread(target=work, daemon=True, name="testcache-classify")
        thread.start()
        thread.join(max(0.0, float(budget_s)))
        if thread.is_alive() or not box or box[0][0] != "ok":
            return None
        return _run_from_answer(box[0][1])
    except Exception:  # noqa: BLE001 — the cache is never on the critical path
        return None
