import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

class ValidatorAgent:
    def __init__(self, max_iter: int = 3):
        """
        Initializes the ValidatorAgent.
        :param max_iter: Maximum correction loops permitted before forcing an approval.
        """
        self.max_iter = max_iter

    def _call_llm(self, system_prompt: str, user_content: str) -> str:
        """
        Mock proxy for the actual LLM engine interface.
        Replace this placeholder with your production client logic (e.g., openai, anthropic, ollama).
        """
        # Example structured prompt transmission:
        # response = client.chat.completions.create(
        #     model="gpt-4o",
        #     messages=[
        #         {"role": "system", "content": system_prompt},
        #         {"role": "user", "content": user_content}
        #     ]
        # )
        # return response.choices[0].message.content.strip()
        
        return "APPROVED"

    def validate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validates code completeness and handles iteration loop guards.
        """
        iteration = payload.get("iteration", 1)

        # Guard: Stop infinite loop refinement matrices if iteration thresholds are reached
        if iteration >= self.max_iter:
            logger.info(f"ValidatorAgent loop guard triggered. Iteration: {iteration} >= MAX_ITER: {self.max_iter}")
            return {
                "status": "approved",
                "reason": "max iterations reached",
                "missing_names": []
            }

        # 1. Setup execution prompts
        system_prompt = (
            "You are a code completeness validator. Check: 1) Is the function body complete and "
            "not cut off? 2) Are all called names either in imports, related_code, or standard library? "
            "3) Any missing definitions that should be found? "
            "Reply exactly: APPROVED if complete. REJECTED: <one-line reason> | MISSING: name1, name2 if incomplete."
        )

        user_content = (
            f"Task Context: {payload.get('task', '')}\n\n"
            f"--- Target Code Block ---\n{payload.get('target_block', '')}\n\n"
            f"--- Declared Imports ---\n{payload.get('imports', [])}\n\n"
            f"--- Found Related Code References ---\n{payload.get('related_code', {})}\n\n"
            f"--- Explicitly Flagged Missing References ---\n{payload.get('missing_refs', [])}\n"
        )

        try:
            # 2. Query LLM Engine
            raw_response = self._call_llm(system_prompt, user_content).strip()
            
            # 3. Parse Response Grammar Matrix
            if raw_response.startswith("APPROVED"):
                return {
                    "status": "approved",
                    "reason": "",
                    "missing_names": []
                }
            
            if raw_response.startswith("REJECTED:"):
                # Isolate reason from missing metadata strings
                content = raw_response.replace("REJECTED:", "", 1).strip()
                reason = content
                missing_names: List[str] = []

                if "| MISSING:" in content:
                    parts = content.split("| MISSING:", 1)
                    reason = parts[0].strip()
                    
                    # Clean up trailing tokens into uniform arrays
                    raw_names = parts[1].split(",")
                    missing_names = [name.strip() for name in raw_names if name.strip()]

                return {
                    "status": "rejected",
                    "reason": reason,
                    "missing_names": missing_names
                }

            # Fallback for unexpected or unstructured LLM answers
            return {
                "status": "rejected",
                "reason": f"Unstructured validator response: {raw_response}",
                "missing_names": []
            }

        except Exception as e:
            logger.error(f"ValidatorAgent error: {e}", exc_info=True)
            return {
                "status": "rejected",
                "reason": f"Internal execution exception encountered: {str(e)}",
                "missing_names": []
            }