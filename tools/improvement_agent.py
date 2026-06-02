import json
import urllib.request
import logging

logger = logging.getLogger(__name__)

class ImprovementAgent:
    def __init__(self, model: str = "qwen2.5-14b-instruct", base_url: str = "http://localhost:1337/v1", api_key: str = "jan", timeout: int = 120):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def process(self, intent: str, context: dict) -> dict:
        """Generates analytical code evaluations and refactoring patterns."""
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        
        prompt = f"""You are a senior codebase refactoring agent. Optimize the target source code according to the requested intent action matrix.

Target Action Intent: {intent}

[TARGET MODULE BLOCK]
{context.get('target_block')}

[IMPORTS MAP]
{context.get('imports')}

[RESOLVED PROJECT CODE DEPENDENCIES]
{json.dumps(context.get('related_code'), indent=2)}

[SURROUNDING SCOPE LINES]
{context.get('context_lines')}

Return your analytical metrics strictly as a valid JSON block matching the structure below. Do not wrap it in prose outside the JSON formatting rules.

{{
  "explanation": "Provide a complete breakdown explaining how the module architecture behaves and where performance problems or inefficiencies occur.",
  "issues": [
    "First found performance issue or security vulnerability description",
    "Second identified structural code smell description"
  ],
  "improved_code": "Output the complete, fully refactored, optimized, production-ready version of the target code block code block here.",
  "changes": [
    "Detail code modification 1",
    "Detail code modification 2"
  ]
}}
"""
        try:
            req_payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3
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
            logger.error(f"ImprovementAgent processing thread failed: {e}")
            return {
                "explanation": f"Failed to execute local optimization pipeline context: {e}",
                "issues": ["Connection validation boundary errors detected."],
                "improved_code": "",
                "changes": []
            }