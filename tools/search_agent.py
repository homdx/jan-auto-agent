import logging
from pathlib import Path
from typing import List, Dict, Any, Set, Optional

logger = logging.getLogger(__name__)

from tools.agent_trace import tracer

from tools.file_reader import list_py_files as _list_py_files, list_source_files as _list_source_files
from tools.block_extractor import extract_block as _extract_block_from_source
import json
from tools.llm_stream import (
    request_completion as _request_completion,
    ollama_chat_url as _ollama_chat_url,
    strip_think as _strip_think,
)

_FILTER_SNIPPET_CHARS = 800
_FILTER_SYSTEM = (
    "You are a code-reference noise filter. Given reference names and their "
    "code, return only the names that are genuine project-level dependencies, "
    "excluding standard-library wrappers and false positives. "
    "Respond with STRICT JSON: a list of approved names. No prose."
)


def list_py_files(base_dir: str, skip_dirs: List[str] = None) -> List[str]:
    """Delegates to tools.file_reader.list_py_files."""
    return _list_py_files(base_dir, skip_dirs or [])


def list_source_files(base_dir: str, skip_dirs: List[str] = None) -> List[str]:
    """Delegates to tools.file_reader.list_source_files (all supported languages)."""
    return _list_source_files(base_dir, skip_dirs or [])


def extract_block(filepath: str, target_name: str) -> Optional[str]:
    """
    Path-based wrapper that bridges the search_agent's 2-arg call site to the
    real 3-arg tools.block_extractor.extract_block(source, name, ext).
    Returns None if the file cannot be read or the block is not found.
    """
    try:
        ext = Path(filepath).suffix
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        result = _extract_block_from_source(source, target_name, ext)
        return result if result else None
    except Exception:
        return None


_DEFAULT_SKIP_DIRS = [
    ".git", "__pycache__", "venv", ".venv", ".tox",
    "node_modules", "dist", "build", ".mypy_cache", ".pytest_cache",
]


