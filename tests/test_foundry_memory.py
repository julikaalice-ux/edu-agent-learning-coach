"""Offline tests for the Foundry Memory semantic mirror over SQLite."""

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from learning_coach.adapters.foundry_memory import FoundryMemoryStore, facts_from_notes
from learning_coach.db import memory


def notes():
    return {"schema_version": "1.0", "profile": [], "concepts": [], "error_records": [], "review_plan": []}


def profile_notes():
    result = notes()
    result["profile"] = [{"key": "learning_stage", "value": "高中", "origin": "self_report",
                          "evidence_quote": "我在读高中"}]
    return result


class FakeFoundryClient:
    """Records calls and returns canned search results. No network."""

    def __init__(self, memories=(), listed=()):
        self.created = []      # (store, scope, content, kind)
        self.searched = []     # (store, scope, options)
        self.listed = []       # (store, scope, limit)
        self.deleted = []      # (store, scope)
        self.closed = False
        self.memories = list(memories)  # (memory_id, content) for search
        self.items = list(listed)       # (memory_id, content) for list fallback

    def create_memory(self, name, *, scope, content, kind):
        self.created.append((name, scope, content, kind))

    def search_memories(self, name, *, scope, items=None, options=None):
        self.searched.append((name, scope, options))
        return SimpleNamespace(memories=[
            SimpleNamespace(memory_item=SimpleNamespace(memory_id=item_id, content=content))
            for item_id, content in self.memories])

    def list_memories(self, name, *, scope, limit=None, **kwargs):
        self.listed.append((name, scope, limit))
        return [SimpleNamespace(memory_id=item_id, content=content)
                for item_id, content in self.items]

    def delete_scope(self, name, scope):
        self.deleted.append((name, scope))

    def close(self):
        self.closed = True


class ExplodingClient(FakeFoundryClient):
    def create_memory(self, *args, **kwargs):
        raise ConnectionError("offline")

    def search_memories(self, *args, **kwargs):
        raise TimeoutError("slow")

    def delete_scope(self, *args, **kwargs):
        raise ConnectionError("offline")


class FactsTests(unittest.TestCase):
    def test_renders_all_four_sections_without_evidence_quotes(self):
        sample = {"profile": [{"key": "learning_stage", "value": "高中", "origin": "self_report",
                               "evidence_quote": "我在读高中"}],
                  "concepts": [{"concept_id": "linear-eq", "label": "一元一次方程",
                                "prerequisites": ["等式性质"], "related": ["移项"],
                                "evidence_quote": "解方程的时候"}],
                  "error_records": [{"concept_id": "linear-eq", "category": "conceptual",
                                     "cause_confirmed": True, "evidence_quote": "我算错了"}],
                  "review_plan": [{"topic": "移项法则", "deadline": "3月1日", "evidence_quote": "下月考试"}]}
        facts = facts_from_notes(sample)
        self.assertEqual(facts, [
            "档案: 学习阶段=高中（自述）",
            "概念: 一元一次方程（linear-eq）；前备: 等式性质；关联: 移项",
            "易错点: linear-eq — 概念性误解",
            "复习安排: 移项法则（截止: 3月1日）",
        ])
        for fact in facts:
            self.assertNotIn("我在读高中", fact)
            self.assertNotIn("我算错了", fact)
            self.assertNotIn("下月考试", fact)

    def test_empty_notes_produce_no_facts(self):
        self.assertEqual(facts_from_notes(notes()), [])


class FoundryMemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inner = memory.LearningStore(Path(self.temp.name) / "memory.sqlite3")
        self.client = FakeFoundryClient()
        self.store = FoundryMemoryStore(self.inner, self.client, "m")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enable(self, session="s"):
        self.store.set_enabled(session, True)

    def apply(self, turn="1", session="s", content=None, learner_text="我在读高中"):
        return self.store.apply_notes(session, turn, content or profile_notes(), learner_text)

    def test_mirrors_facts_only_after_a_successful_inner_write(self):
        self.enable()
        self.assertTrue(self.apply())
        self.assertEqual(self.client.created,
                         [("m", "s", "档案: 学习阶段=高中（自述）", "user_profile")])

    def test_duplicate_turn_never_mirrors_twice(self):
        self.enable()
        self.apply("1")
        self.assertFalse(self.apply("1"))
        self.assertEqual(len(self.client.created), 1)

    def test_empty_notes_mirror_nothing(self):
        self.enable()
        self.assertTrue(self.apply(content=notes()))
        self.assertEqual(self.client.created, [])

    def test_mirror_failure_never_propagates_and_inner_data_stays_intact(self):
        store = FoundryMemoryStore(self.inner, ExplodingClient(), "m")
        store.set_enabled("s", True)
        self.assertTrue(store.apply_notes("s", "1", profile_notes(), "我在读高中"))
        self.assertIn("learning_stage", store.snapshot("s")["profile"])

    def test_snapshot_appends_semantic_when_enabled(self):
        self.enable()
        self.client.memories = [("id1", "档案: 学习阶段=高中"), ("id2", "概念: 分数")]
        snapshot = self.store.snapshot("s")
        self.assertEqual(snapshot["semantic"],
                         [{"id": "id1", "content": "档案: 学习阶段=高中"},
                          {"id": "id2", "content": "概念: 分数"}])
        self.assertEqual(self.client.searched[0][:2], ("m", "s"))

    def test_snapshot_skips_search_when_records_are_off(self):
        self.assertFalse(self.store.is_enabled("s"))
        snapshot = self.store.snapshot("s")
        self.assertEqual(snapshot["semantic"], [])
        self.assertEqual(self.client.searched, [])

    def test_snapshot_degrades_to_sqlite_on_search_error(self):
        store = FoundryMemoryStore(self.inner, ExplodingClient(), "m")
        store.set_enabled("s", True)
        snapshot = store.snapshot("s")
        self.assertEqual(snapshot["semantic"], [])
        self.assertIn("schema_version", snapshot)

    def test_snapshot_falls_back_to_listing_when_search_is_empty(self):
        self.enable()
        self.client.items = [("id1", "档案: 学习阶段=高中")]
        snapshot = self.store.snapshot("s")
        self.assertEqual(snapshot["semantic"], [{"id": "id1", "content": "档案: 学习阶段=高中"}])
        self.assertEqual(self.client.listed, [("m", "s", 6)])

    def test_snapshot_prefers_search_results_over_listing(self):
        self.enable()
        self.client.memories = [("sid", "档案: 语义命中")]
        self.client.items = [("lid", "档案: 列表兜底")]
        snapshot = self.store.snapshot("s")
        self.assertEqual(snapshot["semantic"], [{"id": "sid", "content": "档案: 语义命中"}])
        self.assertEqual(self.client.listed, [])

    def test_semantic_content_is_capped(self):
        self.enable()
        self.client.memories = [("id1", "长" * 500)]
        self.assertEqual(self.store.snapshot("s")["semantic"][0]["content"], "长" * 300)

    def test_clear_deletes_scope_and_swallows_errors(self):
        self.enable()
        self.apply()
        self.store.clear("s")
        self.assertEqual(self.client.deleted, [("m", "s")])
        self.assertFalse(self.store.is_enabled("s"))
        exploding = FoundryMemoryStore(self.inner, ExplodingClient(), "m")
        exploding.set_enabled("s", True)
        exploding.clear("s")  # raising client still completes
        self.assertFalse(exploding.is_enabled("s"))

    def test_enabled_delegates_to_inner_only(self):
        self.enable("s")
        self.assertTrue(self.inner.is_enabled("s"))
        self.assertTrue(self.store.is_enabled("s"))
        self.store.set_enabled("s", False)
        self.assertFalse(self.inner.is_enabled("s"))

    def test_close_closes_inner_and_client(self):
        self.store.close()
        self.assertTrue(self.client.closed)


class ServerWiringTests(unittest.TestCase):
    def test_unset_env_returns_plain_store_and_never_touches_credentials(self):
        from learning_coach import server
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.dict(os.environ, {"LEARNING_COACH_MEMORY_STORE": ""}, clear=False), \
                mock.patch.object(server, "DefaultAzureCredential",
                                  side_effect=AssertionError("credentials must not be built")):
            store = server._memory_store(Path(temp), None)
        self.assertIsInstance(store, memory.LearningStore)
        self.assertNotIsInstance(store, FoundryMemoryStore)

    def test_set_env_wraps_with_the_foundry_client(self):
        from learning_coach import server
        fake = FakeFoundryClient()
        project_client = SimpleNamespace(beta=SimpleNamespace(memory_stores=fake))
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.dict(os.environ, {"LEARNING_COACH_MEMORY_STORE": "m",
                                             "FOUNDRY_PROJECT_ENDPOINT": "https://example.test"}, clear=False), \
                mock.patch("azure.ai.projects.AIProjectClient", return_value=project_client):
            store = server._memory_store(Path(temp), object())
        self.assertIsInstance(store, FoundryMemoryStore)
        self.assertIs(store._client, fake)
