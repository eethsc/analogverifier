"""
Provider-agnostic OpenAI-compatible chat client.

Supported providers (select with env LLM_PROVIDER):
  - "nim"        : NVIDIA NIM  (https://integrate.api.nvidia.com/v1), key NVIDIA_API_KEY
  - "openrouter" : OpenRouter  (https://openrouter.ai/api/v1),          key OPENROUTER_API_KEY

Features:
  - Streaming with a hard wall-clock timeout (NIM sometimes stalls for minutes)
  - Automatic model fallback chain on timeout / 5xx / 429
  - Per-call latency telemetry (TTFT, total, tokens) collected in LLMClient.call_log
"""
import os
import re
import json
import time
import threading
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional


def _load_env():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    for p in [".env", os.path.join(root, ".env")]:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip("'\"")
                            if k not in os.environ and v:
                                os.environ[k] = v
            except Exception:
                pass


_load_env()

def model_extra_body(provider: str, model: str) -> Dict[str, Any]:
    """Per-model request knobs. Reasoning ("thinking") is disabled by default for extraction/synthesis:
    on NIM it multiplies latency 5-30x (see benchmark_results/NIM_LATENCY_BENCH.md). Set LLM_THINKING=1 to enable."""
    if provider == "openrouter":
        body: Dict[str, Any] = {"usage": {"include": True}}       # returns exact USD cost per call
        if os.environ.get("LLM_THINKING") != "1":
            body["reasoning"] = {"enabled": False}
        return body
    if provider != "nim" or os.environ.get("LLM_THINKING") == "1":
        return {}
    m = model.lower()
    if m.startswith("nvidia/nemotron-3") or m.startswith("z-ai/glm") or m.startswith("qwen/"):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    if m.startswith("deepseek-ai/"):
        return {"chat_template_kwargs": {"thinking": False}}
    return {}


PROVIDERS = {
    "nim": {
        "base_url": lambda: os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/") + "/chat/completions",
        "key_env": "NVIDIA_API_KEY",
        # Chosen from tools/nim_latency_bench.py results (see benchmark_results/NIM_LATENCY_BENCH.md)
        "default_model": lambda: os.environ.get("LLM_MODEL", "nvidia/nemotron-3-ultra-550b-a55b"),
        "fallback_models": lambda: [m for m in os.environ.get(
            "LLM_FALLBACK_MODELS", "deepseek-ai/deepseek-v4.1-flash,z-ai/glm-5.3-flash").split(",") if m],
        "supports_json_mode": True,
    },
    "openrouter": {
        "base_url": lambda: os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions"),
        "key_env": "OPENROUTER_API_KEY",
        "default_model": lambda: os.environ.get("LLM_MODEL", "anthropic/claude-haiku-5.5"),
        "fallback_models": lambda: [m for m in os.environ.get(
            "LLM_FALLBACK_MODELS", "openai/gpt-6-luna,deepseek/deepseek-v4.1-flash").split(",") if m],
        "supports_json_mode": True,
    },
}


def repair_truncated_json(raw: str) -> Optional[Dict[str, Any]]:
    """Attempts to repair and parse a truncated or malformed JSON string."""
    text = raw.strip()
    for trim_len in range(len(text), max(10, len(text) - 1000), -5):
        candidate = text[:trim_len].rstrip()
        if candidate.endswith(","):
            candidate = candidate[:-1].rstrip()
        in_str, escape, stack = False, False, []
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
                elif ch in ('}', ']') and stack and stack[-1] == ch:
                    stack.pop()
            escape = False
        if in_str:
            candidate += '"'
        try:
            return json.loads(candidate + "".join(reversed(stack)))
        except Exception:
            continue
    return None


