"""Teaching: one coordinator decision, the program's plan, the tools, one reply.

    look      what this turn can see (history window, notes, records)
    decide    the coordinator decides; the program repairs a known misroute
    answer    a direct reply ends here
    select    the program's rules decide which requested tools may run
    access    external access not yet granted: ask once and hold the turn
    plan      task cards, evidence marks, grading a waiting answer
    execute   research, the visual and the quiz question, concurrently
    respond   the coordinator writes the reply from the results
    finish    program checks and links, then commits (nothing is saved earlier)

The coordinator proposes; the program decides what runs and what is saved.
"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import re

from learning_coach.domain.contracts import ContractError, validate
from learning_coach.domain.context import (accept_notes, brief_card, count_tokens, empty_notes, learner_card,
                                           memory_slice, note_slice, number_turns, recent_window, research_digest)
from learning_coach.domain.routing import (EXPLAIN_INTENTS, empty_results, execution_order, explicit_visual_quote,
                                           recover_explicit_visual_route, recover_practice_route,
                                           recover_quiz_route, require_finished, select_tools)
from learning_coach.service.execution import (CALL_RESERVE, EXPLANATION_SINK, EXTERNAL_DECLINED, StageFailure,
                                              external_allowed)
from learning_coach.service.pipeline import permissions
from learning_coach.service.pipeline.checks import respond_problem, validate_response
from learning_coach.service.pipeline.state import (Key, Memory, apply_task, default_participant, mark_evidence,
                                                   model_view, read_memory, record_quiz_result, track_tasks)
from learning_coach.service.pipeline.texts import (NOTES_OFFER, QUIZ_PICTURE_LINK, RESEARCH_NOTES, VISUAL_FAILED,
                                                   VISUAL_LINK, question, render_question)
from learning_coach.service.visual_generation import VisualGenerationService

LOG = logging.getLogger(__name__)

TASK_SUMMARY_CHARS = 400
# Early output cannot refer to absent artifacts, sources or successful tool
# execution. These guards complement (not replace) teaching evaluation.
_PREMATURE = re.compile(r"https?://|/learning-artifacts/|!\[|[？?]|(?:图|检索|查证).{0,5}(?:已|完成|成功)"
                        r"|(?:已|已经).{0,5}(?:画好|生成|查到|核实)")


@dataclass
class View:
    """What this turn can see, built once before the decision."""
    numbered: list
    current_turn: int
    context: dict
    memory: Memory
    first_meeting: bool
    memory_view: dict
    window: list


@dataclass
class Decision:
    """The coordinator's decision after the program's repairs."""
    value: dict
    check: dict | None
    notes: dict | None
    sub_window: list
    report: dict
    repaired: str | None
    previous_task: str | None
    previous_quiz: dict | None


@dataclass
class Plan:
    """What will run this turn, after the program's rules."""
    value: dict
    tools: dict
    primary: str
    initial_explanation: str | None
    brief: dict
    order: dict
    quiz_context: dict | None
    quiz_evidence: dict | None
    pending_quiz: dict | None


@dataclass
class Results:
    research: dict | None = None
    artifact: dict | None = None
    visual_context: dict | None = None
    quiz_context: dict | None = None
    pictures: list = field(default_factory=list)


