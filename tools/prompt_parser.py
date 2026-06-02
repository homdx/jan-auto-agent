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


def _parse_via_regex(raw: str) -> Optional[ParsedPrompt]:
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
            # Strategy C: Fallback to the remaining single standalone identifier word
            clean_rem = re.sub(r'\b(show|improve|explain|find|view|display|me|the|in|from)\b', ' ', remainder, flags=re.IGNORECASE).strip()
            words = clean_rem.split()
            valid_identifiers = [w for w in words if re.match(r'^[A-Za-z_][\w]*$', w)]
            if valid_identifiers:
                target_name = valid_identifiers[0]

    # 3. Determine Intent Matrix
    intent = "show_and_improve"  # Default fallback condition
    
    normalized_remainder = remainder.lower()
    normalized_raw = cleaned_raw.lower()

    has_show = any(w in normalized_remainder or w in normalized_raw for w in ["show", "find", "view", "display", "get", "read"])
    has_improve = any(w in normalized_remainder or w in normalized_raw for w in ["improve", "fix", "refactor", "optimize", "correct"])
    has_explain = any(w in normalized_remainder or w in normalized_raw for w in ["explain", "describe", "understand", "doc"])

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
        intent = "show_imports"

    return ParsedPrompt(
        file_path=file_path,
        target_name=target_name,
        target_type=target_type,
        intent=intent,
        raw=cleaned_raw
    )


def parse_prompt(raw: str, llm_fallback_fn: Optional[Callable[[str], ParsedPrompt]] = None) -> ParsedPrompt:
    """
    Main orchestrator endpoint for parsing user intents.
    Tries fast deterministic regex matching first; falls back to an LLM agent call if inconclusive.
    """
    parsed = _parse_via_regex(raw)
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