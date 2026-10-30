"""
Web Circuit Reference Search Engine (Tavily + Exa Dual-Engine Integration).

Retrieves transistor-level circuit schematics, SPICE subcircuit macromodels,
manufacturer application notes, and design equations from the web to ground
Hermes Agent and Harness² synthesis when encountering complex or novel ICs.
"""
from __future__ import annotations

import os
import re
import json
import logging
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional

from ai_analog_designer.llm_client import _load_env

_load_env()

logger = logging.getLogger("WebCircuitReferenceSearch")

DEFAULT_EXA_API_KEY = "b7469f29-80ac-4134-9ad8-b7fbb77f1968"
DEFAULT_TAVILY_API_KEY = "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"


class WebCircuitReferenceSearcher:
    """
    Dual-engine web reference retriever combining:
      1. Tavily Search API (synthesized engineering answers + technical web snippets)
      2. Exa Neural Search API (semantic search across datasheets, SPICE repos, IEEE/app notes)
    """

    def __init__(
        self,
        tavily_api_key: Optional[str] = None,
        exa_api_key: Optional[str] = None,
        timeout_s: float = 12.0,
    ):
        _load_env()
        self.tavily_api_key = (
            tavily_api_key
            or os.environ.get("TAVILY_API_KEY")
            or DEFAULT_TAVILY_API_KEY
        ).strip()
        self.exa_api_key = (
            exa_api_key
            or os.environ.get("EXA_API_KEY")
            or DEFAULT_EXA_API_KEY
        ).strip()
        self.timeout_s = timeout_s
        self._cache: Dict[str, Dict[str, Any]] = {}

    def search_tavily(
        self,
        query: str,
        max_results: int = 4,
        search_depth: str = "basic"
    ) -> Dict[str, Any]:
        """Queries Tavily Search API for circuit design references and synthesized answers."""
        if not self.tavily_api_key:
            return {"provider": "tavily", "ok": False, "error": "Missing TAVILY_API_KEY", "results": []}

        payload = {
            "api_key": self.tavily_api_key,
            "query": query,
            "search_depth": search_depth,
            "include_answer": True,
            "include_raw_content": False,
            "max_results": max_results,
        }
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "AnalogVerifier-Hermes/2.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("results", []):
                results.append({
                    "source": "tavily",
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": (item.get("content") or "")[:1500],
                    "score": item.get("score", 0.0),
                })
            return {
                "provider": "tavily",
                "ok": True,
                "answer": data.get("answer") or "",
                "results": results,
            }
        except Exception as e:
            logger.warning(f"Tavily search error for '{query}': {e}")
            return {"provider": "tavily", "ok": False, "error": str(e), "answer": "", "results": []}

    def search_exa(
        self,
        query: str,
        num_results: int = 4
    ) -> Dict[str, Any]:
        """Queries Exa Neural Search API for deep technical documents and SPICE models."""
        if not self.exa_api_key:
            return {"provider": "exa", "ok": False, "error": "Missing EXA_API_KEY", "results": []}

        payload = {
            "query": query,
            "type": "auto",
            "numResults": num_results,
            "contents": {
                "text": {"maxCharacters": 2000},
                "highlights": {"numSentences": 3},
            },
        }
        req = urllib.request.Request(
            "https://api.exa.ai/search",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": self.exa_api_key,
                "Content-Type": "application/json",
                "User-Agent": "AnalogVerifier-Hermes/2.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            results = []
            for item in data.get("results", []):
                highlights = item.get("highlights") or []
                text = item.get("text") or ""
                snippet = "\n".join(highlights) if highlights else text[:1500]
                if text and snippet != text[:1500]:
                    snippet = f"{snippet}\n{text[:1000]}"
                results.append({
                    "source": "exa",
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": snippet.strip()[:1800],
                    "score": item.get("score", 0.0),
                })
            return {
                "provider": "exa",
                "ok": True,
                "results": results,
            }
        except Exception as e:
            logger.warning(f"Exa search error for '{query}': {e}")
            return {"provider": "exa", "ok": False, "error": str(e), "results": []}

    @staticmethod
    def _extract_spice_hints(texts: List[str]) -> List[str]:
        """Extracts any SPICE netlist blocks or model lines discovered in web snippets."""
        hints: List[str] = []
        for txt in texts:
            if not txt:
                continue
            for m in re.finditer(r'(\.subckt\b[\s\S]{10,1500}?\.ends\b[^\r\n]*)', txt, re.I):
                snippet = m.group(1).strip()
                if snippet not in hints:
                    hints.append(snippet)
            for m in re.finditer(r'(?m)^(\.model\s+[A-Za-z0-9_]+\s+(?:NPN|PNP|NMOS|PMOS|D|SW|VSWITCH)\b[^\r\n]*)', txt, re.I):
                model_line = m.group(1).strip()
                if model_line not in hints:
                    hints.append(model_line)
        return hints[:6]

    def search(
        self,
        query: str,
        exa_query: Optional[str] = None,
        max_results_per_engine: int = 4
    ) -> Dict[str, Any]:
        """
        Executes parallel Tavily and Exa searches, deduplicates URLs, extracts SPICE hints,
        and formats a ready-to-use engineering reference report.
        """
        cache_key = f"{query.strip().lower()}||{(exa_query or query).strip().lower()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        q_tavily = query.strip()
        q_exa = (exa_query or query).strip()

        tavily_res: Dict[str, Any] = {"ok": False, "answer": "", "results": []}
        exa_res: Dict[str, Any] = {"ok": False, "results": []}

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_tav = pool.submit(self.search_tavily, q_tavily, max_results_per_engine)
            f_exa = pool.submit(self.search_exa, q_exa, max_results_per_engine)
            try:
                tavily_res = f_tav.result(timeout=self.timeout_s + 2.0)
            except Exception as e:
                logger.warning(f"Tavily future timed out: {e}")
            try:
                exa_res = f_exa.result(timeout=self.timeout_s + 2.0)
            except Exception as e:
                logger.warning(f"Exa future timed out: {e}")

        seen_urls = set()
        combined_results: List[Dict[str, Any]] = []
        raw_texts: List[str] = []

        if tavily_res.get("answer"):
            raw_texts.append(tavily_res["answer"])

        for item in tavily_res.get("results", []) + exa_res.get("results", []):
            url = (item.get("url") or "").strip()
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            combined_results.append(item)
            raw_texts.append(item.get("snippet", ""))

        spice_hints = self._extract_spice_hints(raw_texts)

        # Format markdown summary for Agent & Harness² consumption
        md_lines = [f"### Web Circuit Design References (Query: `{q_tavily}`)"]
        if tavily_res.get("answer"):
            md_lines.append(f"**Synthesized Architectural Overview (Tavily)**:\n{tavily_res['answer']}\n")

        if spice_hints:
            md_lines.append("**Extracted SPICE Models / Subcircuit Snippets**:")
            for h in spice_hints:
                md_lines.append(f"```spice\n{h}\n```")

        if combined_results:
            md_lines.append("**Top Technical Sources (Tavily + Exa)**:")
            for idx, r in enumerate(combined_results[:6], 1):
                src_tag = r.get("source", "web").upper()
                title = r.get("title") or "Technical Reference"
                url = r.get("url") or ""
                snippet = re.sub(r'\s+', ' ', r.get("snippet") or "").strip()[:450]
                md_lines.append(f"{idx}. **[{src_tag}] {title}** ({url})\n   - {snippet}")

        formatted_markdown = "\n".join(md_lines)

        out = {
            "status": "SUCCESS" if (tavily_res.get("ok") or exa_res.get("ok")) else "PARTIAL_OR_FAILED",
            "query": q_tavily,
            "tavily_ok": bool(tavily_res.get("ok")),
            "exa_ok": bool(exa_res.get("ok")),
            "tavily_answer": tavily_res.get("answer", ""),
            "spice_hints": spice_hints,
            "references_count": len(combined_results),
            "references": combined_results[:8],
            "formatted_markdown": formatted_markdown,
        }
        self._cache[cache_key] = out
        return out

    def search_circuit_reference(
        self,
        ic_name: str,
        category: str = "",
        extra_query: str = "",
        failed_specs: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Builds specialized analog IC topology & SPICE reference queries for Tavily and Exa.
        """
        clean_ic = re.sub(r'_(?:VERIFIED|HERMES|TEST|DUT)$', '', ic_name, flags=re.I)
        cat_str = category.replace("_", " ") if category else "analog IC"
        spec_str = ""
        if failed_specs:
            spec_str = " " + " ".join(failed_specs[:3])

        q_tavily = (
            f"{clean_ic} {cat_str} internal circuit schematic transistor topology "
            f"SPICE model design stages {extra_query}{spec_str}"
        ).strip()
        q_exa = (
            f"{clean_ic} {cat_str} SPICE subcircuit netlist .subckt transistor level "
            f"equivalent schematic reference design {extra_query}"
        ).strip()

        return self.search(query=q_tavily, exa_query=q_exa, max_results_per_engine=4)
