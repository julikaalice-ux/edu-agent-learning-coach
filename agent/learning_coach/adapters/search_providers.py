"""HTTP search adapters; provider selection belongs to the composition root."""
import asyncio
import json
import os
import re
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from learning_coach.service.ports import SearchProvider

MAX_RESPONSE_BYTES = 1_048_576
HTTP_TIMEOUT = 8
PROVIDER_TIMEOUT = 20
PROVIDER_HOSTS = {"zh.wikipedia.org", "en.wikipedia.org", "api.tavily.com", "api.bocha.cn"}

class RetrievalError(Exception):
    """Safe public code, never a server response or credential."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RetrievalError("provider_redirect_rejected")


def _fetch_json(request: Request) -> dict:
    """Fetch only fixed API endpoints. Do not follow result URLs or redirects."""
    parsed = urlsplit(request.full_url)
    if parsed.scheme != "https" or parsed.hostname not in PROVIDER_HOSTS:
        raise RetrievalError("provider_endpoint_rejected")
    request.add_header("User-Agent", "LearningCoachResearch/1.0 (educational prototype)")
    request.add_header("Accept", "application/json")
    started = time.monotonic()
    with build_opener(_NoRedirect()).open(request, timeout=HTTP_TIMEOUT) as response:
        chunks, size = [], 0
        while True:
            chunk = response.read(min(16384, MAX_RESPONSE_BYTES + 1 - size))
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise RetrievalError("provider_response_too_large")
            if time.monotonic() - started > PROVIDER_TIMEOUT:
                raise RetrievalError("provider_timeout")
            if not chunk:
                break
            chunks.append(chunk)
    result = json.loads(b"".join(chunks).decode("utf-8"))
    if not isinstance(result, dict) or "error" in result:
        raise RetrievalError("provider_invalid_response")
    return result


class WikipediaProvider:
    """No key needed. All Wikipedia language editions count as one publisher."""

    async def search(self, query: str) -> list[dict]:
        language = "zh" if re.search(r"[\u3400-\u9fff]", query) else "en"
        endpoint = f"https://{language}.wikipedia.org/w/api.php"
        params = {"action": "query", "format": "json", "list": "search",
                  "srsearch": query[:200], "srnamespace": 0, "srlimit": 3,
                  "srprop": "", "utf8": 1}
        result = await asyncio.to_thread(_fetch_json, Request(endpoint + "?" + urlencode(params)))
        hits = result.get("query", {}).get("search", [])
        ids = [str(hit["pageid"]) for hit in hits[:3]
               if isinstance(hit, dict) and type(hit.get("pageid")) is int and hit["pageid"] > 0]
        if not ids:
            return []
        params = {"action": "query", "format": "json", "pageids": "|".join(ids),
                  "prop": "extracts|info", "inprop": "url", "explaintext": 1,
                  "exintro": 1, "exchars": 1200, "exlimit": 3}
        result = await asyncio.to_thread(_fetch_json, Request(endpoint + "?" + urlencode(params)))
        pages = result.get("query", {}).get("pages", {})
        sources = []
        for page in list(pages.values())[:3]:
            if not isinstance(page, dict) or str(page.get("pageid")) not in ids:
                continue
            url = page.get("fullurl", "")
            if isinstance(url, str) and urlsplit(url).hostname == f"{language}.wikipedia.org":
                sources.append({"title": page.get("title", ""), "url": url,
                                "excerpt": page.get("extract", ""), "excerpt_kind": "page_extract"})
        return sources


class TavilyProvider:
    def __init__(self, api_key: str):
        self._api_key = api_key

    async def search(self, query: str) -> list[dict]:
        if not self._api_key:
            raise RetrievalError("provider_not_configured")
        body = {"query": query[:200], "search_depth": "basic", "max_results": 3,
                "include_answer": False, "include_raw_content": "text",
                "include_images": False, "auto_parameters": False, "safe_search": True}
        request = Request("https://api.tavily.com/search", data=json.dumps(body).encode(),
                          headers={"Authorization": f"Bearer {self._api_key}",
                                   "Content-Type": "application/json"}, method="POST")
        result = await asyncio.to_thread(_fetch_json, request)
        sources = []
        for item in result.get("results", [])[:3]:
            if not isinstance(item, dict):
                continue
            raw = item.get("raw_content")
            has_raw = isinstance(raw, str) and bool(raw.strip())
            sources.append({"title": item.get("title", ""), "url": item.get("url", ""),
                            "excerpt": raw if has_raw else item.get("content", ""),
                            "excerpt_kind": "page_extract" if has_raw else "search_snippet"})
        return sources


class BochaProvider:
    """BoCha (博查) web search: mainland-reachable and Chinese-first.

    Response is Bing-shaped: data.webPages.value[].name/url/snippet/summary.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key

    async def search(self, query: str) -> list[dict]:
        if not self._api_key:
            raise RetrievalError("provider_not_configured")
        body = {"query": query[:200], "summary": False, "freshness": "noLimit", "count": 3}
        request = Request("https://api.bocha.cn/v1/web-search", data=json.dumps(body).encode(),
                          headers={"Authorization": f"Bearer {self._api_key}",
                                   "Content-Type": "application/json"}, method="POST")
        result = await asyncio.to_thread(_fetch_json, request)
        if result.get("code") not in (None, 200):
            # e.g. 403 for an exhausted or unactivated package; degrade like
            # any other retrieval failure, never with the provider's wording.
            raise RetrievalError("provider_quota_or_error")
        pages = ((result.get("data") or {}).get("webPages") or {}).get("value") or []
        sources = []
        for item in pages[:3]:
            if not isinstance(item, dict):
                continue
            sources.append({"title": item.get("name") or "", "url": item.get("url") or "",
                            "excerpt": item.get("snippet") or item.get("summary") or "",
                            "excerpt_kind": "search_snippet"})
        return sources


def default_provider() -> SearchProvider:
    bocha = os.environ.get("BOCHA_API_KEY", "").strip()
    if bocha:
        return BochaProvider(bocha)
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    return TavilyProvider(key) if key else WikipediaProvider()


