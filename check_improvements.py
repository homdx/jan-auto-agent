#!/usr/bin/env python3
"""
check_improvements.py — compare N IMPROVEMENTS.md plans against known false
positives, and independently scan each plan's claims against the real source
tree for fabricated APIs / quotes / format claims.

WHY THIS VERSION EXISTS
------------------------
The original script only matched a task to a known false positive when their
`target_files` sets intersected. That has two concrete, confirmed failure
modes:

  1. MISSES real false positives that don't happen to share a target file
     with anything already catalogued in IMPROVEMENTS-FALSE.md — e.g. a task
     that invents a helper function (`_call_with_retry`), claims a file
     handles YAML/TOML when it only ever does JSON, or asks for something
     already implemented. None of these show up as a "known" FP on a fresh
     run, so file-overlap matching against old ground truth can't catch them.

  2. WRONGLY flags legitimate tasks as false positives when they merely
     touch the same file as an unrelated known FP. `target_files` overlap of
     as little as one file among several is treated as a full match with no
     check on *why* the FP was a false positive. A real bug (e.g. an
     unguarded `config.read()` in controller.py) targeting the same file as
     a previously-flagged hallucination about that file gets tarred with the
     same brush.

WHAT CHANGED
------------
  * N folders instead of a hardcoded two (`folders: list[Path]`).
  * Ground-truth matching now requires a configurable overlap RATIO
    (shared files / max(files on either side)), not "at least one file in
    common" — weak single-file coincidences are reported separately instead
    of silently auto-matched.
  * A task with NO target files can no longer be matched (or silently pass);
    if either side is missing target_files, matching falls back to a
    title-similarity check instead of returning "no match" unconditionally.
  * NEW: an independent `--repo` source-grounding pass that does not depend
    on IMPROVEMENTS-FALSE.md at all. For each task it checks:
      - backticked code-statement spans quoted as "the current problematic
        line" are actually present verbatim in the named target file
        (catches fabricated syntax-error quotes)
      - `ClassName.method_name` references resolve to a real class and a
        real method somewhere in the repo (catches invented APIs)
      - target files claimed to need YAML/TOML/XML handling actually
        import/parse those formats (catches format-mismatch hallucinations)
    This is a heuristic, not a proof — it flags candidates for human review,
    it does not replace it. It caught 3 of 4 confirmed false positives in
    manual review; it will not catch "this is already implemented" cases,
    which still need a human/LLM read of the target file.

Usage:
    python check_improvements.py FOLDER [FOLDER ...] [--repo PATH] [--false PATH] [--overlap-ratio 0.5]

Defaults:
    folders = [../test1, ../test2]   (only if none given, for backward compat)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# ─── Parsing ──────────────────────────────────────────────────────────────────

def _normalise_files(raw: str) -> frozenset[str]:
    paths = re.findall(r"`([^`]+)`", raw)
    return frozenset(p.strip() for p in paths if p.strip())


def parse_improvements(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(c(f"  WARNING: could not read {path}: {e}", YELLOW))
        return []

    tasks = []
    blocks = re.split(r"(?=^### AUTO-T)", text, flags=re.MULTILINE)
    for block in blocks:
        m = re.match(r"### (AUTO-T\S+): (.+)", block)
        if not m:
            continue
        task_id = m.group(1)
        title   = m.group(2).strip()

        loc_match = re.search(r"\*\*Location:\*\* `([^`]+)`", block)
        location = loc_match.group(1).strip() if loc_match else ""

        tf_match = re.search(r"\*\*Target files:\*\* (.+)", block)
        target_files = _normalise_files(tf_match.group(1)) if tf_match else frozenset()

        reason_match = re.search(r"\*\*Reason:\*\* (.+)", block)
        reason = reason_match.group(1).strip()[:200] if reason_match else ""

        instr_match = re.search(r"\*\*Instruction:\*\*\s*\n\n(.+)", block, re.S)
        instruction = instr_match.group(1).strip() if instr_match else ""

        tasks.append({
            "id":           task_id,
            "title":        title,
            "location":     location,
            "target_files": target_files,
            "reason":       reason,
            "instruction":  instruction,
        })

    if tasks:
        return tasks

    # AUTO-BUG (fixed): the block parser above only understands the
    # "### AUTO-Txx: title" heading format used by IMPROVEMENTS.md /
    # IMPROVEMENTS-FALSE.md. Curated ground-truth files
    # (GROUND-TRUTH.md, GROUND-TRUTH-GOOGLE.md, GROUND-TRUTH-NVIDIA.md)
    # use a *different* format — a "| ID | Location | Reason |" markdown
    # table under a "## Confirmed FALSE POSITIVE" heading. Previously
    # that meant parse_improvements() silently returned [] for every
    # ground-truth file, so grade_folder() always saw "0 true false
    # positives" and scored every agent-excluded task as a wrongly-
    # excluded FP (0% precision), regardless of what the ground truth
    # actually said. Fall back to the table parser before giving up.
    return _parse_ground_truth_table(text)


_GT_SECTION_RE = re.compile(
    r"^##\s*(?:Bucket\s*\d+\s*[—\-–]\s*)?.*FALSE POSITIVE.*?\n(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_GT_ROW_ID_RE = re.compile(r"^T(\d+)(?:/T(\d+))*$")


def _parse_ground_truth_table(text: str) -> list[dict]:
    """Parse the curated `| ID | Location | Reason |` table format used by
    GROUND-TRUTH*.md files under a '## Confirmed FALSE POSITIVE' heading,
    returning tasks in the same shape parse_improvements() produces so
    downstream code (matching, grading) doesn't need to know which format
    the file was written in. Rows under a differently-named section (e.g.
    '## Confirmed REAL') are intentionally not collected here — this
    function's job is to answer "which candidate IDs does the ground
    truth confirm as true false positives", not to catalogue everything.
    """
    section_match = _GT_SECTION_RE.search(text)
    if not section_match:
        return []

    tasks = []
    for line in section_match.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        m = _GT_ROW_ID_RE.match(cells[0])
        if not m:
            continue  # header row ("ID"), separator row ("----"), etc.

        ids = re.findall(r"T(\d+)", cells[0])
        location, reason = cells[1], cells[2]
        target_files = _normalise_files(location) | _normalise_files(reason)

        for num in ids:
            tasks.append({
                "id":           f"AUTO-T{num}",
                "title":        location,
                "location":     location,
                "target_files": target_files,
                "reason":       reason[:200],
                "instruction":  "",
            })
    return tasks


# ─── Ground-truth matching (fixed) ─────────────────────────────────────────────

def _title_similarity(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def find_false_positive(task: dict, known_fps: list[dict], overlap_ratio: float) -> tuple[dict | None, str]:
    """
    Returns (matched_fp_or_None, match_kind).

    match_kind is one of:
      "file_overlap"  — confident match: shared-file ratio >= overlap_ratio
      "weak_overlap"  — some file(s) shared but below the confidence
                         threshold; NOT auto-flagged as FP, reported separately
                         so a human can judge whether it's the same issue
      "title_fallback"— one/both sides have no target_files at all, so we
                         fell back to comparing titles; only counted as a
                         match above a fairly strict similarity bar
      ""              — no match
    """
    best_weak = None
    for fp in known_fps:
        if task["target_files"] and fp["target_files"]:
            shared = task["target_files"] & fp["target_files"]
            if shared:
                ratio = len(shared) / max(len(task["target_files"]), len(fp["target_files"]))
                if ratio >= overlap_ratio:
                    return fp, "file_overlap"
                if best_weak is None:
                    best_weak = fp
        elif not task["target_files"] or not fp["target_files"]:
            # AUTO-FIX: this used to require BOTH sides to have zero
            # target_files (`and`), contradicting the docstring's own
            # "one/both sides have no target_files at all" — a task with
            # no target files vs. a known-FP that does have some (or vice
            # versa) fell through this branch entirely and silently
            # matched nothing, even when the titles were identical.
            # Neither side has target files to compare — fall back to title.
            sim = _title_similarity(task["title"], fp["title"])
            if sim >= 0.6:
                return fp, "title_fallback"
    if best_weak is not None:
        return best_weak, "weak_overlap"
    return None, ""


# ─── Source-grounding scan (new, independent of IMPROVEMENTS-FALSE.md) ────────

_DOTTED_RE     = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*)\.([a-z_][A-Za-z0-9_]*)\b')
_PRIVATE_RE    = re.compile(r'\b(_[a-z][a-z0-9_]{3,})\b')
_BACKTICK_RE   = re.compile(r'`([^`]+)`')
_FILE_EXTS     = {"py", "md", "ini", "json", "yaml", "yml", "toml", "txt", "cfg", "log", "csv"}
_CREATION_CUES = re.compile(
    r'(define|custom|new (helper|method|function|class|exception)|introduce|create a|'
    r'add a (new|helper))',
    re.IGNORECASE,
)
_TRUNCATION_CUES = re.compile(r'truncat|incomplete|syntax error|cut off|cut-off', re.IGNORECASE)
_FORMAT_WORDS = {
    "yaml": ("yaml", "safe_load", "YAMLError"),
    "toml": ("toml", "tomllib"),
    "xml":  ("xml.etree", "ElementTree", "lxml"),
}

# ── Patterns confirmed real and recurring across multiple review rounds ────────
# These two catch the two most common systematic hallucination classes found
# in manual review: (1) claiming a config.getX() call can raise NoSectionError/
# NoOptionError when it already passes fallback= (155 of 180 getX() calls in
# this codebase already do — it is the dominant, expected style here), and
# (2) "hardening" a deliberately-bad code sample used as a test fixture for a
# static analyzer elsewhere in the repo (collect/dataflow.py & friends).

_SECTION_ERROR_CUES = re.compile(
    r'NoSectionError|NoOptionError|missing \[?\w+\]? section|no \[?\w+\]? section',
    re.IGNORECASE,
)
_CONFIG_GET_RE = re.compile(
    r"\.get(?:int|boolean|float)?\(\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"'][^)]*?\)"
)
_FIXTURE_PATH_MARKER = "fixtures"
_FIXTURE_DOC_CUES = re.compile(
    r"toy module|deliberately|negative case|control case|(?<!not )silently falls through",
    re.IGNORECASE,
)
_CRASH_CLAIM_CUES = re.compile(r'\bmay crash\b|\bcould crash\b|\bcan crash\b|\bmight crash\b', re.IGNORECASE)


def _looks_like_code_statement(span: str) -> bool:
    """Heuristic: is this backticked span an 'existing code line' quote,
    as opposed to a bare filename / identifier / config key?"""
    if "/" in span or span.split(".")[-1] in _FILE_EXTS:
        return False
    if span.strip() in ("if __name__ == '__main__':", 'if __name__ == "__main__":'):
        return False  # extremely common idiom, never worth flagging
    return bool(re.search(r"[=(]", span)) and " " in span


def _preceded_by_creation_cue(text: str, idx: int, window: int = 45) -> bool:
    return bool(_CREATION_CUES.search(text[max(0, idx - window):idx]))


def load_repo_texts(repo: Path) -> tuple[dict[str, str], str]:
    file_texts: dict[str, str] = {}
    for p in repo.rglob("*.py"):
        try:
            file_texts[str(p.relative_to(repo))] = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
    whole = "\n".join(file_texts.values())
    return file_texts, whole


def source_ground_task(task: dict, file_texts: dict[str, str], whole_repo: str) -> list[str]:
    flags: list[str] = []
    instruction = task["instruction"]
    target_text = "".join(file_texts.get(f, "") for f in task["target_files"])
    have_targets_on_disk = any(f in file_texts for f in task["target_files"])

    # 1. Quoted "existing code line" that claims to be truncated/a syntax error, but is
    #    actually a strict PREFIX of a real, complete line in the target — i.e. the file
    #    is fine and the claim is fabricated. (A plain substring check can't catch this:
    #    any prefix of real code is trivially "found" as a substring.)
    if have_targets_on_disk:
        for m in _BACKTICK_RE.finditer(instruction):
            span = m.group(1)
            if not _looks_like_code_statement(span):
                continue
            near_truncation_claim = bool(_TRUNCATION_CUES.search(
                instruction[max(0, m.start() - 60):m.end() + 60]))
            if span in target_text:
                # Exact match — fine, unless the surrounding claim says it's broken/truncated
                # while the target shows a longer complete continuation on the same line.
                if near_truncation_claim:
                    for line in target_text.splitlines():
                        if span in line and line.strip() != span.strip() and len(line) > len(span) + 3:
                            flags.append(
                                f"TRUNCATION_CLAIM_BUT_LINE_COMPLETE: quoted `{span}` as broken/"
                                f"truncated, but the target file's actual line is complete: "
                                f"`{line.strip()[:100]}`")
                            break
            else:
                flags.append(f"QUOTED_CODE_NOT_IN_TARGET: `{span}`")

    # 2. Invented APIs: Class.method / module.function references, and bare private
    #    helpers (_like_this), that don't exist anywhere in the repo — unless the
    #    instruction itself says to create/define them.
    seen = set()
    for m in _DOTTED_RE.finditer(instruction):
        cls, meth = m.group(1), m.group(2)
        if meth in _FILE_EXTS or cls in _FILE_EXTS:
            continue  # e.g. IMPROVEMENTS.md, ARCHITECTURE.md
        if _preceded_by_creation_cue(instruction, m.start()):
            continue
        key = f"{cls}.{meth}"
        if key in seen:
            continue
        seen.add(key)
        cls_exists = bool(re.search(rf"\b(class|def)\s+{re.escape(cls)}\b", whole_repo)) or cls in whole_repo
        meth_exists = bool(re.search(rf"\bdef\s+{re.escape(meth)}\b", whole_repo))
        if not cls_exists and not meth_exists:
            flags.append(f"UNKNOWN_API: {key} (neither '{cls}' nor method '{meth}' found anywhere in repo)")

    for m in _PRIVATE_RE.finditer(instruction):
        ident = m.group(1)
        if ident in seen or _preceded_by_creation_cue(instruction, m.start()):
            continue
        seen.add(ident)
        if not re.search(rf"\b{re.escape(ident)}\b", whole_repo):
            flags.append(f"UNKNOWN_IDENTIFIER: {ident} (referenced as existing, not found anywhere in repo)")

    # 3. Format-mismatch: instruction claims a format the target files never touch.
    if have_targets_on_disk:
        instr_lower = instruction.lower()
        for fmt, markers in _FORMAT_WORDS.items():
            if fmt in instr_lower and not any(m.lower() in target_text.lower() for m in markers):
                flags.append(f"FORMAT_MISMATCH: instruction mentions '{fmt}' but target file(s) show no {fmt} handling")

    # 4. Config section-crash claims where the actual call already has fallback=.
    #    ConfigParser.getX(section, key, fallback=X) never raises NoSectionError/
    #    NoOptionError regardless of whether the section exists — it returns X.
    if have_targets_on_disk and _SECTION_ERROR_CUES.search(instruction):
        for m in _CONFIG_GET_RE.finditer(target_text):
            section, key = m.group(1), m.group(2)
            call_text = target_text[m.start():m.end()]
            if "fallback" in call_text and (section in instruction or key in instruction):
                flags.append(
                    f"CONFIG_FALLBACK_ALREADY_SAFE: instruction warns of NoSectionError/"
                    f"NoOptionError near [{section}] '{key}', but the actual call already "
                    f"has fallback= — cannot raise on a missing section/option")
                break

    # 5. Target file is a deliberately-bad test fixture, not production code.
    for f in task["target_files"]:
        if _FIXTURE_PATH_MARKER in f.lower():
            head = file_texts.get(f, "")[:600]
            if _FIXTURE_DOC_CUES.search(head):
                flags.append(
                    f"TEST_FIXTURE_TARGET: {f} is a fixture whose docstring frames it as "
                    f"intentionally bad/uncovered — likely used as a control case for a "
                    f"static analyzer elsewhere; 'fixing' it may break that analyzer's tests")
                break

    # 6. "May crash downstream" claims — lower confidence: only checks that a named
    #    downstream call site isn't obviously unguarded. Does not trace full call
    #    graphs, so absence of a flag here is weak evidence, not proof of safety.
    if _CRASH_CLAIM_CUES.search(instruction):
        named_calls = set(m.group(0) for m in _DOTTED_RE.finditer(instruction))
        for call in named_calls:
            cls, meth = call.split(".", 1)
            func_body_match = re.search(rf"def\s+{re.escape(meth)}\s*\([^)]*\):(.*?)(?=\n    def |\nclass |\Z)",
                                         whole_repo, re.S)
            if func_body_match and ("fallback=" in func_body_match.group(1)
                                     or "try:" in func_body_match.group(1)
                                     or ".get(" in func_body_match.group(1)):
                flags.append(
                    f"DOWNSTREAM_ALREADY_GUARDED (low confidence): instruction claims {call} "
                    f"'may crash', but {meth}'s body already shows defensive handling — verify "
                    f"the actual call chain before trusting this task")

    return flags


# ─── Grading mode: agent's own IMPROVEMENTS-FALSE.md vs ground truth ──────────
#
# This is the actual workflow: IMPROVEMENTS.md in a folder is a FIXED candidate
# list. Different validate-plan runs (different agents/models) each write their
# own verdict into that same folder's IMPROVEMENTS-FALSE.md — which of the
# candidates THEY think are false positives. This mode grades that verdict
# against ground truth, rather than just scanning the candidate list in
# isolation.
#
# Ground truth, in priority order:
#   1. --ground-truth PATH given explicitly (applies to every folder)
#   2. folder/GROUND-TRUTH.md if present (per-folder curated answer key)
#   3. source-grounding scan used as an automatic proxy (lower confidence —
#      it only catches the failure patterns it knows about; a clean scan is
#      NOT proof a task is legitimate, just that no known red flag fired)

def _match_ground_truth_id(gt_task: dict, candidates: list[dict]) -> str | None:
    """A ground-truth entry should describe one of *candidates*. Match by ID
    first (the common case — ground truth built against this exact list),
    falling back to target-file overlap + title similarity for a ground
    truth file authored separately."""
    by_id = {c["id"]: c for c in candidates}
    if gt_task["id"] in by_id:
        return gt_task["id"]
    best, best_score = None, 0.0
    for c in candidates:
        if gt_task["target_files"] and c["target_files"]:
            shared = gt_task["target_files"] & c["target_files"]
            if not shared:
                continue
            ratio = len(shared) / max(len(gt_task["target_files"]), len(c["target_files"]))
            sim = _title_similarity(gt_task["title"], c["title"])
            score = ratio * 0.6 + sim * 0.4
            if score > best_score:
                best, best_score = c["id"], score
    return best if best_score >= 0.5 else None


def grade_folder(label: str, folder: Path, overlap_ratio: float,
                  ground_truth_path: Path | None,
                  file_texts: dict[str, str] | None, whole_repo: str) -> dict:
    print(f"\n{'='*76}")
    print(c(f"  {label}  ->  {folder}", BOLD + CYAN))
    print(f"{'='*76}")

    kept_path = folder / "IMPROVEMENTS.md"
    verdict_path = folder / "IMPROVEMENTS-FALSE.md"

    kept = parse_improvements(kept_path) if kept_path.exists() else []
    removed = parse_improvements(verdict_path) if verdict_path.exists() else []

    if not kept and not removed:
        print(c("  No candidate tasks found in IMPROVEMENTS.md or IMPROVEMENTS-FALSE.md.", YELLOW))
        return {}

    # AUTO-BUG (fixed): a real --validate-plan run *removes* rejected tasks
    # from IMPROVEMENTS.md and moves them into IMPROVEMENTS-FALSE.md — that
    # file pair is the split of ONE original candidate pool, not "the fixed
    # list" (IMPROVEMENTS.md alone) plus "some unrelated ground truth file"
    # (IMPROVEMENTS-FALSE.md). Grading against IMPROVEMENTS.md alone was
    # silently checking only the surviving ~25% of candidates and treating
    # the agent's own removals as invisible — reconstruct the full original
    # pool from both files, and take agent_false_ids directly from which
    # ids ended up in IMPROVEMENTS-FALSE.md (that IS the agent's verdict,
    # no separate "verdict file" concept needed).
    by_id: dict[str, dict] = {}
    for t in kept + removed:
        by_id.setdefault(t["id"], t)
    candidates = list(by_id.values())
    agent_false_ids: set[str] = {t["id"] for t in removed}
    has_verdict = bool(removed) or verdict_path.exists()

    if not has_verdict:
        print(c(f"  No IMPROVEMENTS-FALSE.md in {folder} yet — no agent verdict to grade. "
                f"Showing source-grounding only.", YELLOW))

    # Resolve ground truth
    gt_source = ""
    true_false_ids: set[str] = set()
    gt_path = ground_truth_path or (folder / "GROUND-TRUTH.md")
    if gt_path.exists():
        gt_source = str(gt_path)
        for gt in parse_improvements(gt_path):
            mid = _match_ground_truth_id(gt, candidates)
            if mid:
                true_false_ids.add(mid)
    elif file_texts is not None:
        gt_source = "source-grounding scan (no curated ground truth found)"
        for cand in candidates:
            if source_ground_task(cand, file_texts, whole_repo):
                true_false_ids.add(cand["id"])
    else:
        print(c("  No ground truth available (no GROUND-TRUTH.md and no --repo) — "
                "cannot grade, pass --repo or --ground-truth.", RED))
        return {}

    print(f"  Reconstructed pool: {len(kept)} kept + {len(removed)} removed = {len(candidates)} total candidates")
    print(f"  Ground truth source: {gt_source}  ({len(true_false_ids)} true false positives)")

    max_id = max(len(t["id"]) for t in candidates)
    tp = fp = fn = tn = 0
    for cand in candidates:
        agent_says_false = cand["id"] in agent_false_ids
        really_false = cand["id"] in true_false_ids
        if agent_says_false and really_false:
            tp += 1; tag = c("TP  (correctly excluded)      ", GREEN)
        elif agent_says_false and not really_false:
            fp += 1; tag = c("FP  (wrongly excluded — legit!)", RED + BOLD)
        elif not agent_says_false and really_false:
            fn += 1; tag = c("FN  (missed — kept a bad task) ", RED + BOLD)
        else:
            tn += 1; tag = c("TN  (correctly kept)           ", GREEN)
        if has_verdict:
            print(f"  {tag} {cand['id']:<{max_id}}  {cand['title'][:50]}")

    total = tp + fp + fn + tn
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall    = tp / (tp + fn) if (tp + fn) else float("nan")
    accuracy  = (tp + tn) / total if total else 0.0

    print()
    print(f"  {'-'*50}")
    print(f"  Total candidates       : {total}")
    if has_verdict:
        print(f"  TP (correctly excluded): {c(str(tp), GREEN)}")
        print(f"  FP (wrongly excluded)  : {c(str(fp), RED if fp else GREEN)}  <- real bugs the agent threw away")
        print(f"  FN (missed, kept in plan): {c(str(fn), RED if fn else GREEN)}  <- hallucinations that survived")
        print(f"  TN (correctly kept)    : {c(str(tn), GREEN)}")
        print(f"  Precision              : {c('n/a' if precision != precision else f'{precision:.0%}', MAGENTA)}"
              f"  (of what agent excluded, how much was really false)")
        print(f"  Recall                 : {c('n/a' if recall != recall else f'{recall:.0%}', MAGENTA)}"
              f"  (of real false positives, how many agent caught)")
        print(f"  Accuracy               : {c(f'{accuracy:.0%}', GREEN if accuracy >= 0.8 else RED)}")
    print(f"  {'-'*50}")

    return {"total": total, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "accuracy": accuracy,
            "has_verdict": has_verdict}


# ─── Colors ───────────────────────────────────────────────────────────────────

GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
MAGENTA = "\033[35m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def c(text, code): return f"{code}{text}{RESET}"


# ─── Report ───────────────────────────────────────────────────────────────────

def report_folder(label: str, md_path: Path, known_fps: list[dict], overlap_ratio: float,
                   file_texts: dict[str, str] | None, whole_repo: str) -> dict:
    print(f"\n{'='*76}")
    print(c(f"  {label}  ->  {md_path}", BOLD + CYAN))
    print(f"{'='*76}")

    if not md_path.exists():
        print(c(f"  ERROR: file not found: {md_path}", RED))
        return {"total": 0, "fp_included": 0, "legitimate": 0, "detection_rate": 0,
                "weak_overlaps": 0, "source_flags": 0}

    tasks = parse_improvements(md_path)
    if not tasks:
        print(c("  No tasks found.", YELLOW))
        return {"total": 0, "fp_included": 0, "legitimate": 0, "detection_rate": 0,
                "weak_overlaps": 0, "source_flags": 0}

    fp_included, legitimate, weak_overlaps = [], [], []
    matched_fp_ids = set()
    total_source_flags = 0

    max_id = max(len(t["id"]) for t in tasks)
    for task in tasks:
        match, kind = find_false_positive(task, known_fps, overlap_ratio)
        s_flags = source_ground_task(task, file_texts, whole_repo) if file_texts is not None else []
        total_source_flags += len(s_flags)

        if kind == "file_overlap" or kind == "title_fallback":
            fp_included.append((task, match))
            matched_fp_ids.add(match["id"])
            status = c("MISSED FP ", RED + BOLD)
        elif kind == "weak_overlap":
            weak_overlaps.append((task, match))
            legitimate.append(task)
            status = c("WEAK MATCH", YELLOW + BOLD)
        elif s_flags:
            legitimate.append(task)
            status = c("SUSPECT   ", MAGENTA + BOLD)
        else:
            legitimate.append(task)
            status = c("OK        ", GREEN)

        files = ", ".join(sorted(task["target_files"])) or "(none)"
        print(f"  {status} {task['id']:<{max_id}}  {task['title'][:45]:<45}  {files[:40]}")
        if kind in ("file_overlap", "title_fallback") and match["reason"]:
            print(c(f"             -> known FP ({kind}): {match['reason']}", RED))
        if kind == "weak_overlap":
            print(c(f"             -> shares a file with FP {match['id']} but below confidence "
                     f"threshold; review manually, not auto-flagged", YELLOW))
        for sf in s_flags:
            print(c(f"             -> source-grounding: {sf}", MAGENTA))

    total      = len(tasks)
    n_fp       = len(fp_included)
    n_ok       = len(legitimate)
    fp_pct     = n_fp / total * 100 if total else 0
    n_detected = len(known_fps) - len(matched_fp_ids)
    det_pct    = max(0.0, n_detected / len(known_fps) * 100) if known_fps else 0

    print()
    print(f"  {'-'*50}")
    print(f"  Total tasks            : {total}")
    print(f"  Legitimate tasks       : {c(str(n_ok), GREEN)}")
    print(f"  Known FPs matched      : {c(str(n_fp), RED if n_fp else GREEN)}  ({fp_pct:.0f}% of tasks)")
    print(f"  Weak overlaps (review) : {c(str(len(weak_overlaps)), YELLOW if weak_overlaps else GREEN)}")
    print(f"  Source-grounding flags : {c(str(total_source_flags), MAGENTA if total_source_flags else GREEN)}"
          f"  (new hallucination candidates, independent of ground truth)")
    if known_fps:
        print(f"  FP detection rate      : {c(f'{det_pct:.0f}%', GREEN if det_pct >= 80 else RED)}"
              f"  ({n_detected}/{len(known_fps)} known FPs excluded)")
    print(f"  {'-'*50}")

    return {"total": total, "fp_included": len(matched_fp_ids), "legitimate": n_ok,
            "detection_rate": det_pct, "weak_overlaps": len(weak_overlaps),
            "source_flags": total_source_flags}


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="*", default=["../test1", "../test2"],
                     help="Two or more folders, each containing an IMPROVEMENTS.md")
    ap.add_argument("--repo", type=str, default=None,
                     help="Path to the source repo, enables the source-grounding scan")
    ap.add_argument("--false", type=str, default=None,
                     help="Explicit path to IMPROVEMENTS-FALSE.md (auto-discovered in the "
                          "given folders otherwise). Legacy mode only, see --legacy.")
    ap.add_argument("--overlap-ratio", type=float, default=0.6,
                     help="Minimum shared-files ratio to auto-match a known FP (default 0.6). "
                          "Lower = more aggressive matching, higher false-match risk.")
    ap.add_argument("--legacy", action="store_true", default=False,
                     help="Use the old behavior: scan each folder's IMPROVEMENTS.md against "
                          "a single shared known-FPs ground truth file. Default is --grade "
                          "mode: each folder's own IMPROVEMENTS.md is a fixed candidate list "
                          "and its IMPROVEMENTS-FALSE.md is graded as an agent's verdict.")
    ap.add_argument("--ground-truth", type=str, default=None,
                     help="Curated answer key (IMPROVEMENTS-FALSE.md format) for grade mode. "
                          "Applies to every folder given. If omitted, each folder's own "
                          "GROUND-TRUTH.md is used if present, else the source-grounding scan "
                          "is used as a (lower-confidence) automatic ground truth.")
    args = ap.parse_args()

    folders = [Path(f) for f in args.folders]
    if len(folders) < 1:
        print(c("ERROR: need at least one folder.", RED))
        sys.exit(1)

    file_texts, whole_repo = (None, "")
    if args.repo:
        repo = Path(args.repo)
        if repo.exists():
            print(f"{c('Source-grounding against:', BOLD)} {repo}")
            file_texts, whole_repo = load_repo_texts(repo)
        else:
            print(c(f"WARNING: --repo path {repo} does not exist; skipping source-grounding.", YELLOW))

    if args.legacy:
        _run_legacy(folders, args, file_texts, whole_repo)
    else:
        _run_grade(folders, args, file_texts, whole_repo)


def _folder_display_keys(folders: list[Path]) -> dict:
    """Map each folder to a comparison-table key unique across *folders*.

    AUTO-FIX (bug 42): keying by folder.name alone let two folders with the
    same basename collide, silently dropping one agent from the comparison
    table. Falls back to the full path only when the basename repeats.
    """
    names = [f.name for f in folders]
    keys = {}
    for f, name in zip(folders, names):
        keys[f] = name if names.count(name) == 1 else str(f)
    return keys


def _run_grade(folders: list[Path], args, file_texts, whole_repo) -> None:
    ground_truth_path = Path(args.ground_truth) if args.ground_truth else None
    folder_keys = _folder_display_keys(folders)
    results = {}
    for f in folders:
        r = grade_folder(f"Folder ({f})", f, args.overlap_ratio, ground_truth_path,
                          file_texts, whole_repo)
        if r:
            results[folder_keys[f]] = r

    graded = {n: r for n, r in results.items() if r.get("has_verdict")}
    if len(graded) < 2:
        msg = "(Comparison table shown once 2+ folders have an IMPROVEMENTS-FALSE.md verdict to grade.)"
        print(f"\n{c(msg, YELLOW)}")
        return

    print(f"\n{'='*76}")
    print(c("  AGENT COMPARISON (grading their false-positive verdicts)", BOLD))
    print(f"{'='*76}")
    names = list(graded.keys())
    col_w = max(12, max(len(n) for n in names) + 2)
    header = "  {:<24}" + "".join(f" {{:>{col_w}}}" for _ in names)
    print(header.format("Metric", *names))
    print(f"  {'-'*(24 + (col_w + 1) * len(names))}")

    def row(label, key, fmt=str):
        print(header.format(label, *[fmt(graded[n][key]) for n in names]))

    row("Total candidates", "total")
    row("TP correctly excluded", "tp")
    row("FP wrongly excluded", "fp")
    row("FN missed (kept bad)", "fn")
    row("TN correctly kept", "tn")
    row("Precision", "precision", lambda v: "n/a" if v != v else f"{v:.0%}")
    row("Recall", "recall", lambda v: "n/a" if v != v else f"{v:.0%}")
    row("Accuracy", "accuracy", lambda v: f"{v:.0%}")
    print(f"  {'-'*(24 + (col_w + 1) * len(names))}")

    def score(n):
        r = graded[n]
        return -(r["accuracy"])

    ranked = sorted(names, key=score)
    if len(set(round(graded[n]["accuracy"], 4) for n in names)) > 1:
        print(f"\n  {c('Best agent:', BOLD)} {c(ranked[0], GREEN)}  (highest accuracy classifying "
              f"real false positives vs legitimate tasks)")
    else:
        print(f"\n  {c('Agents scored the same.', YELLOW)}")
    print()


def _run_legacy(folders: list[Path], args, file_texts, whole_repo) -> None:
    if len(folders) < 2:
        print(c("ERROR: legacy mode needs at least two folders.", RED))
        sys.exit(1)

    if args.false:
        false_path = Path(args.false)
        if not false_path.exists():
            false_path = None
    else:
        candidates = [f / "IMPROVEMENTS-FALSE.md" for f in folders]
        candidates += [Path(__file__).parent / "IMPROVEMENTS-FALSE.md", Path("IMPROVEMENTS-FALSE.md")]
        false_path = next((p for p in candidates if p.exists()), None)

    known_fps: list[dict] = []
    if false_path is not None:
        print(f"\n{c('Ground truth:', BOLD)} {false_path}")
        known_fps = parse_improvements(false_path)
        print(f"  Loaded {len(known_fps)} confirmed false positives.")
    else:
        print(c("\nNo IMPROVEMENTS-FALSE.md found — skipping ground-truth matching, "
                "relying on source-grounding scan only.", YELLOW))

    results = {}
    folder_keys = _folder_display_keys(folders)
    for f in folders:
        results[folder_keys[f]] = report_folder(f"Folder ({f})", f / "IMPROVEMENTS.md", known_fps,
                                                  args.overlap_ratio, file_texts, whole_repo)

    print(f"\n{'='*76}")
    print(c("  COMPARISON", BOLD))
    print(f"{'='*76}")
    names = list(results.keys())
    col_w = max(12, max(len(n) for n in names) + 2)
    header = "  {:<24}" + "".join(f" {{:>{col_w}}}" for _ in names)
    print(header.format("Metric", *names))
    print(f"  {'-'*(24 + (col_w + 1) * len(names))}")

    def row(label, key, fmt=str):
        print(header.format(label, *[fmt(results[n][key]) for n in names]))

    row("Total tasks", "total")
    row("Legitimate tasks", "legitimate")
    row("Known FPs matched", "fp_included")
    row("Weak overlaps", "weak_overlaps")
    row("Source-grounding flags", "source_flags")
    if known_fps:
        row("FP detection rate", "detection_rate", lambda v: f"{v:.0f}%")
    print(f"  {'-'*(24 + (col_w + 1) * len(names))}")

    def score(n):
        r = results[n]
        return (r["fp_included"] + r["source_flags"], -r["detection_rate"])

    ranked = sorted(names, key=score)
    if len(set(score(n) for n in names)) > 1:
        print(f"\n  {c('Better agent:', BOLD)} {c(ranked[0], GREEN)}  "
              f"(fewest combined known-FPs + source-grounding flags)")
    else:
        print(f"\n  {c('Agents scored the same.', YELLOW)}")
    print()


if __name__ == "__main__":
    main()
