import json
import urllib.request
import urllib.error
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# STORY-2.1: Hardcoded prompt extracted to a named module-level constant.
# This is the canonical fallback that PromptStore will always be able to return to.
# Runtime values are injected via .format() in validate() — do not use f-string here.
VALIDATOR_PROMPT_HARDCODED = (
    "You are a specialized code validation sub-agent.\n"
    "Analyze the target code block, verified imports, and cross-references to confirm "
    "if the current code block context is whole, correct, and self-contained.\n"
    "\n"
    "Task Context: {task}\n"
    "Iteration Step: {iteration}/{max_iter}\n"
    "\n"
    "[TARGET CODE BLOCK]\n"
    "{target_block}\n"
    "\n"
    "[CURRENT IMPORTS]\n"
    "{imports}\n"
    "\n"
    "[RESOLVED CROSS-REFERENCES]\n"
    "{related_code}\n"
    "\n"
    "[KNOWN MISSING REFERENCES]\n"
    "{missing_refs}\n"
    "\n"
    "You must return your assessment in strict JSON format. Do not add any text before or after the JSON structure. \n"
    "Format:\n"
    "{{\n"
    '  "status": "needs_fix" or "approved",\n'
    '  "feedback": "Detailed critical assessment...",\n'
    '  "suggested_searches": ["list", "of", "missing", "module", "names", "or", "functions", "to", "find"]\n'
    "}}\n"
)


class ValidatorAgent:
    def __init__(
        self,
        max_iter: int = 3,
        model: str = "qwen2.5-14b-instruct",
        base_url: str = "http://localhost:1337/v1",
        api_key: str = "jan",
        timeout: int = 120,
        prompt_store=None,   # STORY-2.3: injected PromptStore (Optional[PromptStore])
    ):
        self.max_iter = max_iter
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.prompt_store = prompt_store  # None → always use hardcoded constant

    def validate(self, payload: dict) -> dict:
        """Evaluates whether the target block requires additional code scanning cycles."""
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        # STORY-2.3: pull prompt dynamically at call time so any push()/rollback()
        # takes effect on the very next pipeline run with zero code change.
        template = (
            self.prompt_store.get_current("validator_agent")
            if self.prompt_store is not None
            else VALIDATOR_PROMPT_HARDCODED
        )

        # Bug #5 fix: build prompt inside try so a malformed candidate template
        # (stray braces / missing placeholders) is caught rather than aborting the run.
        try:
            prompt = template.format(
                task=payload.get("task"),
                iteration=payload.get("iteration"),
                max_iter=self.max_iter,
                target_block=payload.get("target_block"),
                imports=payload.get("imports"),
                related_code=json.dumps(payload.get("related_code"), indent=2),
                missing_refs=payload.get("missing_refs"),
            )
        except (KeyError, ValueError) as fmt_err:
            logger.error(
                "ValidatorAgent: prompt template has invalid placeholders (%s) — "
                "rolling back to hardcoded prompt for this call", fmt_err
            )
            prompt = VALIDATOR_PROMPT_HARDCODED.format(
                task=payload.get("task"),
                iteration=payload.get("iteration"),
                max_iter=self.max_iter,
                target_block=payload.get("target_block"),
                imports=payload.get("imports"),
                related_code=json.dumps(payload.get("related_code"), indent=2),
                missing_refs=payload.get("missing_refs"),
            )

        try:
            req_payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            }
            req = urllib.request.Request(url, data=json.dumps(req_payload).encode("utf-8"), headers=headers, method="POST")

            # Use the dynamic timeout from agents.ini
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw_res = json.loads(response.read().decode("utf-8"))
                content = raw_res["choices"][0]["message"]["content"].strip()

                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()

                return json.loads(content)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"ValidatorAgent HTTP {e.code}: {body}")
            # Bug #7 fix: errors must NOT be treated as approved — use needs_fix.
            # _api_error sentinel lets prompt_evaluator exclude this from scoring.
            return {"status": "needs_fix", "feedback": f"HTTP {e.code} from API: {body}", "_api_error": True}
        except Exception as e:
            logger.error(f"ValidatorAgent execution loop failed: {e}")
            # Bug #7 fix: same — fail-closed, not fail-open.
            return {"status": "needs_fix", "feedback": f"API Connection Timeout Fallback: {e}", "_api_error": True}
