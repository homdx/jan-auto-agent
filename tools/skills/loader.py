"""tools/skills/loader.py — SKILLS-1: run a standard SKILL.md as a config overlay.

A standard skill is a ``SKILL.md`` file: YAML frontmatter (``name``,
``description``) followed by a markdown body of instructions, optionally
beside sibling resource files. It is PROMPT MATERIAL, not a description of
machinery — which is why running one needs no new runtime at all.

This module turns ``SKILL.md`` + a small hand-written adapter into an
overlay applied to the parsed ``agents.ini`` in memory, before anything
starts. Everything downstream — architect, coder, gate1, validator, the
Gate-3 registry — then runs unmodified, reading the values it always read:

    SKILL.md  +  skills/<name>.skill.ini   ──▶  overlay  ──▶  ConfigParser
     (unchanged)   adapter, ~15 lines           (no LLM)      (in memory)

The adapter's ``base`` key is what makes this cheap. It selects the
*mechanical* mode — ``creative`` parses prose and tolerates missing
acceptance criteria, ``code`` parses fenced blocks and requires them — while
the skill's own identity (prompts, gate order, token caps) lives entirely in
config. Mechanics and identity were conflated under ``task_mode``; separating
them is the whole trick, and it costs zero changes to the 20 modules that
branch on ``task_mode``.

Context budget
--------------
The reason the budget guard is not optional: real skill bodies run from
~2 000 to ~20 000 characters. A 20 000-character body is roughly 5 000
tokens, injected into the system prompt of EVERY agent call. Against
``agents.ini``'s ``num_ctx = 8192`` that consumes more than half the window
before the task text is added, and the run degrades in a way that looks like
model failure rather than misconfiguration.

So :func:`load_skill` refuses to silently overspend. It estimates the body's
token cost, compares it against a fraction of the active profile's
``num_ctx``, and applies the adapter's ``on_overflow`` policy:

``error`` (default)
    Raise :class:`SkillBudgetError` naming the numbers and the minimum
    ``num_ctx`` that would fit. Fail loudly at startup rather than produce a
    bad run.
``sections``
    Keep only the ``##`` sections named in ``[skill.sections] keep``, then
    re-check. Still raises if the trimmed body does not fit.
``truncate``
    Cut at a section boundary to fit, logging what was dropped. Never cuts
    mid-sentence.
"""

from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Agents whose system prompt a skill may target. These are exactly the
#: sections that already honour a ``system_{mode}`` override, so injection is
#: a config write rather than a code change.
INJECTABLE_AGENTS = ("architect", "coder", "validator_agent", "gate1_filter")

#: Mechanical modes an adapter may sit on top of.
BASE_MODES = ("code", "docs", "creative")

#: Minimum profile ``num_ctx`` a skill run is allowed to start with, unless
#: the adapter lowers it explicitly. Below this, even a small skill body
#: leaves too little room for the task text plus the file contents.
DEFAULT_MIN_NUM_CTX = 16384

#: Share of ``num_ctx`` the skill body may occupy.
DEFAULT_BUDGET_FRACTION = 0.25


class SkillError(Exception):
    """Base class for every skill-loading failure."""


class SkillNotFoundError(SkillError):
    """No adapter with that name, or its ``source`` SKILL.md is missing."""


class SkillFormatError(SkillError):
    """The adapter or the SKILL.md is malformed."""


class SkillBudgetError(SkillError):
    """The skill body does not fit the active profile's context window."""


