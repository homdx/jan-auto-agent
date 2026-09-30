"""tools/contest/draft.py — KC-79: a plain brief becomes a ticket grounded in the collect maps.

`python3 -m tools.contest draft --target REPO "speed up pytest tests"` runs
`tools.collect.cli.action_collect(root, llm_call=None)` over REPO — Pass A only,
no Pass B, so a fresh `.collect/` costs one scan and no model call — feeds the
brief verbatim plus the three maps the collect run just wrote
(`MODULE_MAP.md`, `TEST_MAP.md`, `RISK_INDEX.md`, each cut to the operator's
configured budget, never a model window hard-coded here) to one LLM call, lints
the reply against the repo and the artifact it reads from disk, and writes the
ticket to `epic-tasks/<NN>-<slug>.md`.

The brief is never rewritten into a different task: speed, coverage, docs, tests
and bug hunt all take this one code path; what differs is only what the model
reads in the maps.

KC-80 adds the two steps after the lint. A second model — the one
`[contest] gate_llm_profile` names, resolved through the same
``resolve_llm_profile`` the gate is resolved with — reads the ticket against the
brief and a fixed checklist and answers ``{"ok": bool, "problems": [str]}``: an
acceptance that cannot fail, a task met by deleting or skipping tests, a task too
big for one leg, a task that is not the brief. Not ok sends the problems back to
the drafter, then the lint, then the review again, at most
`[contest] draft_review_rounds` such rounds. Still not ok is a refusal like a
lint refusal — the problems printed, the last draft saved as `.rejected.md`,
nothing committed.

A ticket that is ok is committed by :func:`commit_ticket` on the branch
`contest-legs` in the target repo: the branch is created when missing, the two
scripts the agents' prompt runs are copied in when absent, and the commit names
the ticket's path and the scripts' paths explicitly — never `-a`. A repo with
uncommitted tracked changes is refused before the first LLM call, the way
`2legs/prepare_task.sh` refuses it.

:func:`lint_ticket` is the mechanical half: pure, no LLM, one string per problem.
It is what decides between "write it" and "ask again once, then refuse".

Both models are named in the contest config — the drafter by
`[contest] draft_llm_profile` and the reviewer by `[contest] gate_llm_profile` —
resolved through ``tools.auto.llm_profile.resolve_llm_profile`` exactly as
``gate_llm_profile`` is, and passed in as callables: this module never holds a
model name, a URL or a key. An unset profile is a refusal that names the key.

Fail-open throughout: an absent or unreadable `.collect/`, an artifact that is
not a JSON object, a collect run that raises and a malformed budget all degrade
to "no collect data" and never raise into the draft. A collect with no artifact
still gets a ticket — it just cannot prove its own symbols.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tools.collect.cli import action_collect
from tools.git_run import run_git
from tools.llm_stream import (
    build_chat_request,
    make_unverified_context,
    request_completion,
    strip_think,
)

__all__ = [
    "ARTIFACT_FILENAME",
    "COLLECT_DIR",
    "CONTEST_SCRIPTS",
    "DEFAULT_MAP_BUDGET",
    "DEFAULT_REVIEW_ROUNDS",
    "HEADER_FIELDS",
    "LEG_BRANCH",
    "MAP_FILES",
    "OPEN",
    "REJECTED_SUFFIX",
    "REVIEW_CHECKLIST",
    "REVIEW_SYSTEM_PROMPT",
    "RUNNER_SCRIPTS_DIR",
    "SECTIONS",
    "TASKS_DIR",
    "CommitResult",
    "DraftResult",
    "ReviewResult",
    "brief_sources",
    "build_prompt",
    "build_review_prompt",
    "commit_ticket",
    "draft_ticket",
    "llm_call_for",
    "lint_ticket",
    "load_artifact",
    "maps_text",
    "next_round",
    "review_ticket",
    "round_taken",
    "slug_for",
    "symbols_by_path",
    "ticket_format_spec",
    "title_of",
]

#: Where collect writes its output, relative to the repo.
COLLECT_DIR = ".collect"

#: The artifact the lint reads the symbol names out of.
ARTIFACT_FILENAME = "artifact.json"

#: The three maps the draft's prompt carries, in the order it carries them.
MAP_FILES = ("MODULE_MAP.md", "TEST_MAP.md", "RISK_INDEX.md")

#: The ticket folder the ticket lands in, and the one the round reads.
TASKS_DIR = "epic-tasks"

#: Every header field a ticket carries, in the order the lint wants them.
HEADER_FIELDS = ("Status", "Severity", "File", "Symbol", "Round", "Size", "Also touches")

#: Every section a ticket carries.
SECTIONS = ("Why", "What to build", "Acceptance", "Rules")

#: The `**Status:**` the lint wants on a ticket that has just been written.
OPEN = "open"

#: The character budget a map gets when `[contest] draft_map_budget` is unset.
#: A budget the operator may raise or lower; it is never a model window.
DEFAULT_MAP_BUDGET = 8000

#: The rework rounds the review may send a draft back for when
#: `[contest] draft_review_rounds` is unset. 3 = one review, then at most three
#: rounds of problems -> rework -> lint -> review; 0 = one review, no rework.
DEFAULT_REVIEW_ROUNDS = 3

#: The branch a drafted ticket is committed to: the same branch
#: `2legs/prepare_task.sh` commits onto, so a drafted round and a hand-made one
#: start from the same place.
LEG_BRANCH = "contest-legs"

#: The two scripts the agents' prompt runs in their clone; the commit copies
#: them in when the target repo does not have them yet.
CONTEST_SCRIPTS = ("scripts/next_task.py", "scripts/append_task.py")

#: Where the two scripts above come from: this repo's own `scripts/`.
RUNNER_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"

#: Seconds one draft call may take before it is a transport failure.
DRAFT_TIMEOUT = 300.0

#: One draft, one rework. A second problem list is not asked, only printed.
MAX_ATTEMPTS = 2

#: `python3 -m tools.contest draft …` prints a path on success; this is the
#: line it prints when a brief was drafted but never passed the lint.
REJECTED_SUFFIX = ".rejected.md"

#: The system prompt of the draft call. It names the job, not a model.
DRAFT_SYSTEM_PROMPT = (
    "You write one ticket for an autonomous model contest, in Markdown, and "
    "nothing else: no preamble, no commentary, no fences around the ticket. "
    "Every file path and symbol name you write must come from the collect maps "
    "you were given; never invent one. The brief is the task — do not widen it, "
    "narrow it or turn it into a different one. When the source of a file is "
    "given, `## What to build` lists the concrete cases it shows — each branch, "
    "return value, config key and fallback by name — and says outright when a "
    "parameter the brief names is never read; a line that only restates the "
    "brief is not a case."
)

#: The system prompt of the review call. It names the job and the shape of the
#: answer, not a model: the reviewer is the gate's, and `[contest] gate_llm_profile`
#: is what resolves it.
REVIEW_SYSTEM_PROMPT = (
    "You are the reviewer of one ticket for an autonomous model contest. Decide "
    "whether it is worth an agent's hour, against the brief it was drafted from "
    "and every item of the checklist it is given. Answer JSON only, and nothing "
    "else: no preamble, no commentary, no code fence, no key besides the two. "
    '{"ok": true} when every item passes, {"ok": false} with one short sentence '
    "per failing item in \"problems\" — each one something the drafter can act on."
)

#: The checklist the review verdict is against, one line per item. KC-79's lint
#: can only check that the ticket is well formed; these four are what a second
#: model can see that the lint cannot.
REVIEW_CHECKLIST = (
    "- the acceptance can be checked by running commands, not by opinion;\n"
    "- the task cannot be met by deleting, skipping or weakening tests;\n"
    "- it fits one agent in one leg — if it does not, say how to split it;\n"
    "- it does what the brief asked, not something nearby;\n"
    "- `## What to build` names concrete cases (branches, values, keys) an agent "
    "can check off, not the brief restated in other words."
)

#: `**Status:** open — round 55 …` → the label and the value, one pass each.
_FIELD_RE = re.compile(r"^\*\*([^*][^*:]*):\*\*\s*(.*?)\s*$", re.MULTILINE)

#: `# KC-79 — A plain brief becomes a ticket` → the H1's text.
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

#: `## Acceptance` → the section name, whatever follows it on the line.
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

#: `126-slug.md`, `05-l2-probe.md` → the ticket number, leading zeros allowed.
_TICKET_RE = re.compile(r"^0*(\d+)-.*\.md$")

#: `**Symbol:** draft_ticket`, `lint_ticket` → the names, backticks dropped.
_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


#: A fenced code block, and its body — what `## Acceptance` must hold.
_FENCE_RE = re.compile(r"```[^\n]*\n([\s\S]*?)```")


@dataclass(frozen=True)
class ReviewResult:
    """The reviewer's verdict on one draft: ``ok`` and one problem per line."""

    ok: bool
    problems: tuple


