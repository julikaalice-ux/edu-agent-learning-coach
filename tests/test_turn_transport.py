"""The UI sees checked text and an explicit outcome, never rejected drafts."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'agent'), str(ROOT/'backend'), str(ROOT/'tests')]
import proxy
from agent_framework import AgentSession
from learning_coach.api.host import LearningCoachAgent, history_text, wire_text
from learning_coach.service.execution import TURN_OUTCOME, TurnOutcome
from test_multi_agent_host import FakePipeline, FINAL

class TurnTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_draft_never_crosses_host_and_failure_is_explicit(self):
        class Failed(FakePipeline):
            async def run_stream(self, inputs, **kwargs):
                yield {'delta': 'INVALID DRAFT'}
                TURN_OUTCOME.get().status = 'failed'
                TURN_OUTCOME.get().error_code = 'invalid_stage_output'
                yield {'output': FINAL, 'state': {}}
        with patch.dict('os.environ', {'LEARNING_COACH_UI_PROTOCOL':'1'}):
            stream = LearningCoachAgent(Failed()).run('test', stream=True, session=AgentSession())
            updates = [u.text async for u in stream]
        self.assertEqual(len(updates),1)
        self.assertNotIn('INVALID DRAFT',updates[0])
        frame=json.loads(updates[0])
        self.assertEqual(frame['status'],'failed')
        self.assertEqual(frame['error_code'],'invalid_stage_output')

    def test_split_preview_replaced_by_final_and_explicit_status(self):
        preview=wire_text('checked explanation',TurnOutcome(),preview=True)
        final=wire_text('final explanation',TurnOutcome('partial','preparation_failed'))
        raw=preview+final
        events=[('response.output_text.delta',json.dumps({'delta':raw[i:i+3]})) for i in range(0,len(raw),3)]
        events.append(('response.completed',json.dumps({'response':{'output':[{'type':'message','content':[{'type':'output_text','text':raw}]}]}})))
        with patch.object(proxy,'stream_events',return_value=iter(events)):
            actual=list(proxy.stream_reply('conversation','question'))
        self.assertEqual(actual[0],{'replace':'checked explanation'})
        self.assertEqual(actual[-1]['text'],'final explanation')
        self.assertEqual(actual[-1]['status'],'partial')
        self.assertEqual(history_text(raw),'final explanation')

    def test_disconnect_and_upstream_failure_cannot_be_success(self):
        for events in ([], [('response.failed','{}')], [('response.output_text.delta','{"delta":"unfinished"}')]):
            with patch.object(proxy,'stream_events',return_value=iter(events)):
                with self.assertRaises(RuntimeError): list(proxy.stream_reply('c','q'))

    def test_plain_legacy_reply_stays_readable_without_claiming_success(self):
        self.assertEqual(proxy.decode_turn('旧版回复')['status'],'unknown')
        self.assertEqual(proxy.decode_turn('旧版回复')['text'],'旧版回复')
