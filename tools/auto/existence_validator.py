"""tools/auto/existence_validator.py — GATES-3: does the prose describe files that exist?

Documentation that names a file the repository does not contain is worse
than documentation that omits it: a reader copies the command, it fails, and
they conclude the *project* is broken.

Observed twice on ``examples/hello-world``, whose entire contents are
``main.py``, ``README.md`` and ``CHANGELOG.md``::

    Run the tests by executing `python -m unittest test_main.py`
    Run `python -m unittest discover` to execute the test suite.

Neither run had a test file. Gate 1 passed both and could not have caught
them — Gate 1 judges the PLANNED task, not the prose the coder emits. Gate-3
``fact`` was tried and also passed them, for a more interesting reason: its
prompt compares the text against facts stated in the *task*, so it has no
file list to check against. It is the right gate for "this chapter
contradicts the story bible" and structurally the wrong one here.

This gate is the missing piece, and unlike its neighbours it uses **no LLM at
all**. The question "does ``test_main.py`` exist" has an exact answer on
disk. Spending a model round trip on it would be slower, cost tokens on every
attempt, and — worst — be occasionally wrong about a fact that is never
ambiguous. Determinism is the whole point.

Two findings are reported:

``missing file``
    The text references a path that is not in the repository.

``phantom test suite``
    The text tells the reader to run tests, and the repository contains no
    test files whatsoever. This catches ``unittest discover`` and ``pytest``
    with no argument, which name no file and so slip past the first check.

Fail-open, like every Gate-3 gate: any error approves.
"""

from __future__ import annotations

import glob
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from tools.config_safe import safe_getboolean

logger = logging.getLogger(__name__)

#: Extensions a bare token must have to count as a file reference; narrow on purpose, to avoid false rejections.
DEFAULT_EXTENSIONS = (
    "py", "md", "txt", "rst", "json", "yaml", "yml", "ini", "cfg",
    "toml", "sh", "bash", "csv", "sql", "lock",
)

#: Names that usually appear in prose as examples or conventions, not claims about the repo.
DEFAULT_IGNORE = (
    "setup.py", "requirements.txt", "pyproject.toml", "setup.cfg",
    "Pipfile.lock", "poetry.lock", "package.json", "tox.ini",
)

#: Commands that assert a runnable test suite exists.
_TEST_RUNNER_RE = re.compile(
    r"\b(?:pytest|py\.test|nosetests|tox)\b"
    r"|python[0-9.]*\s+-m\s+(?:pytest|unittest|nose)\b"
    r"|\bunittest\s+discover\b",
    re.IGNORECASE,
)

#: How a test file is recognised when deciding whether a suite exists.
_TEST_FILE_RE = re.compile(r"(^test_.*\.py$)|(.*_test\.py$)|(^conftest\.py$)")

#: Only inline code spans are read for file references; prose mentions are not claims.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.DOTALL)

_URLISH_RE = re.compile(r"https?://|www\.")

# Quotes and trailing punctuation are always stripped from a token; brackets are stripped only as a wrapping pair, since they can be part of a filename.
_QUOTE_AND_PUNCT = "\"',;:"
_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSE_TO_OPEN = {close: open_ for open_, close in _OPEN_TO_CLOSE.items()}


def _strip_wrapping_delimiters(token: str) -> str:
    """Strip prose delimiters around *token* without corrupting it.

    FIX-2 #3: this used to be a single ``token.strip("\"'()[],;:")`` call.
    ``str.strip(chars)`` removes a *charset* independently from each end, not
    a matched delimiter pair, so ``"[file].py"`` — whose closing ``]`` is not
    the last character — lost only its leading ``[`` and became
    ``"file].py"``: a name that isn't on disk, so a document that correctly
    referenced an existing file was rejected as broken.

    A bracket/paren is removed only when it wraps the whole token
    (``"(file.py)"`` -> ``"file.py"``) or is unambiguously stray, i.e. it has
    no partner anywhere else in the token (``"file.py)"`` -> ``"file.py"``).
    ``"handler[old].py"`` and ``"[file].py"`` keep every bracket, because a
    partner exists elsewhere in the token — exactly the case this module
    already treats as a real filename character.
    """
    while True:
        token = token.strip(_QUOTE_AND_PUNCT)
        if not token:
            return token
        first, last = token[0], token[-1]
        if first in _OPEN_TO_CLOSE:
            if len(token) >= 2 and last == _OPEN_TO_CLOSE[first]:
                token = token[1:-1]
                continue
            if _OPEN_TO_CLOSE[first] not in token[1:]:
                token = token[1:]
                continue
        elif last in _CLOSE_TO_OPEN:
            if _CLOSE_TO_OPEN[last] not in token[:-1]:
                token = token[:-1]
                continue
        break
    return token