@dataclass(frozen=True)
class CommitResult:
    """One commit's outcome: ``ok``, the line to print, and the commit when one exists."""

    ok: bool
    message: str
    sha: Optional[str] = None


@dataclass(frozen=True)
class DraftResult:
    """One draft attempt's outcome.

    ``path`` is the written ticket, or ``None`` when the lint refused both
    attempts; ``rejected_path`` is the last draft's ``.rejected.md`` copy, when
    the operator asked for the work to be written. ``number`` is the ticket
    number the draft claimed — ``None`` when the draft was refused before a
    number existed to claim — and ``commit`` is the KC-80 commit's outcome,
    ``None`` when no commit was asked for.
    """

    path: Optional[Path]
    rejected: bool
    problems: tuple
    rejected_path: Optional[Path] = None
    number: Optional[int] = None
    commit: Optional[CommitResult] = None


# ─────────────────────────────────────────────────────────────────────────────
# reading the collect output
# ─────────────────────────────────────────────────────────────────────────────

def load_artifact(collect_dir) -> Optional[dict]:
    """The `artifact.json` in *collect_dir*, or `None` when it is not one.

    An absent file, an unreadable one and valid JSON that is not an object all
    give `None`: the draft then drafts without an artifact and the lint's symbol
    check stands down, instead of a broken artifact refusing the brief.
    """
    try:
        payload = json.loads(Path(collect_dir).joinpath(ARTIFACT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _as_budget(budget) -> int:
    """*budget* as a character count: a positive int, else the default budget.

    A missing key, a string, a bool and a zero all give the default — a malformed
    or switched-off budget must never empty the prompt.
    """
    try:
        value = int(budget)
    except (TypeError, ValueError):
        return DEFAULT_MAP_BUDGET
    if isinstance(budget, bool) or value <= 0:
        return DEFAULT_MAP_BUDGET
    return value


def maps_text(collect_dir, budget) -> list:
    """The three maps as ``(name, text)`` pairs, each cut to *budget* characters.

    Absent maps are skipped, so a collect that wrote only two of the three still
    reaches the prompt with the two it has. *budget* <= 0 is the default budget,
    never an empty prompt: a typo in the budget must not starve the draft.
    """
    budget = _as_budget(budget)
    pairs = []
    for name in MAP_FILES:
        try:
            body = Path(collect_dir).joinpath(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pairs.append((name, _cut(body, budget)))
    return pairs


def _cut(text: str, budget: int) -> str:
    """*text* within *budget* characters, marked when it was cut."""
    if len(text) <= budget:
        return text
    return text[:budget] + f"\n[... cut to {budget} characters ...]\n"


def run_collect(root) -> object:
    """`action_collect(root, llm_call=None)` — Pass A over *root*, no Pass B.

    A stale tree refreshes, a fresh one is a no-op, and a collect that raises is
    the caller's to ignore: "no collect data" is a draft without maps, not a
    failed brief.
    """
    return action_collect(Path(root), llm_call=None)


def symbols_by_path(artifact) -> dict:
    """``{path: {symbol names}}`` off an artifact payload; ``{}`` when it is not one.

    A module's `public_symbols` are matched by `qualname` and by the last
    component of it, so `pkg.a` and `a` both satisfy a symbol written as `a`.
    Anything malformed is skipped, entry by entry: one bad record must not drop
    the rest of the map, and an artifact with no modules is simply empty.
    """
    out: dict = {}
    if not isinstance(artifact, dict):
        return out
    modules = artifact.get("modules")
    if not isinstance(modules, list):
        return out
    for module in modules:
        if not isinstance(module, dict):
            continue
        rel = module.get("path")
        if not isinstance(rel, str) or not rel:
            continue
        names = out.setdefault(rel, set())
        symbols = module.get("public_symbols")
        if not isinstance(symbols, list):
            continue
        for symbol in symbols:
            if not isinstance(symbol, dict):
                continue
            qual = symbol.get("qualname")
            if not isinstance(qual, str) or not qual:
                continue
            # collect writes `pkg/a.py:a` — the path and the name separated by a
            # colon — so the short name is the part after the last `:`, and a
            # dotted one keeps the last component as well.
            short = qual.split(":")[-1]
            names.add(qual)
            names.add(short)
            names.add(short.rsplit(".", 1)[-1])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# the ticket's own shape
# ─────────────────────────────────────────────────────────────────────────────

def _fields(text: str) -> dict:
    """`{label: value}` off every `**Label:** value` line, first one wins."""
    out: dict = {}
    for match in _FIELD_RE.finditer(text or ""):
        label = match.group(1).strip()
        if label and label not in out:
            out[label] = match.group(2).strip()
    return out


def _value(fields: dict, label: str) -> Optional[str]:
    value = fields.get(label)
    if value is None:
        return None
    value = value.strip().strip("`").strip()
    return value or None


def _section(text: str, name: str) -> Optional[str]:
    """The body of `## name`, from its heading to the next `## `, or `None`."""
    lines = (text or "").split("\n")
    start = None
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match and match.group(1).strip().lower() == name.strip().lower():
            start = index + 1
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start, len(lines)):
        if _SECTION_RE.match(lines[index]):
            end = index
            break
    return "\n".join(lines[start:end])


def _has_command(body: str) -> bool:
    """Whether *body* holds a code block with a line in it.

    Markdown has two code blocks: the fence and the 4-space (or tab) indent.
    The indent is the KC tickets' own house style — KC-79's `## Acceptance`
    is one — so a draft written the way its own ticket is must not be
    refused. An indented line counts only outside a fence and when it is not
    a list item's continuation (a `-`/`*`/`1.` marker after the indent).
    """
    for block in _FENCE_RE.findall(body or ""):
        for line in block.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return True
    for line in _FENCE_RE.sub("", body or "").splitlines():
        if not (line.startswith("    ") or line.startswith("\t")):
            continue
        stripped = line.strip()
        if stripped and not stripped.startswith("#") \
                and not re.match(r"(?:[-*+]|\d+[.)])\s", stripped):
            return True
    return False


def _paths_of(raw) -> list:
    """The repo paths in a header value, in order, without repeats.

    Backticks and commas are separators, not path characters; a token must carry a
    dot to count as a path, so a bare phrase such as `the parser` in `Also touches`
    is prose and not a file. `(none)` is the ticket's own way of saying there is
    no such field.
    """
    out = []
    for token in re.split(r"[,\s]+", (raw or "").replace("`", "")):
        token = token.strip().rstrip(".,;:)`")
        if not token or token.lower() == "(none)" or "." not in token:
            continue
        if token not in out:
            out.append(token)
    return out


def title_of(text: str) -> str:
    """The ticket's H1 text, or `""` when the draft has no H1."""
    match = _TITLE_RE.search(text or "")
    return match.group(1).strip() if match else ""


def slug_for(title: str, fallback: str = "ticket", round_no=None) -> str:
    """The file name from a ticket title: lower-cased, dashes, no spaces.

    With *round_no*, a title that already opens with that number
    (`# 129-delta-validator — …`) loses it, so the file is
    `129-delta-validator-….md` and not `129-129-delta-validator-….md`.
    """
    words = re.sub(r"[^A-Za-z0-9]+", "-", (title or "").lower()).strip("-")
    if round_no is not None:
        words = re.sub(rf"^0*{int(round_no)}(?:-|$)", "", words)
    return words[:60] or fallback


def ticket_format_spec() -> str:
    """The ticket's shape as the model must write it — header fields, then sections.

    The same order every ticket in `epic-tasks/` carries, so a draft that follows
    it is one the round's own readers parse. The values shown are the accepted
    ones for each field, not examples of a specific ticket.
    """
    hints = {
        "Status": "open",
        "Severity": "LOW | MEDIUM | HIGH | CRITICAL",
        "File": "<the one repo path the work changes>",
        "Symbol": "<the function or class names in it, comma-separated>",
        "Round": "<NN>",
        "Size": "XS | S | M | L",
        "Also touches": "<every other repo path the work names, comma-separated, or (none)>",
    }
    lines = ["# <ID> — <what the ticket does>", ""]
    for label in HEADER_FIELDS:
        lines.append(f"**{label}:** {hints.get(label, '<value>')}")
    lines += ["", "---", ""]
    for section in SECTIONS:
        lines.append(f"## {section}")
        lines.append("")
    lines.append(
        "Every header field above must be present; `## Acceptance` must hold a "
        "fenced code block with the command that proves the work."
    )
    return "\n".join(lines)


#: `tools/auto/delta_validator.py` in a brief → a candidate repo path.
_BRIEF_PATH_RE = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+")


def brief_sources(repo, brief: str, budget) -> list:
    """The files the brief names, as ``(path, text)`` pairs, each cut to *budget*.

    Only existing files inside *repo* count, each once, in the brief's order: a
    test file the brief asks for does not exist yet and is skipped. The maps say
    where things are; this is what the drafter needs to name the cases.
    """
    budget = _as_budget(budget)
    repo = Path(repo)
    pairs, seen = [], set()
    for rel in _BRIEF_PATH_RE.findall(brief or ""):
        rel = (rel[2:] if rel.startswith("./") else rel).rstrip(".")
        if rel in seen or not _safe_rel(rel) or not (repo / rel).is_file():
            continue
        seen.add(rel)
        try:
            body = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pairs.append((rel, _cut(body, budget)))
    return pairs


def build_prompt(brief: str, maps: list, *, round_no: int, format_spec: str = None,
                 sources: list = None) -> str:
    """The one prompt the draft call gets: the brief, the maps, the format.

    *maps* is `maps_text`, already cut. The brief is copied verbatim and the
    ticket's own `**Round:**` value is told to the model, so the lint can check
    the draft against the number that will decide its file name.
    """
    parts = [
        "Write the ticket for the brief below, in the format at the end.",
        "",
        "## The brief — the task, unchanged",
        "",
        (brief or "").strip(),
        "",
        f"It lands in `epic-tasks/{int(round_no):02d}-<slug>.md`; write "
        f"`**Round:** {int(round_no)}`.",
    ]
    for rel, body in sources or ():
        parts += ["", f"## Source of `{rel}` — name the cases from it", "",
                  "```", body.rstrip(), "```"]
    for name, body in maps:
        parts += ["", f"## {name}", "", body.strip()]
    if not maps:
        parts += [
            "",
            "## The collect maps",
            "",
            "No collect data was available for this repo. Write the ticket from the "
            "brief alone and keep `**File:**` and `**Symbol:**` to paths and names "
            "the brief itself names.",
        ]
    parts += [
        "",
        "## The ticket format — every field, every section, in this order",
        "",
        format_spec or ticket_format_spec(),
        "",
        "Then the ticket, and nothing else.",
    ]
    return "\n".join(parts)


def _rework(draft: str, problems: list) -> str:
    """The rework prompt: the rejected draft back, with its problem list appended."""
    lines = [
        "",
        "## Your draft above is rejected — the problems",
    ]
    lines += [f"- {problem}" for problem in problems]
    lines += [
        "",
        "Rewrite the whole ticket and fix every problem above.",
    ]
    return "\n".join(lines)


def _rework_prompt(prompt: str, draft: str, problems: list) -> str:
    """The drafter's rework prompt: its own draft back with the problems appended.

    KC-79's lint problems and KC-80's review problems both go through here, so
    the drafter always sees the original prompt, its own draft and one problem
    list to work through.
    """
    return prompt + "\n\n## Your draft\n\n" + draft.strip() + _rework(draft, problems)


def _as_rounds(value) -> int:
    """*value* as a number of review rounds: a non-negative int, else the default.

    Absent is the default, and a string that is not a number, a bool and a
    negative are the default too — a typo must not loop the draft forever, and
    it must not turn the review into no review either. ``0`` is a real value:
    one review, no rework.
    """
    if isinstance(value, bool):
        return DEFAULT_REVIEW_ROUNDS
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_REVIEW_ROUNDS
    return count if count >= 0 else DEFAULT_REVIEW_ROUNDS


# ─────────────────────────────────────────────────────────────────────────────
# the lint
# ─────────────────────────────────────────────────────────────────────────────

def _safe_rel(rel: str) -> bool:
    """Whether *rel* is a repo-relative path worth resolving: no absolute, no `..`."""
    return bool(rel) and not rel.startswith("/") and ".." not in rel.split("/")


def _in_repo(repo: Path, rel: str) -> bool:
    """Whether *rel* is an existing file or directory in *repo*."""
    return _safe_rel(rel) and (repo / rel).exists()


def _new_under_existing(repo: Path, rel: str) -> bool:
    """Whether *rel* is a file the repo does not have yet, under a directory it does.

    A new test file is normal, so `tests/test_new.py` passes while
    `no/such/dir/x.py` does not.
    """
    return _safe_rel(rel) and (repo / rel).parent.is_dir()


def lint_ticket(text, artifact, *, repo=None, tasks_dir=None, round_no=None) -> list:
    """The problems with a drafted ticket, one string each — pure, no LLM.

    ``artifact`` is `load_artifact` (so it may be ``None``), *repo* and
    *tasks_dir* the repo and its `epic-tasks/`, and *round_no* the number the
    caller chose. Every input the caller does not supply turns that check off
    rather than failing the draft: no repo means no path check, no artifact
    means no symbol check, no tasks dir means no taken-round check.
    """
    problems: list = []
    if not isinstance(text, str) or not text.strip():
        return ["the draft is empty"]

    fields = _fields(text)

    for label in HEADER_FIELDS:
        if _value(fields, label) is None:
            problems.append(f"missing **{label}:** header field")

    status = _value(fields, "Status")
    if status is not None and status.lower() != OPEN:
        problems.append(f"**Status:** must be '{OPEN}', got '{status}'")

    size = _value(fields, "Size")
    if size is not None and size.upper() not in ("XS", "S", "M", "L"):
        problems.append(f"**Size:** must be XS, S, M or L, got '{size}'")

    file_paths = []
    for label in ("File", "Also touches"):
        raw = _value(fields, label)
        paths = _paths_of(raw)
        if label == "File":
            file_paths = paths
            if raw is not None and not paths:
                problems.append("**File:** must name a repo path")
        if repo is None:
            continue
        for rel in paths:
            if _in_repo(repo, rel) or _new_under_existing(repo, rel):
                continue
            problems.append(
                f"**{label}:** '{rel}' is not in the repo and its directory is not either")

    symbols = [name for name in _SYMBOL_RE.findall(_value(fields, "Symbol") or "")]
    if symbols and file_paths and artifact is not None and repo is not None:
        existing = [rel for rel in file_paths if _in_repo(repo, rel)]
        if existing:
            known = symbols_by_path(artifact)
            for name in symbols:
                if not any(name in known.get(rel, set()) for rel in existing):
                    problems.append(
                        f"**Symbol:** '{name}' is not in {ARTIFACT_FILENAME} for "
                        f"'{existing[0]}' — the file is new, or the artifact is stale")

    acceptance = _section(text, "Acceptance")
    if acceptance is None:
        problems.append("missing '## Acceptance' section")
    elif not _has_command(acceptance):
        problems.append("'## Acceptance' has no command in a code block")

    round_raw = _value(fields, "Round")
    if round_raw is not None:
        number = round_raw.split()[0]
        if not number.isdigit():
            problems.append(f"**Round:** must be a ticket number, got '{round_raw}'")
        else:
            wanted = int(number)
            if round_no is not None and wanted != int(round_no):
                problems.append(
                    f"**Round:** must be {int(round_no)}, the round this draft writes, "
                    f"got {wanted}")
            elif tasks_dir is not None and round_taken(tasks_dir, wanted):
                problems.append(
                    f"**Round:** {wanted} is taken — {TASKS_DIR}/{wanted:02d}-*.md already exists")
    elif round_no is not None:
        problems.append(f"**Round:** must be {int(round_no)}")

    return problems


# ─────────────────────────────────────────────────────────────────────────────
# the round number and the file name
# ─────────────────────────────────────────────────────────────────────────────

def round_taken(tasks_dir, number) -> bool:
    """Whether `<tasks_dir>/<NN>-*.md` exists for *number* — `False` for no folder."""
    try:
        folder = Path(tasks_dir)
        if not folder.is_dir():
            return False
        for path in folder.glob("*.md"):
            match = _TICKET_RE.match(path.name)
            if match and int(match.group(1)) == int(number):
                return True
    except (TypeError, ValueError, OSError):
        return False
    return False


def next_round(tasks_dir, start: int = 1) -> int:
    """The first number from *start* with no ticket in *tasks_dir*."""
    number = int(start)
    while round_taken(tasks_dir, number):
        number += 1
    return number


# ─────────────────────────────────────────────────────────────────────────────
# the review: the gate model's verdict on the draft
# ─────────────────────────────────────────────────────────────────────────────

def build_review_prompt(brief: str, ticket_text: str, checklist: str = None) -> str:
    """The one prompt the review call gets: the brief, the ticket, the checklist.

    The same shape as `build_prompt`: the brief first, because the ticket has to
    do what the brief asked and not something nearby, then the ticket as
    drafted, then the checklist the verdict is judged against.
    """
    return "\n".join([
        "Decide whether the ticket below is worth an agent's hour. Judge it "
        "against the brief it was drafted from and every item of the checklist "
        "at the end.",
        "",
        "## The brief — the task, unchanged",
        "",
        (brief or "").strip(),
        "",
        "## The ticket — as drafted",
        "",
        (ticket_text or "").strip(),
        "",
        "## The checklist — every item must pass",
        "",
        checklist or REVIEW_CHECKLIST,
        "",
        'Answer JSON only: {"ok": true|false, "problems": ["one sentence each"]}.',
    ])


def review_ticket(text, brief, review_call) -> ReviewResult:
    """One ask of the reviewer — the gate model — as a verdict.

    *review_call* is a callable taking the prompt text and returning the reply:
    a fake in tests, `llm_call_for(config.gate_settings,
    system=REVIEW_SYSTEM_PROMPT)` on the real path. This module supplies only the
    brief, the ticket and the checklist; the model, the URL and the key are the
    gate's own, resolved through the same call the gate's profile is.

    A reply that is not the JSON answer is ``ok: False`` with the raw reply as
    the one problem, and a call that raises or comes back empty is the same: a
    reviewer that cannot answer refuses the ticket rather than letting it through.
    """
    if not isinstance(text, str) or not text.strip():
        return ReviewResult(False, ("the draft is empty",))
    reply = _call(review_call, build_review_prompt(brief, text))
    return _parse_review_reply(reply)


def _parse_review_reply(reply: str) -> ReviewResult:
    """A reply as a verdict; anything that is not the JSON answer is not ok.

    ``ok`` must be a real boolean, else the whole reply is the one problem — the
    reviewer answered, and it was not an answer. An approval needs no ``problems``:
    the reviewer is told to answer ``{"ok": true}`` when everything passes, and the
    missing or non-list ``problems`` on a refusal is not what an approval owes. A
    refusal without a list gets the reply itself as the problem, so the drafter
    always has one thing to work with.
    """
    raw = (reply or "").strip()
    payload = _json_object(raw)
    if not isinstance(payload, dict):
        return ReviewResult(False, (raw or "the reviewer answered nothing",))
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        return ReviewResult(False, (raw or "the reviewer answered nothing",))
    if ok:
        return ReviewResult(True, ())
    problems = payload.get("problems")
    if not isinstance(problems, list):
        return ReviewResult(False, (raw or "the reviewer answered nothing",))
    found = tuple(str(item).strip() for item in problems if str(item).strip())
    if not found:
        found = ("the reviewer refused it without naming a problem",)
    return ReviewResult(False, found)


def _json_object(text: str):
    """The JSON object in *text*, or `None` when there is no one to read.

    A bare object, one wrapped in a fence, or one sitting inside a sentence: the
    first that parses wins. Fence stripping re-uses `_FENCE_RE`, so the reply
    shape the draft's own maps use is accepted here too.
    """
    body = (text or "").strip()
    if not body:
        return None
    body = _FENCE_RE.sub(lambda match: match.group(1), body).strip()
    brace = body.find("{")
    candidates = (body,) if brace < 0 else (body, body[brace:])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            try:
                value, _end = json.JSONDecoder().raw_decode(candidate)
            except ValueError:
                continue
        if isinstance(value, dict):
            return value
    return None


def _review_rounds(text, brief, *, prompt, llm_call, review_call, rounds,
                   artifact, repo, tasks_dir, number) -> tuple:
    """`draft_ticket`'s review loop, as ``(text, problems)``.

    One review, then at most *rounds* rounds of "the problems go back to the
    drafter, the lint runs again, the review runs again". The first verdict that
    is ok — or the lint's problems, if a rework broke the ticket — stops the
    loop; the last refusal's problems are the ones the caller prints.
    """
    for attempt in range(rounds + 1):
        verdict = review_ticket(text, brief, review_call)
        if verdict.ok:
            return text, ()
        if attempt >= rounds:
            return text, verdict.problems
        text = _call(llm_call, _rework_prompt(prompt, text, list(verdict.problems)))
        problems = lint_ticket(text, artifact, repo=repo, tasks_dir=tasks_dir,
                               round_no=number)
        if problems:
            return text, problems
    return text, ("the review refused the ticket on every round",)


# ─────────────────────────────────────────────────────────────────────────────
# the draft
# ─────────────────────────────────────────────────────────────────────────────

def draft_ticket(brief, *, repo, llm_call, config=None, round_no=None, out=None,
                 collect_fn: Optional[Callable] = None, write: bool = True,
                 format_spec: str = None, review_call: Optional[Callable] = None,
                 review_rounds=None, commit: bool = False) -> DraftResult:
    """One brief, one collect run, one ticket — drafted, reviewed, committed.

    *llm_call* is a callable taking the prompt text and returning the draft — a
    fake in tests, `llm_call_for(config.draft_settings)` on the real path.
    *config* supplies the map budget and the review's round count; *round_no* the
    ticket number, else the next free one; *out* the file to write, else
    `epic-tasks/<NN>-<slug>.md`.

    *review_call* is the gate model's callable, KC-80's second step: with it the
    lint's clean ticket is judged against the brief and the checklist, and a
    refusal sends the problems back to the drafter, then the lint, then the
    review again, at most *review_rounds* rounds (else
    `[contest] draft_review_rounds`, else 3). Without it the ticket is exactly
    KC-79's: lint and stop. *review_rounds* may also be given outright, which
    wins over the config.

    *commit* is KC-80's third step: `commit_ticket` on `LEG_BRANCH` in *repo*.
    A repo with uncommitted tracked changes is refused before the first LLM call,
    because a draft that cannot be committed should not burn the calls to be
    refused.

    The order is fixed: collect, prompt, lint, one rework with the problem list
    appended, lint again, the review's rounds, write or refuse. A refusal prints
    its problem list and saves the last draft as `.rejected.md` next to the
    output — never written to `epic-tasks/` under a number that is supposed to be
    free, and never committed.
    """
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError("draft_ticket: the brief is empty")

    repo = Path(repo)
    tasks_dir = repo / TASKS_DIR
    collect_dir = repo / COLLECT_DIR
    budget = _as_budget(getattr(config, "draft_map_budget", DEFAULT_MAP_BUDGET)
                        if config is not None else DEFAULT_MAP_BUDGET)
    if review_rounds is None:
        review_rounds = _as_rounds(getattr(config, "draft_review_rounds",
                                           DEFAULT_REVIEW_ROUNDS)
                                   if config is not None else DEFAULT_REVIEW_ROUNDS)
    else:
        review_rounds = _as_rounds(review_rounds)
    number = int(round_no) if round_no is not None else None

    # The commit is what refuses a dirty repo, so the refusal is here and not at
    # the end: nothing is drafted and nothing is dialed for a repo the commit
    # cannot touch. Untracked files do not count — the ticket about to be written
    # is one, and so is everything the collect run left behind.
    if commit:
        refusal = _clean_refusal(repo)
        if refusal is not None:
            return DraftResult(path=None, rejected=True, problems=(refusal,),
                               number=number, commit=CommitResult(False, refusal))

    try:
        (collect_fn or run_collect)(repo)
    except Exception:  # noqa: BLE001 — a broken collect is "no collect data"
        pass

    maps = maps_text(collect_dir, budget)
    artifact = load_artifact(collect_dir)

    if number is None:
        number = next_round(tasks_dir)
    prompt = build_prompt(brief, maps, round_no=number, format_spec=format_spec,
                          sources=brief_sources(repo, brief, budget))

    text = _call(llm_call, prompt)
    problems = lint_ticket(text, artifact, repo=repo, tasks_dir=tasks_dir, round_no=number)

    if problems:
        text = _call(llm_call, _rework_prompt(prompt, text, problems))
        problems = lint_ticket(text, artifact, repo=repo, tasks_dir=tasks_dir, round_no=number)

    if not problems and review_call is not None:
        text, problems = _review_rounds(
            text, brief, prompt=prompt, llm_call=llm_call, review_call=review_call,
            rounds=review_rounds, artifact=artifact, repo=repo, tasks_dir=tasks_dir,
            number=number)

    target = Path(out) if out else tasks_dir / f"{number:02d}-{slug_for(title_of(text), round_no=number)}.md"
    if problems:
        rejected_path = _write(target.with_name(target.stem + REJECTED_SUFFIX), text) if write else None
        return DraftResult(path=None, rejected=True, problems=tuple(problems),
                           rejected_path=rejected_path, number=number)

    path = _write(target, text) if write else target
    commit_result = None
    if commit and path.is_file():
        commit_result = commit_ticket(repo, path)
        if not commit_result.ok:
            return DraftResult(
                path=None, rejected=True,
                problems=(f"{path} is written but not committed: {commit_result.message}",),
                number=number, commit=commit_result)
    return DraftResult(path=path, rejected=False, problems=(), number=number,
                       commit=commit_result)


def _call(llm_call: Callable, prompt: str) -> str:
    """One ask of *llm_call*; a raise or a non-string answer is an empty draft.

    An empty draft fails the lint for every header field and is refused like any
    other bad draft — a broken model call never raises out of the draft.
    """
    try:
        answer = llm_call(prompt)
    except Exception:  # noqa: BLE001 — the transport failed, the brief still gets a refusal
        return ""
    return str(answer or "").strip()


def _write(path: Path, text: str) -> Path:
    """*text* to *path*, creating its parent; the path it wrote."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = text if text.endswith("\n") else text + "\n"
    path.write_text(body, encoding="utf-8")
    return path


def llm_call_for(settings, system: str = DRAFT_SYSTEM_PROMPT,
                 timeout: float = DRAFT_TIMEOUT) -> Callable:
    """A callable for *settings* — `build_chat_request` + `request_completion`.

    The real path behind `contest draft`: the model, the URL and the key all come
    from the resolved profile, none of them from this module. A call that raises
    is left to the caller, which treats it as an empty draft.
    """
    api_format = str(getattr(settings, "api_format", "") or "openai")

    def ask(prompt: str) -> str:
        url, headers, payload = build_chat_request(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            api_format=api_format,
            temperature=float(settings.temperature),
            max_tokens=int(settings.max_tokens),
            system=system,
            user_msg=prompt,
            num_ctx=int(settings.num_ctx or 0),
            think=bool(settings.think),
            response_format=bool(settings.response_format),
            stream=False,
        )
        text = request_completion(
            url, headers, payload, timeout,
            api_format=api_format,
            ssl_context=(make_unverified_context()
                         if not getattr(settings, "verify_ssl", True) else None),
        )
        return strip_think(text) or ""

    return ask


# ─────────────────────────────────────────────────────────────────────────────
# the commit: the ticket on contest-legs, nothing else
# ─────────────────────────────────────────────────────────────────────────────

def _git(repo, *args) -> tuple:
    """`git <args>` in *repo* as `(returncode, stdout, stderr)` — never raises.

    Through `tools.git_run.run_git`, so a transient held index is waited out the
    way every other git call in the tree is. A git that cannot be run at all
    comes back as a non-zero code with the reason, so a broken environment is a
    refusal the operator can read, not an exception in the draft.
    """
    try:
        proc = run_git(("git", *args), cwd=str(repo))
    except (OSError, subprocess.TimeoutExpired):
        return 1, "", "git could not be run"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _dirty_paths(repo) -> Optional[list]:
    """The repo's uncommitted tracked paths, or `None` when that cannot be read.

    `--untracked-files=no` is the point: the ticket about to be written is
    untracked, and so is everything the collect run left, so neither counts as
    dirt. `None` is a git that failed, never a clean tree.
    """
    code, out, _err = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if code != 0:
        return None
    return [line for line in out.splitlines() if line.strip()]


def _clean_refusal(repo) -> Optional[str]:
    """The refusal for a repo the commit must not touch, or `None` when it is clean."""
    dirty = _dirty_paths(repo)
    if dirty is None:
        return f"{repo} cannot be read with git status — commit its tickets by hand"
    if dirty:
        shown = ", ".join(dirty[:5]) + (", …" if len(dirty) > 5 else "")
        return (f"{repo} has uncommitted changes ({shown}) — commit or stash them "
                "first, as 2legs/prepare_task.sh does")
    return None


def _repo_rel(repo, path) -> Optional[str]:
    """*path* relative to *repo* as a posix path, or `None` when it is outside it."""
    try:
        relative = Path(path).resolve().relative_to(Path(repo).resolve())
    except (ValueError, OSError):
        return None
    if str(relative) in (".", ""):
        return None
    return relative.as_posix()


def commit_ticket(repo, ticket_path, *, message=None, branch: str = LEG_BRANCH,
                  scripts_dir=None) -> CommitResult:
    """Commit *ticket_path* on *branch* in *repo* and nothing else.

    The order is the one `2legs/prepare_task.sh` uses: refuse a repo with
    uncommitted tracked changes, check out the branch creating it when missing,
    copy the two scripts the agents' prompt runs in when they are absent, and
    commit with the paths spelled out — never `-a`, so nothing the operator left
    in the tree rides along. The branch is left checked out, because
    `run --target REPO --base HEAD` needs the ticket at HEAD, and the operator's
    own branch keeps every commit it had.

    Fail-open throughout: a repo that is not a git repo, a branch that cannot be
    checked out, a script that cannot be copied and a commit that fails all come
    back as a refusal with the reason. Nothing is deleted, stashed or reset.
    """
    repo = Path(repo)
    ticket = Path(ticket_path)
    rel = _repo_rel(repo, ticket)
    if rel is None:
        return CommitResult(False, f"{ticket} is not inside {repo}")
    if not ticket.is_file():
        return CommitResult(False, f"{ticket} does not exist — nothing to commit")

    refusal = _clean_refusal(repo)
    if refusal is not None:
        return CommitResult(False, refusal)

    paths = [rel]
    source_dir = Path(scripts_dir) if scripts_dir is not None else RUNNER_SCRIPTS_DIR
    for rel_script in CONTEST_SCRIPTS:
        target = repo / rel_script
        if target.exists():
            continue
        source = source_dir / Path(rel_script).name
        if not source.is_file():
            return CommitResult(False,
                                f"{source} is missing — {rel_script} cannot be copied in")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        except OSError as exc:
            return CommitResult(False, f"cannot copy {rel_script} into {repo}: {exc}")
        paths.append(rel_script)

    code, _out, err = _git(repo, "rev-parse", "--verify", "--quiet",
                           f"refs/heads/{branch}")
    if code == 0:
        code, _out, err = _git(repo, "checkout", "-q", branch)
    else:
        code, _out, err = _git(repo, "checkout", "-q", "-b", branch)
    if code != 0:
        return CommitResult(False, f"cannot check out {branch} in {repo}: {err.strip()}")

    code, _out, err = _git(repo, "add", "--", *paths)
    if code != 0:
        return CommitResult(False,
                            f"cannot stage {', '.join(paths)} in {repo}: {err.strip()}")

    subject = message or f"contest: ticket {ticket.stem}"
    code, _out, err = _git(repo, "commit", "-q", "-m", subject, "--", *paths)
    if code != 0:
        return CommitResult(False,
                            f"cannot commit {', '.join(paths)} in {repo}: {err.strip()}")

    code, out, _err = _git(repo, "rev-parse", "HEAD")
    sha = out.strip() if code == 0 and out.strip() else None
    return CommitResult(True, f"{branch} holds {', '.join(paths)}", sha)

