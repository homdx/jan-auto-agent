from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ParsedPrompt:
    file_path: str        # As written by user (relative or absolute)
    target_name: str      # Function/class name only, no keywords
    target_type: str      # "function" | "class" | "unknown"
    intent: str           # "show" | "improve" | "explain" | "show_and_improve" | "show_imports"
    raw: str              # Original prompt


#: Strategy A's declaration keywords. A keyword is what introduces the symbol, so it can never
#: be the symbol itself.
_DECLARATION_KEYWORDS = "def|class|function|func"

#: Strategy C's stopwords: the intent and filler words, plus the declaration keywords. Without the
#: keywords, a verified head that falls through (`the parse_data class represent`) would hand
#: `class` over as the target, because `class` does occur in the source (HARDEN-20 C).
_CANDIDATE_STOPWORDS = (
    "show|improve|explain|find|view|display|me|the|in|from|optimize|fix|refactor|correct"
    "|get|read|describe|understand|doc|" + _DECLARATION_KEYWORDS
)


#: Prose that stands where a symbol would be when there is no source to look the name up in (the
#: first, source-less pass of `main.py`, and a caller with no file): the question words and the
#: verbs a sentence puts after "the X function" — "what does the handle_request function return".
#: Applied only without a source; with one, the definition test is the judge and a symbol that is
#: really called `fail` or `work` is found as such.
_PROSE_WORDS = frozenset((
    "what", "how", "why", "when", "where", "which", "who", "does", "do", "did", "is", "are", "was",
    "were", "tell", "about", "this", "that", "these", "those", "it", "its", "can", "could", "would",
    "should", "will", "and", "or", "of", "to", "for", "with", "work", "works", "working", "return",
    "returns", "returning", "fail", "fails", "failing", "represent", "represents", "mean", "means",
    "raise", "raises", "throw", "throws", "take", "takes", "accept", "accepts", "compute", "computes",
    "behave", "behaves", "handle", "handles", "break", "breaks", "crash", "crashes", "wrong",
))
_PROSE_WORD_RE = re.compile(r"\b(?:" + "|".join(sorted(_PROSE_WORDS, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def _declaration_head_is_a_symbol(remainder: str, match: "re.Match[str]", source: str) -> bool:
    """True when Strategy A's head names a **symbol** rather than a verb (HARDEN-20 C).

    `parse_prompt`'s first strategy is `\\b(def|class|function|func)\\s+(name)\\b`, which takes
    whatever word follows the keyword and short-circuits the strategies that would find the
    real symbol, so "what does the handle_request function return" came out with
    `target_name="return"`. The match is therefore validated the way the later strategy
    validates a candidate: the name must be a symbol of *source*, or be written in the
    prompt as a code reference — followed straight away by `(` or `:`.

    A plain word search in the source is not the test here: a prose verb like `return` occurs
    in every file that returns anything, so the head would still be taken for the symbol. A
    definition position is what counts — `def name`, `class name`, `function name`,
    `func name`, or an assignment or annotation to it (`name =`, `name:`).

    With no *source* there is nothing to verify against, so the head is accepted as before:
    an unverifiable match is not a rejection.
    """
    name = re.escape(match.group(2))
    if not source:
        # Nothing to verify against, so the name cannot be looked up: a prose word after the keyword
        # ("function return", "class represent") is a verb, a code reference (`def f(`, `class A:`)
        # and any other word are taken as before.
        if re.match(r'[ \t]*[(:]', remainder[match.end():]):
            return True
        return match.group(2).lower() not in _PROSE_WORDS
    if match.group(1) in ("def", "func"):
        return True                      # a prefix keyword: the word after it is the name, defined yet or not
    if re.search(r'\b(?:def|class|function|func)\s+' + name + r'\b', source):
        return True
    if re.search(r'\b' + name + r'\s*[=:]', source):
        return True
    if re.match(r'[ \t]*[(:]', remainder[match.end():]):
        return True                      # `def handle_request(`, written as a code reference
    return False


def _parse_via_regex(raw: str, source: str = "") -> Optional[ParsedPrompt]:
    """
    Attempts to break down the natural language prompt into structured tokens
    using high-predictability regex patterns. Returns None if a file path
    cannot be reliably extracted.
    """
    cleaned_raw = raw.strip()
    
    # 1. File path. A dotted symbol (MyClass.foo) can look like a file; prefer candidates marked with "in ".
    # REP-PROMPT-PATH: a path in quotes or backticks ("in `a.py`", "in 'my file.py'") is the
    # whole quoted span, and "~/proj/a.py" / "C:\\proj\\a.py" are paths too — the backtick broke
    # the `(?:^|\s)` anchor, "~" and the drive colon were outside the class, and the prompt
    # fell to a chat answer with the file never read, or to the wrong "file.py".
    file_matches = list(re.finditer(r'(?:^|\s)(in\s+)?[`\'"]([^`\'"\n]+\.[A-Za-z0-9]+)[`\'"]', cleaned_raw))
    file_matches += list(re.finditer(
        r'(?:^|\s)(in\s+)?((?:~|[A-Za-z]:)?[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)\b', cleaned_raw))
    file_matches.sort(key=lambda m: m.start())
    if not file_matches:
        return None

    in_matches = [m for m in file_matches if m.group(1)]
    file_match = in_matches[0] if in_matches else file_matches[0]

    file_path = file_match.group(2)
    
    # Isolate remaining text to reduce token search spaces
    matched_segment = file_match.group(0)
    remainder = cleaned_raw.replace(matched_segment, " ").strip()

    # 2. Extract Target Type and Target Name
    target_name = ""
    target_type = "unknown"

    # Strategy A: Target explicit language declaration keywords. HARDEN-20 C: the head is
    # validated before it wins — a verb after the keyword (`... function return`) falls through
    # to the strategies that find the real symbol.
    decl_match = re.search(r'\b(' + _DECLARATION_KEYWORDS + r')\s+([A-Za-z_][\w]*)\b', remainder)
    if decl_match and _declaration_head_is_a_symbol(remainder, decl_match, source):
        kw, name = decl_match.group(1), decl_match.group(2)
        target_name = name
        target_type = "class" if kw == "class" else "function"
        remainder = remainder.replace(decl_match.group(0), " ")
    else:
        # Strategy B: Capture 'show <target> from' syntax variants
        from_match = re.search(r'\b(show|improve|explain|find|view|display)\s+([A-Za-z_][\w]*)\s+from\b', cleaned_raw, re.IGNORECASE)
        if from_match:
            target_name = from_match.group(2)
            # Strip only the target name from the remainder; the intent verb must stay so the keyword search sees it.
            remainder = re.sub(
                r'\b' + re.escape(target_name) + r'\b', ' ', remainder, count=1
            ).strip()
        else:
            # Strategy C: the single standalone identifier word, accepted only if it exists in the source.
            clean_rem = re.sub(r'\b(' + _CANDIDATE_STOPWORDS + r')\b', ' ', remainder, flags=re.IGNORECASE).strip()
            if not source:
                clean_rem = _PROSE_WORD_RE.sub(' ', clean_rem).strip()   # no source to check a word against
            words = [w.strip("`'\"") for w in clean_rem.split()]
            # REP-PROMPT-DOTTED: "Loader.load" is a target too, each part checked in the source;
            # it used to be dropped, and "show Loader.load in a.py" showed the file's imports.
            valid_identifiers = [w for w in words if re.match(r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$', w)]
            for candidate in valid_identifiers:
                # Accept only when the symbol is actually defined or referenced in source.
                if source and not all(
                    re.search(r'\b' + re.escape(part) + r'\b', source)
                    for part in candidate.split(".")
                ):
                    continue
                target_name = candidate
                break

    # 3. Determine Intent Matrix
    intent = "show_and_improve"  # Default fallback condition

    normalized_remainder = remainder.lower()

    # Keyword search runs on the remainder only, with word boundaries: the raw prompt would match names in file or symbol names.
    def _has_kw(words: list[str]) -> bool:
        pattern = re.compile(r'\b(?:' + '|'.join(re.escape(w) for w in words) + r')\b')
        return bool(pattern.search(normalized_remainder))

    has_show    = _has_kw(["show", "find", "view", "display", "get", "read"])
    has_improve = _has_kw(["improve", "fix", "refactor", "optimize", "correct"])
    has_explain = _has_kw(["explain", "describe", "understand", "doc"])

    if has_show and has_improve:
        intent = "show_and_improve"
    elif has_show:
        intent = "show"
    elif has_improve:
        intent = "improve"
    elif has_explain:
        intent = "explain"

    # 4. Enforce Empty Target Rules
    if not target_name:
        if intent in ("show", "show_and_improve"):
            intent = "show_imports"
        # For improve/explain with no named target, keep intent — whole file is the target

    return ParsedPrompt(
        file_path=file_path,
        target_name=target_name,
        target_type=target_type,
        intent=intent,
        raw=cleaned_raw
    )


def parse_prompt(raw: str, llm_fallback_fn: Optional[Callable[[str], ParsedPrompt]] = None, source: str = "") -> ParsedPrompt:
    """
    Main orchestrator endpoint for parsing user intents.
    Tries fast deterministic regex matching first; falls back to an LLM agent call if inconclusive.
    Pass `source` (file contents) so Strategy-C can verify a candidate symbol actually exists.
    """
    parsed = _parse_via_regex(raw, source=source)
    if parsed is not None:
        return parsed

    if llm_fallback_fn is not None:
        return llm_fallback_fn(raw)
        
    # Standard static fallback if regex completely fails to pull structural paths and no LLM runtime is wired
    return ParsedPrompt(
        file_path="",
        target_name="",
        target_type="unknown",
        intent="show_and_improve",
        raw=raw
    )