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

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Extensions a bare token must carry to be treated as a file reference.
#: Deliberately narrow — the cost of a false positive here is a rejected
#: attempt and a confused author, so anything ambiguous is left alone.
DEFAULT_EXTENSIONS = (
    "py", "md", "txt", "rst", "json", "yaml", "yml", "ini", "cfg",
    "toml", "sh", "bash", "csv", "sql", "lock",
)

#: Names that routinely appear in prose as examples or conventions rather
#: than as claims about this repository.
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

#: Inline code spans and fenced blocks — the only places a file reference is
#: read from. Prose mentioning "the main script" is not a claim about a path.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n(.*?)```", re.DOTALL)

_URLISH_RE = re.compile(r"https?://|www\.")


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
        token = token.strip().strip("\"'()[],;:")
        if not token or _URLISH_RE.search(token):
            return False
        if token.startswith("-"):          # a CLI flag, not a path
            return False
        suffix = token.rsplit(".", 1)
        if len(suffix) != 2:
            return False
        return suffix[1].lower() in self._extensions

    def _normalise(self, token: str) -> str:
        return token.strip().strip("\"'()[],;:").lstrip("./")

    def references(self, text: str) -> list[str]:
        """Distinct file references in *text*, in order of appearance."""
        seen: list[str] = []
        for raw in _candidate_tokens(text):
            if not self._looks_like_path(raw):
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
            if any(part.startswith(".") for part in path.parts):
                continue
            if _TEST_FILE_RE.match(path.name):
                return True
        return False

    # ── main entry ───────────────────────────────────────────────────────────

    def check(self, text: str, base_dir, rel_path: str = "") -> ExistenceVerdict:
        """Verify *text*'s file references against *base_dir*. Never raises."""
        try:
            base = Path(base_dir)
            # An unusable base_dir must fail OPEN, and it does not do so on its
            # own: rglob() on a missing directory returns an empty iterator
            # rather than raising, so every reference would look missing and the
            # gate would reject every document it ever saw. The except clause
            # below never fires for this case, so it is caught explicitly.
            if not base.is_dir():
                logger.warning(
                    "ExistenceValidator: base_dir %s is not a directory — approving.",
                    base,
                )
                return ExistenceVerdict(approved=True, reason="base_dir unavailable")
            missing: list[str] = []
            for ref in self.references(text):
                if ref == rel_path or (base / ref).exists():
                    continue
                # A bare name may sit anywhere in the tree ("test_main.py"
                # referenced from a nested doc), so fall back to a name match
                # before calling it missing.
                name = Path(ref).name
                if any(
                    p.name == name and not any(q.startswith(".") for q in p.parts)
                    for p in base.rglob(name)
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
        check_test_suite=config.getboolean(
            "validator_agent", "existence_check_test_suite", fallback=True
        ),
        max_existence_revisions=max_rev,
    )
    logger.info(
        "ExistenceValidator: enabled (max_existence_revisions=%d, no LLM).", max_rev
    )
    return validator
