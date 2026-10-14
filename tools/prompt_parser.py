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
    
    # 1. File path. A dotted symbol (MyClass.foo) can look like a file; prefer candidates marked with "in ".
    file_matches = list(re.finditer(r'(?:^|\s)(in\s+)?([A-Za-z0-9_./\\-]+\.[A-Za-z0-9]+)\b', cleaned_raw))
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
            # Strip only the target name from the remainder; the intent verb must stay so the keyword search sees it.
            remainder = re.sub(
                r'\b' + re.escape(target_name) + r'\b', ' ', remainder, count=1
            ).strip()
        else:
            # Strategy C: the single standalone identifier word, accepted only if it exists in the source.
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