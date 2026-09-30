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
reads in the maps. Nothing here commits, and nothing here moves a branch — KC-80
does that.

:func:`lint_ticket` is the mechanical half: pure, no LLM, one string per problem.
It is what decides between "write it" and "ask again once, then refuse".

The model is the one `[contest] draft_llm_profile` names in the contest config,
resolved through ``tools.auto.llm_profile.resolve_llm_profile`` exactly as
``gate_llm_profile`` is, and passed in as a callable: this module never holds a
model name, a URL or a key. An unset profile is a refusal that names the key.

Fail-open throughout: an absent or unreadable `.collect/`, an artifact that is
not a JSON object, a collect run that raises and a malformed budget all degrade
to "no collect data" and never raise into the draft. A collect with no artifact
still gets a ticket — it just cannot prove its own symbols.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tools.collect.cli import action_collect
from tools.llm_stream import (
    build_chat_request,
    make_unverified_context,
    request_completion,
    strip_think,
)

__all__ = [
    "ARTIFACT_FILENAME",
    "COLLECT_DIR",
    "DEFAULT_MAP_BUDGET",
    "HEADER_FIELDS",
    "MAP_FILES",
    "OPEN",
    "SECTIONS",
    "TASKS_DIR",
    "DraftResult",
    "build_prompt",
    "draft_ticket",
    "llm_call_for",
    "lint_ticket",
    "load_artifact",
    "maps_text",
    "next_round",
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
    "narrow it or turn it into a different one."
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
class DraftResult:
    """One draft attempt's outcome.

    ``path`` is the written ticket, or ``None`` when the lint refused both
    attempts; ``rejected_path`` is the last draft's ``.rejected.md`` copy, when
    the operator asked for the work to be written.
    """

    path: Optional[Path]
    rejected: bool
    problems: tuple
    rejected_path: Optional[Path] = None


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


def slug_for(title: str, fallback: str = "ticket") -> str:
    """The file name from a ticket title: lower-cased, dashes, no spaces."""
    words = re.sub(r"[^A-Za-z0-9]+", "-", (title or "").lower()).strip("-")
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


def build_prompt(brief: str, maps: list, *, round_no: int, format_spec: str = None) -> str:
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
# the draft
# ─────────────────────────────────────────────────────────────────────────────

def draft_ticket(brief, *, repo, llm_call, config=None, round_no=None, out=None,
                 collect_fn: Optional[Callable] = None, write: bool = True,
                 format_spec: str = None) -> DraftResult:
    """One brief, one collect run, at most two LLM calls, one ticket.

    *llm_call* is a callable taking the prompt text and returning the draft — a
    fake in tests, `llm_call_for(config.draft_settings)` on the real path.
    *config* supplies the map budget; *round_no* the ticket number, else the
    next free one; *out* the file to write, else `epic-tasks/<NN>-<slug>.md`.

    The order is fixed: collect, prompt, lint, one rework with the problem list
    appended, lint again, write or refuse. A second problem list is printed and
    the last draft saved as `.rejected.md` next to the output — never written to
    `epic-tasks/` under a number that is supposed to be free.
    """
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError("draft_ticket: the brief is empty")

    repo = Path(repo)
    tasks_dir = repo / TASKS_DIR
    collect_dir = repo / COLLECT_DIR
    budget = _as_budget(getattr(config, "draft_map_budget", DEFAULT_MAP_BUDGET)
                        if config is not None else DEFAULT_MAP_BUDGET)

    try:
        (collect_fn or run_collect)(repo)
    except Exception:  # noqa: BLE001 — a broken collect is "no collect data"
        pass

    maps = maps_text(collect_dir, budget)
    artifact = load_artifact(collect_dir)

    number = int(round_no) if round_no is not None else next_round(tasks_dir)
    prompt = build_prompt(brief, maps, round_no=number, format_spec=format_spec)

    text = _call(llm_call, prompt)
    problems = lint_ticket(text, artifact, repo=repo, tasks_dir=tasks_dir, round_no=number)

    if problems:
        second = prompt + "\n\n## Your draft\n\n" + text.strip() + _rework(text, problems)
        text = _call(llm_call, second)
        problems = lint_ticket(text, artifact, repo=repo, tasks_dir=tasks_dir, round_no=number)

    target = Path(out) if out else tasks_dir / f"{number:02d}-{slug_for(title_of(text))}.md"
    if problems:
        rejected_path = _write(target.with_name(target.stem + REJECTED_SUFFIX), text) if write else None
        return DraftResult(path=None, rejected=True, problems=tuple(problems),
                           rejected_path=rejected_path)

    path = _write(target, text) if write else target
    return DraftResult(path=path, rejected=False, problems=(), rejected_path=None)


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

