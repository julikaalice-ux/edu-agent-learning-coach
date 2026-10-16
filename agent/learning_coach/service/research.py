"""Search/verification orchestration over injected application ports."""
import asyncio

from learning_coach.service.ports import AgentRunner, SearchProvider
from learning_coach.domain.contracts import MAX_QUESTION_CHARS
from learning_coach.domain.research_contracts import (QUERY_SCHEMA, VERIFY_SCHEMA, MAX_QUERIES, MAX_SOURCES, MAX_CLAIMS,
                                 MAX_QUERY_CHARS, MAX_QUOTE_CHARS,
                                 _clean_source, _independent_count)

PROVIDER_TIMEOUT = 20
RESULTS_PER_QUERY = 3

class ResearchService:
    def __init__(self, runner: AgentRunner, provider: SearchProvider):
        self.runner = runner
        self.provider = provider

    async def run(self, question: str, learner: dict | None = None) -> dict:
        """learner: the program's brief learner card (stage, goal), used only to
        pitch the search queries at the right level."""
        result = {"queries": [], "sources": [], "claims": [], "overall": "unavailable", "warnings": []}
        if not isinstance(question, str) or not question.strip():
            result["warnings"].append("empty_question")
            return result
        try:
            request = {"question": question[:MAX_QUESTION_CHARS]}
            if learner:
                request["learner"] = {key: learner.get(key) for key in ("stage", "goal")}
            planned = await self.runner("researcher", request, QUERY_SCHEMA)
            candidates = planned.get("queries", []) if isinstance(planned, dict) else []
            if not isinstance(candidates, list):
                candidates = []
            queries = list(dict.fromkeys(q.strip()[:MAX_QUERY_CHARS] for q in candidates[:MAX_QUERIES]
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
                return found[:RESULTS_PER_QUERY] if isinstance(found, list) else None
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
            verified = await self.runner("verifier", {"question": question[:MAX_QUESTION_CHARS],
                "sources": result["sources"]}, VERIFY_SCHEMA)
            claims = verified.get("claims", []) if isinstance(verified, dict) else []
            if not isinstance(claims, list):
                claims = []
        except Exception:
            result["warnings"].append("verification_failed")
            return result
        for claim in claims[:MAX_CLAIMS]:
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
            result["claims"].append({"text": claim["text"][:MAX_QUOTE_CHARS], "status": status,
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
