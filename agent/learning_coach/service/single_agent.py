"""Single-agent baseline for the single vs multi-agent comparison.

One model does every model job of a turn in one tool-using conversation: it
writes the search queries, judges the sources, designs the visual, writes the
question and writes the reply. Everything else is the same as the
multi-agent pipeline, so the comparison isolates the agent split:

- the same teaching rules (the prompt is assembled from the same files),
- the same model deployment and the same search, rendering and image tools,
- the same program checks and state machine: onboarding, record commands,
  grading a clear choice, quiz progress, notes consent, source-link and URL
  checks, and commit only after the reply passes validation.

What it does not have: a separate quiz writer, an independent reviewer for
visuals, and the coordinator's conversation notes (it sees the same recent
window only).
"""

from copy import deepcopy
import asyncio
import logging

from learning_coach.domain.contracts import (QUIZ_SCHEMA, TASK_SCHEMA, TEXT, DIFFICULTY, EASY, ContractError,
                                             Participant, QuizAction, QuizKind, check_question, object_schema,
                                             public_quiz, selected_choice, validate, values)
from learning_coach.domain.research_contracts import Evidence
from learning_coach.domain.context import learner_card, memory_slice, number_turns, recent_window
from learning_coach.domain.note_contracts import NOTES_SCHEMA
from learning_coach.domain.research_contracts import (MAX_QUERIES, MAX_QUERY_CHARS, MAX_SOURCES,
                                                      VERIFY_SCHEMA)
from learning_coach.domain.routing import INTENT_PRACTICE, Status
from learning_coach.domain.teaching_visuals import visual_handoff
from learning_coach.domain.visual_contracts import VISUAL_SCHEMA
from learning_coach.domain.visual_quality import gallery_problem, needs_explanatory_visual
from learning_coach.service.execution import EXTERNAL_DECLINED, Capability, external_allowed
from learning_coach.service.pipeline import LearningPipeline, permissions
from learning_coach.service.pipeline.checks import validate_response
from learning_coach.service.pipeline.quiz import CHECK_QUESTIONS, PICTURE_TIMEOUT_SECONDS, new_quiz, recent_questions
from learning_coach.service.pipeline.state import (Key, Mode, apply_task, default_participant, mark_evidence,
                                                   model_view, read_memory, record_quiz_result, track_tasks)
from learning_coach.service.pipeline.teach import attach_links, offer_notes_after_practice
from learning_coach.service.pipeline.texts import question as ask, render_question
from learning_coach.service.ports import (Artifacts, ImageGenerator, LearningMemory, SearchProvider,
                                          SingleAgentPort, StorageError, ToolSpec)
from learning_coach.service.research import assess_claims, clean_queries, search_sources
from learning_coach.service.visual_generation import _RENDER_CORRECTIONS, find_pictures, photo_entry


LOG = logging.getLogger(__name__)

MAX_SEARCHES = 2          # search calls per turn
MAX_VISUAL_DRAFTS = 2     # same as the multi-agent draft + one redraw
MAX_QUESTION_REJECTS = 2  # same as the quiz writer's two tries

QUERIES = {"type": "array", "minItems": 1, "maxItems": MAX_QUERIES,
           "items": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS, "pattern": r"\S"}}
SEARCH_PARAMETERS = object_schema({"question": TEXT, "queries": QUERIES})
CLAIMS_PARAMETERS = object_schema({"claims": VERIFY_SCHEMA["properties"]["claims"]})
VISUAL_PARAMETERS = object_schema({"goal": TEXT, "spec": VISUAL_SCHEMA})
QUESTION_PARAMETERS = object_schema({
    "kind": {"enum": values(QuizKind)},
    "topic": TEXT,
    "difficulty": DIFFICULTY,
    "total": {"type": "integer", "minimum": 1, "maximum": 20},
    "question": QUIZ_SCHEMA,
})


