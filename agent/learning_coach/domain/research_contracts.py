"""Pure source/evidence contracts and provenance checks."""
from difflib import SequenceMatcher
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

MAX_QUERIES = 4
MAX_SOURCES = 12
MAX_EXCERPT = 3000
MAX_CLAIMS = 8
MAX_QUERY_CHARS = 200
MAX_QUOTE_CHARS = 800
MAX_EVIDENCE = 12
MAX_URL_CHARS = 2048
MAX_TITLE_CHARS = 300
WEB_PORTS = {None, 80, 443}
# Two excerpts at least this long and this similar count as one source.
DUPLICATE_MIN_CHARS = 80
DUPLICATE_SIMILARITY = 0.85

def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


EVIDENCE_SCHEMA = _object({"source_id": {"type": "string"},
    "quote": {"type": "string", "minLength": 1, "maxLength": MAX_QUOTE_CHARS},
    "relation": {"type": "string", "enum": ["supports", "contradicts"]}})
VERIFY_SCHEMA = _object({"claims": {"type": "array", "maxItems": MAX_CLAIMS,
    "items": _object({"text": {"type": "string", "minLength": 1, "maxLength": 800},
        "status": {"type": "string", "enum": ["unavailable", "likely", "verified", "disputed"]},
        "evidence": {"type": "array", "maxItems": MAX_EVIDENCE, "items": EVIDENCE_SCHEMA},
        "independent": {"type": "boolean"},
        "explanation": {"type": "string", "maxLength": 800}})}})

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
    if len(item["url"]) > MAX_URL_CHARS:
        return None
    try:
        parsed = urlsplit(item["url"])
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or not host or "." not in host
                or parsed.username or parsed.password or parsed.port not in WEB_PORTS
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
            "title": item["title"][:MAX_TITLE_CHARS], "url": url, "publisher": _publisher(url),
            "excerpt": item["excerpt"][:MAX_EXCERPT], "excerpt_kind": kind}


def _independent_count(sources: list[dict]) -> int:
    unique = []
    for source in sources:
        text = re.sub(r"\W+", "", source["excerpt"]).casefold()
        duplicate = any(source["publisher"] == previous["publisher"]
                        or SequenceMatcher(None, text, previous_text, autojunk=False).ratio() >= DUPLICATE_SIMILARITY
                        or (min(len(text), len(previous_text)) >= DUPLICATE_MIN_CHARS
                            and (text in previous_text or previous_text in text))
                        for previous, previous_text in unique)
        if not duplicate:
            unique.append((source, text))
    return len(unique)