@dataclass
class ExistenceVerdict:
    """Gate-3 verdict. ``approved`` is the field :mod:`gate_registry` reads."""

    approved: bool
    missing: list[str] = field(default_factory=list)
    phantom_tests: bool = False
    reason: str = ""

    def feedback(self) -> str:
        """Coder-facing message. Names every offending reference explicitly."""
        if self.approved:
            return ""
        parts: list[str] = []
        if self.missing:
            listed = ", ".join(f"`{m}`" for m in self.missing)
            parts.append(
                f"The text references {listed}, which do(es) not exist in this "
                f"repository. Remove the reference, or describe only what is "
                f"actually present. Do not document files that have not been "
                f"created."
            )
        if self.phantom_tests:
            parts.append(
                "The text tells the reader to run a test suite, but this "
                "repository contains no test files. Remove the testing "
                "instructions."
            )
        return "\n".join(parts)


def _candidate_tokens(text: str) -> list[str]:
    """Every token from inline code spans and fenced blocks."""
    tokens: list[str] = []
    for span in _INLINE_CODE_RE.findall(text):
        tokens.extend(span.split())
    for block in _FENCE_RE.findall(text):
        for line in block.splitlines():
            tokens.extend(line.split())
    return tokens


def _parts_inside_base(path: Path, base: Path) -> tuple[str, ...]:
    """Components of *path* relative to *base*, i.e. what is inside the repo.

    `Path.parts` is rooted at the filesystem root, so it contains the BASE
    DIRECTORY'S OWN COMPONENTS too. Judging hidden-ness against it meant any
    base path with a dot component — `/home/u/.cache/repo`, `/srv/.deploy/src`,
    `/tmp/.venv/proj` — made EVERY file in the repository look hidden:
    `_repo_has_tests` then reported a real test suite as phantom, and
    `check()`'s name-match fallback reported present files as missing. Both
    were silent and total rather than intermittent. Hidden detection must judge
    only what is inside the repository.

    `rglob` always yields paths under *base*, so `relative_to` normally cannot
    fail; the fallback keeps a differently-normalised base (a relative base,
    a symlinked prefix) from turning into an unhandled error and silently
    disabling the filter in the opposite direction.
    """
    try:
        return path.relative_to(base).parts
    except ValueError:
        return path.parts


def _is_hidden(path: Path, base: Path) -> bool:
    """True if *path* sits inside a dot-directory relative to *base*."""
    return any(part.startswith(".") for part in _parts_inside_base(path, base))


def _re_windows_drive(token: str) -> bool:
    """Whether *token* opens with a drive letter ("C:/x.ini", "D:x.ini")."""
    return len(token) > 2 and token[0].isalpha() and token[1] == ":" and token[2] in "/\\"