class Teacher:
    def __init__(self, deps, *, quiz, decision_schema):
        self.deps, self.quiz, self.decision_schema = deps, quiz, decision_schema

    async def run(self, turn):
        view = self.look(turn)
        decision = await self.decide(turn, view)
        if decision.value["mode"] == "answer":
            return self.answer(turn, view, decision)
        proposed, tools = select_tools(decision.value["plan"], turn.messages[-1]["text"],
                                       memory_enabled=view.memory.enabled)
        missing = self._missing_access(proposed)
        if missing and not EXTERNAL_DECLINED.get():
            # Nothing runs before the learner answers; the turn is held and run again then.
            return permissions.ask(turn, turn.state, missing, proposed["task_summary"] or turn.text,
                                   proposed["intent"]["primary"])
        plan = self.plan(turn, view, decision, proposed, tools, missing)
        results = await self.execute(turn, view, decision, plan)
        output = await self.respond(turn, view, decision, plan, results)
        return self.finish(turn, view, decision, plan, results, output)

    def capabilities(self):
        return permissions.capabilities(research=self.deps.research, image_finder=self.deps.image_finder,
                                        image_generator=self.deps.image_generator)

    # ------------------------------------------------------------ look and decide

    def look(self, turn) -> View:
        state, budgets = turn.state, self.deps.budgets
        numbered = number_turns(turn.messages)
        context = state.setdefault(Key.CONTEXT, {})
        memory = read_memory(state, self.deps.store, turn.memory_key)
        # First meeting: the client has not seen this learner before, there is no
        # reply yet in this conversation and nothing remembered.
        first_meeting = (not turn.returning_visitor and Key.ONBOARDING not in state
                         and not any(item["role"] == "assistant" for item in turn.messages)
                         and not (memory.snapshot.get("profile") or memory.snapshot.get("concepts")))
        focus = " ".join(filter(None, (state.get(Key.ACTIVE_TASK), (state.get(Key.QUIZ) or {}).get("topic"),
                                       turn.text)))
        # Recent turns within the budget; what falls out of it lives on in the
        # conversation notes the coordinator keeps.
        window = recent_window(numbered, budgets.decide_window, min_turns=budgets.min_turns,
                               message_max=budgets.message_max)
        return View(numbered, numbered[-1]["turn"], context, memory, first_meeting,
                    memory_slice(memory.snapshot, focus, budgets.memory), window)

    async def decide(self, turn, view) -> Decision:
        state, budgets, text = turn.state, self.deps.budgets, turn.text
        decide_input = {
            "phase": "decide", "messages": view.window,
            "context_notes": view.context.get("notes") or empty_notes(),
            "learner": learner_card(state, view.memory.snapshot, memory_enabled=view.memory.enabled),
            "state": model_view(state), "memory": view.memory_view,
            "memory_enabled": view.memory.enabled, "first_meeting": view.first_meeting,
            "capabilities": self.capabilities(), "external_declined": EXTERNAL_DECLINED.get(),
        }
        if explicit_visual_quote(text) is not None:
            # Said up front: a full direct answer here would only be thrown
            # away by the visual route repair below (about 20 s of writing).
            decide_input["visual_required"] = True
        value = validate(await self.deps.runner("coordinator", decide_input, self.decision_schema), self.decision_schema)
        view.context["notes"], note_problems = accept_notes(
            value.get("context_notes"), view.context.get("notes"), view.numbered, budgets.notes)
        # Read before any routing repair: repaired decisions are rebuilt without it.
        check = value.get("check_question")
        sub_window = [{"turn": item["turn"], "role": item["role"], "text": item["text"]}
                      for item in recent_window(view.numbered, budgets.sub_window, min_turns=1,
                                                message_max=budgets.message_max)]
        report = {"window_turns": [view.window[0]["turn"], view.current_turn] if view.window else [],
                  "note_problems": note_problems, "decide_tokens": count_tokens(decide_input)}
        value, repaired = self._repair(value, state, turn.messages[-1]["text"], view.memory.enabled)
        return Decision(value, check, view.context.get("notes"), sub_window, report, repaired,
                        state.get(Key.ACTIVE_TASK), deepcopy(state.get(Key.QUIZ)))

    @staticmethod
    def _repair(value, state, learner_text, memory_enabled):
        """Known misroutes the program fixes without another model call; at most one applies."""
        value, repaired = recover_quiz_route(value, state, learner_text, memory_enabled=memory_enabled)
        if repaired:
            return value, "pending_quiz_answer"
        value, repaired = recover_practice_route(value, state, learner_text)
        if repaired:
            return value, "practice_without_tools"
        value, repaired = recover_explicit_visual_route(value, learner_text)
        if repaired:
            return value, "explicit_visual_without_tools"
        return value, None

    # ------------------------------------------------------------ a direct reply

    def answer(self, turn, view, decision):
        state = turn.state
        output, task = decision.value["response"], decision.value["task"]
        primary = output["intent"]["primary"]
        apply_task(state, primary, task)
        track_tasks(state, decision.previous_task, decision.previous_quiz, view.current_turn, turn.text)
        stopping = task["end_task"] or task["pause_task"]
        validate_response(output, self.deps.response_schema, output["intent"], None, stopping=stopping)
        if stopping:
            output["next_step"] = None
        offered = False
        if decision.check and primary in EXPLAIN_INTENTS and not stopping:
            # The explanation stands on its own; the reply ends with an
            # offer of a check question instead of the question itself.
            offered = self.quiz.offer_check(output, decision.check, state,
                                            brief_card(state, view.memory.snapshot, default_participant(state)))
        validate(output, self.deps.response_schema)
        if respond_problem(output, state, view.memory.enabled):
            # A single decide call cannot be redrafted here, so failing the
            # turn is safer than publishing a waiting question's answer.
            raise ContractError("answer_visible_before_answer")
        state[Key.LAST_EXECUTION] = {"mode": "answer", "tools": empty_results(), "context": decision.report}
        if offered:
            state[Key.LAST_EXECUTION]["check_offered"] = True
        return output, state

    # ------------------------------------------------------------ plan

    def plan(self, turn, view, decision, plan, tools, missing) -> Plan:
        """The tools the program's rules allow, adjusted for declined access; the quiz answer graded."""
        state, text = turn.state, turn.text
        primary = plan["intent"]["primary"]
        if missing:
            # A declined request cannot be re-enabled by a model plan. Continue
            # from existing knowledge/local drawings and expose the limitation.
            if "research" in missing:
                plan.update(research_query=None, search_queries=None)
                plan["tool_reasons"]["research"] = None
                tools["research"] = "skipped_policy"
            if missing & {"image_search", "image_generation"}:
                plan["visual_source"] = "local"
            plan.update(visual_uses_research=False, quiz_uses_research=False)
        initial = None if missing else self._initial_explanation(decision.value, plan, state, view.memory.enabled)
        apply_task(state, primary, plan)
        track_tasks(state, decision.previous_task, decision.previous_quiz, view.current_turn, text)
        brief = brief_card(state, view.memory.snapshot, plan["participant"])
        if tools["notes"] == "pending":
            # Learning evidence is only marked here; notes are compiled after a
            # quiz set when the learner agrees, from the learner's own words.
            tools["notes"] = "deferred"
            mark_evidence(view.context, view.current_turn)
        # Grading is program state and needs no model; do it first so the evidence is ready.
        quiz_context, quiz_evidence, pending_quiz = self.quiz.grade(plan, text, state)
        # The coordinator declared content dependencies; the program derives the order.
        return Plan(plan, tools, primary, initial, brief, execution_order(plan), quiz_context, quiz_evidence,
                    pending_quiz)

    def _missing_access(self, plan) -> set:
        """External capabilities this plan needs but the turn has not been granted."""
        required = set()
        if plan["research_query"]:
            required.add("research")
        source = plan.get("visual_source") or "local"
        if plan["visual_request"] and source in ("web", "generated"):
            capability = "image_search" if source == "web" else "image_generation"
            if self.capabilities()[capability]["available"]:
                required.add(capability)
        return {capability for capability in required if not external_allowed(capability)}

    def _initial_explanation(self, decision, plan, state, memory_enabled):
        """Deliver the coordinator's explanation now while a local visual is drawn; its text, or None."""
        text = decision.get("initial_explanation")
        if (not text or plan["intent"]["primary"] not in EXPLAIN_INTENTS
                or not plan["visual_request"] or plan.get("visual_source") not in (None, "local")
                or plan["research_query"] or plan["quiz_action"] != "none"
                or plan["end_task"] or plan["pause_task"] or (state.get(Key.QUIZ) or {}).get("awaiting")):
            return None
        if _PREMATURE.search(text):
            return None
        output = {"schema_version": "1.0", "intent": deepcopy(plan["intent"]),
                  "message": text.strip(), "next_step": None}
        try:
            validate_response(output, self.deps.response_schema, plan["intent"], None)
            if respond_problem(output, state, memory_enabled):
                return None
        except ContractError:
            return None
        sink = EXPLANATION_SINK.get()
        if sink is not None:
            sink(output)
        return output["message"]

    # ------------------------------------------------------------ execute

    async def execute(self, turn, view, decision, plan) -> Results:
        """Research, the visual and the quiz question run concurrently; declared dependencies wait.

        Nothing writes memory or publishes artifacts here; that happens in
        finish, after the reply has passed validation.
        """
        tools, order, results = plan.tools, plan.order, Results()
        running = {}

        async def run_research():
            if not plan.value["research_query"] or not external_allowed("research"):
                return None
            try:
                found = await self.deps.research.run(plan.value["research_query"], plan.value["search_queries"])
            except (StageFailure, OSError, TimeoutError, ValueError):
                found = {"overall": "unavailable", "sources": [], "claims": [],
                         "queries": [], "warnings": ["research_failed"]}
            tools["research"] = found["overall"]
            return found

        async def prepare_visual():
            if tools["visual"] != "pending":
                return None, None
            # A visual of stable knowledge does not wait for, or receive, this turn's research.
            found = await running["research"] if order["visual_waits_research"] else None
            result = await VisualGenerationService(self.deps.runner, self.deps.artifacts, self.deps.image_generator,
                                                   image_finder=self.deps.image_finder).generate(
                turn.session_id, {
                    "goal": plan.value["visual_request"], "research": found,
                    "visual_source": plan.value.get("visual_source") or "local",
                    "initial_explanation": plan.initial_explanation, "capabilities": self.capabilities(),
                    "learning_goal": (plan.value["task_summary"] or "")[:TASK_SUMMARY_CHARS],
                    "recent_messages": decision.sub_window,
                    "profile": view.memory_view.get("profile", {}),
                    "learner": plan.brief,
                    "context_notes": note_slice(decision.notes, "covered", "requests"),
                }, learner_text=turn.text)
            tools["visual"] = "ready" if result[0] else "unavailable"
            return result

        async def prepare_quiz():
            if plan.pending_quiz is None:
                return plan.quiz_context
            extra = {"learner": plan.brief, "context_notes": note_slice(decision.notes, "covered", "stuck"),
                     "recent_messages": decision.sub_window}
            if order["quiz_waits_research"]:
                found = research_digest(await running["research"])
                extra["research"] = {"overall": found["overall"], "claims": [
                    claim for claim in found["claims"] if claim["status"] in ("verified", "likely")]}
            if order["quiz_waits_visual"]:
                _, visual_context = await running["visual"]
                if visual_context and visual_context.get("status") == "ready":
                    extra["visual_context"] = visual_context
            written, picture = await self.quiz.write_question(plan.value["participant"], plan.pending_quiz, extra,
                                                              session_id=turn.session_id)
            if picture:
                results.pictures.append(picture)
            return written

        reservation = CALL_RESERVE.set(2)  # the reply plus its allowed format retry
        try:
            async with asyncio.TaskGroup() as group:
                running["research"] = group.create_task(run_research())
                running["visual"] = group.create_task(prepare_visual())
                quiz_task = group.create_task(prepare_quiz())
        except ExceptionGroup:
            # TaskGroup first cancels/joins siblings. Keep unexpected child
            # failures behind the same fixed turn fallback, without leaking
            # private exception text. External cancellation still propagates.
            raise StageFailure("preparation_failed") from None
        finally:
            CALL_RESERVE.reset(reservation)
        results.research = running["research"].result()
        results.quiz_context = quiz_task.result()
        if tools["quiz"] == "pending":
            outcome = results.quiz_context or {}
            tools["quiz"] = ("finished" if outcome.get("finished") else
                             "question_ready" if "new_question" in outcome else
                             "awaiting_answer" if "awaiting" in outcome else
                             "feedback_ready" if "feedback" in outcome else "needs_input")
        results.artifact, results.visual_context = running["visual"].result()
        require_finished(tools)
        return results

    # ------------------------------------------------------------ respond

    async def respond(self, turn, view, decision, plan, results):
        state, budgets = turn.state, self.deps.budgets
        window = recent_window(view.numbered, budgets.respond_window, min_turns=min(2, budgets.min_turns),
                               message_max=budgets.message_max)
        # Question payloads stay out of the responder's view: the program
        # appends the new question to next_step afterwards, and showing it
        # here lets the coordinator solve it and hint the answer in its
        # feedback ("同时除以 4" right before "4x = 20"). A scalar flag keeps
        # the responder aware that the upcoming question carries a picture.
        quiz_view = {key: value for key, value in (results.quiz_context or {}).items()
                     if key not in ("new_question", "awaiting")}
        if ((results.quiz_context or {}).get("new_question") or {}).get("has_picture"):
            quiz_view["new_question_has_picture"] = True
        response_input = {
            "phase": "respond", "messages": window,
            "initial_explanation": plan.initial_explanation,
            "capabilities": self.capabilities(), "external_declined": EXTERNAL_DECLINED.get(),
            "context_notes": decision.notes or empty_notes(),
            "learner": learner_card(state, view.memory.snapshot, memory_enabled=view.memory.enabled),
            "plan": plan.value, "memory": view.memory_view,
            "state": {key: state.get(key) for key in (Key.ACTIVE_TASK, Key.PENDING_TASKS, Key.RESUME_STAGE)},
            "research": research_digest(results.research), "quiz": quiz_view,
            "visual_status": ("ready" if results.artifact else
                              "unavailable" if plan.tools["visual"] == "unavailable" else "not_requested"),
            "visual_context": results.visual_context,
            "memory_enabled": view.memory.enabled, "first_meeting": view.first_meeting,
            "tool_results": deepcopy(plan.tools),
        }
        decision.report["respond_tokens"] = count_tokens(response_input)
        output = await self.deps.runner("coordinator", response_input, self.deps.response_schema)
        problem = respond_problem(output, state, view.memory.enabled)
        if problem:
            # One corrected redraft; still leaking falls back like other contract errors.
            output = await self.deps.runner("coordinator", {**response_input, "correction": problem},
                                       self.deps.response_schema)
            if respond_problem(output, state, view.memory.enabled):
                raise ContractError("respond_correction_rejected")
        return output

    # ------------------------------------------------------------ finish

    def finish(self, turn, view, decision, plan, results, output):
        state, value, tools = turn.state, plan.value, plan.tools
        visual_failed = tools["visual"] == "unavailable"
        if plan.initial_explanation:
            # The delivered explanation is an immutable prefix. A model that
            # repeats that exact prefix does not cause duplicate visible text.
            supplement = output["message"].strip()
            if supplement.startswith(plan.initial_explanation):
                supplement = supplement[len(plan.initial_explanation):].lstrip()
            output["message"] = plan.initial_explanation + ("\n\n" + supplement if supplement else "")
        stopping = value["end_task"] or value["pause_task"] or value["quiz_action"] == "stop"
        validate_response(output, self.deps.response_schema, value["intent"], results.research, stopping=stopping)
        if stopping:
            output["next_step"] = None
        quiz_context = results.quiz_context or {}
        if quiz_context.get("new_question"):
            output["next_step"] = question(render_question(quiz_context["new_question"]))
        offer_notes = False
        if quiz_context.get("finished"):
            finished = state.get(Key.QUIZ) or {}
            record_quiz_result(view.context, topic=finished.get("topic") or value["quiz_topic"],
                               kind=finished.get("kind", "practice"), answered=quiz_context.get("answered"),
                               correct=quiz_context.get("correct_count"), errors=finished.get("errors", []))
            if finished.get("kind") != "check":
                offer_notes = offer_notes_after_practice(output, view.memory.enabled)
        # Every accepted visual can have a meaningful observation/action step;
        # preserve it instead of replacing it with an unrelated practice offer.
        visual_asks = results.artifact is not None and output.get("next_step") is not None
        if (decision.check and not stopping and plan.primary in EXPLAIN_INTENTS and not visual_asks
                and tools["quiz"] in ("not_requested", "skipped_policy")):
            self.quiz.offer_check(output, decision.check, state, plan.brief)
        if tools["quiz"] in ("needs_input", "awaiting_answer") and (
                not output["next_step"] or output["next_step"]["kind"] != "question"):
            raise ContractError("quiz_wait_requires_question")
        # Also check the program-selected question text before appending trusted
        # source/artifact links; schemas alone do not validate URLs in text.
        validate_response(output, self.deps.response_schema, value["intent"], results.research, stopping=stopping)
        attach_links(output, results.research, results.artifact, visual_failed, results.pictures)
        validate(output, self.deps.response_schema)

        # Nothing persistent is committed before public output is valid.
        if results.artifact:
            self.deps.artifacts.commit(turn.session_id, results.artifact)
            tools["visual"] = "published"
        for picture in results.pictures:
            self.deps.artifacts.commit(turn.session_id, picture)
        self.quiz.save_evidence(turn, plan.quiz_evidence, view.memory.enabled, output)
        if offer_notes:
            state[Key.NOTES_OFFER] = True
        require_finished(tools)
        state[Key.LAST_EXECUTION] = {"mode": "tools", "tools": tools, "order": plan.order, "context": decision.report}
        if results.visual_context:
            state[Key.LAST_EXECUTION]["visual"] = visual_report(results.visual_context)
        if decision.repaired:
            state[Key.LAST_EXECUTION]["routing_repaired"] = decision.repaired
        return output, state


