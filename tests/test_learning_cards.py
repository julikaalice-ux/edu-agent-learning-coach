"""End-to-end local records protocol and deterministic retry boundary; no network."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from learning_coach.db.memory import LearningStore
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.service.pipeline.shell import clean_history
from tests.test_pipeline import FakeRunner, FakeResearch, FakeArtifacts, RESPONSE_SCHEMA, notes

QUESTION = {'question': '2/7 + 3/7 = ?', 'choices': ['5/7','5/14','6/7'], 'answer_index': 0,
            'explanation': '分母不变，分子相加。', 'concept_id': 'fractions'}
STUDY = {'concept_id': 'fractions', 'title': '同分母分数加法', 'summary': '分母不变，分子相加。',
         'explanation': '每份大小相同，合并份数。', 'example': '2/7+3/7=5/7', 'pitfall': '不能相加分母',
         'conditions': '分母相同', 'source_turns': [1]}

class CardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.path = Path(self.temp.name)/'db'
        self.store = LearningStore(self.path); self.store.set_enabled('s',True)
    def tearDown(self):
        self.store.close(); self.temp.cleanup()
    def seed(self, independent=True):
        evidence = {'kind':'quiz','correct':False,'independent':independent,'actor':'learner',
            'difficulty':'简单','concept_id':'fractions','question_snapshot':QUESTION,'selected':1,'topic':'分数'}
        return self.store.apply_notes('s','wrong',notes(),'B',evidence)
    def test_wrong_answer_creates_card_without_private_fields(self):
        self.seed(); card = self.store.learning_view('s')['mistakes'][0]
        self.assertEqual(card['question'], QUESTION['question'])
        for key in ('answer_index','answer','explanation','original_choice'):
            self.assertNotIn(key,card)
        self.assertEqual(card['choices'][0], {'id':'0','text':'5/7'})
        self.assertEqual(self.store.snapshot('s')['concepts'][0]['attempts'],1)
    def test_companion_and_disabled_do_not_save(self):
        self.seed(False); self.assertEqual(self.store.learning_view('s')['mistakes'],[])
        self.store.set_enabled('s',False); self.assertFalse(self.seed())
    def test_retry_idempotency_and_no_mastery_inflation(self):
        self.seed(); card = self.store.learning_view('s')['mistakes'][0]['id']; before=self.store.snapshot('s')
        first=self.store.retry_card('s',card,'attempt','0')
        self.assertTrue(first['correct']); self.assertFalse(first['assisted'])
        self.assertEqual(first,self.store.retry_card('s',card,'attempt','0'))
        self.assertEqual(self.store.learning_view('s')['mistakes'][0]['attempts'],1)
        with self.assertRaises(ValueError): self.store.retry_card('s',card,'attempt','1')
        self.assertEqual(before,self.store.snapshot('s'))
    def test_reveal_and_cross_owner_checks(self):
        self.seed(); card=self.store.learning_view('s')['mistakes'][0]['id']
        self.store.set_enabled('other',True)
        with self.assertRaises(ValueError): self.store.retry_card('other',card,'attempt','0')
        with self.assertRaises(ValueError): self.store.retry_card('s',card,'attempt','99')
        self.store.retry_card('s',card,'view',reveal=True)
        self.assertTrue(self.store.retry_card('s',card,'attempt','0')['assisted'])
        self.store.set_enabled('s',False)
        self.assertEqual(self.store.learning_view('s')['mistakes'],[])
        with self.assertRaises(ValueError): self.store.retry_card('s',card,'other','0')
    def test_study_persists_edits_and_clear(self):
        self.store.apply_notes('s','note',{**notes(),'study_notes':[STUDY]},'')
        self.store.close(); self.store=LearningStore(self.path)
        card=self.store.learning_view('s')['study_notes'][0]
        fields={k:card[k] for k in ('title','summary','explanation','example','pitfall','conditions')}
        fields['summary']='修改后的个人笔记'
        self.store.edit_study_note('s',card['id'],fields)
        self.assertIn('未经核验',self.store.learning_view('s')['study_notes'][0]['source'])
        self.store.clear('s'); self.store.set_enabled('s',True)
        self.assertEqual(self.store.learning_view('s')['study_notes'],[])

class LearningProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.store=LearningStore(Path(self.temp.name)/'db')
        self.runner=FakeRunner([],note_results=[{**notes(),'study_notes':[STUDY]}])
        self.pipeline=LearningPipeline(self.runner,FakeResearch(),self.store,FakeArtifacts(),RESPONSE_SCHEMA)
        self.history=[]; self.state={}; self.counter=0
    async def asyncTearDown(self):
        self.store.close(); self.temp.cleanup()
    async def action(self, action, **values):
        self.counter+=1
        message={'role':'user','text':'/learning '+json.dumps({'action':action,**values})}
        output,self.state=await self.pipeline.run(self.history+[message],session_id='s',state=self.state,turn_id=str(self.counter))
        self.history += [message,{'role':'assistant','text':output['message']}]
        return json.loads(output['message'])
    async def test_controls_never_call_model_and_actual_toggle(self):
        result=await self.action('records',enabled=True)
        self.assertTrue(result['view']['enabled']); self.assertTrue(self.store.is_enabled('s'))
        result=await self.action('records',enabled=False)
        self.assertFalse(result['view']['enabled']); self.assertEqual(self.runner.calls,[])
    async def test_compile_real_card_and_filter_control_history(self):
        await self.action('records',enabled=True)
        self.history += [{'role':'user','text':'讲讲分数加法'}, {'role':'assistant','text':'分母相同，分子相加。例如2/7+3/7=5/7。'}]
        result=await self.action('compile')
        self.assertEqual(result['view']['study_notes'][0]['summary'],STUDY['summary'])
        request=self.runner.calls_for('notes')[0]
        self.assertEqual(request['teaching_messages'][0]['turn'],1)
        self.assertNotIn('learning_protocol',json.dumps(request))
        self.assertEqual(self.state['context']['notes_upto_turn'],1)
        self.history += [{'role':'user','text':'再讲一个例子'}, {'role':'assistant','text':'1/7+3/7=4/7。'}]
        self.runner.note_results.append({**notes(),'study_notes':[{**STUDY,'source_turns':[2]}]})
        result=await self.action('compile')
        self.assertEqual(len(result['view']['study_notes']),2)
    async def test_minor_consent_not_bypassed(self):
        self.state={'onboarding':{'who':'self','stage':'小学','step':'done','records':'off'}}
        result=await self.action('records',enabled=True)
        self.assertFalse(result['view']['enabled']); self.assertIn('家长',result['message'])
    async def test_bad_source_rejects_whole_note(self):
        self.store.set_enabled('s',True)
        self.history=[{'role':'user','text':'讲讲分数'}, {'role':'assistant','text':'分母表示等分的份数。'}]
        self.runner.note_results.clear();self.runner.note_results.append({**notes(),'study_notes':[{**STUDY,'source_turns':[999]}]})
        result=await self.action('compile')
        self.assertEqual(result['view']['study_notes'],[]); self.assertIn('没有整理成功',result['message'])

    async def test_full_quiz_flow_saves_wrong_question(self):
        from tests.test_pipeline import plan, QUIZ
        self.store.set_enabled('s',True)
        self.runner.plans.extend([plan('C',quiz_action='start',quiz_topic='分数'), plan('C',quiz_action='continue',quiz_topic='分数')])
        first,state=await self.pipeline.run([{'role':'user','text':'考我分数'}],session_id='s',turn_id='quiz-start')
        history=[{'role':'user','text':'考我分数'},{'role':'assistant','text':first['message']+' '+first['next_step']['text']},{'role':'user','text':'B'}]
        _,state=await self.pipeline.run(history,session_id='s',state=state,turn_id='quiz-answer')
        cards=self.store.learning_view('s')['mistakes']
        self.assertEqual(len(cards),1);self.assertEqual(cards[0]['question'],QUIZ['question'])
        self.assertNotIn('ANSWER_KEY_PRIVATE',json.dumps(cards))

    async def test_host_transports_controls_with_session_owner(self):
        from learning_coach.api.host import LearningCoachAgent
        from unittest.mock import patch
        host=LearningCoachAgent(self.pipeline); session=host.create_session()
        with patch('learning_coach.api.host.platform_learner_id',return_value='owner-a'):
            response=await host.run('/learning {"action":"records","enabled":true}',session=session)
            self.assertTrue(json.loads(response.text)['view']['enabled'])
            self.assertTrue(self.store.is_enabled('owner-a'))
        with patch('learning_coach.api.host.platform_learner_id',return_value='owner-b'):
            response=await host.run('/learning {"action":"read"}',session=session)
            self.assertFalse(json.loads(response.text)['view']['enabled'])

class ProxyProtocolTests(unittest.TestCase):
    def test_learning_proxy_uses_same_conversation_and_no_text_heuristics(self):
        import importlib.util
        from unittest.mock import patch
        path=Path(__file__).resolve().parents[1]/'backend/proxy.py'
        spec=importlib.util.spec_from_file_location('learning_proxy_test',path)
        proxy=importlib.util.module_from_spec(spec);spec.loader.exec_module(proxy)
        envelope={'learning_protocol':1,'ok':True,'message':'','view':{'enabled':False,'mistakes':[],'study_notes':[]}}
        upstream={'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(envelope)}]}]}
        with patch.object(proxy,'call',return_value=upstream) as call:
            result=proxy.learning_action('conversation-1',{'action':'read'})
        self.assertEqual(result['conversation'],'conversation-1')
        self.assertEqual(call.call_args.args[1]['conversation'],'conversation-1')
        self.assertTrue(call.call_args.args[1]['input'].startswith('/learning '))
        with patch.object(proxy,'call',return_value={'output':[]}), self.assertRaisesRegex(RuntimeError,'learning_protocol_unavailable'):
            proxy.learning_action('conversation-1',{'action':'read'})
