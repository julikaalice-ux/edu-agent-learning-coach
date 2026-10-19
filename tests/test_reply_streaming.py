"""The coordinator's reply text is shown while it is written, only where the program keeps it."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning_coach.db.memory import LearningStore
from learning_coach.domain.partial_reply import partial_message, written_value
from learning_coach.service.execution import REPLY_SINK
from learning_coach.service.pipeline import LearningPipeline
from learning_coach.service.pipeline.streaming import ReplyStream
from learning_coach.service.pipeline.texts import INTRO, INTRO_RECORDS_OFF
from test_pipeline import FakeArtifacts, FakeResearch, FakeRunner, RESPONSE_SCHEMA, messages, plan

ANSWER = {"schema_version": "1.0", "intent": {"primary": "A", "is_implicit": False},
          "message": "分母表示整体平均分成的份数，\n分子表示取出的份数。",
          "next_step": {"kind": "question", "text": "四分之三的分母是几？"}}


def visible(output):
    step = output.get("next_step")
    return output["message"] + ("\n\n" + step["text"] if step else "")


class StreamingRunner(FakeRunner):
    """Writes a coordinator reply in small pieces whenever the program lets the runner stream."""

    def __init__(self, *args, piece=5, **kwargs):
        super().__init__(*args, **kwargs)
        self.piece = piece
        self.streamed = []

    async def __call__(self, role, payload, schema):
        result = await super().__call__(role, payload, schema)
        sink = REPLY_SINK.get()
        if sink is not None:
            self.streamed.append(payload.get("phase"))
            # The model's spacing (", " and ": "), and non-ASCII text as written.
            raw = json.dumps(result, ensure_ascii=False)
            for end in range(self.piece, len(raw) + self.piece, self.piece):
                sink(raw[:end])
                await asyncio.sleep(0)
        return result


class PartialReplyTests(unittest.TestCase):
    def test_reads_the_message_while_it_is_written(self):
        raw = '{"mode": "answer", "response": {"intent": {"primary": "B"}, "message": "先看\\n分母'
        text, head = partial_message(raw)
        self.assertEqual(text, "先看\n分母")
        self.assertEqual(written_value(head, "primary"), "B")
        self.assertEqual(written_value(head, "mode"), "answer")

    def test_nothing_before_the_message_starts(self):
        self.assertEqual(partial_message('{"mode": "answer", "response": {"mes'), (None, '{"mode": "answer", "response": {"mes'))

    def test_waits_for_split_escapes_and_surrogate_halves(self):
        self.assertEqual(partial_message('{"message": "a\\')[0], "a")
        self.assertEqual(partial_message('{"message": "a\\u4e')[0], "a")
        self.assertEqual(partial_message('{"message": "a\\u4e2d')[0], "a中")
        self.assertEqual(partial_message('{"message": "a\\ud83d')[0], "a")
        self.assertEqual(partial_message('{"message": "a\\ud83d\\ude00')[0], "a😀")

    def test_stops_at_the_end_of_the_string_and_at_what_the_parse_rejects(self):
        self.assertEqual(partial_message('{"message": "完整", "next_step": null}')[0], "完整")
        self.assertEqual(partial_message('{"message": "一行\n二行"')[0], "一行")
        self.assertEqual(partial_message('{"message": "坏\\x41"')[0], "坏")


class ReplyStreamTests(unittest.TestCase):
    def test_sends_only_new_text(self):
        pieces = []
        stream = ReplyStream(pieces.append)
        for raw in ('{"message": "', '{"message": "分', '{"message": "分母', '{"message": "分母", "x'):
            stream(raw)
        self.assertEqual(pieces, ["分", "母"])

    def test_a_retry_that_writes_the_same_text_continues_and_a_different_one_stops(self):
        pieces = []
        stream = ReplyStream(pieces.append)
        stream('{"message": "分母表示')
        stream('{"message": "分母')       # a retry starts again from empty text
        stream('{"message": "分母表示总份数')
        self.assertEqual("".join(pieces), "分母表示总份数")
        stream('{"message": "完全不同')
        stream('{"message": "完全不同的说法，更长')
        self.assertTrue(stream.closed)
        self.assertEqual("".join(pieces), "分母表示总份数")

    def test_a_reply_that_may_not_be_shown_is_never_sent(self):
        pieces = []
        stream = ReplyStream(pieces.append, allow=lambda head: written_value(head, "primary") != "C")
        stream('{"response": {"intent": {"primary": "C"}, "message": "来做题')
        self.assertEqual(pieces, [])
        self.assertTrue(stream.closed)


class PipelineStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "learning.sqlite3")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner, research=None):
        return LearningPipeline(runner, research or FakeResearch(), self.store, FakeArtifacts(), RESPONSE_SCHEMA)

    async def events(self, pipeline, text, **kwargs):
        kwargs.setdefault("returning_visitor", True)
        return [event async for event in pipeline.run_stream(messages(text), session_id=kwargs.pop("session_id", "s"),
                                                             **kwargs)]

    @staticmethod
    def shown(events):
        return "".join(event["delta"] for event in events if "delta" in event)

    async def test_a_direct_answer_is_shown_while_it_is_written(self):
        runner = StreamingRunner([plan()], replies=[deepcopy(ANSWER)])
        events = await self.events(self.pipeline(runner), "分子和分母是什么关系？")
        self.assertEqual(runner.streamed, ["decide"])
        self.assertGreater(sum("delta" in event for event in events), 2)
        self.assertEqual(self.shown(events), ANSWER["message"])
        self.assertTrue(visible(events[-1]["output"]).startswith(self.shown(events)))
        self.assertNotIn('"', self.shown(events))

    async def test_a_first_question_shows_the_introduction_first(self):
        runner = StreamingRunner([plan()], replies=[deepcopy(ANSWER)])
        events = await self.events(self.pipeline(runner), "分子和分母是什么关系？", returning_visitor=False)
        first = next(event["delta"] for event in events if "delta" in event)
        self.assertTrue(first.startswith(INTRO + INTRO_RECORDS_OFF + "\n\n"))
        self.assertEqual(events[-1]["output"]["message"], self.shown(events))

    async def test_practice_written_as_a_direct_answer_is_not_shown(self):
        # The program turns it into a quiz, so the draft would be thrown away.
        runner = StreamingRunner([plan("C")])
        events = await self.events(self.pipeline(runner), "给我出几道分数题")
        self.assertEqual(self.shown(events), "")
        self.assertIn("decide", runner.streamed)

    async def test_nothing_is_streamed_around_a_quiz_question(self):
        runner = StreamingRunner([plan("C", quiz_action="start", quiz_topic="分数", quiz_total=2),
                                  plan("C", quiz_action="continue", quiz_topic="分数", quiz_total=2)])
        pipeline = self.pipeline(runner)
        first = await self.events(pipeline, "出两道分数题")
        output, state = first[-1]["output"], first[-1]["state"]
        self.assertEqual(self.shown(first), "")
        second = await self.events(pipeline, "A", state=state)
        self.assertEqual(self.shown(second), "")
        self.assertEqual(runner.streamed, ["decide"])  # the first decision only; it has no message field
        self.assertIsNotNone(output["next_step"])

    async def test_the_reply_after_research_is_shown_while_it_is_written(self):
        reply = {**deepcopy(ANSWER), "next_step": None}
        runner = StreamingRunner([plan(research_query="分数的定义")], replies=[reply])
        events = await self.events(self.pipeline(runner), "查一下分数的定义", external_permissions={"research"})
        self.assertEqual(runner.streamed, ["decide", "respond"])
        self.assertEqual(self.shown(events), ANSWER["message"])
        self.assertTrue(events[-1]["output"]["message"].startswith(ANSWER["message"]))

    async def test_without_a_listener_the_runner_is_never_asked_to_stream(self):
        runner = StreamingRunner([plan()], replies=[deepcopy(ANSWER)])
        await self.pipeline(runner).run(messages("分子和分母是什么关系？"), session_id="s", returning_visitor=True)
        self.assertEqual(runner.streamed, [])


class RunnerStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_streams_each_attempt_and_parses_the_whole_reply(self):
        try:
            from learning_coach.adapters.runner import ROLES, StructuredRunner
        except ImportError as error:  # the SDK is not installed
            self.skipTest(str(error))
        replies = iter(['{"ok": tru', '{"ok": true}'])

        class Stream:
            def __init__(self, text):
                self.text = text

            def __aiter__(self):
                async def pieces():
                    for index in range(0, len(self.text), 3):
                        yield SimpleNamespace(text=self.text[index:index + 3])
                return pieces()

            async def get_final_response(self):
                return SimpleNamespace(text=self.text)

        class Agent:
            def __init__(self):
                self.streams = 0

            def run(self, content, *, options=None, stream=False):
                assert stream
                self.streams += 1
                return Stream(next(replies))

        agent = Agent()
        agents = {role: agent for role in ROLES}
        runner = StructuredRunner(agents, retry_delay=0)
        seen = []
        token = REPLY_SINK.set(seen.append)
        try:
            result = await runner("coordinator", {"phase": "respond"},
                                  {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                                   "additionalProperties": False})
        finally:
            REPLY_SINK.reset(token)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(agent.streams, 2)
        self.assertEqual(seen[0], '{"o')
        self.assertIn('{"ok": true}', seen)
        self.assertEqual(seen[seen.index('{"ok": tru') + 1], '{"o')  # the retry starts from empty text


if __name__ == "__main__":
    unittest.main()