def _strip_think(text: str) -> str:
    """Removes <think>...</think> reasoning blocks some models emit inline."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


class LLMTimeout(Exception):
    pass


class LLMBudgetExceeded(Exception):
    pass


# Per-task model routing: tag prefix -> env var holding the model id (falls back to LLM_MODEL / provider default)
TASK_MODEL_ENV = [("hkg", "LLM_EXTRACT_MODEL"), ("synth", "LLM_SYNTH_MODEL"), ("tb", "LLM_TB_MODEL")]

_COST_LOCK = threading.Lock()
_SPENT_USD = [0.0]


def spent_usd() -> float:
    return _SPENT_USD[0]


# Process-wide cap on in-flight requests (NIM returns HTTP 429 above ~4-6 parallel calls per key).
_GLOBAL_SEM = threading.BoundedSemaphore(int(os.environ.get("LLM_MAX_CONCURRENCY", "4")))
_RATE_LOCK = threading.Lock()
_NEXT_SLOT = [0.0]


def _acquire_slot(min_interval_s: float):
    """Blocks until a concurrency slot is free and the minimum spacing between request starts has elapsed."""
    _GLOBAL_SEM.acquire()
    with _RATE_LOCK:
        now = time.monotonic()
        wait = _NEXT_SLOT[0] - now
        if wait > 0:
            time.sleep(wait)
        _NEXT_SLOT[0] = max(now, _NEXT_SLOT[0]) + min_interval_s


class LLMClient:
    """OpenAI-compatible chat completion client with streaming timeouts and model fallback."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        base_url: Optional[str] = None,
        provider: Optional[str] = None,
        request_timeout: Optional[float] = None,
        first_token_timeout: Optional[float] = None,
    ):
        self.provider = (provider or os.environ.get("LLM_PROVIDER") or
                         ("nim" if os.environ.get("NVIDIA_API_KEY") else "openrouter")).lower()
        cfg = PROVIDERS[self.provider]
        self.api_key = api_key or os.environ.get(cfg["key_env"], "")
        self.base_url = base_url or cfg["base_url"]()
        self.default_model = default_model or cfg["default_model"]()
        self.fallback_models = [m for m in cfg["fallback_models"]() if m != self.default_model]
        self.supports_json_mode = cfg["supports_json_mode"]
        self.request_timeout = float(request_timeout or os.environ.get("LLM_TIMEOUT_S", 240))
        self.first_token_timeout = float(first_token_timeout or os.environ.get("LLM_TTFT_TIMEOUT_S", 90))
        self.min_interval_s = float(os.environ.get("LLM_MIN_INTERVAL_S", 0.5))
        self.call_log: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _stream_call(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(payload, stream=True)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.provider == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/eethsc/analogverifier", "X-Title": "AnalogVerifier"})
        req = urllib.request.Request(self.base_url, data=json.dumps(payload).encode("utf-8"), headers=headers)

        t0 = time.perf_counter()
        ttft = None
        parts: List[str] = []
        usage = None
        finish_reason = None
        # socket timeout bounds the wait for each chunk (incl. first token)
        with urllib.request.urlopen(req, timeout=self.first_token_timeout) as resp:
            for raw in resp:
                now = time.perf_counter()
                if now - t0 > self.request_timeout:
                    raise LLMTimeout(f"total time exceeded {self.request_timeout:.0f}s")
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if obj.get("usage"):
                    usage = obj["usage"]
                for ch in obj.get("choices", []) or []:
                    d = ch.get("delta") or {}
                    piece = d.get("content") or ""
                    if (piece or d.get("reasoning_content") or d.get("reasoning")) and ttft is None:
                        ttft = now - t0
                    if piece:
                        parts.append(piece)
                    if ch.get("finish_reason"):
                        finish_reason = ch["finish_reason"]
        total = time.perf_counter() - t0
        text = "".join(parts)
        return {
            "text": text,
            "ttft_s": round(ttft if ttft is not None else total, 2),
            "total_s": round(total, 2),
            "completion_tokens": (usage or {}).get("completion_tokens") or len(text) // 4,
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "cost_usd": (usage or {}).get("cost"),
            "finish_reason": finish_reason,
        }

    def complete(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 8192,
        response_format: Optional[Dict[str, str]] = None,
        retries: int = 3,
        tag: str = "",
    ) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        if model is None:
            for prefix, env in TASK_MODEL_ENV:
                if tag.startswith(prefix) and os.environ.get(env):
                    model = os.environ[env]
                    break
        budget = float(os.environ.get("LLM_BUDGET_USD", "0") or 0)
        if budget and _SPENT_USD[0] >= budget:
            raise LLMBudgetExceeded(f"LLM budget exhausted: spent ${_SPENT_USD[0]:.3f} >= LLM_BUDGET_USD=${budget}")
        chain = [model or self.default_model] + [m for m in self.fallback_models if m != (model or self.default_model)]
        last_err = None
        for m in chain:
            for attempt in range(1, retries + 1):
                payload: Dict[str, Any] = {"model": m, "messages": messages,
                                           "temperature": temperature, "max_tokens": max_tokens}
                payload.update(model_extra_body(self.provider, m))
                if response_format and self.supports_json_mode:
                    payload["response_format"] = response_format
                t0 = time.perf_counter()
                _acquire_slot(self.min_interval_s)
                try:
                    res = self._stream_call(payload)
                    text = _strip_think(res["text"])
                    if not text.strip():
                        raise RuntimeError("empty completion")
                    self._log(tag, m, attempt, "ok", res)
                    return text
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8", errors="ignore")[:300]
                    last_err = f"HTTP {e.code}: {body}"
                    self._log(tag, m, attempt, f"http_{e.code}", {"total_s": round(time.perf_counter() - t0, 2)}, last_err)
                    if e.code == 400 and "response_format" in payload and response_format:
                        # some models reject json mode; retry without it
                        response_format = None
                        continue
                    if e.code in (401, 403, 404):
                        break  # model unavailable for this key -> next model
                    time.sleep((8 if e.code == 429 else 2) * attempt)
                except LLMBudgetExceeded:
                    raise
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                    self._log(tag, m, attempt, "timeout" if "timed out" in str(e).lower() or isinstance(e, LLMTimeout) else "error",
                              {"total_s": round(time.perf_counter() - t0, 2)}, last_err)
                    if isinstance(e, LLMTimeout) or "timed out" in str(e).lower():
                        break  # stalled model -> fall back immediately instead of retrying
                    time.sleep(2 * attempt)
                finally:
                    _GLOBAL_SEM.release()
        raise RuntimeError(f"LLM call failed on all models {chain}: {last_err}")

    def _log(self, tag, model, attempt, status, res, error=None):
        entry = {"ts": time.time(), "tag": tag, "provider": self.provider, "model": model, "attempt": attempt,
                 "status": status, **{k: v for k, v in res.items() if k != "text"}}
        if entry.get("cost_usd"):
            with _COST_LOCK:
                _SPENT_USD[0] += float(entry["cost_usd"])
            entry["spent_total_usd"] = round(_SPENT_USD[0], 4)
        if error:
            entry["error"] = error[:300]
        with self._lock:
            self.call_log.append(entry)
        cost = f" cost=${entry['cost_usd']:.4f} spent=${entry['spent_total_usd']:.3f}" if entry.get("cost_usd") else ""
        print(f"[LLM] {tag or 'call'} model={model} status={status} ttft={entry.get('ttft_s')}s "
              f"total={entry.get('total_s')}s tokens={entry.get('completion_tokens')}{cost}", flush=True)

    def latency_summary(self) -> Dict[str, Any]:
        ok = [c for c in self.call_log if c["status"] == "ok"]
        tot = sorted(c["total_s"] for c in ok)
        return {
            "calls": len(self.call_log),
            "ok": len(ok),
            "failed_or_timed_out": len(self.call_log) - len(ok),
            "total_llm_seconds": round(sum(c.get("total_s", 0) for c in self.call_log), 1),
            "median_s": tot[len(tot) // 2] if tot else None,
            "max_s": tot[-1] if tot else None,
            "cost_usd": round(sum(c.get("cost_usd") or 0 for c in self.call_log), 4),
            "by_tag": {t: round(sum(c["total_s"] for c in ok if c["tag"] == t), 1) for t in {c["tag"] for c in ok}},
        }

    # ------------------------------------------------------------------
    def complete_json(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        tag: str = "",
    ) -> Dict[str, Any]:
        json_sys_prompt = (system_prompt or "") + "\n\nCRITICAL: Respond ONLY with a valid JSON object. Do NOT enclose in markdown formatting or backticks."
        raw_text = self.complete(prompt, system_prompt=json_sys_prompt, model=model, temperature=temperature,
                                 max_tokens=max_tokens, response_format={"type": "json_object"}, tag=tag)
        return self.parse_json_text(raw_text)

    @staticmethod
    def parse_json_text(raw_text: str) -> Dict[str, Any]:
        cleaned = raw_text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    pass
            repaired = repair_truncated_json(cleaned[start:] if start != -1 else cleaned)
            if repaired is not None:
                return repaired
            raise ValueError(f"Failed to parse JSON from LLM response:\n{raw_text[:500]}")

    # Backwards-compat helpers used by extract_specs_from_prompt
    def chat_completion(self, messages: List[Dict[str, str]], temperature: float = 0.1, **kw) -> str:
        sys_p = "\n".join(m["content"] for m in messages if m["role"] == "system") or None
        user_p = "\n".join(m["content"] for m in messages if m["role"] != "system")
        return self.complete(user_p, system_prompt=sys_p, temperature=temperature, tag=kw.get("tag", "chat"))

    def parse_json_response(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            return self.parse_json_text(text)
        except Exception:
            return None
