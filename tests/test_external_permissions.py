"""Program-owned, one-request authorization; plans cannot grant themselves access."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from test_pipeline import (FakeArtifacts, FakeResearch, FakeRunner, LearningPipeline,
                           LearningStore, RESPONSE_SCHEMA, messages, plan)
from test_direct_routing import tool_decision
from learning_coach.service.execution import EXTERNAL_PERMISSIONS


class ExternalPermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / 'notes.sqlite3')
        self.addCleanup(self.store.close)
        self.addCleanup(self.temp.cleanup)
        self.research = FakeResearch()
        self.artifacts = FakeArtifacts()
        self.text = '帮我查证分数定义，再画图解释'

    def pipeline(self, runner):
        return LearningPipeline(runner, self.research, self.store, self.artifacts, RESPONSE_SCHEMA)

    def research_plan(self, **kwargs):
        return plan(research_query='分数定义', **kwargs)

    async def test_no_network_or_visual_side_effect_before_program_approval(self):
        runner = FakeRunner([self.research_plan(visual_request='分数图')])
        pipeline = self.pipeline(runner)
        output, state = await pipeline.run(messages(self.text), session_id='s')
        self.assertEqual(state['pending_external']['capabilities'], ['research'])
        self.assertIn('允许本次外部访问', output['next_step']['text'])
        self.assertEqual(self.research.calls, [])
        self.assertEqual(self.artifacts.prepared, [])
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(runner.calls[0]['payload']['capabilities']['research']['authorized'])

    async def test_approve_restores_original_question_and_grant_expires(self):
        runner = FakeRunner([self.research_plan(), self.research_plan(), self.research_plan()])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages(self.text), session_id='s', turn_id='1')
        output, state = await pipeline.run(messages('允许本次外部访问'), session_id='s', state=state, turn_id='2')
        self.assertEqual(self.research.calls, ['分数定义'])
        self.assertNotIn('pending_external', state)
        decided = runner.calls_for('coordinator', 'decide')[-1]
        self.assertEqual(decided['messages'][-1]['text'], self.text)
        self.assertTrue(decided['capabilities']['research']['authorized'])
        self.assertEqual(EXTERNAL_PERMISSIONS.get(), frozenset())
        _, later = await pipeline.run(messages('另外查证小数'), session_id='s', state=state, turn_id='3')
        self.assertIn('pending_external', later)
        self.assertEqual(len(self.research.calls), 1)

    async def test_successive_capability_approvals_do_not_lose_first_grant(self):
        runner = FakeRunner([self.research_plan(),
                             self.research_plan(visual_request='参考图', visual_source='web'),
                             self.research_plan(visual_request='参考图', visual_source='web')])
        pipeline = self.pipeline(runner)
        pipeline.image_finder = object()  # configured capability; local fallback does not call it
        _, state = await pipeline.run(messages(self.text), session_id='s', turn_id='1')
        _, state = await pipeline.run(messages('允许本次外部访问'), session_id='s', state=state, turn_id='2')
        self.assertEqual(state['pending_external']['granted'], ['research'])
        self.assertEqual(state['pending_external']['capabilities'], ['image_search'])
        _, state = await pipeline.run(messages('允许本次外部访问'), session_id='s', state=state, turn_id='3')
        self.assertNotIn('pending_external', state)
        self.assertEqual(self.research.calls, ['分数定义'])

    async def test_denial_prevents_repeated_external_plan_and_early_research_claim(self):
        decision = tool_decision(self.text, research_query='分数定义', visual_request='参考图', visual_source='web')
        decision['initial_explanation'] = '根据查到的新资料得到的结论。'
        runner = FakeRunner([decision, decision])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages(self.text), session_id='s', turn_id='1')
        events = [event async for event in pipeline.run_stream(messages('先不联网，继续讲解'),
                  session_id='s', state=state, turn_id='2')]
        self.assertEqual(len(events), 1)
        self.assertNotIn('pending_external', events[-1]['state'])
        self.assertEqual(self.research.calls, [])
        self.assertTrue(runner.calls_for('coordinator', 'respond')[-1]['external_declined'])
        self.assertEqual(runner.calls_for('visualizer')[-1]['visual_source'], 'local')

    async def test_nonapproval_message_discards_pending_and_cannot_authorize_later(self):
        runner = FakeRunner([self.research_plan(), plan(), self.research_plan()])
        pipeline = self.pipeline(runner)
        _, state = await pipeline.run(messages(self.text), session_id='s', turn_id='1')
        _, state = await pipeline.run(messages('先解释减法'), session_id='s', state=state, turn_id='2')
        self.assertNotIn('pending_external', state)
        _, state = await pipeline.run(messages('允许本次外部访问'), session_id='s', state=state, turn_id='3')
        self.assertIn('pending_external', state)
        self.assertEqual(self.research.calls, [])

    async def test_replay_of_approval_does_not_repeat_search(self):
        runner = FakeRunner([self.research_plan(), self.research_plan()])
        pipeline = self.pipeline(runner)
        _, before = await pipeline.run(messages(self.text), session_id='s', turn_id='1')
        first, after = await pipeline.run(messages('允许本次外部访问'), session_id='s', state=before, turn_id='2')
        replay, _ = await pipeline.run(messages('允许本次外部访问'), session_id='s', state=before, turn_id='2')
        self.assertEqual(first, replay)
        self.assertEqual(self.research.calls, ['分数定义'])

    async def test_host_grant_does_not_leak_to_concurrent_session(self):
        started, release = asyncio.Event(), asyncio.Event()
        class DelayedResearch(FakeResearch):
            async def run(self, query, queries=None):
                started.set()
                await release.wait()
                return await super().run(query, queries)
        self.research = DelayedResearch()
        runner = FakeRunner([self.research_plan(), self.research_plan()])
        pipeline = self.pipeline(runner)
        first = asyncio.create_task(pipeline.run(messages(self.text), session_id='yes', external_permissions={'research'}))
        await asyncio.wait_for(started.wait(), 1)
        try:
            _, blocked = await pipeline.run(messages(self.text), session_id='no')
            self.assertIn('pending_external', blocked)
        finally:
            release.set()
            await first
        self.assertEqual(self.research.calls, ['分数定义'])

    async def test_pending_permission_cannot_move_to_another_session(self):
        pipeline = self.pipeline(FakeRunner([self.research_plan()]))
        _, state = await pipeline.run(messages(self.text), session_id='s')
        with self.assertRaisesRegex(ValueError, 'owner_mismatch'):
            await pipeline.run(messages('允许本次外部访问'), session_id='other', state=state)
        self.assertEqual(self.research.calls, [])

    async def test_unknown_host_permission_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unknown_external_permission'):
            await self.pipeline(FakeRunner()).run(messages(self.text), session_id='s', external_permissions={'all'})


if __name__ == '__main__':
    unittest.main()
