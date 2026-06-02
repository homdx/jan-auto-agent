import os
import logging
from pathlib import Path
from typing import List, Dict, Any, Set, Optional

logger = logging.getLogger(__name__)

# Assuming these are implemented in TASK-01 and TASK-02
try:
    from utils.fs import list_py_files
    from utils.parser import extract_block
except ImportError:
    # Placeholders for linter/standalone structural reference
    def list_py_files(base_dir: str, skip_dirs: List[str] = None) -> List[str]: return []
    def extract_block(filepath: str, target_name: str) -> Optional[str]: return None


class SearchAgent:
    def __init__(self, max_file_kb: int = 500):
        self.max_file_kb = max_file_kb
        # Dependency injection for the LLM client could go here
        
    def _evaluate_with_llm(self, found_refs: Dict[str, Dict[str, str]]) -> List[str]:
        """
        Mock for the single-batch LLM call to filter out noise like stdlib wrappers.
        In production, this submits the keys and code snippets to the LLM and 
        returns a list of 'approved' reference names.
        """
        if not found_refs:
            return []
            
        # Example pseudo-implementation:
        # prompt = f"Analyze these code blocks and return a JSON list of names that are NOT just standard library wrappers: {found_refs}"
        # response = llm_client.generate(prompt)
        # return response.json_list
        
        return list(found_refs.keys())

    def run(
        self,
        references: List[str],
        base_dir: str,
        already_searched: Optional[List[str]] = None,
        file_ext_hint: str = ".py",
        visited_names: Optional[Set[str]] = None,
        current_depth: int = 0
    ) -> Dict[str, Any]:
        """
        Scans local files to find definitions of referenced names.
        Never raises exceptions; returns partial results on failure.
        """
        already_searched_set = set(already_searched or [])
        visited_names = visited_names or set()
        
        result = {
            "found": {},
            "not_found": [],
            "searched_files": []
        }

        # Guard: Max depth 2
        if current_depth > 2:
            logger.warning("SearchAgent hit max depth limit (2). Stopping recursion.")
            result["not_found"] = references
            return result

        try:
            base_path = Path(base_dir)
            if not base_path.exists() or not base_path.is_dir():
                result["not_found"] = references
                return result

            # 1. Gather Candidate Files
            raw_candidates = list_py_files(base_dir, skip_dirs=[".git", "__pycache__", "venv", ".tox"])
            valid_candidates = []
            
            for file_path in raw_candidates:
                p = Path(file_path)
                str_path = str(p)
                
                if file_ext_hint and p.suffix != file_ext_hint:
                    continue
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

        return result