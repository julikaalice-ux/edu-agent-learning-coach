"""Offline retrieval/evidence tests: no model, credentials or public API calls."""

import asyncio
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from learning_coach.service import research
from learning_coach.adapters import search_providers as providers


def source(url="https://one.example/article", excerpt="Water expands when it freezes under normal pressure.", kind="page_extract"):
    return {"title": "Water", "url": url, "excerpt": excerpt, "excerpt_kind": kind}


class FakeProvider:
    def __init__(self, items=None, fail=False):
        self.items = items or []
        self.fail = fail
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        if self.fail:
            raise OSError("SENSITIVE server error")
        return self.items


class FakeRunner:
    def __init__(self, alter=None, queries=None):
        self.calls = []
        self.alter = alter
        self.queries = queries if queries is not None else ["water freezing volume", "water ice density evidence"]

    async def __call__(self, role, payload, schema):
        self.calls.append((role, payload, schema))
        if role == "researcher":
            return {"queries": self.queries}
        claim = {"text": "Freezing water expands.", "status": "verified", "independent": True,
                 "explanation": "Comparing supplied excerpts.", "evidence": [
                     {"source_id": item["id"], "quote": item["excerpt"], "relation": "supports"}
                     for item in payload["sources"]]}
        if self.alter:
            self.alter(claim)
        return {"claims": [claim]}


class ResearchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_independent_full_excerpts_can_be_verified(self):
        provider = FakeProvider([source(), source("https://two.test/ice",
            "The structure of ordinary ice occupies more volume than the same mass of liquid.")])
        runner = FakeRunner()
        result = await research.ResearchService(runner, provider).run("Why does water expand?")
        self.assertEqual(result["overall"], "verified")
        self.assertEqual([call[0] for call in runner.calls], ["researcher", "verifier"])
        self.assertEqual(len(result["sources"]), 2)

    async def test_wikipedia_languages_are_one_publisher(self):
        provider = FakeProvider([source("https://zh.wikipedia.org/wiki/水", "水冻结时体积增大。"),
                                 source("https://en.wikipedia.org/wiki/Water")])
        result = await research.ResearchService(FakeRunner(), provider).run("水")
        self.assertEqual(result["overall"], "likely")
        self.assertEqual({s["publisher"] for s in result["sources"]}, {"wikipedia.org"})

    async def test_same_domain_and_subdomains_not_independent(self):
        provider = FakeProvider([source("https://science.example.com/ice"),
                                 source("https://www.example.com/ice", "An unrelatedly worded account of ice volume.")])
        result = await research.ResearchService(FakeRunner(), provider).run("ice")
        self.assertEqual(result["overall"], "likely")

    async def test_mirrored_excerpts_not_independent(self):
        provider = FakeProvider([source(), source("https://mirror.test/copied")])
        result = await research.ResearchService(FakeRunner(), provider).run("ice")
        self.assertEqual(result["overall"], "likely")

    async def test_duplicate_result_urls_do_not_inflate_sources(self):
        result = await research.ResearchService(FakeRunner(), FakeProvider([source(), source()])).run("ice")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["overall"], "likely")

    async def test_search_snippets_cannot_reach_verified(self):
        provider = FakeProvider([source(kind="search_snippet"),
            source("https://two.test/ice", "Ice volume is greater than liquid volume.", "search_snippet")])
        result = await research.ResearchService(FakeRunner(), provider).run("ice")
        self.assertEqual(result["overall"], "likely")

    async def test_fabricated_source_id_is_rejected(self):
        def forge(claim):
            claim["evidence"] = [{"source_id": "invented", "quote": "true", "relation": "supports"}]
        result = await research.ResearchService(FakeRunner(forge), FakeProvider([source()])).run("ice")
        self.assertEqual(result["overall"], "unavailable")
        self.assertEqual(result["claims"][0]["evidence"], [])
        self.assertIn("invalid_evidence_rejected", result["warnings"])

    async def test_rewritten_excerpt_is_rejected(self):
        def forge(claim):
            claim["evidence"][0]["quote"] = "Not an actual retrieved quote"
        result = await research.ResearchService(FakeRunner(forge), FakeProvider([source()])).run("ice")
        self.assertEqual(result["overall"], "unavailable")

    async def test_verifier_must_confirm_independent_publication(self):
        def related(claim):
            claim["independent"] = False
        provider = FakeProvider([source(), source("https://two.test/ice", "Ice has a relatively open lattice.")])
        result = await research.ResearchService(FakeRunner(related), provider).run("ice")
        self.assertEqual(result["overall"], "likely")

    async def test_contradiction_sets_disputed(self):
        def conflict(claim):
            claim["evidence"][0]["relation"] = "contradicts"
        result = await research.ResearchService(FakeRunner(conflict), FakeProvider([source()])).run("ice")
        self.assertEqual(result["overall"], "disputed")

    async def test_empty_results_skip_verifier(self):
        runner = FakeRunner()
        result = await research.ResearchService(runner, FakeProvider()).run("ice")
        self.assertEqual(result["overall"], "unavailable")
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("no_evidence", result["warnings"])

    async def test_network_failure_is_safe_unavailable(self):
        result = await research.ResearchService(FakeRunner(), FakeProvider(fail=True)).run("ice")
        self.assertEqual(result["overall"], "unavailable")
        self.assertIn("provider_unavailable", result["warnings"])
        self.assertNotIn("SENSITIVE", json.dumps(result))

    async def test_query_and_result_counts_are_bounded(self):
        provider = FakeProvider([source(f"https://site{i}.example/item", f"Excerpt {i}") for i in range(20)])
        result = await research.ResearchService(FakeRunner(queries=[str(i) for i in range(20)]), provider).run("ice")
        self.assertEqual(len(provider.queries), 4)
        self.assertLessEqual(len(result["sources"]), 12)

    async def test_invalid_plan_skips_network(self):
        provider = FakeProvider()
        result = await research.ResearchService(FakeRunner(queries=["same", "same"]), provider).run("ice")
        self.assertEqual(provider.queries, [])
        self.assertIn("query_plan_invalid", result["warnings"])

    async def test_malformed_evidence_is_rejected_without_crashing(self):
        def malformed(claim):
            claim["status"] = []
            claim["evidence"][0]["relation"] = []
        result = await research.ResearchService(FakeRunner(malformed), FakeProvider([source()])).run("ice")
        self.assertEqual(result["overall"], "unavailable")
        self.assertIn("invalid_evidence_rejected", result["warnings"])

    async def test_bad_provider_source_is_not_evidence(self):
        invalid = source()
        invalid["excerpt_kind"] = []
        result = await research.ResearchService(FakeRunner(), FakeProvider([invalid])).run("ice")
        self.assertEqual(result["overall"], "unavailable")

    async def test_provider_timeout_does_not_wait_unboundedly(self):
        class SlowProvider:
            async def search(self, query):
                await asyncio.sleep(60)
        with patch.object(research, "PROVIDER_TIMEOUT", .01):
            result = await research.ResearchService(FakeRunner(), SlowProvider()).run("ice")
        self.assertEqual(result["overall"], "unavailable")


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_wikipedia_uses_fixed_api_and_plaintext_extraction(self):
        payloads = [{"query": {"search": [{"pageid": 123}]}},
                    {"query": {"pages": {"123": {"pageid": 123, "title": "水", "extract": "原文。",
                     "fullurl": "https://zh.wikipedia.org/wiki/水"}}}}]
        with patch.object(providers, "_fetch_json", side_effect=payloads) as fetch:
            items = await providers.WikipediaProvider().search("水 https://localhost/private")
        first = urlsplit(fetch.call_args_list[0].args[0].full_url)
        second = parse_qs(urlsplit(fetch.call_args_list[1].args[0].full_url).query)
        self.assertEqual(first.hostname, "zh.wikipedia.org")
        self.assertEqual(parse_qs(first.query)["list"], ["search"])
        self.assertEqual(parse_qs(first.query)["srlimit"], ["3"])
        self.assertEqual(second["explaintext"], ["1"])
        self.assertEqual(second["pageids"], ["123"])
        self.assertEqual(items[0]["excerpt"], "原文。")
        self.assertEqual(items[0]["excerpt_kind"], "page_extract")

    async def test_tavily_uses_bearer_and_distinguishes_raw_from_snippet(self):
        payload = {"answer": "not evidence", "results": [
            {"title": "A", "url": "https://one.example/a", "raw_content": "verbatim", "content": "summary"},
            {"title": "B", "url": "https://two.test/b", "raw_content": None, "content": "snippet"}]}
        with patch.object(providers, "_fetch_json", return_value=payload) as fetch:
            items = await providers.TavilyProvider("fake-test-key").search("water")
        request = fetch.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.tavily.com/search")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-test-key")
        self.assertFalse(body["include_answer"])
        self.assertEqual(body["include_raw_content"], "text")
        self.assertEqual(body["max_results"], 3)
        self.assertEqual([item["excerpt_kind"] for item in items], ["page_extract", "search_snippet"])
        self.assertEqual([item["excerpt"] for item in items], ["verbatim", "snippet"])

    async def test_unconfigured_tavily_does_not_send_request(self):
        with patch.object(providers, "_fetch_json") as fetch:
            with self.assertRaises(providers.RetrievalError):
                await providers.TavilyProvider("").search("ice")
            fetch.assert_not_called()


class HttpBoundaryTests(unittest.TestCase):
    def test_non_provider_endpoint_rejected_before_network(self):
        with patch.object(providers, "build_opener") as opener:
            with self.assertRaises(providers.RetrievalError):
                providers._fetch_json(Request("https://localhost/private"))
            opener.assert_not_called()

    def test_redirect_rejected(self):
        with self.assertRaises(providers.RetrievalError):
            providers._NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.example")

    def test_response_size_limit(self):
        with patch.object(providers, "MAX_RESPONSE_BYTES", 10), patch.object(providers, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b"x" * 11)
            with self.assertRaisesRegex(providers.RetrievalError, "too_large"):
                providers._fetch_json(Request("https://en.wikipedia.org/w/api.php"))
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], providers.HTTP_TIMEOUT)

    def test_private_result_urls_rejected_without_fetching(self):
        for url in ("http://127.0.0.1/item", "http://[::1]/item", "https://admin:pass@one.example/a", "file:///etc/passwd"):
            with self.subTest(url=url):
                self.assertIsNone(research._clean_source(source(url)))


if __name__ == "__main__":
    unittest.main()