class ExistenceValidator:
    """Check that every file a document references is really there."""

    def __init__(
        self,
        *,
        extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
        ignore: tuple[str, ...] = DEFAULT_IGNORE,
        check_test_suite: bool = True,
        max_existence_revisions: int = 2,
    ) -> None:
        self._extensions = tuple(e.lower().lstrip(".") for e in extensions)
        self._ignore = {i.lower() for i in ignore}
        self._check_test_suite = check_test_suite
        self.max_existence_revisions = max_existence_revisions

    # ── helpers ──────────────────────────────────────────────────────────────

    def _looks_like_path(self, token: str) -> bool:
        token = _strip_wrapping_delimiters(token.strip())
        if not token or _URLISH_RE.search(token):
            return False
        if token.startswith("-"):          # a CLI flag, not a path
            return False
        # REP-EXISTENCE: a wildcard ("tests/test_*.py", "*.md"), a home or environment path
        # ("~/.app/x.json", "$HOME/x.ini", "%APPDATA%\\x.ini") and a Windows path
        # ("C:\\Users\\me\\x.ini") name no file of the repository; they were reported missing
        # and the Coder was told to delete valid test commands and install steps.
        if any(c in token for c in "*?") or token[0] in "~$%" or "\\" in token:
            return False
        if _re_windows_drive(token):
            return False
        suffix = token.rsplit(".", 1)
        if len(suffix) != 2:
            return False
        return suffix[1].lower() in self._extensions

    def _normalise(self, token: str) -> str:
        token = _strip_wrapping_delimiters(token.strip())
        # Strip only real leading "./" and "../" segments (lstrip would also eat the leading "." of ".hidden").
        while token.startswith("./") or token.startswith("../"):
            token = token.split("/", 1)[1] if "/" in token else token
        return token.lstrip("/")

    def references(self, text: str, base_dir=None) -> list[str]:
        """Distinct file references in *text*, in order of appearance.

        With *base_dir*, an absolute path whose first directory is not in the repository
        ("/etc/myapp/config.yaml") is a system path, not a reference; "/docs/usage.md" with a
        ``docs/`` in the repository stays a repo-root-relative one (REP-EXISTENCE).
        """
        seen: list[str] = []
        for raw in _candidate_tokens(text):
            if not self._looks_like_path(raw):
                continue
            bare = _strip_wrapping_delimiters(raw.strip())
            if base_dir is not None and bare.startswith("/") and "/" in bare[1:]:
                first = bare.lstrip("/").split("/", 1)[0]
                if not (Path(base_dir) / first).exists():
                    continue
            token = self._normalise(raw)
            if token.lower() in self._ignore:
                continue
            if token not in seen:
                seen.append(token)
        return seen

    @staticmethod
    def _repo_has_tests(base_dir: Path) -> bool:
        for path in base_dir.rglob("*.py"):
            if _is_hidden(path, base_dir):
                continue
            if _TEST_FILE_RE.match(path.name):
                return True
        return False

    # ── main entry ───────────────────────────────────────────────────────────

    def check(self, text: str, base_dir, rel_path: str = "") -> ExistenceVerdict:
        """Verify *text*'s file references against *base_dir*. Never raises."""
        try:
            base = Path(base_dir)
            # A missing base_dir must fail closed: rglob on it returns nothing, so check the directory explicitly.
            if not base.is_dir():
                logger.warning(
                    "ExistenceValidator: base_dir %s is not a directory — approving.",
                    base,
                )
                return ExistenceVerdict(approved=True, reason="base_dir unavailable")
            missing: list[str] = []
            for ref in self.references(text, base):
                if ref == rel_path or (base / ref).exists():
                    continue
                # A bare name may sit anywhere in the tree; match by name before calling it missing.
                name = Path(ref).name
                # Escape glob characters so "handler[old].py" matches literally.
                if any(
                    p.name == name and not _is_hidden(p, base)
                    for p in base.rglob(glob.escape(name))
                ):
                    continue
                missing.append(ref)

            phantom = False
            if self._check_test_suite and _TEST_RUNNER_RE.search(text):
                phantom = not self._repo_has_tests(base)

            if not missing and not phantom:
                return ExistenceVerdict(approved=True, reason="all references exist")
            return ExistenceVerdict(
                approved=False, missing=missing, phantom_tests=phantom,
                reason=f"{len(missing)} missing reference(s)"
                       + ("; phantom test suite" if phantom else ""),
            )
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            logger.warning("ExistenceValidator: check failed — %s; approving.", exc)
            return ExistenceVerdict(approved=True, reason=f"error: {exc}")


def make_existence_validator(config, *, task_mode: str = "docs"):
    """Build an :class:`ExistenceValidator` from *config*, or ``None``.

    Reads ``[validator_agent] existence_check`` (boolean, **default true**),
    ``max_existence_revisions`` (int, default 2), ``existence_extensions`` and
    ``existence_ignore`` (comma-separated).

    The default is ``true`` on purpose. Its neighbour ``fact`` sits behind
    ``fact_check_creative = false``, which meant that listing ``fact`` in
    ``[gates]`` produced a gate that was silently absent — indistinguishable
    from never having configured it. Listing this gate is enough to get it.
    """
    try:
        if not config.getboolean("validator_agent", "existence_check", fallback=True):
            logger.info("ExistenceValidator: disabled (existence_check = false).")
            return None
    except ValueError:
        logger.warning(
            "ExistenceValidator: existence_check is not a boolean — enabling."
        )

    def _list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = config.get("validator_agent", key, fallback="").strip()
        if not raw:
            return default
        return tuple(p.strip() for p in raw.split(",") if p.strip())

    try:
        max_rev = config.getint("validator_agent", "max_existence_revisions", fallback=2)
    except ValueError:
        max_rev = 2

    validator = ExistenceValidator(
        extensions=_list("existence_extensions", DEFAULT_EXTENSIONS),
        ignore=_list("existence_ignore", DEFAULT_IGNORE),
        check_test_suite=safe_getboolean(
            config, "validator_agent", "existence_check_test_suite", fallback=True
        ),
        max_existence_revisions=max_rev,
    )
    logger.info(
        "ExistenceValidator: enabled (max_existence_revisions=%d, no LLM).", max_rev
    )
    return validator