def single_turn_schema(response_schema):
    """The four-field reply plus the task controls the program applies."""
    return object_schema({
        "response": response_schema,
        "task": TASK_SCHEMA,
        "stop_quiz": {"type": "boolean"},
        "observe_learning": {"type": "boolean"},
    })


def _problem(code, message):
    return {"ok": False, "problem": code, "message": message}


class _Turn:
    """What the tools did during one turn; read by the program afterwards."""

    def __init__(self):
        self.calls = []
        self.searches = 0
        self.question_text = None
        self.queries, self.sources, self.warnings = [], {}, []
        self.claims, self.overall = None, None
        self.visual_drafts, self.artifact, self.visual_context = 0, None, None
        self.question, self.picture, self.rejects = None, None, 0
        self.denied_capabilities = set()

    def research(self):
        """Same shape as ResearchService.run(), or None when nothing was searched."""
        if not self.searches:
            return None
        warnings = list(self.warnings)
        if not self.sources:
            warnings.append("no_evidence")
        elif self.claims is None:
            warnings.append("claims_not_checked")
        return {"queries": self.queries, "sources": list(self.sources.values())[:MAX_SOURCES],
                "claims": self.claims or [], "overall": self.overall or Evidence.UNAVAILABLE,
                "warnings": list(dict.fromkeys(warnings))}


