"""Free web research using Agent-Reach backends (Jina Reader + Exa/DuckDuckGo).

Replaces OpenAI web_search_preview (~$0.03/call) with zero-cost search and page
reading. Backends (same as Agent-Reach):
  - Exa semantic search via mcporter (primary when Node/mcporter is installed)
  - DuckDuckGo HTML search (fallback, no API key, works on headless servers)
  - Jina Reader for reading result pages as markdown
"""
import json
import logging
import re
import shutil
import subprocess
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import requests

import config

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (compatible; TradingAgent/1.0)"
_JINA_BASE = "https://r.jina.ai/"
_DDG_HTML = "https://html.duckduckgo.com/html/"
_REQUEST_TIMEOUT = 20
_EXA_TIMEOUT = 45


def _mcporter_bin() -> str | None:
    """Return mcporter executable path, or None if not installed."""
    return shutil.which("mcporter")


def _clean_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://duckduckgo.com" + url
    return url


def _unwrap_ddg_redirect(url: str) -> str:
    """DuckDuckGo HTML results wrap outbound links."""
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path == "/l/":
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        if target:
            return unquote(target)
    return url


def parse_exa_text_results(text: str) -> list[dict]:
    """
    Parse Exa MCP text output into structured search hits.

    Exa returns blocks like:
      Title: ...
      URL: https://...
      Highlights:
      ...
      ---
      Title: ...
    """
    if not text.strip():
        return []

    blocks = re.split(r"\n---+\n", text)
    results = []
    for block in blocks:
        title_match = re.search(r"^Title:\s*(.+)$", block, re.M)
        url_match = re.search(r"^URL:\s*(\S+)$", block, re.M)
        if not url_match:
            continue
        title = title_match.group(1).strip() if title_match else url_match.group(1)
        url = url_match.group(1).strip()
        highlights_match = re.search(r"^Highlights:\s*\n(.*)", block, re.M | re.S)
        snippet = highlights_match.group(1).strip() if highlights_match else ""
        snippet = snippet[:500] + ("..." if len(snippet) > 500 else "")
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def search_web(query: str, max_results: int = 5) -> list[dict]:
    """
    Search the web. Tries Exa/mcporter first, then DuckDuckGo HTML.

    Returns list of {title, url, snippet}.
    """
    if not query.strip():
        return []

    exa_results = _search_exa_mcporter(query, max_results)
    if exa_results:
        return exa_results

    return _search_duckduckgo(query, max_results)


def _search_exa_mcporter(query: str, max_results: int) -> list[dict]:
    """Exa semantic search via mcporter (Agent-Reach primary search backend)."""
    if not config.EXA_SEARCH_ENABLED:
        return []

    mcporter = _mcporter_bin()
    if not mcporter:
        return []

    args_json = json.dumps({"query": query, "numResults": max_results})
    cmd = [
        mcporter,
        "call",
        "--http-url",
        config.EXA_MCP_URL,
        "--tool",
        "web_search_exa",
        "--args",
        args_json,
        "--output",
        "json",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_EXA_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Exa/mcporter search timed out")
        return []

    if proc.returncode != 0:
        logger.debug(f"Exa search failed (rc={proc.returncode}): {proc.stderr[:300]}")
        return []

    results = _parse_exa_mcporter_json(proc.stdout)
    if results:
        logger.info(f"Exa search ({len(results)} results): {query[:60]}")
    return results[:max_results]


def _parse_exa_mcporter_json(stdout: str) -> list[dict]:
    """Extract search hits from mcporter --output json response."""
    stdout = stdout.strip()
    if not stdout:
        return []

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        logger.debug("Exa response was not JSON; skipping")
        return []

    text_parts = []
    if isinstance(payload, dict):
        for item in payload.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))

    combined = "\n".join(text_parts)
    return parse_exa_text_results(combined)


def _search_duckduckgo(query: str, max_results: int) -> list[dict]:
    """DuckDuckGo HTML search — no API key, server-friendly fallback."""
    try:
        resp = requests.post(
            _DDG_HTML,
            data={"q": query},
            headers={"User-Agent": _UA},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning(f"DuckDuckGo search failed: {e}")
        return []

    results = []
    seen_urls = set()

    for match in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>',
        resp.text,
    ):
        raw_url = _clean_url(match.group(1))
        url = _unwrap_ddg_redirect(raw_url)
        title = unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        if not url.startswith("http") or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append({"title": title, "url": url, "snippet": ""})
        if len(results) >= max_results:
            break

    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>', resp.text, re.S)
    for i, snippet_html in enumerate(snippets[: len(results)]):
        snippet = unescape(re.sub(r"<[^>]+>", " ", snippet_html))
        results[i]["snippet"] = " ".join(snippet.split())

    logger.info(f"DuckDuckGo search ({len(results)} results): {query[:60]}")
    return results


def read_url(url: str, max_chars: int = 3500) -> str:
    """Read a URL as markdown via Jina Reader (Agent-Reach web backend)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    jina_url = f"{_JINA_BASE}{url}"
    try:
        resp = requests.get(
            jina_url,
            headers={"User-Agent": _UA, "Accept": "text/plain"},
            timeout=_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        text = resp.text.strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return text
    except requests.RequestException as e:
        logger.warning(f"Jina Reader failed for {url[:80]}: {e}")
        return ""


def research_query(
    query: str,
    max_results: int = 3,
    max_chars_per_page: int = 2000,
) -> str:
    """
    Search the web and read top results. Returns combined text for the trading agent.

    Drop-in replacement for OpenAI web_search_preview research calls.
    """
    results = search_web(query, max_results=max_results)
    if not results:
        return "No web search results found."

    sections = []
    for i, hit in enumerate(results, 1):
        title = hit.get("title", "Untitled")
        url = hit.get("url", "")
        snippet = hit.get("snippet", "")

        block = f"### Source {i}: {title}\nURL: {url}"
        if snippet:
            block += f"\nSnippet: {snippet}"

        page_text = read_url(url, max_chars=max_chars_per_page) if url else ""
        if page_text:
            block += f"\n\n{page_text}"
        elif snippet:
            block += "\n(Full page unavailable; using snippet only.)"
        else:
            continue

        sections.append(block)

    if not sections:
        return "Web search returned links but page content could not be retrieved."

    return "\n\n".join(sections)


def search_backend_status() -> dict:
    """Report which search backends are available (for logging/diagnostics)."""
    return {
        "exa_mcporter": bool(_mcporter_bin()) and config.EXA_SEARCH_ENABLED,
        "duckduckgo": True,
        "jina_reader": True,
    }
