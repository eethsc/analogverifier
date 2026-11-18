"""
MCP Client for the external `ieee-xplore-mcp` server over stdio.
Provides programmatic access to `search_papers`, `get_paper`, and `read_paper`
from Python using the official `mcp` SDK.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any

logger = logging.getLogger("IEEEMCPClient")


def _find_ieee_mcp_executable() -> str | None:
    return (
        os.environ.get("IEEE_MCP_BIN")
        or shutil.which("ieee-xplore-mcp.cmd")
        or shutil.which("ieee-xplore-mcp")
    )


async def _call_ieee_mcp_async(tool_name: str, arguments: dict[str, Any], ieee_api_key: str) -> Any:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    exe = _find_ieee_mcp_executable()
    if not exe:
        raise FileNotFoundError("ieee-xplore-mcp executable not found on PATH")

    env = dict(os.environ)
    if ieee_api_key:
        env["IEEE_API_KEY"] = ieee_api_key

    params = StdioServerParameters(command=exe, args=[], env=env)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            res = await session.call_tool(tool_name, arguments)
            chunks = []
            for block in getattr(res, "content", []) or []:
                text = getattr(block, "text", None)
                if text:
                    chunks.append(text)
            raw_text = "\n".join(chunks).strip()
            try:
                return json.loads(raw_text)
            except Exception:
                return raw_text


def call_ieee_mcp(tool_name: str, arguments: dict[str, Any], ieee_api_key: str = "") -> Any:
    """Synchronous wrapper to call an `ieee-xplore-mcp` tool."""
    return asyncio.run(_call_ieee_mcp_async(tool_name, arguments, ieee_api_key))


def search_papers_via_mcp(query: str, limit: int = 3, ieee_api_key: str = "") -> list[dict[str, Any]]:
    """Queries `ieee-xplore-mcp` `search_papers` and normalizes paper metadata."""
    raw = call_ieee_mcp("search_papers", {"query": query, "limit": limit}, ieee_api_key=ieee_api_key)
    items: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        items = raw.get("results") or raw.get("papers") or raw.get("articles") or []
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, str) and raw:
        # Some MCP servers return formatted text or markdown when non-JSON
        return [{
            "title": f"IEEE MCP Search Results for '{query}'",
            "venue": "IEEE Xplore (MCP)",
            "year": "",
            "authors": [],
            "doi": "",
            "url": "",
            "abstract": raw[:1600],
        }]

    papers: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        doi = item.get("doi") or ""
        authors = item.get("authors") or []
        if isinstance(authors, dict):
            authors = [a.get("full_name", "") for a in authors.get("authors", []) if a.get("full_name")]
        elif isinstance(authors, list):
            authors = [a.get("full_name", a) if isinstance(a, dict) else str(a) for a in authors[:3]]
        papers.append({
            "id": item.get("id") or item.get("article_number") or "",
            "title": item.get("title") or "Untitled IEEE Paper",
            "venue": item.get("venue") or item.get("publication_title") or "IEEE Xplore (MCP)",
            "year": str(item.get("year") or item.get("publication_year") or ""),
            "authors": authors[:3],
            "doi": doi,
            "url": item.get("url") or item.get("html_url") or (f"https://doi.org/{doi}" if doi else ""),
            "abstract": str(item.get("abstract") or "").strip()[:1600],
        })
    return papers
