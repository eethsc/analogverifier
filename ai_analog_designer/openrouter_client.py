import os
import re
import json
import time
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

def _load_env():
    env_paths = [".env", os.path.join(os.path.dirname(__file__), "..", ".env")]
    for p in env_paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip("'\"")
                            if k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass

_load_env()

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions")

def repair_truncated_json(raw: str) -> Optional[Dict[str, Any]]:
    """Attempts to repair and parse a truncated or malformed JSON string."""
    text = raw.strip()
    for trim_len in range(len(text), max(10, len(text) - 1000), -5):
        candidate = text[:trim_len].rstrip()
        if candidate.endswith(","):
            candidate = candidate[:-1].rstrip()
        
        # Balance quotes and brackets
        in_str = False
        escape = False
        stack = []
        for ch in candidate:
            if ch == '\\' and not escape:
                escape = True
                continue
            if ch == '"' and not escape:
                in_str = not in_str
            elif not in_str:
                if ch == '{':
                    stack.append('}')
                elif ch == '[':
                    stack.append(']')
                elif ch in ('}', ']'):
                    if stack and stack[-1] == ch:
                        stack.pop()
            escape = False

        if in_str:
            candidate += '"'
        closing = "".join(reversed(stack))
        try:
            return json.loads(candidate + closing)
        except Exception:
            continue
    return None

class OpenRouterClient:
    """
    Robust API client for OpenRouter supporting LLM reasoning, code synthesis,
    and structured JSON generation.
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "google/gemini-2.5-flash",
        base_url: str = OPENROUTER_BASE_URL
    ):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or OPENROUTER_API_KEY
        self.default_model = default_model
        self.base_url = base_url

    def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        response_format: Optional[Dict[str, str]] = None,
        retries: int = 3
    ) -> str:
        """Sends chat completion request to OpenRouter with automatic retries."""
        target_model = model or self.default_model
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        if response_format:
            payload["response_format"] = response_format

        data_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/eethsc/analogverifier",
            "X-Title": "AnalogVerifier-AI-Designer"
        }

        req = urllib.request.Request(self.base_url, data=data_bytes, headers=headers)

        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    return resp_data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                print(f"[!] OpenRouter HTTP Error ({e.code}) on attempt {attempt}: {err_body}")
                if attempt == retries:
                    raise RuntimeError(f"OpenRouter call failed after {retries} attempts: {e} | Body: {err_body}")
                time.sleep(2 * attempt)
            except Exception as e:
                print(f"[!] OpenRouter Network Error on attempt {attempt}: {e}")
                if attempt == retries:
                    raise
                time.sleep(2 * attempt)
        return ""

    def complete_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 8192
    ) -> Dict[str, Any]:
        """Requests structured JSON output and safely parses it with automatic truncated JSON repair."""
        json_sys_prompt = (system_prompt or "") + "\n\nCRITICAL: Respond ONLY with a valid JSON object. Do NOT enclose in markdown formatting or backticks."
        raw_text = self.complete(
            prompt,
            system_prompt=json_sys_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"}
        )
        
        # Strip markdown ```json ... ``` wrapper if model generated it
        cleaned = raw_text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Fallback 1: Extract first { ... }
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(cleaned[start:end+1])
                except json.JSONDecodeError:
                    pass

            # Fallback 2: Automated Truncated JSON Repair
            repaired = repair_truncated_json(cleaned)
            if repaired is not None:
                return repaired

            raise ValueError(f"Failed to parse JSON from LLM response:\n{raw_text}")