class SearchAgent:
    def __init__(
        self,
        max_file_kb: int = 500,
        skip_dirs: Optional[List[str]] = None,
        max_depth: int = 2,
        *,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: str = "",
        api_format: str = "openai",
        timeout: int = 120,
        ssl_context=None,
    ):
        self.max_file_kb = max_file_kb
        # None  → use the project-wide defaults
        # []    → skip nothing (caller explicitly wants no exclusions)
        # [...]  → use exactly what the caller passed
        self.skip_dirs: List[str] = _DEFAULT_SKIP_DIRS if skip_dirs is None else skip_dirs
        self.max_depth = max_depth
        # LLM noise-filter config. When model/base_url are absent the filter is
        # disabled and every reference is approved (backward-compatible no-LLM mode).
        self.model = model
        self.base_url = base_url.rstrip("/") if base_url else None
        self.api_key = api_key
        self.api_format = api_format
        self.timeout = timeout
        self.ssl_context = ssl_context
        
    def _evaluate_with_llm(self, found_refs: Dict[str, Dict[str, str]]) -> List[str]:
        """Filter discovered references down to genuine project-level dependencies.

        Submits the reference names and their code snippets to the LLM in a
        single batch and returns only the names the model approves (dropping
        stdlib wrappers and false-positive matches).

        Fail-open: if the LLM is not configured, or the call/parse fails for
        any reason, every found reference is approved — SearchAgent is a
        best-effort context gatherer and must never break the run.
        """
        if not found_refs:
            return []

        all_names = list(found_refs.keys())

        # No LLM wired in → approve everything (backward-compatible behaviour).
        if not self.model or not self.base_url:
            logger.debug(
                "_evaluate_with_llm: no LLM configured — approving all %d ref(s): %s",
                len(all_names), all_names,
            )
            return all_names

        # Build a single-batch prompt: name + a bounded code snippet per ref.
        blocks = []
        for name, data in found_refs.items():
            snippet = (data.get("code") or "")[:_FILTER_SNIPPET_CHARS]
            blocks.append(f"### {name}\n{snippet}")
        user_msg = (
            "Below are code references discovered in a project. Return ONLY the "
            "names that are genuine project-level dependencies worth including as "
            "context. EXCLUDE standard-library wrappers, trivial built-in usage, "
            "and false-positive matches.\n\n"
            "Reference names (you may only return names from this exact list):\n"
            f"{', '.join(all_names)}\n\n"
            + "\n\n".join(blocks)
            + '\n\nReturn STRICT JSON only: a list of approved names, e.g. '
              '["foo", "bar"]. No prose, no markdown fences.'
        )

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.api_format == "ollama":
            url = _ollama_chat_url(self.base_url)
            payload: Dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": _FILTER_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                "options": {"temperature": 0.0},
            }
        else:
            url = f"{self.base_url}/chat/completions"
            payload = {
                "model": self.model,
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": _FILTER_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
            }

        try:
            tracer.event("search_agent", "llm", "llm_request",
                         content=user_msg, model=self.model)
            raw = _request_completion(
                url, headers, payload, self.timeout,
                api_format=self.api_format, ssl_context=self.ssl_context,
            )
            cleaned = _strip_think(raw or "").strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```", 2)[1]
                if cleaned.lstrip().lower().startswith("json"):
                    cleaned = cleaned.lstrip()[4:]
            approved = json.loads(cleaned)
            if not isinstance(approved, list):
                raise ValueError(f"expected a JSON list, got {type(approved).__name__}")
            # Keep only names that were actually in the input (guard against
            # the model inventing names), preserving the original order.
            approved_set = {str(a) for a in approved}
            result = [n for n in all_names if n in approved_set]
            tracer.event("llm", "search_agent", "llm_response",
                         content=f"approved {len(result)}/{len(all_names)}: {result}")
            logger.info("_evaluate_with_llm: approved %d/%d reference(s)",
                        len(result), len(all_names))
            return result
        except Exception as exc:
            logger.warning(
                "_evaluate_with_llm: LLM filter failed (%s) — approving all "
                "%d ref(s) [fail-open]", exc, len(all_names),
            )
            return all_names

    def run(
        self,
        references: List[str],
        base_dir: str,
        already_searched: Optional[List[str]] = None,
        file_ext_hint: str = "",
        visited_names: Optional[Set[str]] = None,
        current_depth: int = 0
    ) -> Dict[str, Any]:
        """
        Scans local files to find definitions of referenced names.
        Never raises exceptions; returns partial results on failure.

        ``file_ext_hint`` is kept for backward compatibility but is no longer
        used as a hard filter — all extensions in ``_SEARCHABLE_EXTS`` are
        searched so non-Python projects receive context too.  Pass a non-empty
        value to *prefer* files with that extension (they are scanned first).
        """
        already_searched_set = set(already_searched or [])
        visited_names = visited_names or set()
        
        result = {
            "found": {},
            "not_found": [],
            "searched_files": []
        }

        # Guard: Max depth uses config value
        if current_depth > self.max_depth:
            logger.warning(
                "SearchAgent hit max depth limit (%d). Stopping recursion.",
                self.max_depth,
            )
            result["not_found"] = references
            return result

        try:
            base_path = Path(base_dir)
            if not base_path.exists() or not base_path.is_dir():
                result["not_found"] = references
                return result

            # 1. Gather Candidate Files — all supported source languages.
            # If file_ext_hint is set, preferred-extension files come first so
            # the most likely match is found quickly; others follow.
            raw_candidates = list_source_files(base_dir, skip_dirs=self.skip_dirs)
            if file_ext_hint:
                preferred = [f for f in raw_candidates if Path(f).suffix == file_ext_hint]
                others    = [f for f in raw_candidates if Path(f).suffix != file_ext_hint]
                raw_candidates = preferred + others
            valid_candidates = []

            for file_path in raw_candidates:
                p = Path(file_path)
                str_path = str(p)

                if str_path in already_searched_set:
                    continue

                # Guard: File size limit
                if p.exists() and (p.stat().st_size / 1024) <= self.max_file_kb:
                    valid_candidates.append(str_path)

            # 2. Scan for References
            refs_to_search = [r for r in references if r not in visited_names]
            found_raw: Dict[str, Dict[str, str]] = {}
            searched_this_run: Set[str] = set()

            for ref in refs_to_search:
                visited_names.add(ref)  # Guard: Prevent circular resolution
                ref_found = False

                for candidate_file in valid_candidates:
                    searched_this_run.add(candidate_file)
                    
                    code_block = extract_block(candidate_file, ref)
                    if code_block:
                        found_raw[ref] = {
                            "code": code_block,
                            "file": candidate_file
                        }
                        ref_found = True
                        break  # Stop at first match per reference

                if not ref_found:
                    result["not_found"].append(ref)

            result["searched_files"] = list(searched_this_run)

            # 3. LLM Noise Filtering (Single Batch Call)
            if found_raw:
                approved_refs = self._evaluate_with_llm(found_raw)
                
                for ref, data in found_raw.items():
                    if ref in approved_refs:
                        result["found"][ref] = data
                    else:
                        result["not_found"].append(ref)

        except Exception as e:
            # Guard: Never raise
            logger.error(f"SearchAgent encountered an error: {e}", exc_info=True)
            
            # Map any remaining unprocessed references to not_found to maintain state consistency
            processed_refs = set(result["found"].keys()).union(set(result["not_found"]))
            unprocessed = set(references) - processed_refs
            result["not_found"].extend(list(unprocessed))

        tracer.event("search_agent", "orchestrator", "result",
                     params={"found": list(result["found"].keys()),
                             "not_found": result["not_found"],
                             "searched_files": result["searched_files"]})
        return result

def make_search_agent(config, base_dir="."):
    """Factory: build a SearchAgent from a ConfigParser config (or return
    a default instance when config is None)."""
    if config is None:
        return SearchAgent()
    active  = config.get("api", "active", fallback="local")
    section = f"api_{active}"
    return SearchAgent(
        model=config.get(section, "model", fallback=None),
        base_url=config.get(section, "base_url", fallback=None),
        api_key=config.get(section, "api_key", fallback=""),
        api_format=config.get(section, "api_format", fallback="openai"),
    )
