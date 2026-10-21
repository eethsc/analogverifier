"""Backwards-compatibility shim. The real implementation is provider-agnostic: see llm_client.py."""
from .llm_client import LLMClient, repair_truncated_json, _load_env  # noqa: F401

OpenRouterClient = LLMClient
