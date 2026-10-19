"""Learning notes: compiled from the learner's own words, only when they agree or ask."""

import logging

from learning_coach.domain.contracts import ContractError, validate
from learning_coach.domain.context import brief_card, learner_messages, memory_slice, note_slice, number_turns
from learning_coach.domain.note_contracts import NOTES_SCHEMA
from learning_coach.domain.routing import INTENT_CHAT
from learning_coach.service.execution import StageFailure
from learning_coach.service.pipeline.state import Key, memory_enabled
from learning_coach.service.pipeline.texts import NEXT_TOPIC, question, reply
from learning_coach.service.ports import StorageError

LOG = logging.getLogger(__name__)

FOCUS_CHARS = 2000  # the learner's latest words used to pick related records


class NotesService:
    def __init__(self, deps, *, draft):
        self.deps = deps
        # draft(payload) -> notes draft; the pipeline decides which agent writes it.
        self.draft = draft

    async def compile(self, turn):
        """Turn this conversation's learner messages into learning notes, once agreed."""
        state = turn.state
        if not memory_enabled(state, self.deps.store, turn.memory_key):
            return reply("现在没有开启学习记录，笔记不会保存。想保存，可以先说“开启记录”。", INTENT_CHAT), state
        context = state.setdefault(Key.CONTEXT, {})
        numbered = number_turns(turn.messages)
        earlier = [item for item in numbered if item["turn"] < numbered[-1]["turn"]]
        shown = learner_messages(earlier, after_turn=context.get("notes_upto_turn", 0),
                                 budget=self.deps.budgets.notes_messages, message_max=self.deps.budgets.message_max)
        if not shown:
            return reply("这次对话里还没有新的内容可以整理。", INTENT_CHAT, question(NEXT_TOPIC)), state
        turns = {item["turn"] for item in shown}
        # Evidence must quote what the learner actually said in these turns.
        said = "\n".join(item["text"] for item in earlier if item["role"] == "user" and item["turn"] in turns)
        memory = self.deps.store.snapshot(turn.memory_key)
        payload = {
            "learner_messages": shown,
            "evidence_turns": [item for item in context.get("evidence_turns", []) if item in turns],
            "context_notes": note_slice(context.get("notes"), "covered", "stuck"),
            "quiz_results": context.get("quiz_results", []),
            "learner": brief_card(state, memory),
            "memory": memory_slice(memory, " ".join(item["text"] for item in shown)[-FOCUS_CHARS:],
                                   self.deps.budgets.memory),
        }
        try:
            notes = validate(await self.draft(payload), NOTES_SCHEMA)
            key = (f"notes:{turn.event_id}" if turn.memory_key == turn.session_id
                   else f"{turn.session_id}:notes:{turn.event_id}")
            written = self.deps.store.apply_notes(turn.memory_key, key, notes, said)
        except (StageFailure, ContractError, ValueError, OSError, StorageError):
            LOG.warning("notes_compile_failed")
            return reply("这次笔记没有整理成功，学习记录没有改动。想再试，可以说“整理笔记”。", INTENT_CHAT,
                         question(NEXT_TOPIC)), state
        # Up to and including this request, so the request itself is never compiled later.
        context["notes_upto_turn"] = numbered[-1]["turn"]
        context["evidence_turns"] = []
        counts = [(len(notes["concepts"]), "个知识点"), (len(notes["error_records"]), "个易错点"),
                  (len(notes["review_plan"]), "项复习安排"), (len(notes["profile"]), "条学习情况")]
        saved = "、".join(f"{number} {label}" for number, label in counts if number)
        if not written:
            message = "这部分内容之前已经整理过了。"
        elif saved:
            message = f"整理好了，记下了{saved}。下次可以接着复习。"
        else:
            message = "看了一遍，这次没有需要新记下的内容。"
        return reply(message, INTENT_CHAT, question(NEXT_TOPIC)), state

    @staticmethod
    def decline(turn):
        return reply("好，这次先不整理。之后想整理，随时说“整理笔记”。", INTENT_CHAT, question(NEXT_TOPIC)), turn.state
