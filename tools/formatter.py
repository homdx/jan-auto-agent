from __future__ import annotations

import sys
from typing import Any, Dict, List, TYPE_CHECKING

# Prevent circular imports if ParsedPrompt type hints are resolved statically
if TYPE_CHECKING:
    from tools.prompt_parser import ParsedPrompt


class OutputFormatter:
    """
    TASK-08 — Final Output Formatter
    Handles formatting, visual presentation, and filtering of the pipeline outputs.
    """

    @staticmethod
    def format_time(seconds: float) -> str:
        """Converts raw float seconds into an MM:SS runtime stamp format."""
        mins, secs = divmod(int(max(0.0, seconds)), 60)
        return f"{mins:02d}:{secs:02d}"

    @classmethod
    def render(
        cls,
        parsed: ParsedPrompt,
        imports: List[str],
        block: str,
        search_result: Dict[str, Any],
        improvement: Dict[str, Any],
        elapsed_time: float,
        iteration: int,
        output_config: Dict[str, Any]
    ) -> None:
        """
        Renders the formatted evaluation report output to stdout.
        Respects section toggles and intent parameters.
        """
        # Extract individual presentation switches from configuration matrix
        show_timing = output_config.get("show_timing", True)
        show_iter = output_config.get("show_iteration_count", True)
        max_iterations = output_config.get("max_iterations", 3)

        divider = "────────────────────────────────────────"
        
        # 1. Structure the Top Header Metadata Banner
        target_prefix = ""
        if parsed.target_type == "function":
            target_prefix = "def "
        elif parsed.target_type == "class":
            target_prefix = "class "
            
        target_display = f"{target_prefix}{parsed.target_name}".strip()
        if not target_display:
            target_display = "File Imports Only"

        print(divider)
        print(f"Source: {parsed.file_path:<25} Target: {target_display}")
        
        # Build performance metrics evaluation matrix row
        metrics_line: List[str] = []
        if show_timing:
            metrics_line.append(f"Time:   {cls.format_time(elapsed_time)}")
        if show_iter:
            metrics_line.append(f"iter: {iteration}/{max_iterations}")
            
        if metrics_line:
            print("  |  ".join(metrics_line))
        print(divider)

        # 2. Render Gathered Project File Imports
        if imports:
            print(f"# IMPORTS (from {parsed.file_path})")
            for imp in imports:
                if imp.strip():
                    print(imp.strip())
            print()

        # 3. Render Captured Source Code Target Block
        if block and block.strip():
            print("# TARGET BLOCK")
            print(block.strip())
            print()

        # 4. Render Project Code Cross-References
        found_refs = search_result.get("found", {})
        if isinstance(found_refs, dict):
            for ref_name, ref_data in found_refs.items():
                if isinstance(ref_data, dict):
                    source_file = ref_data.get("file", "unknown_source")
                    ref_code = ref_data.get("code", "").strip()
                    if ref_code:
                        print(f"# REFERENCED FROM {source_file}")
                        print(ref_code)
                        print()

        # 5. Evaluate Intent Matrices Filtering Threshold Rules
        intent = parsed.intent

        # Section: EXPLANATION (Skipped for intent='show')
        if intent != "show":
            explanation_text = improvement.get("explanation", "").strip()
            if explanation_text:
                print("# EXPLANATION")
                print(explanation_text)
                print()

        # Sections: ISSUES, IMPROVED CODE, CHANGES (Skipped for intent='show' or intent='explain')
        if intent not in ("show", "explain"):
            # Sub-Section: ISSUES
            print("# ISSUES")
            issues = improvement.get("issues", [])
            if issues:
                for issue in issues:
                    clean_issue = str(issue).lstrip("-* ").strip()
                    if clean_issue:
                        print(f"- {clean_issue}")
            else:
                print("- No explicit issues identified.")
            print()

            # Sub-Section: IMPROVED CODE
            improved_code_text = improvement.get("improved_code", "").strip()
            if improved_code_text:
                print("# IMPROVED CODE")
                print(improved_code_text)
                print()

            # Sub-Section: CHANGES
            print("# CHANGES")
            changes = improvement.get("changes", [])
            if changes:
                for change in changes:
                    clean_change = str(change).lstrip("-* ").strip()
                    if clean_change:
                        print(f"- {clean_change}")
            else:
                print("- No modification entries logged.")
            print()

        print(divider)