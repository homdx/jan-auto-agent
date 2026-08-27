"""tools/auto/delta_validator.py — GATES-3: did the coder actually change anything?

Confirmed in production on ``examples/hello-world`` (config
``agents_128k.ini``, coder model ``deepseek-v4-flash``, task_mode
creative): the coder was asked to prepend a new CHANGELOG.md entry and
returned the file **byte-for-byte unchanged** — no new prose at all. The
creative Gate-2 validator approved it anyway (its prompt checks task
fulfilment, coherence, repetition-vs-*other* chapters, contradictions and
misattribution — every one of those is a property of the output text in
isolation; none of them diffs the output against the pre-task file, so an
unchanged file that trivially preserves everything sails straight
through). ``git add`` then had nothing to stage, and
``commit_on_success.py`` — by design, on the reasonable-sounding
assumption that "the acceptance check passed, so the code must already be
correct" — marked the task DONE anyway. That assumption does not hold
when ``acceptance_check`` is the literal string ``"true"``, as it commonly
is for creative/docs tasks: a check that cannot fail proves nothing about
whether the coder did any work.

This gate is the missing piece, and like its neighbour ``existence`` it
uses **no LLM at all**. "Is this file identical to what's already
committed at HEAD" has an exact, cheap answer from git — spending a model
round-trip on it would be slower and, worse, occasionally wrong about a
fact that is never ambiguous.

Runs first in the Gate-3 order (see ``gate_registry.GATES``) precisely
because it is the cheapest check and the most unambiguous failure: no
sense spending canon/fact/continuity/theme/prosody LLM calls reviewing
prose that turns out to be a no-op.

Deliberately narrow, matching every other Gate-3 gate's fail-open
contract:

* No git repository, no HEAD yet, or the file didn't exist at HEAD (a
  genuinely new file, the primary Creative.MD chapter-filling workflow) —
  there is no baseline to compare against, so this can't be a no-op by
  definition. Approved.
* Any git or filesystem error — approved. A bug in this gate must never
  block a real, changed submission.

Only the exact byte-identical-after-stripping case is rejected. This is
intentionally NOT a "did you make a *substantial* enough change" quality
gate — that question belongs to canon/fact/continuity/theme/prosody, all
of which already run after this one and see real content once it exists.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DeltaVerdict:
    """Gate-3 verdict. ``approved`` is the field :mod:`gate_registry` reads."""

    approved: bool
    rel_path: str = ""
    reason: str = ""

    def feedback(self) -> str:
        """Coder-facing message. Never vague about what's wrong."""
        if self.approved:
            return ""
        return (
            f"`{self.rel_path}` is unchanged from the version already committed "
            f"at HEAD — no new content was written at all. Re-read the "
            f"instruction and actually add or modify what it asks for. Do not "
            f"return the file as-is, and do not just restate the existing "
            f"content back."
        )


class DeltaValidator:
    """Check that a target file actually differs from its committed HEAD version."""

    def __init__(self, *, max_delta_revisions: int = 1) -> None:
        self.max_delta_revisions = max_delta_revisions

    @staticmethod
    def _read_at_head(base_dir: Path, rel_path: str) -> Optional[str]:
        """Return *rel_path*'s content at HEAD, or ``None`` when there is no
        baseline to compare against (new file, no commits yet, not a git
        repo, or any other reason git can't answer) — every one of those
        means "not a no-op", never "identical"."""
        try:
            # AUTO-FIX: "HEAD:{rel_path}" resolves *rel_path* relative to
            # the repo's top-level directory, not to base_dir — identical
            # in production (each task sandbox is its own repo, so
            # base_dir IS the top level) but silently wrong, not merely
            # "no baseline", whenever base_dir is a subdirectory of a
            # larger repo (confirmed while testing this file against
            # examples/hello-world inside this very repo: it resolved to
            # this project's OWN top-level file of the same name instead
            # of failing safe). The "./" prefix is git's documented way to
            # make a ":path" pathspec cwd-relative instead of
            # top-level-relative — identical result in the common case,
            # correct result in the uncommon one.
            result = subprocess.run(
                ["git", "-C", str(base_dir), "show", f"HEAD:./{rel_path}"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "DeltaValidator: git show HEAD:%s failed — %s; treating as no baseline.",
                rel_path, exc,
            )
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def check(self, text: str, base_dir, rel_path: str = "") -> DeltaVerdict:
        """Verify *text* differs from *rel_path*'s content at HEAD. Never raises."""
        try:
            base = Path(base_dir)
            if not base.is_dir():
                logger.warning(
                    "DeltaValidator: base_dir %s is not a directory — approving.", base
                )
                return DeltaVerdict(approved=True, reason="base_dir unavailable")
            if not rel_path:
                return DeltaVerdict(approved=True, reason="no rel_path given")

            prior = self._read_at_head(base, rel_path)
            if prior is None:
                return DeltaVerdict(approved=True, reason="no prior version to compare")
            if text.strip() != prior.strip():
                return DeltaVerdict(approved=True, reason="content differs from HEAD")

            return DeltaVerdict(
                approved=False, rel_path=rel_path,
                reason="unchanged from the version already committed at HEAD",
            )
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            logger.warning("DeltaValidator: check failed — %s; approving.", exc)
            return DeltaVerdict(approved=True, reason=f"error: {exc}")


def make_delta_validator(config, *, task_mode: str = "creative"):
    """Build a :class:`DeltaValidator` from *config*, or ``None`` when disabled.

    Reads ``[validator_agent] delta_check`` (boolean, **default true**) and
    ``max_delta_revisions`` (int, default 1 — this failure mode is about as
    unambiguous as Gate-3 rejections get, so one retry before accepting at
    cap matches ``canon``/``fact``/``continuity``'s default rather than the
    more forgiving 2 used by the genuinely-subjective ``theme``/``prosody``
    gates).
    """
    try:
        if not config.getboolean("validator_agent", "delta_check", fallback=True):
            logger.info("DeltaValidator: disabled (delta_check = false).")
            return None
    except ValueError:
        logger.warning("DeltaValidator: delta_check is not a boolean — enabling.")

    try:
        max_rev = config.getint("validator_agent", "max_delta_revisions", fallback=1)
    except ValueError:
        max_rev = 1

    validator = DeltaValidator(max_delta_revisions=max_rev)
    logger.info("DeltaValidator: enabled (max_delta_revisions=%d, no LLM).", max_rev)
    return validator