class SingleAgentPipeline(LearningPipeline):
    def __init__(self, agent: SingleAgentPort, provider: SearchProvider, store: LearningMemory,
                 artifacts: Artifacts, response_schema: dict, *,
                 image_generator: ImageGenerator | None = None, image_finder=None, onboarding: bool = False,
                 demo_stage: str | None = None, budgets=None):
        super().__init__(None, None, store, artifacts, response_schema, image_generator=image_generator,
                         image_finder=image_finder, onboarding=onboarding, demo_stage=demo_stage,
                         budgets=budgets)
        self.agent, self.provider = agent, provider
        self.turn_schema = single_turn_schema(response_schema)

    async def run_stream(self, messages, *, session_id, state=None, turn_id=None,
                         learner_id=None, returning_visitor=False, external_permissions=None):
        """Run one turn while streaming the reply's message text.

        Yields {"delta": text} events as the final message is written, then a
        final {"output": ..., "state": ...} event carrying the validated reply
        (the same values a plain run() would return). The deltas are the
        unvalidated draft; the final event is authoritative.
        """
        queue = asyncio.Queue()

        def on_delta(text):
            queue.put_nowait(text)

        task = asyncio.create_task(self.run(
            messages, session_id=session_id, state=state, turn_id=turn_id,
            learner_id=learner_id, returning_visitor=returning_visitor, on_delta=on_delta,
            external_permissions=external_permissions))
        getter = asyncio.create_task(queue.get())
        try:
            while True:
                finished, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                # Drain a delta that completed together with the final task.
                if getter in finished:
                    yield {"delta": getter.result()}
                    getter = asyncio.create_task(queue.get())
                    continue
                if task in finished:
                    break
            while True:
                try:
                    yield {"delta": queue.get_nowait()}
                except asyncio.QueueEmpty:
                    break
            output, state = task.result()
            yield {"output": output, "state": state}
        finally:
            # Closing a chat stream cancels its workers, including a suspended
            # model/tool call; no abandoned turn may commit a later result.
            for worker in (getter, task):
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(getter, task, return_exceptions=True)

    # Program-triggered drafts (“换一题”, “整理笔记”) go to the same single agent.
    async def _draft_notes(self, payload):
        return await self.agent("compile_notes", payload, NOTES_SCHEMA, [])

    async def _draft_question(self, request):
        return await self.agent("write_question", request, QUIZ_SCHEMA, [])

    def _grade_waiting(self, text, state):
        """Grade a clear choice on the waiting question, exactly as the multi-agent pipeline does."""
        quiz = state.get("quiz") or {}
        question = quiz.get("awaiting")
        if not question or quiz.get("finished"):
            return None, None
        selected = selected_choice(text, question)
        if selected is None:
            return None, None
        plan = {"intent": {"primary": INTENT_PRACTICE, "is_implicit": True}, "end_task": False,
                "pause_task": False, "quiz_action": QuizAction.CONTINUE, "quiz_topic": quiz.get("topic"),
                "quiz_total": quiz.get("total", CHECK_QUESTIONS), "difficulty": quiz.get("difficulty", EASY),
                "participant": quiz.get("participant", Participant.UNKNOWN)}
        graded, evidence, pending = self.quiz.grade(plan, text, state)
        quiz = state["quiz"]
        feedback = (graded or {}).get("feedback") or {
            "correct": selected == question["answer_index"], "explanation": question["explanation"]}
        info = {"feedback": feedback, "answered": quiz["answered"], "total": quiz["total"],
                "correct_count": quiz["correct"], "finished": quiz.get("finished", False),
                "kind": quiz.get("kind", QuizKind.PRACTICE)}
        if pending is not None:
            info["next_question_needed"] = {"topic": quiz["topic"], "difficulty": quiz["difficulty"],
                                            "number": quiz["answered"] + 1}
        return info, evidence

    def _tools(self, turn, session_id, state, learner_text):
        def needs_permission(capability, action):
            turn.denied_capabilities.add(capability)
            if EXTERNAL_DECLINED.get():
                return _problem("permission_declined", f"本次未授权{action}。继续用已有知识和本地图解说明，不再询问授权。")
            return {**_problem("permission_required", f"{action}需要学习者明确授权。请说明用途并征求本次授权，或继续使用已有知识和本地图解。"),
                    "required_capability": capability}

        async def search(question, queries):
            turn.calls.append("search")
            if not external_allowed(Capability.RESEARCH):
                return needs_permission(Capability.RESEARCH, "联网检索")
            if turn.searches >= MAX_SEARCHES:
                return _problem("search_limit", "本轮已经检索过两次，用已有资料继续。")
            turn.searches += 1
            turn.question_text = question
            cleaned, warnings = clean_queries(question, queries)
            found, more = await search_sources(self.provider, cleaned)
            turn.queries += [query for query in cleaned if query not in turn.queries]
            turn.warnings += warnings + more
            new = {key: value for key, value in found.items() if key not in turn.sources}
            turn.sources.update(new)
            if not turn.sources:
                return {"ok": True, "sources": [], "message": "没有搜到资料；不能声称已查证。"}
            return {"ok": True, "sources": list(new.values()),
                    "message": "先用 record_claims 核对要用的说法；链接由程序附在回复后面。"}

        async def record_claims(claims):
            turn.calls.append("record_claims")
            if not turn.sources:
                return _problem("no_sources", "还没有搜到资料，不能核对。")
            turn.claims, turn.overall, warnings = assess_claims(turn.sources, claims)
            turn.warnings += warnings
            return {"ok": True, "overall": turn.overall,
                    "claims": [{"text": claim["text"], "status": claim["status"]} for claim in turn.claims],
                    "message": "回复里只能把 verified 和 likely 的说法当作有依据。"}

        async def draw_visual(goal, spec):
            turn.calls.append("draw_visual")
            if turn.artifact is not None:
                return _problem("one_visual_per_turn", "本轮已经有一张图。")
            if turn.visual_drafts >= MAX_VISUAL_DRAFTS:
                return _problem("draft_limit", "本轮画图已试过两次，不再画。")
            turn.visual_drafts += 1
            try:
                validate(spec, VISUAL_SCHEMA)
            except ContractError:
                return _problem("schema_invalid", "草稿未符合图示协议；请使用给定类型与字段，不输出网页代码或额外字段。")
            explanatory = needs_explanatory_visual(goal, learner_text)
            if spec["kind"] == "illustration" and explanatory:
                return _problem("interaction_problem", "这个请求需要解释机制或演示变化；静态 AI 配图不能完成，请改用可检查的图解或分步 scene。")
            if spec["kind"] == "gallery":
                return await draw_gallery(spec, explanatory)
            try:
                image = None
                if spec["kind"] == "illustration":
                    if not external_allowed(Capability.IMAGE_GENERATION):
                        return needs_permission(Capability.IMAGE_GENERATION, "调用外部图片生成服务")
                    if self.image_generator is None:
                        raise RuntimeError("image_backend_unavailable")
                    image = await asyncio.wait_for(self.image_generator(spec["prompt"]),
                                                   timeout=PICTURE_TIMEOUT_SECONDS)
                artifact = (self.artifacts.prepare(session_id, spec, image=image) if image is not None
                            else self.artifacts.prepare(session_id, spec))
            except ValueError as error:
                if str(error) == "invalid_visual_dependency":
                    return _problem("render_unavailable", "本地图形渲染不可用，图不发布。")
                return _problem("render_invalid", _RENDER_CORRECTIONS.get(
                    str(error), "草稿无法渲染；检查图形边界、元素标识、连线引用、坐标范围和数值条件。"))
            except (RuntimeError, OSError, StorageError, TimeoutError):
                return _problem("render_unavailable", "本次图示渲染或图片生成服务不可用。")
            context = visual_handoff(spec)
            context.update(status="ready", quality={"verdict": "not_reviewed", "review_scope": "none",
                                                    "pixel_reviewed": False, "attempts": turn.visual_drafts})
            context["limitations"].append("图示只经过本地渲染检查，没有独立审阅。")
            turn.artifact, turn.visual_context = artifact, context
            return {"ok": True, "visual_context": context, "message": "图的链接由程序附在回复后面，不要自己写链接。"}

        async def draw_gallery(spec, explanatory):
            # No separate reviewer in this version: each panel takes its first
            # (best-ranked, Commons first) candidate.
            if self.image_finder is None:
                return _problem("gallery_unavailable", "这个环境不能用网络图片，请改用程序画的图。")
            if not external_allowed(Capability.IMAGE_SEARCH):
                return needs_permission(Capability.IMAGE_SEARCH, "联网搜图")
            problem = gallery_problem(spec, explanatory)
            if problem:
                return _problem(problem[0]["code"], problem[0]["message"])
            found = await find_pictures(self.image_finder, spec)
            photos = [photo_entry(options[0]) if options else None for options in found]
            if not any(photos):
                return _problem("no_pictures", "没有找到可用的网络图片；请改用程序画的图。")
            try:
                artifact = self.artifacts.prepare(session_id, spec, photos=photos)
            except ValueError as error:
                return _problem("render_invalid", "配套图（companion）" + _RENDER_CORRECTIONS.get(
                    str(error), "无法渲染；检查图形边界、元素标识、连线引用、坐标范围和数值条件。"))
            except (RuntimeError, OSError, StorageError):
                return _problem("render_unavailable", "本次图示渲染不可用。")
            context = visual_handoff(spec)
            context.update(status="ready", quality={"verdict": "not_reviewed", "review_scope": "none",
                                                    "pixel_reviewed": False, "attempts": turn.visual_drafts})
            context["limitations"].append("图片按搜索排序自动选取，没有独立审阅。")
            turn.artifact, turn.visual_context = artifact, context
            return {"ok": True, "visual_context": context, "message": "图的链接由程序附在回复后面，不要自己写链接。"}

        async def ask_question(kind, topic, difficulty, total, question):
            turn.calls.append("ask_question")
            if turn.question is not None:
                return _problem("one_question_per_turn", "本轮已经出过一道题。")
            if turn.rejects >= MAX_QUESTION_REJECTS:
                return _problem("question_limit", "本轮出题已被退回两次，不再出题。")
            try:
                validate(question, QUIZ_SCHEMA)
            except ContractError:
                turn.rejects += 1
                return _problem("schema_invalid", "题目不符合出题协议。")
            running = state.get("quiz") or {}
            active = running and not running.get("finished")
            if active and running.get("awaiting"):
                return _problem("question_waiting", "上一题还在等学习者回答，不能再出新题。")
            if kind == QuizKind.CHECK and active and running.get("kind", QuizKind.PRACTICE) != QuizKind.CHECK:
                return _problem("practice_running", "正在做一组练习，不插入检查题。")
            continue_set = (kind == QuizKind.PRACTICE and active
                            and running.get("kind", QuizKind.PRACTICE) == QuizKind.PRACTICE)
            history = recent_questions(running)
            problem = check_question(question)
            if problem is None and question["question"].strip().casefold() in {
                    item.strip().casefold() for item in history}:
                problem = "repeated_question"
            picture = None
            if problem is None and question["image"] is not None:
                picture = await self.quiz.picture(session_id, question)
                if picture is None:
                    return _problem("picture_not_available_write_text_only", "配图画不出来，请改出不用图的题。")
            if problem is not None:
                turn.rejects += 1
                return _problem(problem, "题目没通过程序检查，请按问题修改后重出。")
            if not continue_set:
                participant = running.get("participant") or default_participant(state)
                running = state[Key.QUIZ] = new_quiz(kind=kind, topic=topic, total=CHECK_QUESTIONS if kind == QuizKind.CHECK else total,
                                                     difficulty=difficulty, participant=participant, history=history)
            running["awaiting"] = question
            running.setdefault("question_history", []).append(question["question"])
            running["asked"] = running.get("asked", 0) + 1
            turn.question, turn.picture = question, picture
            return {"ok": True, "shown_to_learner": render_question(public_quiz(question)),
                    "answered": running["answered"], "total": running["total"],
                    "message": "题目由程序放在 next_step；message 里不要重复题目和选项，也不要透露答案。"}

        return [
            ToolSpec("search", "用 2–4 条搜索词检索资料，返回来源编号、标题、发布方和原文节选。", SEARCH_PARAMETERS, search),
            ToolSpec("record_claims", "核对要在回复里使用的说法：每条逐字引用节选作证据，程序检查后给出可信等级。",
                     CLAIMS_PARAMETERS, record_claims),
            ToolSpec("draw_visual", "画一张教学图（声明式规格，程序渲染），每轮最多一张。", VISUAL_PARAMETERS, draw_visual),
            ToolSpec("ask_question", "出一道单选题（练习题或讲完后的检查题），答案由程序保管和判分，每轮最多一道。",
                     QUESTION_PARAMETERS, ask_question),
        ]

    async def _teach(self, turn):
        state, text, budgets = turn.state, turn.text, self.budgets
        # An authorization pause must not consume an answer or advance a quiz:
        # the original learner turn will run again once consent is resolved.
        permission_baseline = deepcopy(state)
        numbered = number_turns(turn.messages)
        current_turn = numbered[-1]["turn"]
        context = state.setdefault(Key.CONTEXT, {})
        memory = read_memory(state, self.store, turn.memory_key)
        first_meeting = (not turn.returning_visitor and Key.ONBOARDING not in state
                         and not any(item["role"] == "assistant" for item in turn.messages)
                         and not (memory.snapshot.get("profile") or memory.snapshot.get("concepts")))
        previous_task, previous_quiz = state.get(Key.ACTIVE_TASK), deepcopy(state.get(Key.QUIZ))
        quiz_feedback, quiz_evidence = self._grade_waiting(text, state)

        visible_state = model_view(state)
        focus = " ".join(filter(None, (state.get(Key.ACTIVE_TASK), (state.get(Key.QUIZ) or {}).get("topic"), text)))
        payload = {
            "messages": recent_window(numbered, budgets.decide_window, min_turns=budgets.min_turns,
                                      message_max=budgets.message_max),
            "learner": learner_card(state, memory.snapshot, memory_enabled=memory.enabled),
            "state": visible_state, "memory": memory_slice(memory.snapshot, focus, budgets.memory),
            "memory_enabled": memory.enabled, "first_meeting": first_meeting,
            "quiz_feedback": quiz_feedback,
            "external_declined": EXTERNAL_DECLINED.get(),
            "capabilities": {
                **permissions.capabilities(research=self.provider, image_finder=self.image_finder,
                                           image_generator=self.image_generator),
                "local_visual": {"available": self.artifacts is not None, "authorized": True},
            },
        }
        log = _Turn()
        tools = self._tools(log, turn.session_id, state, text)
        if turn.on_delta is not None and hasattr(self.agent, "stream_call"):
            drafted = await self.agent.stream_call("teach", payload, self.turn_schema, tools, on_delta=turn.on_delta)
        else:
            drafted = await self.agent("teach", payload, self.turn_schema, tools)
        result = validate(drafted, self.turn_schema)
        if log.denied_capabilities and not EXTERNAL_DECLINED.get():
            return permissions.ask(turn, permission_baseline, sorted(log.denied_capabilities),
                                   "为当前问题补充外部资料或图片", result["response"]["intent"]["primary"])
        output, task = result["response"], result["task"]
        primary = output["intent"]["primary"]
        stopping = task["end_task"] or task["pause_task"] or result["stop_quiz"]
        apply_task(state, primary, {**task, "quiz_action": QuizAction.STOP if result["stop_quiz"] else QuizAction.NONE})
        track_tasks(state, previous_task, previous_quiz, current_turn, text)
        research = log.research()
        validate_response(output, self.response_schema, output["intent"], research, stopping=stopping)
        if stopping:
            output["next_step"] = None
        elif log.question is not None:
            output["next_step"] = ask(render_question(public_quiz(log.question)))

        offer_notes = False
        if quiz_feedback and quiz_feedback["finished"]:
            done = state.get(Key.QUIZ) or previous_quiz or {}
            record_quiz_result(context, topic=done.get("topic"), kind=quiz_feedback["kind"],
                               answered=quiz_feedback["answered"], correct=quiz_feedback["correct_count"],
                               errors=done.get("errors", []))
            if quiz_feedback["kind"] != QuizKind.CHECK and log.question is None:
                offer_notes = offer_notes_after_practice(output, memory.enabled)
        waiting = (state.get(Key.QUIZ) or {}).get("awaiting")
        if (waiting and not stopping and log.question is None
                and (not output["next_step"] or output["next_step"]["kind"] != "question")):
            # A question the learner still owes an answer to stays in view.
            output["next_step"] = ask(render_question(public_quiz(waiting)))
        validate_response(output, self.response_schema, output["intent"], research, stopping=stopping)
        attach_links(output, research, log.artifact, bool(log.visual_drafts),
                     [log.picture] if log.picture else [])
        validate(output, self.response_schema)

        # Nothing persistent is committed before the public output is valid.
        if log.artifact:
            self.artifacts.commit(turn.session_id, log.artifact)
        if log.picture:
            self.artifacts.commit(turn.session_id, log.picture)
        self.quiz.save_evidence(turn, quiz_evidence, memory.enabled, output)
        if result["observe_learning"] and memory.enabled:
            mark_evidence(context, current_turn)
        if offer_notes:
            state[Key.NOTES_OFFER] = True
        state[Key.LAST_EXECUTION] = {"mode": Mode.SINGLE, "tool_calls": log.calls,
                                     "research": research["overall"] if research else Status.NOT_REQUESTED,
                                     "visual": Status.PUBLISHED if log.artifact else
                                     Status.UNAVAILABLE if log.visual_drafts else Status.NOT_REQUESTED,
                                     "question": log.question is not None}
        return output, state
