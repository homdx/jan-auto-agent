import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ImprovementAgent:
    def __init__(self, temperature: float = 0.4, max_tokens: int = 2000):
        """
        Initializes the ImprovementAgent with parameters mapped from agents.ini.
        """
        self.temperature = temperature
        self.max_tokens = max_tokens

    def _call_llm(self, system_prompt: str, user_content: str) -> str:
        """
        Mock proxy for the actual LLM engine interface.
        Configured to apply self.temperature and self.max_tokens configurations.
        """
        # Example:
        # response = client.chat.completions.create(
        #     model="gpt-4o",
        #     temperature=self.temperature,
        #     max_tokens=self.max_tokens,
        #     messages=[...],
        # )
        # return response.choices[0].message.content
        return ""

    def _parse_list_items(self, block: str) -> List[str]:
        """Splits markdown bulleted text blocks into clean string arrays."""
        return [
            line.lstrip("-* ").strip() 
            for line in block.splitlines() 
            if line.strip()
        ]

    def _parse_structured_output(self, raw_text: str) -> Dict[str, Any]:
        """
        Slices the raw LLM string token cleanly based on deterministic string anchor 
        headers. Resilient against minor structural formatting shifts.
        """
        headers = ["EXPLANATION:", "ISSUES:", "IMPROVED CODE:", "CHANGES:"]
        positions = {h: raw_text.find(h) for h in headers}
        
        # Determine sorting order based on where headers fall in the response
        found_headers = sorted([h for h in headers if positions[h] != -1], key=lambda x: positions[x])
        
        extracted_sections: Dict[str, str] = {}
        for idx, current_header in enumerate(found_headers):
            start_pos = positions[current_header] + len(current_header)
            # Slice up until the start of the next chronological header or end of string
            end_pos = positions[found_headers[idx + 1]] if idx + 1 < len(found_headers) else len(raw_text)
            extracted_sections[current_header] = raw_text[start_pos:end_pos].strip()

        return {
            "explanation": extracted_sections.get("EXPLANATION:", "No explanation provided."),
            "issues": self._parse_list_items(extracted_sections.get("ISSUES:", "")),
            "improved_code": extracted_sections.get("IMPROVED CODE:", ""),
            "changes": self._parse_list_items(extracted_sections.get("CHANGES:", ""))
        }

    def process(self, intent: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Main routing endpoint for the Improvement Agent.
        """
        target_block = context.get("target_block", "")
        
        # Mode: show — Skip completely
        if intent == "show":
            logger.info("Intent is 'show'. Skipping optimization pipeline.")
            return {
                "status": "skipped",
                "raw_code": target_block
            }

        # Build Context String for LLM
        user_content = (
            f"Target Code Block:\n```python\n{target_block}\n```\n\n"
            f"Imports:\n{context.get('imports', [])}\n\n"
            f"Related Code Context:\n{context.get('related_code', {})}\n"
        )

        # Mode: explain
        if intent == "explain":
            system_prompt = (
                "You are an expert technical communicator. Analyze the provided code context "
                "and explain exactly what the code does, its architecture, and responsibilities. "
                "Do not make code modifications."
            )
            raw_response = self._call_llm(system_prompt, user_content)
            return {
                "status": "explained",
                "explanation": raw_response.strip()
            }

        # Modes: improve / show_and_improve
        if intent in ("improve", "show_and_improve"):
            system_prompt = (
                "You are an expert senior code quality agent. Analyze the provided code context, "
                "identify vulnerabilities, structural issues, or bugs, and generate an optimized version.\n\n"
                "You MUST return your output following this exact layout strictly:\n\n"
                "EXPLANATION:\n<what this code does in 2-3 sentences>\n\n"
                "ISSUES:\n- <issue 1>\n- <issue 2>\n\n"
                "IMPROVED CODE:\n<full improved function/class block here>\n\n"
                "CHANGES:\n- <change 1>\n- <change 2>"
            )
            raw_response = self._call_llm(system_prompt, user_content)
            
            parsed_data = self._parse_structured_output(raw_response)
            parsed_data["status"] = "improved"
            return parsed_data

        # Fallback condition for unknown intents
        return {
            "status": "unsupported_intent",
            "raw_code": target_block
        }