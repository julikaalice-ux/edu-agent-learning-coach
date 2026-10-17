"""Search/verification orchestration over injected application ports.

Searching and checking claims are separate steps so that the single-agent
baseline (service/single_agent.py) can use the same program checks as tools.
"""
import asyncio

from learning_coach.service.ports import AgentRunner, SearchProvider
from learning_coach.domain.contracts import MAX_QUESTION_CHARS
from learning_coach.domain.research_contracts import (VERIFY_SCHEMA, MAX_QUERIES, MAX_SOURCES, MAX_CLAIMS,
                                 MAX_QUERY_CHARS, MAX_QUOTE_CHARS,
                                 _clean_source, _independent_count)

PROVIDER_TIMEOUT = 20
RESULTS_PER_QUERY = 3


def clean_queries(question: str, queries) -> tuple[list[str], list[str]]:
    """De-duplicated, bounded queries; the question itself when none are usable."""
    candidates = queries if isinstance(queries, list) else []
    cleaned = list(dict.fromkeys(q.strip()[:MAX_QUERY_CHARS] for q in candidates[:MAX_QUERIES]
                                 if isinstance(q, str) and q.strip()))
    if cleaned:
        return cleaned, []
    return [question.strip()[:MAX_QUERY_CHARS]], ["queries_missing"]


async def search_sources(provider: SearchProvider, queries: list[str]) -> tuple[dict, list[str]]:
    """Run the queries concurrently; return (sources by id, warnings)."""
    async def search(query: str):
        try:
            found = await asyncio.wait_for(provider.search(query), PROVIDER_TIMEOUT)
            return found[:RESULTS_PER_QUERY] if isinstance(found, list) else None
        except Exception:
            return None

    batches = await asyncio.gather(*(search(query) for query in queries))
    sources, warnings = {}, []
    for batch in batches:
        if batch is None:
            warnings.append("provider_unavailable")
            continue
        for item in batch:
            source = _clean_source(item)
            if source:
                sources.setdefault(source["id"], source)
    return sources, warnings


def assess_claims(sources: dict, claims) -> tuple[list[dict], str, list[str]]:
    """Program check of model-proposed claims against the retrieved excerpts.

    Returns (claims with program status, overall status, warnings). A model
    assertion never upgrades missing or only indirect evidence.
    """
    checked, warnings = [], []
    for claim in (claims if isinstance(claims, list) else [])[:MAX_CLAIMS]:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
            continue
        evidence, invalid = [], False
        candidates = claim.get("evidence", [])
        if not isinstance(candidates, list):
            candidates, invalid = [], True
        for item in candidates[:MAX_SOURCES]:
            if not isinstance(item, dict):
                invalid = True
                continue
            source = sources.get(item.get("source_id")) if isinstance(item.get("source_id"), str) else None
            quote, relation = item.get("quote"), item.get("relation")
            if (source is None or not isinstance(quote, str) or not quote.strip()
                    or len(quote) > MAX_QUOTE_CHARS or quote not in source["excerpt"]
                    or not isinstance(relation, str) or relation not in {"supports", "contradicts"}):
                invalid = True
                continue
            evidence.append({"source_id": source["id"], "quote": quote, "relation": relation})
        supported = [sources[e["source_id"]] for e in evidence if e["relation"] == "supports"]
        contradicted = any(e["relation"] == "contradicts" for e in evidence)
        requested = claim.get("status")
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
            warnings.append("invalid_evidence_rejected")
        if requested == "verified" and status != "verified":
            warnings.append("verification_downgraded")
        checked.append({"text": claim["text"][:MAX_QUOTE_CHARS], "status": status, "evidence": evidence})
    statuses = [claim["status"] for claim in checked]
    if "disputed" in statuses:
        overall = "disputed"
    elif statuses and all(status == "verified" for status in statuses):
        overall = "verified"
    elif any(status in {"likely", "verified"} for status in statuses):
        overall = "likely"
    else:
        overall = "unavailable"
        warnings.append("no_supported_claims")
    return checked, overall, warnings


class ResearchService:
    def __init__(self, runner: AgentRunner, provider: SearchProvider):
        self.runner = runner
        self.provider = provider

    async def run(self, question: str, queries: list[str] | None = None) -> dict:
        """Search with the coordinator's queries, then have the verifier check the sources.

        The coordinator writes the queries in its plan. Without usable ones the
        research question itself becomes the only query, so research degrades
        instead of failing.
        """
        result = {"queries": [], "sources": [], "claims": [], "overall": "unavailable", "warnings": []}
        if not isinstance(question, str) or not question.strip():
            result["warnings"].append("empty_question")
            return result
        result["queries"], warnings = clean_queries(question, queries)
        result["warnings"].extend(warnings)
        sources, warnings = await search_sources(self.provider, result["queries"])
        result["warnings"].extend(warnings)
        result["sources"] = list(sources.values())[:MAX_SOURCES]
        if not sources:
            result["warnings"].append("no_evidence")
            result["warnings"] = list(dict.fromkeys(result["warnings"]))
            return result
        try:
            verified = await self.runner("verifier", {"question": question[:MAX_QUESTION_CHARS],
                "sources": result["sources"]}, VERIFY_SCHEMA)
            claims = verified.get("claims", []) if isinstance(verified, dict) else []
        except Exception:
            result["warnings"].append("verification_failed")
            return result
        result["claims"], result["overall"], warnings = assess_claims(sources, claims)
        result["warnings"].extend(warnings)
        result["warnings"] = list(dict.fromkeys(result["warnings"]))
        return result
