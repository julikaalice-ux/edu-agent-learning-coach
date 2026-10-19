"""Find existing pictures on the web for gallery pages: Wikimedia Commons first, BoCha as backup.

Commons results carry a license and an author; only licenses that allow
reuse with attribution are kept (public domain, CC0, CC BY, CC BY-SA).
BoCha's image results have no license information, so they come after the
Commons ones and are shown with their source page.

Every picture is downloaded here, bounded in size and time, and only from
public addresses (no redirects, no private networks), so callers get bytes
they can show to a reviewer and embed in a page.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener

from learning_coach.adapters.search_providers import RetrievalError, _NoRedirect

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
BOCHA_API = "https://api.bocha.cn/v1/web-search"
USER_AGENT = "LearningCoachEdu/1.0 (educational demo; https://github.com/julikaalice-ux/edu-agent-learning-coach)"
THUMB_WIDTH = 800
MAX_JSON_BYTES = 1_048_576
# Larger pictures fall back to the search engine's thumbnail; every candidate
# is also sent to the reviewer, so this bounds the review request too.
MAX_IMAGE_BYTES = 1_500_000
# Short: Commons is unreachable from mainland China without a VPN, and the
# BoCha results for the same panel should not wait long for it.
HTTP_TIMEOUT = 6
DOWNLOAD_TIMEOUT = 10
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF8", "image/gif"))
_TAGS = re.compile(r"<[^>]+>")
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")
# Commons files that are pictures (its thumbnails of these are PNG or JPEG);
# documents, audio and video are skipped.
COMMONS_PICTURE_TYPES = {"image/jpeg", "image/png", "image/svg+xml", "image/webp", "image/gif", "image/tiff"}


def license_allowed(name: str) -> bool:
    """Reuse with attribution only: no non-commercial or no-derivatives terms."""
    value = (name or "").strip().lower()
    if not value or "nc" in re.split(r"[\s\-_/]+", value) or "nd" in re.split(r"[\s\-_/]+", value):
        return False
    return (value.startswith("cc0") or "public domain" in value or value.startswith("pd")
            or re.match(r"cc[\s\-]by(?:[\s\-]sa)?(?:[\s\-]\d|\s*$)", value) is not None)


def _plain(value, limit: int) -> str:
    text = _TAGS.sub(" ", value if isinstance(value, str) else "")
    text = re.sub(r"\s+", " ", text.replace("&nbsp;", " ").replace("&amp;", "&")).strip()
    return text[:limit]


def _public_host(host: str) -> bool:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError):
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        # 198.18.0.0/15 is what "fake-IP" proxy tools answer for every host;
        # nothing real is reachable there, so it is not a private network.
        if not ip.is_global and ip not in _FAKE_IP:
            return False
    return bool(addresses)


def _read(response, limit: int, deadline: float) -> bytes:
    chunks, size = [], 0
    while True:
        chunk = response.read(min(65536, limit + 1 - size))
        size += len(chunk)
        if size > limit:
            raise RetrievalError("response_too_large")
        if time.monotonic() > deadline:
            raise RetrievalError("download_timeout")
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _json(request: Request) -> dict:
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", "application/json")
    with build_opener(_NoRedirect()).open(request, timeout=HTTP_TIMEOUT) as response:
        data = _read(response, MAX_JSON_BYTES, time.monotonic() + HTTP_TIMEOUT * 2)
    result = json.loads(data.decode("utf-8"))
    if not isinstance(result, dict):
        raise RetrievalError("invalid_response")
    return result


_FILLER = {"a", "an", "the", "of", "in", "on", "under", "with", "and", "at", "for", "by", "to", "from"}


def simpler_query(query: str) -> str:
    """The first three content words of an English query, or "" when that changes nothing."""
    words = [word for word in re.split(r"[\s,;]+", query or "") if word]
    core = [word for word in words if word.lower() not in _FILLER][:3]
    shorter = " ".join(core)
    return shorter if core and shorter != " ".join(words) else ""


def download_image(url: str) -> tuple[bytes, str]:
    """(bytes, media type) of one public https picture; raises RetrievalError otherwise."""
    parsed = urlsplit(url or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RetrievalError("image_url_rejected")
    if not _public_host(parsed.hostname):
        raise RetrievalError("image_host_rejected")
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"})
    with build_opener(_NoRedirect()).open(request, timeout=HTTP_TIMEOUT) as response:
        declared = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = _read(response, MAX_IMAGE_BYTES, time.monotonic() + DOWNLOAD_TIMEOUT)
    media_type = next((kind for magic, kind in _MAGIC if data.startswith(magic)), None)
    if media_type is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        media_type = "image/webp"
    if media_type is None or (declared and declared not in IMAGE_TYPES):
        raise RetrievalError("not_a_picture")
    return data, media_type


def commons_candidates(query: str, limit: int) -> list[dict]:
    """Search File: pages; keep reusable licenses; best match first (no download)."""
    params = {"action": "query", "format": "json", "formatversion": 2, "generator": "search",
              "gsrsearch": query[:80], "gsrnamespace": 6, "gsrlimit": 12,
              "prop": "imageinfo", "iiprop": "url|mime|extmetadata", "iiurlwidth": THUMB_WIDTH,
              "iiextmetadatafilter": "LicenseShortName|Artist|ImageDescription|ObjectName",
              "iiextmetadatalanguage": "zh"}
    result = _json(Request(COMMONS_API + "?" + urlencode(params)))
    pages = sorted((result.get("query") or {}).get("pages") or [], key=lambda page: page.get("index", 99))
    found = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        license_name = _plain((meta.get("LicenseShortName") or {}).get("value"), 60)
        thumb = info.get("thumburl") or ""
        if (info.get("mime") not in COMMONS_PICTURE_TYPES or not license_allowed(license_name)
                or urlsplit(thumb).hostname != "upload.wikimedia.org"):
            continue
        title = _plain((meta.get("ObjectName") or {}).get("value"), 120) or (page.get("title") or "")[5:]
        found.append({"source": "commons", "title": title,
                      "description": _plain((meta.get("ImageDescription") or {}).get("value"), 300),
                      "author": _plain((meta.get("Artist") or {}).get("value"), 120),
                      "license": license_name, "page_url": info.get("descriptionurl") or "",
                      "url": thumb})
        if len(found) >= limit:
            break
    return found


def bocha_candidates(api_key: str, query: str, limit: int) -> list[dict]:
    """Image results that come with BoCha's web search (Bing-shaped data.images.value)."""
    body = {"query": query[:100], "summary": False, "freshness": "noLimit", "count": 5}
    request = Request(BOCHA_API, data=json.dumps(body).encode(), method="POST",
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    result = _json(request)
    if result.get("code") not in (None, 200):
        raise RetrievalError("provider_quota_or_error")
    items = ((result.get("data") or {}).get("images") or {}).get("value") or []
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("contentUrl") or item.get("thumbnailUrl") or ""
        if not url.startswith("https://"):
            continue
        found.append({"source": "bocha", "title": _plain(item.get("name"), 120),
                      "description": "", "author": "", "license": "",
                      "page_url": item.get("hostPageUrl") or "", "url": url,
                      "fallback_url": item.get("thumbnailUrl") or ""})
        if len(found) >= limit:
            break
    return found


# A source that cannot be reached is skipped for a while instead of making
# every panel wait for its timeout again.
SOURCE_PAUSE_SECONDS = 600


class WebImageFinder:
    """Commons (by the English query) and BoCha (by the Chinese query) in parallel; Commons first."""

    def __init__(self, bocha_api_key: str = ""):
        self._bocha_api_key = bocha_api_key
        self._paused_until = {}

    reports_stats = True

    async def find(self, query_zh: str, query_en: str, *, limit: int = 3, stats: dict | None = None) -> list[dict]:
        """stats (optional) receives counts for evaluations: listed, retried, downloaded."""
        stats = stats if stats is not None else {}

        async def safely(name, function, *args):
            if self._paused_until.get(name, 0) > time.monotonic():
                return []
            try:
                return await asyncio.to_thread(function, *args)
            except (OSError, ValueError, RetrievalError) as error:
                if isinstance(error, OSError):  # unreachable (DNS, TLS, refused, timeout), not "no results"
                    self._paused_until[name] = time.monotonic() + SOURCE_PAUSE_SECONDS
                return []
        lookups = [safely("commons", commons_candidates, query_en or query_zh, limit)]
        if self._bocha_api_key:
            lookups.append(safely("bocha", bocha_candidates, self._bocha_api_key, query_zh or query_en, limit))
        listed = [item for found in await asyncio.gather(*lookups) for item in found]
        shorter = simpler_query(query_en)
        if not listed and shorter:
            # Long, specific search words often match nothing on Commons; try the core words once.
            listed = await safely("commons", commons_candidates, shorter, limit)
            stats["retried"] = shorter
        listed = listed[:limit + 2]
        stats["listed"] = len(listed)
        stats["paused"] = sorted(name for name, until in self._paused_until.items() if until > time.monotonic())

        async def fetch(item):
            for url in (item["url"], item.get("fallback_url")):
                if not url:
                    continue
                try:
                    data, media_type = await asyncio.to_thread(download_image, url)
                except (OSError, ValueError, RetrievalError):
                    continue
                picture = {key: item[key] for key in ("source", "title", "description", "author",
                                                      "license", "page_url")}
                return {**picture, "data": data, "media_type": media_type}
            return None
        pictures = await asyncio.gather(*(fetch(item) for item in listed))
        found = [picture for picture in pictures if picture is not None][:limit]
        stats["downloaded"] = len(found)
        return found


def default_image_finder() -> WebImageFinder | None:
    """LEARNING_COACH_WEB_IMAGES=0 turns web pictures off; BoCha joins when its key is set."""
    if os.environ.get("LEARNING_COACH_WEB_IMAGES", "1").strip() == "0":
        return None
    return WebImageFinder(os.environ.get("BOCHA_API_KEY", "").strip())