# ─────────────────────────────────────────────────────────────────────────────
# Token estimation
# ─────────────────────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Estimate the token cost of *text*, erring on the high side.

    No tokenizer is available offline here, so this uses a characters-per-token
    ratio. The ratio is not constant across scripts: Latin text runs about 4
    characters per token, while Cyrillic and other non-ASCII scripts commonly
    run closer to 2 because they cost multiple tokens per word. A single
    4.0 divisor would therefore UNDERSTATE a Russian skill body by roughly
    half — the exact direction of error that lets an oversized skill through
    the guard, which is the failure this whole module exists to prevent.

    So the divisor adapts to the share of non-ASCII characters, and the result
    is rounded up. An overestimate costs a false rejection the user can
    override; an underestimate costs a silently broken run.
    """
    if not text:
        return 0
    non_ascii = sum(1 for ch in text if ord(ch) > 127)
    ratio = non_ascii / len(text)
    chars_per_token = 4.0 if ratio < 0.10 else 2.5
    return int(len(text) / chars_per_token) + 1


# ─────────────────────────────────────────────────────────────────────────────
# SKILL.md parsing
# ─────────────────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class SkillDoc:
    """A parsed ``SKILL.md``."""

    name: str
    description: str
    body: str
    path: Path

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.body)

    def sections(self) -> dict[str, str]:
        """Split the body on ``##`` headings.

        Text before the first ``##`` is returned under the key ``""`` so an
        intro paragraph is never lost when trimming by section.
        """
        out: dict[str, str] = {}
        current = ""
        buf: list[str] = []
        for line in self.body.splitlines():
            if line.startswith("## "):
                out[current] = "\n".join(buf).strip()
                current = line[3:].strip()
                buf = [line]
            else:
                buf.append(line)
        out[current] = "\n".join(buf).strip()
        return {k: v for k, v in out.items() if v}


def parse_skill_md(path: Path) -> SkillDoc:
    """Parse a standard ``SKILL.md``.

    Frontmatter is read with a small line scanner rather than a YAML library:
    the standard frontmatter is flat ``key: value`` pairs, and adding a YAML
    dependency to read two strings would be a poor trade. A body-only file
    (no frontmatter at all) is accepted, taking its name from the directory —
    some skills in the wild ship that way.
    """
    if not path.is_file():
        raise SkillNotFoundError(f"SKILL.md not found: {path}")
    text = path.read_text(encoding="utf-8")

    name = path.parent.name
    description = ""
    body = text

    match = _FRONTMATTER_RE.match(text)
    if match:
        body = text[match.end():]
        for line in match.group(1).splitlines():
            key, sep, value = line.partition(":")
            if not sep:
                continue
            key = key.strip().lower()
            value = value.strip().strip("\"'")
            if key == "name" and value:
                name = value
            elif key == "description" and value:
                description = value

    if not body.strip():
        raise SkillFormatError(f"{path} has no body content")

    return SkillDoc(name=name, description=description, body=body.strip(), path=path)


# ─────────────────────────────────────────────────────────────────────────────
# Adapter + overlay
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SkillOverlay:
    """The resolved result: what to write into the ConfigParser."""

    name: str
    base: str
    doc: SkillDoc
    injected_body: str
    #: ``(section, key, value)`` triples, applied in order.
    entries: list[tuple[str, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.injected_body)


def _adapter_path(skill: str, base_dir: Path, skills_dir: Path) -> Path:
    """Resolve *skill* to an adapter file.

    Accepts a bare name (``hello-docs``), a name with extension, or a path.
    """
    candidate = Path(skill)
    if candidate.suffix == ".ini" and candidate.exists():
        return candidate
    for root in (skills_dir, base_dir / "skills", Path("skills")):
        for suffix in (".skill.ini", ".ini"):
            path = root / f"{skill}{suffix}"
            if path.is_file():
                return path
    available = sorted(
        p.name.replace(".skill.ini", "")
        for p in skills_dir.glob("*.skill.ini")
    ) if skills_dir.is_dir() else []
    raise SkillNotFoundError(
        f"unknown skill {skill!r}"
        + (f" — available: {', '.join(available)}" if available else
           f" — no adapters found in {skills_dir}")
    )


def _active_num_ctx(config: configparser.ConfigParser) -> int:
    """Read ``num_ctx`` from the active API profile."""
    active = config.get("api", "active", fallback="local")
    section = f"api_{active}"
    if not config.has_section(section):
        section = "api_local"
    try:
        return config.getint(section, "num_ctx", fallback=0)
    except ValueError:
        return 0


def _fit_body(
    doc: SkillDoc,
    *,
    budget_tokens: int,
    policy: str,
    keep_sections: list[str],
    num_ctx: int,
    fraction: float,
) -> tuple[str, list[str]]:
    """Return ``(body, notes)`` fitting *budget_tokens*, per *policy*."""
    notes: list[str] = []
    body = doc.body
    if estimate_tokens(body) <= budget_tokens:
        return body, notes

    def _overflow_error(current: str) -> SkillBudgetError:
        need = estimate_tokens(current)
        required = int(need / fraction) + 1
        return SkillBudgetError(
            f"skill {doc.name!r} body is ~{need} tokens, but the budget is "
            f"{budget_tokens} ({fraction:.0%} of num_ctx={num_ctx}). "
            f"Use a profile with num_ctx >= {required}, raise "
            f"[skill] budget_fraction, or set [skill] on_overflow = sections "
            f"and list the sections to keep."
        )

    if policy == "error":
        raise _overflow_error(body)

    if policy == "sections":
        if not keep_sections:
            raise SkillFormatError(
                f"skill {doc.name!r}: on_overflow = sections requires "
                f"[skill.sections] keep = <comma-separated ## headings>"
            )
        available = doc.sections()
        unknown = [s for s in keep_sections if s not in available]
        if unknown:
            raise SkillFormatError(
                f"skill {doc.name!r}: [skill.sections] keep names missing "
                f"headings {unknown} — available: {sorted(k for k in available if k)}"
            )
        kept = [available[s] for s in keep_sections]
        body = "\n\n".join(kept)
        dropped = [s for s in available if s and s not in keep_sections]
        notes.append(
            f"kept {len(kept)} section(s); dropped {len(dropped)}: {', '.join(dropped) or '-'}"
        )
        if estimate_tokens(body) > budget_tokens:
            raise _overflow_error(body)
        return body, notes

    if policy == "truncate":
        # Cut at a section boundary, never mid-sentence.
        pieces = list(doc.sections().items())
        acc: list[str] = []
        dropped: list[str] = []
        for heading, chunk in pieces:
            candidate = "\n\n".join(acc + [chunk])
            if estimate_tokens(candidate) > budget_tokens:
                dropped.append(heading or "(intro)")
                continue
            acc.append(chunk)
        if not acc:
            raise _overflow_error(body)
        notes.append(f"truncated at section boundary; dropped: {', '.join(dropped)}")
        return "\n\n".join(acc), notes

    raise SkillFormatError(
        f"skill {doc.name!r}: unknown on_overflow policy {policy!r} "
        f"(expected error, sections, or truncate)"
    )


def load_skill(
    skill: str,
    config: configparser.ConfigParser,
    base_dir: Path | str = ".",
    *,
    skills_dir: Path | str = "skills",
) -> SkillOverlay:
    """Resolve *skill* into a :class:`SkillOverlay` against *config*.

    Reads the adapter and the SKILL.md, enforces the context budget, and
    computes every config entry to write — but does NOT mutate *config*.
    Separating resolution from application keeps the budget check testable
    and means a rejected skill leaves the config untouched.
    """
    base_dir = Path(base_dir)
    skills_dir = Path(skills_dir)
    adapter_path = _adapter_path(skill, base_dir, skills_dir)

    adapter = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    try:
        adapter.read(adapter_path, encoding="utf-8")
    except configparser.Error as exc:
        raise SkillFormatError(f"{adapter_path} is not a valid .ini file: {exc}") from exc

    if not adapter.has_section("skill"):
        raise SkillFormatError(f"{adapter_path} has no [skill] section")

    name = adapter.get("skill", "name", fallback=adapter_path.stem.replace(".skill", ""))
    base = adapter.get("skill", "base", fallback="code").strip().lower()
    if base not in BASE_MODES:
        raise SkillFormatError(
            f"{adapter_path}: [skill] base = {base!r} is not one of {', '.join(BASE_MODES)}"
        )

    source = adapter.get("skill", "source", fallback="").strip()
    if not source:
        raise SkillFormatError(f"{adapter_path}: [skill] source is required")
    source_path = Path(source)
    if not source_path.is_absolute():
        for root in (adapter_path.parent, base_dir, Path(".")):
            if (root / source_path).is_file():
                source_path = root / source_path
                break
    doc = parse_skill_md(source_path)

    # ── budget ───────────────────────────────────────────────────────────────
    num_ctx = _active_num_ctx(config)
    min_num_ctx = adapter.getint("skill", "min_num_ctx", fallback=DEFAULT_MIN_NUM_CTX)
    fraction = adapter.getfloat("skill", "budget_fraction", fallback=DEFAULT_BUDGET_FRACTION)
    policy = adapter.get("skill", "on_overflow", fallback="error").strip().lower()

    if num_ctx <= 0:
        raise SkillBudgetError(
            f"skill {name!r}: the active API profile declares no num_ctx, so the "
            f"context budget cannot be checked. Set num_ctx in the active "
            f"[api_*] section (>= {min_num_ctx} recommended)."
        )
    if num_ctx < min_num_ctx:
        raise SkillBudgetError(
            f"skill {name!r} requires num_ctx >= {min_num_ctx}, but the active "
            f"profile has {num_ctx}. Switch to a larger profile "
            f"(agents_32k.ini or above), or lower [skill] min_num_ctx if you "
            f"have measured that this skill fits."
        )

    budget_tokens = int(num_ctx * fraction)
    keep = [
        s.strip()
        for s in adapter.get("skill.sections", "keep", fallback="").split(",")
        if s.strip()
    ]
    injected, notes = _fit_body(
        doc,
        budget_tokens=budget_tokens,
        policy=policy,
        keep_sections=keep,
        num_ctx=num_ctx,
        fraction=fraction,
    )

    # ── entries ──────────────────────────────────────────────────────────────
    entries: list[tuple[str, str, str]] = [("auto", "task_mode", base)]

    prompt_key = "system" if base == "code" else f"system_{base}"
    targets = []
    if adapter.has_section("skill.inject"):
        for agent in INJECTABLE_AGENTS:
            what = adapter.get("skill.inject", agent, fallback="none").strip().lower()
            if what in ("", "none", "false", "no"):
                continue
            if what != "body":
                raise SkillFormatError(
                    f"{adapter_path}: [skill.inject] {agent} = {what!r} "
                    f"(expected 'body' or 'none')"
                )
            targets.append(agent)
    if not targets:
        raise SkillFormatError(
            f"{adapter_path}: [skill.inject] names no agent — a skill that "
            f"injects nothing has no effect"
        )
    for agent in targets:
        entries.append((agent, prompt_key, injected))

    if adapter.has_section("skill.overlay"):
        for dotted, value in adapter.items("skill.overlay"):
            section, sep, key = dotted.partition(".")
            if not sep or not key:
                raise SkillFormatError(
                    f"{adapter_path}: [skill.overlay] key {dotted!r} must be "
                    f"'section.key' (e.g. coder.max_tokens)"
                )
            entries.append((section, key, value))

    if adapter.has_section("gates"):
        for mode, value in adapter.items("gates"):
            entries.append(("gates", mode, value))

    return SkillOverlay(
        name=name, base=base, doc=doc, injected_body=injected,
        entries=entries, notes=notes,
    )


def apply_overlay(config: configparser.ConfigParser, overlay: SkillOverlay) -> None:
    """Write *overlay* into *config*, in place.

    Deliberately last-write-wins over the profile: a skill is an explicit,
    per-run choice, so it outranks the ambient ``agents.ini``. The reverse
    (profile beats skill) would make the skill silently partial, which is
    worse than an override the user asked for.
    """
    for section, key, value in overlay.entries:
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, key, value)


def apply_skill(
    config: configparser.ConfigParser,
    skill: str,
    base_dir: Path | str = ".",
    *,
    skills_dir: Path | str = "skills",
) -> SkillOverlay:
    """Load *skill* and apply it to *config*. Convenience wrapper."""
    overlay = load_skill(skill, config, base_dir, skills_dir=skills_dir)
    apply_overlay(config, overlay)
    logger.info(
        "skill %r loaded — base=%s, ~%d tokens injected into %s%s",
        overlay.name, overlay.base, overlay.tokens,
        ", ".join(sorted({s for s, k, _ in overlay.entries if k.startswith("system")})),
        (" (" + "; ".join(overlay.notes) + ")") if overlay.notes else "",
    )
    return overlay


def list_skills(skills_dir: Path | str = "skills") -> list[str]:
    """Return the names of every adapter in *skills_dir*."""
    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        return []
    return sorted(p.name.replace(".skill.ini", "") for p in skills_dir.glob("*.skill.ini"))
