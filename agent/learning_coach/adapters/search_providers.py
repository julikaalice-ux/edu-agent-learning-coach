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
PROVIDER_HOSTS = {"zh.wikipedia.org", "en.wikipedia.org", "api.tavily.com", "api.bocha.cn",
                  "open.bigmodel.cn"}

# Knowledge-looking queries go to the knowledge-focused provider; news and
# current-events queries keep the whole index.
_EDUCATIONAL = re.compile(r"(公式|定义|是什么|为什么|怎么|原理|定理|概念|区别|起源|性质|规律|证明|推导|成分|结构|分类)")

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
    ``summary: true`` asks for a page-length summary instead of a short
    snippet; BoCha's raw snippets are too thin for claim checking, and its
    site filter is unreliable, so long summaries carry the verification.
    The summary is provider-generated prose, not the page's own words, so
    excerpts stay ``search_snippet`` (conservative evidence tier).
    """

    def __init__(self, api_key: str):
        self._api_key = api_key

    async def search(self, query: str) -> list[dict]:
        if not self._api_key:
            raise RetrievalError("provider_not_configured")
        body = {"query": query[:200], "summary": True, "freshness": "noLimit", "count": 3}
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
                            "excerpt": item.get("summary") or item.get("snippet") or "",
                            "excerpt_kind": "search_snippet"})
        return sources


class ZhipuProvider:
    """Zhipu (智谱) web search: knowledge-grade retrieval for teaching queries.

    Paid per call (search_pro); the response items carry title/content/link,
    where content is a provider summary and therefore stays on the
    conservative search_snippet evidence tier.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key

    async def search(self, query: str) -> list[dict]:
        if not self._api_key:
            raise RetrievalError("provider_not_configured")
        body = {"search_query": query[:100], "search_engine": "search_pro",
                "count": 3, "content_size": "high", "search_recency_filter": "noLimit"}
        request = Request("https://open.bigmodel.cn/api/paas/v4/web_search", data=json.dumps(body).encode(),
                          headers={"Authorization": f"Bearer {self._api_key}",
                                   "Content-Type": "application/json"}, method="POST")
        result = await asyncio.to_thread(_fetch_json, request)
        items = result.get("search_result") or []
        if not isinstance(items, list):
            raise RetrievalError("provider_invalid_response")
        sources = []
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            sources.append({"title": item.get("title") or "", "url": item.get("link") or "",
                            "excerpt": item.get("content") or "",
                            "excerpt_kind": "search_snippet"})
        return sources


class RoutedProvider:
    """BoCha for news and events; Zhipu for knowledge questions."""

    def __init__(self, bocha: SearchProvider, zhipu: SearchProvider):
        self._bocha, self._zhipu = bocha, zhipu

    async def search(self, query: str) -> list[dict]:
        provider = self._zhipu if _EDUCATIONAL.search(query) else self._bocha
        return await provider.search(query)


def default_provider() -> SearchProvider:
    bocha = os.environ.get("BOCHA_API_KEY", "").strip()
    zhipu = os.environ.get("ZHIPU_API_KEY", "").strip()
    if bocha and zhipu:
        return RoutedProvider(BochaProvider(bocha), ZhipuProvider(zhipu))
    if bocha:
        return BochaProvider(bocha)
    if zhipu:
        return ZhipuProvider(zhipu)
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    return TavilyProvider(key) if key else WikipediaProvider()