def offer_notes_after_practice(output, memory_enabled) -> bool:
    """A practice set is done: the reply ends; with records on, it asks once whether to compile notes."""
    output["next_step"] = question(NOTES_OFFER) if memory_enabled else None
    return memory_enabled


def attach_links(output, research, artifact, visual_failed, pictures):
    """Source notes and links the program appends after a checked reply."""
    if research is not None:
        if research["overall"] in RESEARCH_NOTES:
            output["message"] += "\n\n" + RESEARCH_NOTES[research["overall"]]
        sources = research.get("sources", [])[:3]
        if sources:
            output["message"] += "\n\n参考资料：" + "；".join(
                f"[{source['title'].replace('[', '（').replace(']', '）')}]({source['url']})" for source in sources)
    if artifact:
        output["message"] += f"\n\n[{VISUAL_LINK}]({artifact['url']})"
    elif visual_failed:
        output["message"] += "\n\n" + VISUAL_FAILED
    for picture in pictures:
        output["message"] += f"\n\n[{QUIZ_PICTURE_LINK}]({picture['url']})"


def visual_report(visual_context) -> dict:
    """Outcome codes of a visual for evaluations and logs (review text only, no learner content)."""
    quality, failure = visual_context.get("quality") or {}, visual_context.get("failure") or {}
    drafts = quality.get("attempts", failure.get("attempts"))
    # Codes only in the log: which draft was sent back for what.
    LOG.info("visual_result kind=%s status=%s failure=%s drafts=%s sent_back=%s",
             visual_context.get("kind"), visual_context.get("status"), failure.get("code"), drafts,
             "|".join(entry.split("：", 1)[0].replace(" ", "") for entry in failure.get("history", [])) or "-")
    return {"kind": visual_context.get("kind"), "status": visual_context.get("status"), "drafts": drafts,
            "failure": failure.get("code"),
            "issues": [f"{issue['code']}: {issue['message']}" for issue in failure.get("issues", [])][:6],
            "history": failure.get("history", [])}
