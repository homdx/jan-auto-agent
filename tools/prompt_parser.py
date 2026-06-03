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


def _parse_via_regex(raw: str, source: str = "") -> Optional[ParsedPrompt]:
    """
    Attempts to break down the natural language prompt into structured tokens
    using high-predictability regex patterns. Returns None if a file path
    cannot be reliably extracted.
    """
    cleaned_raw = raw.strip()
    
    # 1. Extract File Path
    # Matches common relative/absolute path architectures ending with an extension
    file_match = re.search(r'(?:^|\s)(?:in\s+)?([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)\b', cleaned_raw)
    if not file_match:
        return None

    file_path = file_match.group(1)
    
    # Isolate remaining text to reduce token search spaces
    matched_segment = file_match.group(0)
    remainder = cleaned_raw.replace(matched_segment, " ").strip()

    # 2. Extract Target Type and Target Name
    target_name = ""
    target_type = "unknown"

    # Strategy A: Target explicit language declaration keywords
    decl_match = re.search(r'\b(def|class|function|func)\s+([A-Za-z_][\w]*)\b', remainder)
    if decl_match:
        kw, name = decl_match.group(1), decl_match.group(2)
        target_name = name
        target_type = "class" if kw == "class" else "function"
        remainder = remainder.replace(decl_match.group(0), " ")
    else:
        # Strategy B: Capture 'show <target> from' syntax variants
        from_match = re.search(r'\b(show|improve|explain|find|view|display)\s+([A-Za-z_][\w]*)\s+from\b', cleaned_raw, re.IGNORECASE)
        if from_match:
            target_name = from_match.group(2)
        else:
            # Strategy C: Fallback to the remaining single standalone identifier word.
            # Only accept it if the symbol actually exists somewhere in the source file
            # (def/class declaration or bare reference) to avoid grabbing prose words like
            # "bug" from "fix the bug in app.py".
            clean_rem = re.sub(r'\b(show|improve|explain|find|view|display|me|the|in|from|optimize|fix|refactor|correct|get|read|describe|understand|doc)\b', ' ', remainder, flags=re.IGNORECASE).strip()
            words = clean_rem.split()
            valid_identifiers = [w for w in words if re.match(r'^[A-Za-z_][\w]*$', w)]
            for candidate in valid_identifiers:
                # Accept only when the symbol is actually defined or referenced in source.
                if source and not re.search(
                    r'\b' + re.escape(candidate) + r'\b', source
                ):
                    continue
                target_name = candidate
                break

    # 3. Determine Intent Matrix
    intent = "show_and_improve"  # Default fallback condition

    normalized_remainder = remainder.lower()
    normalized_raw = cleaned_raw.lower()

    # Use word-boundary regex so a keyword like 'read' does not spuriously match
    # inside a file name such as 'README.md' or 'thread.py'.
    def _has_kw(words: list[str]) -> bool:
        pattern = re.compile(r'\b(?:' + '|'.join(re.escape(w) for w in words) + r')\b')
        return bool(pattern.search(normalized_remainder) or pattern.search(normalized_raw))

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