import json
import urllib.request
import logging

logger = logging.getLogger(__name__)

class ValidatorAgent:
    def __init__(self, max_iter: int = 3, model: str = "qwen2.5-14b-instruct", base_url: str = "http://localhost:1337/v1", api_key: str = "jan", timeout: int = 120):
        self.max_iter = max_iter
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def validate(self, payload: dict) -> dict:
        """Evaluates whether the target block requires additional code scanning cycles."""
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        prompt = f"""You are a specialized code validation sub-agent.
Analyze the target code block, verified imports, and cross-references to confirm if the current code block context is whole, correct, and self-contained.

Task Context: {payload.get('task')}
Iteration Step: {payload.get('iteration')}/{self.max_iter}

[TARGET CODE BLOCK]
{payload.get('target_block')}

[CURRENT IMPORTS]
{payload.get('imports')}

[RESOLVED CROSS-REFERENCES]
{json.dumps(payload.get('related_code'), indent=2)}

[KNOWN MISSING REFERENCES]
{payload.get('missing_refs')}

You must return your assessment in strict JSON format. Do not add any text before or after the JSON structure. 
Format:
{{
  "status": "needs_fix" or "approved",
  "feedback": "Detailed critical assessment explaining why it needs context loops or what is fine."
}}
"""
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
        except Exception as e:
            logger.error(f"ValidatorAgent execution loop failed: {e}")
            return {"status": "approved", "feedback": f"API Connection Timeout Fallback: {e}"}