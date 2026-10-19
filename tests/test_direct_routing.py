"""Decision-driven execution: no unnecessary second model call or blanket fan-out."""

import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_pipeline import FakeArtifacts, FakeResearch, FakeRunner, LearningPipeline, LearningStore
from test_pipeline import RESPONSE_SCHEMA, messages, notes, plan, validate
from learning_coach.adapters.runner import StageFailure


TOOLS = ("research", "quiz", "notes", "visual")


def tool_decision(text, primary="A", **updates):
    proposed = plan(primary, **updates)
    selected = {"research": bool(proposed["research_query"]),
                "quiz": proposed["quiz_action"] in ("start", "continue"),
                "notes": proposed["observe_learning"], "visual": bool(proposed["visual_request"])}
    codes = {"research": "explicit_lookup", "quiz": "continue_practice" if proposed["quiz_action"] == "continue" else "explicit_practice",
             "notes": "profile_update", "visual": "explicit_visual"}
    proposed["tool_reasons"] = {key: {"code": codes[key], "evidence_quote": text} if selected[key] else None for key in TOOLS}
    return {"mode": "tools", "plan": proposed}


def direct_decision(primary="A", *, end=False, pause=False, message="先从已有知识出发。"):
    return {"mode": "answer", "response": {"schema_version": "1.0", "intent": {"primary": primary, "is_implicit": False},
            "message": message, "next_step": {"kind": "question", "text": "你想先看哪个部分？"}},
            "task": {"task_summary": "学习分数", "pause_task": pause, "end_task": end}}


class DirectRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These orchestration fixtures model a host-authorized research turn.
        # Default-deny and consent lifecycle have separate boundary regressions.
        from learning_coach.service.execution import EXTERNAL_PERMISSIONS
        token = EXTERNAL_PERMISSIONS.set(frozenset({"research"}))
        self.addCleanup(EXTERNAL_PERMISSIONS.reset, token)
        self.temp = tempfile.TemporaryDirectory()
        self.store = LearningStore(Path(self.temp.name) / "notes.sqlite3")
        self.artifacts = FakeArtifacts()
        self.research = FakeResearch()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def pipeline(self, runner, research=None):
        return LearningPipeline(runner, research or self.research, self.store, self.artifacts, RESPONSE_SCHEMA)

    def assert_wire(self, output):
        validate(output, RESPONSE_SCHEMA)
        self.assertEqual(set(output), {"schema_version", "intent", "message", "next_step"})

    def spy_evidence(self):
        """Quiz evidence the program writes each turn (notes themselves are deferred)."""
        captured, original = [], self.store.apply_notes

        def record(*args, **kwargs):
            captured.append(kwargs.get("quiz_evidence"))
            return original(*args, **kwargs)
        return patch.object(self.store, "apply_notes", side_effect=record), captured

    @staticmethod
    def execution(state):
        return {key: state["last_execution"][key] for key in ("mode", "tools")}

    async def test_ordinary_learning_and_emotion_can_finish_in_one_coordinator_call(self):
        for primary in "ABDEFG":
            with self.subTest(primary=primary):
                runner = FakeRunner([direct_decision(primary)])
                output, state = await self.pipeline(runner).run(messages("帮助我理解当前问题"), session_id=primary)
                self.assert_wire(output)
                self.assertEqual(output["intent"]["primary"], primary)
                self.assertEqual([(call["role"], call["payload"]["phase"]) for call in runner.calls], [("coordinator", "decide")])
                self.assertEqual(self.execution(state), {"mode": "answer", "tools": dict.fromkeys(TOOLS, "not_requested")})
        self.assertEqual(self.research.calls, [])
        self.assertEqual(self.artifacts.prepared, [])

    async def test_explicit_visual_keyword_repairs_direct_answer_into_visual(self):
        visual = {"kind": "diagram", "title": "细胞结构示意图",
                  "nodes": [{"id": "a", "label": "细胞膜", "x": 50, "y": 20},
                            {"id": "b", "label": "细胞核", "x": 50, "y": 80}],
                  "edges": [{"from": "a", "to": "b"}]}
        runner = FakeRunner([direct_decision("A")], visuals=[visual])
        output, state = await self.pipeline(runner).run(
            messages("画一张细胞结构的示意图"), session_id="s")
        self.assert_wire(output)
        roles = [(call["role"], call["payload"].get("phase")) for call in runner.calls]
        self.assertIn(("visualizer", None), roles)
        self.assertEqual(state["last_execution"]["tools"]["visual"], "published")
        self.assertEqual(self.artifacts.committed[0][1]["spec"]["kind"], "diagram")

    async def test_visual_opt_out_and_implicit_requests_are_not_forced(self):
        for text in ("不用图，文字解释一下光合作用", "这个概念我不太懂"):
            with self.subTest(text=text):
                runner = FakeRunner([direct_decision("A")])
                output, state = await self.pipeline(runner).run(messages(text), session_id="s")
                self.assert_wire(output)
                self.assertEqual(state["last_execution"]["mode"], "answer")
        self.assertEqual(self.artifacts.prepared, [])

    async def test_each_requested_capability_runs_without_unselected_workers(self):
        cases = {
            "research": {"research_query": "分数定义"},
            "quiz": {"quiz_action": "start", "quiz_topic": "分数"},
            "notes": {"observe_learning": True},
            "visual": {"visual_request": "分数对照"},
        }
        for selected, updates in cases.items():
            with self.subTest(selected=selected):
                self.store.set_enabled(selected, True)
                text = "我在工作，请查资料、出一道题、记录学习情况或画图解释这个问题。"
                research = FakeResearch()
                runner = FakeRunner([tool_decision(text, **updates)], note_results=[notes(True)])
                output, state = await self.pipeline(runner, research).run(messages(text), session_id=selected)
                self.assert_wire(output)
                self.assertEqual(len(research.calls), int(selected == "research"))
                self.assertEqual(len(runner.calls_for("quizzer")), int(selected == "quiz"))
                # Learning evidence is marked for the notes compiled after a quiz set.
                self.assertEqual(runner.calls_for("notes"), [])
                self.assertEqual(len(runner.calls_for("visualizer")), int(selected == "visual"))
                self.assertEqual(len(runner.calls_for("coordinator", "respond")), 1)
                statuses = state["last_execution"]["tools"]
                self.assertEqual(state["last_execution"]["mode"], "tools")
                self.assertEqual(statuses[selected], {"research": "verified", "quiz": "question_ready", "notes": "deferred", "visual": "published"}[selected])
                self.assertEqual(state["context"].get("evidence_turns", []), [1] if selected == "notes" else [])
                for other in set(TOOLS) - {selected}:
                    self.assertEqual(statuses[other], "not_requested")
                before_commit = runner.calls_for("coordinator", "respond")[0]["tool_results"]
                self.assertEqual(before_commit[selected], {"visual": "ready"}.get(selected, statuses[selected]))

    async def test_missing_fabricated_or_mismatched_reasons_fail_before_side_effects(self):
        text = "查一下分数的定义"
        valid = tool_decision(text, research_query="分数定义")
        invalids = []
        missing_key = deepcopy(valid)
        del missing_key["plan"]["tool_reasons"]["research"]
        invalids.append(missing_key)
        missing_reason = deepcopy(valid)
        missing_reason["plan"]["tool_reasons"]["research"] = None
        invalids.append(missing_reason)
        fabricated_quote = deepcopy(valid)
        fabricated_quote["plan"]["tool_reasons"]["research"]["evidence_quote"] = "用户没有说过这句话"
        invalids.append(fabricated_quote)
        unused_reason = deepcopy(valid)
        unused_reason["plan"]["tool_reasons"]["visual"] = {"code": "explicit_visual", "evidence_quote": text}
        invalids.append(unused_reason)
        wrong_capability_code = deepcopy(valid)
        wrong_capability_code["plan"]["tool_reasons"]["research"]["code"] = "explicit_practice"
        invalids.append(wrong_capability_code)
        invalids.append(tool_decision(text))  # No selected capability belongs on the direct path.
        for number, candidate in enumerate(invalids):
            with self.subTest(number=number):
                session = f"invalid-{number}"
                self.store.set_enabled(session, True)
                baseline = {"revision": 3, "active_task": "之前的任务"}
                runner = FakeRunner([candidate])
                output, state = await self.pipeline(runner).run(messages(text), session_id=session, state=baseline)
                self.assert_wire(output)
                self.assertEqual(output["intent"]["primary"], "G")
                self.assertEqual(state, baseline)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(self.store.snapshot(session)["profile"], {})
        self.assertEqual(self.research.calls, [])
        self.assertEqual(self.artifacts.prepared, [])
        self.assertEqual(self.artifacts.committed, [])

    async def test_policy_blocks_tools_despite_self_consistent_tool_plan(self):
        text = "我现在先停一下，需要帮助"
        for number, (primary, updates) in enumerate([
            ("E", {}), ("F", {}), ("G", {}), ("A", {"pause_task": True}), ("A", {"end_task": True}),
            ("G", {"pause_task": True}), ("G", {"end_task": True}),
        ]):
            with self.subTest(primary=primary, updates=updates):
                session = f"blocked-{number}"
                self.store.set_enabled(session, True)
                decision = tool_decision(text, primary, research_query="不要检索", visual_request="不要绘图",
                                         observe_learning=True, quiz_action="start", quiz_topic="分数", **updates)
                runner = FakeRunner([decision])
                output, state = await self.pipeline(runner).run(messages(text), session_id=session)
                self.assert_wire(output)
                self.assertEqual(output["intent"]["primary"], primary)
                self.assertEqual([call["role"] for call in runner.calls], ["coordinator", "coordinator"])
                self.assertEqual(set(state["last_execution"]["tools"].values()), {"skipped_policy"})
                if updates.get("pause_task") or updates.get("end_task"):
                    self.assertIsNone(output["next_step"])
        self.assertEqual(self.research.calls, [])
        self.assertEqual(self.artifacts.prepared, [])

    async def test_memory_off_and_planning_intent_skip_forbidden_capability(self):
        for primary, selected, fields in (("A", "notes", {"observe_learning": True}),
                                          ("D", "quiz", {"quiz_action": "start", "quiz_topic": "分数"})):
            with self.subTest(selected=selected):
                text = "帮我安排学习计划"
                runner = FakeRunner([tool_decision(text, primary, **fields)])
                output, state = await self.pipeline(runner).run(messages(text), session_id=selected)
                self.assert_wire(output)
                self.assertEqual(state["last_execution"]["tools"][selected], "skipped_policy")
                self.assertEqual(runner.calls_for("notes"), [])
                self.assertEqual(runner.calls_for("quizzer"), [])

    async def test_tool_failure_returns_status_and_finishes_without_restarting_roles(self):
        self.store.set_enabled("s", True)
        text = "我在工作，查一下并画图记录一下"
        runner = FakeRunner([tool_decision(text, research_query="分数", visual_request="对照分数", observe_learning=True)])
        research = FakeResearch(StageFailure("PRIVATE_SEARCH_FAILURE"))
        output, state = await self.pipeline(runner, research).run(messages(text), session_id="s")
        self.assert_wire(output)
        self.assertNotIn("PRIVATE", output["message"])
        self.assertIn("未完成检索验证", output["message"])
        self.assertEqual(len(runner.calls_for("coordinator", "decide")), 1)
        self.assertEqual(len(runner.calls_for("coordinator", "respond")), 1)
        self.assertEqual(runner.calls_for("notes"), [])
        # The failed search does not reach the drawing, which uses basic knowledge only.
        self.assertIsNone(runner.calls_for("visualizer")[0]["research"])
        self.assertEqual(len(research.calls), 1)
        statuses = state["last_execution"]["tools"]
        self.assertEqual(statuses["research"], "unavailable")
        self.assertEqual(statuses["notes"], "deferred")
        self.assertEqual(statuses["visual"], "published")

    async def test_limited_sources_are_disclosed_and_kept_out_of_the_visual(self):
        for status in ("likely", "disputed", "unavailable"):
            with self.subTest(status=status):
                text = "查证一下，再画图解释"
                runner = FakeRunner([tool_decision(text, research_query="知识点", visual_request="展示结论")])
                research = FakeResearch({"overall": status, "sources": [], "claims": []})
                output, state = await self.pipeline(runner, research).run(messages(text), session_id=status)
                self.assert_wire(output)
                self.assertEqual(state["last_execution"]["tools"]["research"], status)
                self.assertEqual(state["last_execution"]["tools"]["visual"], "published")
                self.assertEqual(len(research.calls), 1)
                self.assertEqual(len(runner.calls_for("visualizer")), 1)
                self.assertIsNone(runner.calls_for("visualizer")[0]["research"])
                self.assertEqual(len(runner.calls_for("coordinator", "respond")), 1)
                self.assertIn({"likely": "证据有限", "disputed": "来源存在冲突", "unavailable": "未完成检索验证"}[status], output["message"])

    async def test_invalid_direct_response_and_unretrieved_url_cannot_escape(self):
        malformed = direct_decision()
        malformed["response"]["unexpected"] = "PRIVATE_EXTRA"
        forged = direct_decision(message="PRIVATE_LINK https://unretrieved.invalid/source")
        upper = direct_decision(message="PRIVATE_LINK HTTPS://unretrieved.invalid/source")
        for index, decision in enumerate((malformed, forged, upper)):
            with self.subTest(index=index):
                runner = FakeRunner([decision])
                output, state = await self.pipeline(runner).run(messages("解释分数"), session_id=str(index))
                self.assert_wire(output)
                self.assertNotIn("PRIVATE", output["message"])
                self.assertEqual(state, {})
                self.assertEqual(len(runner.calls), 1)

    async def test_pending_answer_repairs_direct_and_ineligible_tools_without_extra_decision(self):
        for primary in "ABCDEFG":
            for mode in ("answer", "tools"):
                with self.subTest(primary=primary, mode=mode):
                    decision = direct_decision(primary)
                    if mode == "tools":
                        fields = ({"quiz_action": "continue", "quiz_topic": "分数"} if primary in "DEFG"
                                  else {"research_query": "不应新增检索", "visual_request": "不应新增绘图"})
                        decision = tool_decision("A", primary, **fields)
                    runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数"), decision])
                    pipeline = self.pipeline(runner)
                    _, before = await pipeline.run(messages("考考我"), session_id=primary, turn_id="start")
                    output, after = await pipeline.run(messages("A"), session_id=primary, state=before, turn_id="answer")
                    self.assert_wire(output)
                    self.assertEqual(output["intent"]["primary"], "C")
                    self.assertEqual(after["quiz"]["answered"], 1)
                    self.assertEqual(after["quiz"]["correct"], 1)
                    self.assertEqual(after["quiz"]["asked"], 2)
                    self.assertEqual(after["last_execution"]["routing_repaired"], "pending_quiz_answer")
                    self.assertEqual(len(runner.calls_for("coordinator", "decide")), 2)
                    self.assertEqual(len(runner.calls_for("coordinator", "respond")), 2)
                    self.assertEqual(runner.calls_for("visualizer"), [])
        self.assertEqual(self.research.calls, [])
        self.assertEqual(self.artifacts.prepared, [])

    async def test_direct_pause_preserves_quiz_and_direct_stop_clears_it(self):
        for primary in ("E", "G"):
            with self.subTest(primary=primary):
                pause, stop = direct_decision(primary, pause=True), direct_decision(primary, end=True)
                if primary == "G":
                    pause["response"]["next_step"] = None
                    stop["response"]["next_step"] = None
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数"), pause, stop])
                pipeline = self.pipeline(runner)
                _, state = await pipeline.run(messages("考考我"), session_id=primary, turn_id="start")
                pause_output, paused = await pipeline.run(messages("A"), session_id=primary, state=state, turn_id="pause")
                self.assertIsNone(pause_output["next_step"])
                self.assertEqual(pause_output["intent"]["primary"], primary)
                self.assertEqual(paused["quiz"], state["quiz"])
                self.assertNotIn("routing_repaired", paused["last_execution"])
                self.assertEqual(paused["resume_stage"], "paused")
                output, stopped = await pipeline.run(messages("A"), session_id=primary, state=paused, turn_id="stop")
                self.assert_wire(output)
                self.assertEqual(output["intent"]["primary"], primary)
                self.assertIsNone(output["next_step"])
                self.assertNotIn("quiz", stopped)
                self.assertEqual(stopped["pending_tasks"], [])
                self.assertIsNone(stopped["active_task"])
                self.assertEqual(len(runner.calls_for("coordinator", "respond")), 1)
                self.assertNotIn("routing_repaired", stopped["last_execution"])

    async def test_recovery_grades_wrong_and_final_answers_once_including_notes(self):
        for answer, total, correct in (("B", 3, 0), ("A", 1, 1), ("B", 1, 0)):
            with self.subTest(answer=answer, total=total):
                session = f"{answer}-{total}"
                self.store.set_enabled(session, True)
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数", quiz_total=total), direct_decision("A")])
                pipeline = self.pipeline(runner)
                _, before = await pipeline.run(messages("考考我"), session_id=session, turn_id="start")
                self.assertEqual(before["quiz"]["participant"], "learner")
                spy, written = self.spy_evidence()
                with spy:
                    output, after = await pipeline.run(messages(answer), session_id=session, state=before, turn_id="answer")
                self.assert_wire(output)
                self.assertEqual(after["quiz"]["answered"], 1)
                self.assertEqual(after["quiz"]["correct"], correct)
                self.assertEqual(after["quiz"]["asked"], 1)
                self.assertEqual(after["quiz"]["finished"], total == 1)
                self.assertEqual(len(runner.calls_for("quizzer")), 1)
                if total == 1:
                    # The set is done and records are on: offer to compile notes.
                    self.assertIn("整理成笔记", output["next_step"]["text"])
                    self.assertTrue(after["notes_offer"])
                feedback = runner.calls_for("coordinator", "respond")[-1]["quiz"]
                self.assertEqual(feedback["feedback"]["correct"], bool(correct))
                self.assertEqual(runner.calls_for("notes"), [])
                evidence = written[0]
                self.assertEqual(evidence["actor"], "learner")
                self.assertIs(evidence["independent"], True)
                stored = self.store.snapshot(session)["concepts"][0]
                self.assertEqual((stored["attempts"], stored["correct_count"]), (1, correct))
                calls = len(runner.calls)
                replay, replay_state = await pipeline.run(messages(answer), session_id=session, state=before, turn_id="answer")
                self.assertEqual(replay, output)
                self.assertEqual(replay_state, after)
                self.assertEqual(len(runner.calls), calls)
                self.assertEqual(self.store.snapshot(session)["concepts"][0]["attempts"], 1)

    async def test_recovery_restores_companion_and_legacy_unknown_without_independent_credit(self):
        for participant in ("companion_report", None):
            with self.subTest(participant=participant):
                session = participant or "legacy"
                self.store.set_enabled(session, True)
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数", participant=participant or "learner"),
                                     direct_decision("A")])
                _, before = await self.pipeline(runner).run(messages("考考我"), session_id=session, turn_id="start")
                if participant is None:
                    del before["quiz"]["participant"]
                else:
                    self.assertEqual(before["quiz"]["participant"], participant)
                # A fresh pipeline represents importing persisted legacy state,
                # rather than letting its newer in-process cache hide the fixture.
                spy, written = self.spy_evidence()
                with spy:
                    output, after = await self.pipeline(runner).run(messages("A"), session_id=session, state=before, turn_id="answer")
                self.assert_wire(output)
                self.assertEqual(after["quiz"]["answered"], 1)
                evidence = written[0]
                self.assertIs(evidence["independent"], False)
                self.assertEqual(evidence["actor"], "companion" if participant else "unknown")
                self.assertEqual(self.store.snapshot(session)["concepts"], [])

    async def test_stored_participant_is_not_upgraded_by_valid_continue(self):
        for stored in ("companion_report", "unknown"):
            with self.subTest(stored=stored):
                session = f"stored-{stored}"
                self.store.set_enabled(session, True)
                continuation = tool_decision("A", "C", quiz_action="continue", quiz_topic="分数",
                                             participant="learner", observe_learning=True)
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数", participant=stored), continuation])
                pipeline = self.pipeline(runner)
                _, before = await pipeline.run(messages("考考我"), session_id=session, turn_id="start")
                spy, written = self.spy_evidence()
                with spy:
                    output, after = await pipeline.run(messages("A"), session_id=session, state=before, turn_id="answer")
                self.assert_wire(output)
                self.assertNotIn("routing_repaired", after["last_execution"])
                self.assertEqual(after["quiz"]["participant"], stored)
                self.assertEqual(after["last_execution"]["tools"]["notes"], "deferred")
                self.assertIs(written[0]["independent"], False)
                self.assertEqual(written[0]["actor"], "companion" if stored == "companion_report" else "unknown")
                self.assertEqual(runner.calls_for("coordinator", "respond")[-1]["plan"]["participant"], stored)
                self.assertEqual(self.store.snapshot(session)["concepts"], [])

    async def test_repaired_tools_preserve_current_nonlearner_participant(self):
        for observed in ("companion_report", "unknown"):
            with self.subTest(observed=observed):
                session = f"observed-{observed}"
                self.store.set_enabled(session, True)
                continuation = tool_decision("A", "D", quiz_action="continue", quiz_topic="分数", participant=observed)
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数", participant="learner"), continuation])
                pipeline = self.pipeline(runner)
                _, before = await pipeline.run(messages("考考我"), session_id=session, turn_id="start")
                self.assertEqual(before["quiz"]["participant"], "learner")
                spy, written = self.spy_evidence()
                with spy:
                    output, after = await pipeline.run(messages("A"), session_id=session, state=before, turn_id="answer")
                self.assert_wire(output)
                self.assertEqual(after["last_execution"]["routing_repaired"], "pending_quiz_answer")
                self.assertEqual(after["quiz"]["participant"], observed)
                self.assertIs(written[0]["independent"], False)
                self.assertEqual(written[0]["actor"], "companion" if observed == "companion_report" else "unknown")
                self.assertEqual(runner.calls_for("coordinator", "respond")[-1]["plan"]["participant"], observed)
                self.assertEqual(self.store.snapshot(session)["concepts"], [])

    async def test_recovery_handles_missing_or_false_reason_but_not_invalid_structure(self):
        for defect in ("missing_reason", "wrong_quote", "malformed_structure"):
            with self.subTest(defect=defect):
                decision = tool_decision("A", "C", quiz_action="continue", quiz_topic="分数")
                if defect == "missing_reason":
                    decision["plan"]["tool_reasons"]["quiz"] = None
                elif defect == "wrong_quote":
                    decision["plan"]["tool_reasons"]["quiz"]["evidence_quote"] = "并未说过的话"
                else:
                    del decision["plan"]["tool_reasons"]["quiz"]
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数"), decision])
                pipeline = self.pipeline(runner)
                _, before = await pipeline.run(messages("考考我"), session_id=defect, turn_id="start")
                output, after = await pipeline.run(messages("A"), session_id=defect, state=before, turn_id="answer")
                self.assert_wire(output)
                self.assertEqual(len(runner.calls_for("coordinator", "decide")), 2)
                if defect == "malformed_structure":
                    self.assertEqual(output["intent"]["primary"], "G")
                    self.assertEqual(after, before)
                else:
                    self.assertEqual(after["quiz"]["answered"], 1)
                    self.assertEqual(after["last_execution"]["routing_repaired"], "pending_quiz_answer")

    async def test_valid_continue_plan_keeps_selected_tools_and_ambiguous_answer_is_not_graded(self):
        for answer, mode in (("A", "tools"), ("我还拿不准", "tools"), ("我还拿不准", "answer")):
            with self.subTest(answer=answer, mode=mode):
                decision = (tool_decision(answer, "C", quiz_action="continue", quiz_topic="分数", research_query="保留检索")
                            if mode == "tools" else direct_decision("A"))
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数"), decision])
                research = FakeResearch()
                pipeline = self.pipeline(runner, research)
                _, before = await pipeline.run(messages("考考我"), session_id=answer, turn_id="start")
                output, after = await pipeline.run(messages(answer), session_id=answer, state=before, turn_id="answer")
                self.assert_wire(output)
                self.assertNotIn("routing_repaired", after["last_execution"])
                self.assertEqual(research.calls, ["保留检索"] if mode == "tools" else [])
                self.assertEqual(after["quiz"]["answered"], int(answer == "A"))
                if answer != "A":
                    self.assertEqual(after["quiz"], before["quiz"])
                    self.assertEqual(after["last_execution"]["tools"]["quiz"], "awaiting_answer" if mode == "tools" else "not_requested")

    async def test_tools_stop_with_no_work_requests_is_effective_for_emotion_and_unclear_intents(self):
        for primary in "EFG":
            with self.subTest(primary=primary):
                stop = tool_decision("A", primary, quiz_action="stop")
                self.assertEqual(set(stop["plan"]["tool_reasons"].values()), {None})
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数"), stop])
                pipeline = self.pipeline(runner)
                _, before = await pipeline.run(messages("考考我"), session_id=primary,
                                                state={"pending_tasks": ["复习"]}, turn_id="start")
                output, after = await pipeline.run(messages("A"), session_id=primary, state=before, turn_id="stop")
                self.assert_wire(output)
                self.assertEqual(output["intent"]["primary"], primary)
                self.assertIsNone(output["next_step"])
                self.assertNotIn("quiz", after)
                self.assertIsNone(after["active_task"])
                self.assertEqual(after["pending_tasks"], [])
                self.assertNotIn("routing_repaired", after["last_execution"])
                self.assertEqual(len(runner.calls_for("quizzer")), 1)
                self.assertEqual(runner.calls_for("notes"), [])
                self.assertEqual(runner.calls_for("visualizer"), [])
        self.assertEqual(self.research.calls, [])
        for task_flag in ("pause_task", "end_task"):
            with self.subTest(task_flag=task_flag):
                decision = tool_decision("A", "G", research_query="不要调用检索", **{task_flag: True})
                runner = FakeRunner([plan("C", quiz_action="start", quiz_topic="分数"), decision])
                pipeline = self.pipeline(runner)
                _, before = await pipeline.run(messages("考考我"), session_id=task_flag, turn_id="start")
                output, after = await pipeline.run(messages("A"), session_id=task_flag, state=before, turn_id="control")
                self.assert_wire(output)
                self.assertEqual(output["intent"]["primary"], "G")
                self.assertIsNone(output["next_step"])
                self.assertNotIn("routing_repaired", after["last_execution"])
                self.assertEqual(after["last_execution"]["tools"]["research"], "skipped_policy")
                if task_flag == "pause_task":
                    self.assertEqual(after["quiz"], before["quiz"])
                else:
                    self.assertNotIn("quiz", after)
        self.assertEqual(self.research.calls, [])

    async def test_evidence_rejects_cherry_picked_character_but_accepts_full_short_turn(self):
        for text, quote, accepted in (("查询分数定义", "查", False),
                                      ("请查 \n询分数", "查 \n询", True), ("A", "A", True), ("好", "好", True)):
            with self.subTest(text=text, quote=quote):
                decision = tool_decision(text, research_query="分数定义")
                decision["plan"]["tool_reasons"]["research"]["evidence_quote"] = quote
                runner, research = FakeRunner([decision]), FakeResearch()
                output, state = await self.pipeline(runner, research).run(messages(text), session_id=text)
                self.assert_wire(output)
                if accepted:
                    self.assertEqual(state["last_execution"]["tools"]["research"], "verified")
                    self.assertEqual(research.calls, ["分数定义"])
                else:
                    self.assertEqual(output["intent"]["primary"], "G")
                    self.assertEqual(state, {})
                    self.assertEqual(research.calls, [])

    async def test_learning_evidence_is_marked_not_compiled_during_the_turn(self):
        self.store.set_enabled("s", True)
        text = "我在工作"
        previous = self.store.snapshot("s")
        runner = FakeRunner([tool_decision(text, observe_learning=True)])
        output, state = await self.pipeline(runner).run(messages(text), session_id="s")
        self.assert_wire(output)
        self.assertEqual(state["last_execution"]["tools"]["notes"], "deferred")
        self.assertEqual(state["context"]["evidence_turns"], [1])
        self.assertEqual(runner.calls_for("notes"), [])
        self.assertNotIn("笔记", output["message"])
        self.assertEqual(self.store.snapshot("s"), previous)
        self.assertEqual(runner.calls_for("coordinator", "respond")[0]["tool_results"]["notes"], "deferred")

    async def test_direct_practice_intent_enters_the_quiz_state_machine(self):
        text = "考考我分数的知识"
        runner = FakeRunner([direct_decision("C")])
        output, state = await self.pipeline(runner).run(messages(text), session_id="practice")
        self.assert_wire(output)
        execution = state["last_execution"]
        self.assertEqual(execution["mode"], "tools")
        self.assertEqual(execution["routing_repaired"], "practice_without_tools")
        self.assertEqual(execution["tools"]["quiz"], "question_ready")
        self.assertEqual(len(runner.calls_for("coordinator", "decide")), 1)
        self.assertIn("A.", output["next_step"]["text"])
        self.assertEqual(state["quiz"]["topic"], "学习分数")
        self.assertFalse(state["quiz"]["finished"])

    async def test_direct_practice_with_active_quiz_continues_without_restarting(self):
        baseline = {"quiz": {"topic": "分数", "total": 3, "difficulty": "简单", "asked": 1, "answered": 1,
                             "correct": 1, "awaiting": None, "finished": False, "question_history": ["旧题"],
                             "participant": "learner"}, "revision": 2}
        runner = FakeRunner([direct_decision("C")])
        output, after = await self.pipeline(runner).run(messages("继续考我"), session_id="practice", state=baseline)
        self.assert_wire(output)
        execution = after["last_execution"]
        self.assertEqual(execution["mode"], "tools")
        self.assertEqual(execution["routing_repaired"], "practice_without_tools")
        self.assertEqual(after["quiz"]["topic"], "分数")
        self.assertEqual(after["quiz"]["asked"], baseline["quiz"]["asked"] + 1)
        self.assertIsNotNone(after["quiz"]["awaiting"])

    async def test_direct_practice_does_not_override_pause_or_end(self):
        for end, pause in ((True, False), (False, True)):
            runner = FakeRunner([direct_decision("C", end=end, pause=pause)])
            output, state = await self.pipeline(runner).run(messages("考考我"), session_id=f"ctl-{end}-{pause}")
            self.assert_wire(output)
            self.assertEqual(self.execution(state),
                             {"mode": "answer", "tools": dict.fromkeys(TOOLS, "not_requested")})
            self.assertIsNone(output["next_step"])

    async def test_tools_continue_without_active_quiz_starts_a_new_one(self):
        text = "继续考我分数"
        runner = FakeRunner([tool_decision(text, "C", quiz_action="continue", quiz_topic="分数")])
        output, state = await self.pipeline(runner).run(messages(text), session_id="degraded")
        self.assert_wire(output)
        self.assertEqual(state["last_execution"]["mode"], "tools")
        self.assertNotIn("routing_repaired", state["last_execution"])
        self.assertEqual(state["quiz"]["topic"], "分数")
        self.assertEqual(state["quiz"]["asked"], 1)
        self.assertEqual(state["last_execution"]["tools"]["quiz"], "question_ready")
        self.assertIn("A.", output["next_step"]["text"])

    async def test_tools_continue_without_quiz_or_topic_asks_for_topic(self):
        text = "继续"
        runner = FakeRunner([tool_decision(text, "C", quiz_action="continue")])
        output, state = await self.pipeline(runner).run(messages(text), session_id="needs-topic")
        self.assert_wire(output)
        self.assertEqual(state["last_execution"]["tools"]["quiz"], "needs_input")
        self.assertIsNotNone(output["next_step"])
        self.assertIsNone(state.get("quiz"))

    async def test_original_whitespace_is_valid_evidence_and_quiz_reason_must_match_action(self):
        text = " \n查一下分数\n "
        runner = FakeRunner([tool_decision(text, research_query="分数定义")])
        output, state = await self.pipeline(runner).run(messages(text), session_id="whitespace")
        self.assert_wire(output)
        self.assertEqual(state["last_execution"]["tools"]["research"], "verified")
        for action, wrong_code in (("start", "continue_practice"), ("continue", "explicit_practice")):
            with self.subTest(action=action):
                decision = tool_decision("考考我", "C", quiz_action=action, quiz_topic="分数")
                decision["plan"]["tool_reasons"]["quiz"]["code"] = wrong_code
                invalid = FakeRunner([decision])
                output, after = await self.pipeline(invalid).run(messages("考考我"), session_id=action)
                self.assert_wire(output)
                self.assertEqual(output["intent"]["primary"], "G")
                self.assertEqual(after, {})
                self.assertEqual(invalid.calls_for("quizzer"), [])

    async def test_visual_runtime_failure_is_unavailable_without_secret_leak(self):
        text = "画图解释分数"
        runner = FakeRunner([tool_decision(text, visual_request="分数对比")])
        with patch.object(self.artifacts, "prepare", side_effect=RuntimeError("PRIVATE_TOKEN_COLLISION")):
            output, state = await self.pipeline(runner).run(messages(text), session_id="s")
        self.assert_wire(output)
        self.assertEqual(state["last_execution"]["tools"]["visual"], "unavailable")
        self.assertIn("未生成可用的互动图", output["message"])
        self.assertNotIn("PRIVATE", output["message"])
        self.assertEqual(self.artifacts.committed, [])
        self.assertEqual(len(runner.calls_for("coordinator", "respond")), 1)

    async def test_unexpected_child_failure_cancels_and_joins_other_child(self):
        self.store.set_enabled("s", True)
        visual_started, hold = asyncio.Event(), asyncio.Event()
        child_tasks, cancelled = [], []
        text = "查一下资料并画图"

        class FailingResearch(FakeResearch):
            async def run(self, query, queries=None):
                child_tasks.append(asyncio.current_task())
                await visual_started.wait()
                raise LookupError("PRIVATE_UNEXPECTED_CHILD_FAILURE")

        class HoldingRunner(FakeRunner):
            async def __call__(self, role, payload, schema):
                if role == "visualizer":
                    child_tasks.append(asyncio.current_task())
                    visual_started.set()
                    try:
                        await hold.wait()
                    except asyncio.CancelledError:
                        cancelled.append(role)
                        raise
                return await super().__call__(role, payload, schema)

        runner = HoldingRunner([tool_decision(text, research_query="分数", visual_request="分数对比",
                                              visual_uses_research=False)])
        before = {"active_task": "之前的任务", "revision": 3}
        with self.assertLogs("learning_coach.service.pipeline", level="WARNING") as logs:
            output, after = await asyncio.wait_for(
                self.pipeline(runner, FailingResearch()).run(messages(text), session_id="s", state=before), timeout=5)
        self.assert_wire(output)
        self.assertEqual(output["intent"]["primary"], "G")
        self.assertEqual(after, before)
        self.assertEqual(cancelled, ["visualizer"])
        self.assertTrue(all(task.done() for task in child_tasks))
        self.assertEqual(len(child_tasks), 2)
        self.assertNotIn("PRIVATE", output["message"] + " ".join(logs.output))
        self.assertEqual(runner.calls_for("coordinator", "respond"), [])
        self.assertEqual(self.artifacts.committed, [])
        self.assertEqual(self.store.snapshot("s")["profile"], {})

    async def test_research_dependents_wait_and_evidence_is_only_marked(self):
        self.store.set_enabled("s", True)
        research_finished, quiz_started, visual_started = (asyncio.Event() for _ in range(3))
        text = "我在工作，请查资料、出一道题并画图解释，再记录学习情况"

        class SlowResearch(FakeResearch):
            async def run(self, query, queries=None):
                await asyncio.sleep(0)
                result = await super().run(query, queries)
                research_finished.set()
                return result

        class OrderRunner(FakeRunner):
            async def __call__(self, role, payload, schema):
                if role == "visualizer" or role == "quizzer":
                    if not research_finished.is_set():
                        raise AssertionError("unstated research dependency must wait for research")
                    # Question and visual do not depend on each other, so they overlap.
                    if role == "visualizer":
                        visual_started.set()
                        await quiz_started.wait()
                    else:
                        quiz_started.set()
                        await visual_started.wait()
                return await super().__call__(role, payload, schema)

        runner = OrderRunner([tool_decision(text, research_query="分数", visual_request="分数对照", observe_learning=True,
                                            quiz_action="start", quiz_topic="分数")])
        research = SlowResearch()
        output, state = await asyncio.wait_for(self.pipeline(runner, research).run(messages(text), session_id="s"), timeout=5)
        self.assert_wire(output)
        self.assertEqual(state["last_execution"]["order"], {
            "visual_waits_research": True, "quiz_waits_research": True, "quiz_waits_visual": False})
        self.assertEqual(state["last_execution"]["tools"]["notes"], "deferred")
        self.assertEqual(state["last_execution"]["tools"]["visual"], "published")
        self.assertEqual(state["last_execution"]["tools"]["quiz"], "question_ready")
        self.assertIn("research", runner.calls_for("quizzer")[0])
        self.assertEqual(runner.calls_for("notes"), [])
        # Research searches with the coordinator's own queries; sub-agents get the learner card.
        self.assertEqual(research.queries[0], ["分数"])
        self.assertIn("learner", runner.calls_for("visualizer")[0])
        self.assertIn("learner", runner.calls_for("quizzer")[0])

    async def test_visual_unrelated_to_research_does_not_wait_or_receive_it(self):
        text = "查一下最新的火星探测进展，顺便画一下太阳系结构"
        visual_started = asyncio.Event()

        class GatedResearch(FakeResearch):
            async def run(self, query, queries=None):
                await visual_started.wait()
                return await super().run(query)

        class Runner(FakeRunner):
            async def __call__(self, role, payload, schema):
                if role == "visualizer":
                    visual_started.set()
                return await super().__call__(role, payload, schema)

        runner = Runner([tool_decision(text, research_query="火星探测", visual_request="太阳系结构",
                                       visual_uses_research=False)])
        _, state = await asyncio.wait_for(self.pipeline(runner, GatedResearch()).run(messages(text), session_id="m"), timeout=5)
        self.assertFalse(state["last_execution"]["order"]["visual_waits_research"])
        self.assertIsNone(runner.calls_for("visualizer")[0]["research"])

    async def test_question_from_the_picture_waits_for_the_visual(self):
        text = "画一张光合作用的图，然后看图出一道题考我"
        runner = FakeRunner([tool_decision(text, visual_request="光合作用场所与原料", quiz_action="start",
                                           quiz_topic="光合作用", quiz_uses_visual=True)])
        _, state = await self.pipeline(runner).run(messages(text), session_id="v")
        self.assertTrue(state["last_execution"]["order"]["quiz_waits_visual"])
        roles = [(call["role"], call["payload"].get("phase")) for call in runner.calls]
        self.assertLess(roles.index(("verifier", "visual_review")), roles.index(("quizzer", "quiz")))
        self.assertEqual(runner.calls_for("quizzer")[0]["visual_context"]["status"], "ready")

    async def test_cancellation_stops_both_parallel_children_without_commit(self):
        self.store.set_enabled("s", True)
        started = {name: asyncio.Event() for name in ("research", "visualizer")}
        cancelled, worker_tasks = set(), []
        hold = asyncio.Event()
        text = "我在工作，请查资料并画图解释"

        async def block(name):
            worker_tasks.append(asyncio.current_task())
            started[name].set()
            try:
                await hold.wait()
            except asyncio.CancelledError:
                cancelled.add(name)
                raise

        class BlockingResearch(FakeResearch):
            async def run(self, query, queries=None):
                await block("research")

        class BlockingRunner(FakeRunner):
            async def __call__(self, role, payload, schema):
                if role == "visualizer":
                    await block("visualizer")
                return await super().__call__(role, payload, schema)

        runner = BlockingRunner([tool_decision(text, research_query="分数", visual_request="分数对照",
                                               visual_uses_research=False)])
        baseline = {"active_task": "之前的学习任务", "revision": 2}
        pipeline = self.pipeline(runner, BlockingResearch())
        work = asyncio.create_task(pipeline.run(messages(text), session_id="s", state=baseline, turn_id="cancelled"))
        try:
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), timeout=5)
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
        finally:
            if not work.done():
                work.cancel()
                try:
                    await work
                except asyncio.CancelledError:
                    pass
        self.assertEqual(cancelled, {"research", "visualizer"})
        self.assertEqual(len(worker_tasks), 2)
        self.assertTrue(all(task.done() and task.cancelled() for task in worker_tasks))
        self.assertEqual(runner.calls_for("coordinator", "respond"), [])
        self.assertEqual(self.artifacts.prepared, [])
        self.assertEqual(self.artifacts.committed, [])
        self.assertEqual(self.store.snapshot("s")["profile"], {})
        self.assertEqual(baseline, {"active_task": "之前的学习任务", "revision": 2})
        self.assertTrue(self.store.apply_notes("s", "cancelled", notes(), ""))


if __name__ == "__main__":
    unittest.main()
