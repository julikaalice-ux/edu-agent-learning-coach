"""Hosted learning records belong to the conversation unless records are scoped to the user."""

from pathlib import Path
import importlib.util
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SDK_AVAILABLE = importlib.util.find_spec("agent_framework") is not None
RECORDS_ON = '/learning {"action":"records","enabled":true}'
READ = '/learning {"action":"read"}'


@unittest.skipUnless(SDK_AVAILABLE, "agent_framework is not installed")
class RecordsScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from learning_coach.api.host import LearningCoachAgent
        from learning_coach.db.memory import LearningStore
        from learning_coach.service.pipeline import LearningPipeline
        from test_pipeline import FakeArtifacts, FakeResearch, FakeRunner, RESPONSE_SCHEMA
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")
        pipeline = LearningPipeline(FakeRunner(), FakeResearch(), self.store, FakeArtifacts(), RESPONSE_SCHEMA)
        self.host = LearningCoachAgent(pipeline)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def enabled(self, session, user, text=READ):
        context = SimpleNamespace(user_id=user)
        with patch("learning_coach.api.host.get_request_context", return_value=context):
            response = await self.host.run(text, session=session)
        return json.loads(response.text)["view"]["enabled"]

    async def test_visitors_sharing_one_identity_do_not_share_records(self):
        first, second = self.host.create_session(), self.host.create_session()
        self.assertTrue(await self.enabled(first, "space-service-principal", RECORDS_ON))
        self.assertFalse(await self.enabled(second, "space-service-principal"))
        self.assertTrue(await self.enabled(first, "space-service-principal"))
        self.assertFalse(self.store.is_enabled("space-service-principal"))

    async def test_another_user_on_the_same_conversation_starts_empty(self):
        session = self.host.create_session()
        self.assertTrue(await self.enabled(session, "user-a", RECORDS_ON))
        self.assertFalse(await self.enabled(session, "user-b"))

    async def test_user_scope_keeps_records_across_conversations(self):
        with patch.dict("os.environ", {"LEARNING_COACH_RECORDS_SCOPE": "user"}):
            self.assertTrue(await self.enabled(self.host.create_session(), "user-a", RECORDS_ON))
            self.assertTrue(await self.enabled(self.host.create_session(), "user-a"))
        self.assertTrue(self.store.is_enabled("user-a"))

    def test_without_a_platform_user_records_stay_with_the_session(self):
        from learning_coach.api.host import records_key
        self.assertIsNone(records_key(None, "s"))
        self.assertNotEqual(records_key("u", "s1"), records_key("u", "s2"))
        self.assertLessEqual(len(records_key("u" * 200, "s" * 200)), 200)


if __name__ == "__main__":
    unittest.main()
