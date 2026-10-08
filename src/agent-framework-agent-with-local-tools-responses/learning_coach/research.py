"""Bounded retrieval and evidence checking; model output never supplies source URLs.

API contracts: https://www.mediawiki.org/wiki/API:Search
https://www.mediawiki.org/wiki/Extension:TextExtracts#API
https://docs.tavily.com/documentation/api-reference/endpoint/search
"""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import hashlib
import ipaddress
import json
import os
import re
import time
from typing import Awaitable, Callable, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_QUERIES = 4
MAX_SOURCES = 12
MAX_EXCERPT = 3000
MAX_RESPONSE_BYTES = 1_048_576
HTTP_TIMEOUT = 8
PROVIDER_TIMEOUT = 20
PROVIDER_HOSTS = {"zh.wikipedia.org", "en.wikipedia.org", "api.tavily.com"}


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


QUERY_SCHEMA = _object({"queries": {"type": "array", "minItems": 2,
    "maxItems": MAX_QUERIES, "items": {"type": "string", "minLength": 1, "maxLength": 200}}})
EVIDENCE_SCHEMA = _object({"source_id": {"type": "string"},
    "quote": {"type": "string", "minLength": 1, "maxLength": 800},
    "relation": {"type": "string", "enum": ["supports", "contradicts"]}})
VERIFY_SCHEMA = _object({"claims": {"type": "array", "maxItems": 8,
    "items": _object({"text": {"type": "string", "minLength": 1, "maxLength": 800},
        "status": {"type": "string", "enum": ["unavailable", "likely", "verified", "disputed"]},
        "evidence": {"type": "array", "maxItems": 12, "items": EVIDENCE_SCHEMA},
        "independent": {"type": "boolean"},
        "explanation": {"type": "string", "maxLength": 800}})}})

Runner = Callable[[str, dict, dict], Awaitable[dict]]


class SearchProvider(Protocol):
    async def search(self, query: str) -> list[dict]: ...


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


def default_provider() -> SearchProvider:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    return TavilyProvider(key) if key else WikipediaProvider()


def _publisher(url: str) -> str:
    """Conservative domain grouping, not a public-suffix or ownership database.

    Grouping all *.co.uk together may undercount, but never splits a publisher's
    subdomains. Different domains can share ownership: the verifier must also
    affirm independent publication; near-copy detection supplies a second check.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return ".".join(host.split(".")[-2:])


def _clean_source(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    if not all(isinstance(item.get(key), str) and item[key].strip() for key in ("title", "url", "excerpt")):
        return None
    if len(item["url"]) > 2048:
        return None
    try:
        parsed = urlsplit(item["url"])
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not host or "." not in host
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
                or host.endswith((".local", ".localhost", ".internal"))):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
    except ValueError:
        return None
    url = urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.query, ""))
    kind = item.get("excerpt_kind")
    if not isinstance(kind, str) or kind not in {"page_extract", "search_snippet"}:
        return None
    return {"id": "src_" + hashlib.sha256(url.encode()).hexdigest()[:12],
            "title": item["title"][:300], "url": url, "publisher": _publisher(url),
            "excerpt": item["excerpt"][:MAX_EXCERPT], "excerpt_kind": kind}


def _independent_count(sources: list[dict]) -> int:
    unique = []
    for source in sources:
        text = re.sub(r"\W+", "", source["excerpt"]).casefold()
        duplicate = any(source["publisher"] == previous["publisher"]
                        or SequenceMatcher(None, text, previous_text, autojunk=False).ratio() >= .85
                        or (min(len(text), len(previous_text)) >= 80
                            and (text in previous_text or previous_text in text))
                        for previous, previous_text in unique)
        if not duplicate:
            unique.append((source, text))
    return len(unique)


class ResearchService:
    def __init__(self, runner: Runner, provider: SearchProvider | None = None):
        self.runner = runner
        self.provider = provider if provider is not None else default_provider()

    async def run(self, question: str) -> dict:
        result = {"queries": [], "sources": [], "claims": [], "overall": "unavailable", "warnings": []}
        if not isinstance(question, str) or not question.strip():
            result["warnings"].append("empty_question")
            return result
        try:
            planned = await self.runner("researcher", {"question": question[:4000]}, QUERY_SCHEMA)
            candidates = planned.get("queries", []) if isinstance(planned, dict) else []
            if not isinstance(candidates, list):
                candidates = []
            queries = list(dict.fromkeys(q.strip()[:200] for q in candidates[:MAX_QUERIES]
                                        if isinstance(q, str) and q.strip()))
            if len(queries) < 2:
                result["warnings"].append("query_plan_invalid")
                return result
        except Exception:
            result["warnings"].append("query_planning_failed")
            return result
        result["queries"] = queries

        async def search(query: str):
            try:
                found = await asyncio.wait_for(self.provider.search(query), PROVIDER_TIMEOUT)
                return found[:3] if isinstance(found, list) else None
            except Exception:
                return None

        batches = await asyncio.gather(*(search(query) for query in queries))
        sources = {}
        for batch in batches:
            if batch is None:
                result["warnings"].append("provider_unavailable")
                continue
            for item in batch:
                source = _clean_source(item)
                if source:
                    sources.setdefault(source["id"], source)
        result["sources"] = list(sources.values())[:MAX_SOURCES]
        if not sources:
            result["warnings"].append("no_evidence")
            result["warnings"] = list(dict.fromkeys(result["warnings"]))
            return result
        try:
            verified = await self.runner("verifier", {"question": question[:4000],
                "sources": result["sources"]}, VERIFY_SCHEMA)
            claims = verified.get("claims", []) if isinstance(verified, dict) else []
            if not isinstance(claims, list):
                claims = []
        except Exception:
            result["warnings"].append("verification_failed")
            return result
        for claim in claims[:8]:
            if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
                continue
            evidence, invalid = [], False
            candidates = claim.get("evidence", [])
            if not isinstance(candidates, list):
                candidates, invalid = [], True
            for item in candidates[:12]:
                if not isinstance(item, dict):
                    invalid = True
                    continue
                source = sources.get(item.get("source_id")) if isinstance(item.get("source_id"), str) else None
                quote, relation = item.get("quote"), item.get("relation")
                if (source is None or not isinstance(quote, str) or not quote.strip()
                        or len(quote) > 800 or quote not in source["excerpt"]
                        or not isinstance(relation, str) or relation not in {"supports", "contradicts"}):
                    invalid = True
                    continue
                evidence.append({"source_id": source["id"], "quote": quote, "relation": relation})
            supported = [sources[e["source_id"]] for e in evidence if e["relation"] == "supports"]
            contradicted = any(e["relation"] == "contradicts" for e in evidence)
            requested = claim.get("status")
            # A model assertion never upgrades missing or only indirect evidence.
            if contradicted:
                status = "disputed"
            elif isinstance(requested, str) and requested in {"likely", "verified"} and supported:
                complete = [s for s in supported if s["excerpt_kind"] == "page_extract"]
                status = ("verified" if requested == "verified" and not invalid
                          and claim.get("independent") is True
                          and _independent_count(complete) >= 2 else "likely")
            else:
                status = "unavailable"
            if invalid:
                result["warnings"].append("invalid_evidence_rejected")
            if requested == "verified" and status != "verified":
                result["warnings"].append("verification_downgraded")
            result["claims"].append({"text": claim["text"][:800], "status": status,
                                     "evidence": evidence})
        statuses = [claim["status"] for claim in result["claims"]]
        if "disputed" in statuses:
            result["overall"] = "disputed"
        elif statuses and all(status == "verified" for status in statuses):
            result["overall"] = "verified"
        elif any(status in {"likely", "verified"} for status in statuses):
            result["overall"] = "likely"
        else:
            result["warnings"].append("no_supported_claims")
        result["warnings"] = list(dict.fromkeys(result["warnings"]))
        return result
