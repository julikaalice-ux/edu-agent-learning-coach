"""Learning notes: compiled from the learner's own words, only when they agree or ask."""

import logging

from learning_coach.domain.contracts import ContractError, validate
from learning_coach.domain.context import brief_card, memory_slice, note_slice, notes_window, number_turns
from learning_coach.domain.note_contracts import NOTES_SCHEMA
from learning_coach.domain.routing import INTENT_CHAT
from learning_coach.service.execution import StageFailure
from learning_coach.service.pipeline.state import NOTES_FOR_QUESTIONS, USER, Key, memory_enabled
from learning_coach.service.pipeline.texts import NEXT_TOPIC, question, reply
from learning_coach.service.ports import StorageError

LOG = logging.getLogger(__name__)

FOCUS_CHARS = 2000      # the learner's latest words used to pick related records
KNOWN_CONCEPTS = 20     # concept IDs of saved mistakes and notes shown to the notes agent
MISTAKES_LISTED = 5     # mistakes named in the reply to "整理错题"
QUESTION_CHARS = 40     # of each named mistake's question
# Sections whose every entry must quote the learner's own words.
EVIDENCE_SECTIONS = ("profile", "concepts", "error_records", "review_plan")
LEARNING_PREFIX = "/learning "   # a page action; it leaves the visible history afterwards


class NotesService:
    def __init__(self, deps, *, draft):
        self.deps = deps
        # draft(payload) -> notes draft; the pipeline decides which agent writes it.
        self.draft = draft

    async def compile(self, turn):
        """Turn this conversation's learner messages and explanations into learning notes, once agreed."""
        state = turn.state
        if not memory_enabled(state, self.deps.store, turn.memory_key):
            return reply("现在没有开启学习记录，笔记不会保存。想保存，可以先说“开启记录”。", INTENT_CHAT), state
        context = state.setdefault(Key.CONTEXT, {})
        numbered = number_turns(turn.messages)
        earlier = [item for item in numbered if item["turn"] < numbered[-1]["turn"]]
        shown, teaching, last = notes_window(
            earlier, after_turn=context.get("notes_upto_turn", 0), learner_budget=self.deps.budgets.notes_messages,
            teaching_budget=self.deps.budgets.notes_teaching, message_max=self.deps.budgets.message_max)
        if not shown and not teaching:
            return reply("这次对话里还没有新的内容可以整理。", INTENT_CHAT, question(NEXT_TOPIC)), state
        turns = {item["turn"] for item in shown}
        # Evidence must quote what the learner actually said in these turns (the
        # full words, so an omission mark in a shortened message cannot be quoted).
        said = "\n".join(item["text"] for item in earlier if item["role"] == USER and item["turn"] in turns)
        memory = self.deps.store.snapshot(turn.memory_key)
        cards = self._cards(turn)
        payload = {
            "learner_messages": shown,
            "teaching_messages": teaching,
            "evidence_turns": [item for item in context.get("evidence_turns", []) if item in turns],
            "context_notes": note_slice(context.get("notes"), *NOTES_FOR_QUESTIONS),
            "quiz_results": context.get("quiz_results", []),
            "learner": brief_card(state, memory),
            "memory": memory_slice(memory, " ".join(item["text"] for item in shown)[-FOCUS_CHARS:],
                                   self.deps.budgets.memory),
            "known_concepts": known_concepts(cards),
        }
        try:
            notes = validate(await self.draft(payload), NOTES_SCHEMA)
            dropped = keep_checked(notes, said, {item["turn"] for item in teaching})
            key = (f"notes:{turn.event_id}" if turn.memory_key == turn.session_id
                   else f"{turn.session_id}:notes:{turn.event_id}")
            written = self.deps.store.apply_notes(turn.memory_key, key, notes, said)
        except (StageFailure, ContractError, ValueError, OSError, StorageError):
            LOG.warning("notes_compile_failed")
            return reply("这次笔记没有整理成功，学习记录没有改动。想再试，可以说“整理笔记”。", INTENT_CHAT,
                         question(NEXT_TOPIC)), state
        if dropped:
            LOG.info("notes_items_dropped count=%s", dropped)
        # Everything given was compiled: up to and including this request, so the
        # request itself is never compiled later (a page action leaves the history,
        # so its turn number is used again by the next message). Otherwise only up
        # to the last turn given; the next compile continues from there.
        complete = not earlier or last >= earlier[-1]["turn"]
        context["notes_upto_turn"] = (numbered[-1]["turn"] - int(turn.text.startswith(LEARNING_PREFIX))
                                      if complete else last)
        context["evidence_turns"] = [item for item in context.get("evidence_turns", [])
                                     if item > context["notes_upto_turn"]]
        edited = {card["concept_id"] for card in cards["study_notes"] if card.get("edited")}
        kept = sum(1 for card in notes.get("study_notes", []) if card["concept_id"] in edited)
        counts = [(len(notes.get("study_notes", [])) - kept, "篇知识笔记"), (len(notes["concepts"]), "个知识点"),
                  (len(notes["error_records"]), "个易错点"), (len(notes["review_plan"]), "项复习安排"),
                  (len(notes["profile"]), "条学习情况")]
        saved = "、".join(f"{number} {label}" for number, label in counts if number)
        if not written:
            message = "这部分内容之前已经整理过了。"
        elif saved:
            message = f"整理好了，记下了{saved}。下次可以接着复习。"
        else:
            message = "看了一遍，这次没有需要新记下的内容。"
        if written and kept:
            message += f"\n{kept} 张你自己改过的笔记保留了你的版本，没有覆盖。"
        if written and dropped:
            message += f"\n有 {dropped} 条对不上你的原话或讲解内容，没有保存。"
        if not complete:
            message += "\n内容比较多，这次先整理到这里；再说一次“整理笔记”会接着整理后面的部分。"
        return reply(message, INTENT_CHAT, question(NEXT_TOPIC)), state

    def show_mistakes(self, turn):
        """The saved mistakes, read from the program's records; nothing is compiled."""
        state = turn.state
        if not memory_enabled(state, self.deps.store, turn.memory_key):
            return reply("现在没有开启学习记录，答错的题不会保存。想保存，可以先说“开启记录”。", INTENT_CHAT), state
        try:
            mistakes = self.deps.store.learning_view(turn.memory_key)["mistakes"]
        except (StorageError, OSError):
            return reply("错题记录这次没有读出来，请稍后再试。", INTENT_CHAT, question(NEXT_TOPIC)), state
        if not mistakes:
            return reply("还没有保存的错题。开启记录后，自己独立做题答错的题会自动保存下来。", INTENT_CHAT,
                         question(NEXT_TOPIC)), state
        solved = sum(1 for card in mistakes if card.get("solved"))
        recent = mistakes[-MISTAKES_LISTED:]
        lines = "\n".join(f"{number}. {card['title']}：{shorten(card['question'], QUESTION_CHARS)}"
                          for number, card in enumerate(recent, 1))
        message = (f"答错的题已经自动保存了，一共 {len(mistakes)} 道"
                   + (f"，其中 {solved} 道重做已经答对" if solved else "") + "。"
                   + ("最近的几道：" if len(mistakes) > len(recent) else "") + f"\n{lines}\n\n"
                   "在「错题集合」里可以逐题重做、看解析和上次的选择。")
        return reply(message, INTENT_CHAT, question(NEXT_TOPIC)), state

    def _cards(self, turn) -> dict:
        view = getattr(self.deps.store, "learning_view", None)
        try:
            return view(turn.memory_key) if view is not None else {"mistakes": [], "study_notes": []}
        except (StorageError, OSError):
            return {"mistakes": [], "study_notes": []}

    @staticmethod
    def decline(turn):
        return reply("好，这次先不整理。之后想整理，随时说“整理笔记”。", INTENT_CHAT, question(NEXT_TOPIC)), turn.state


def known_concepts(cards) -> list[dict]:
    """Concept IDs already used by this learner's mistakes and notes, so new notes reuse them."""
    known = {}
    for card in [*cards.get("mistakes", []), *cards.get("study_notes", [])]:
        known.setdefault(card["concept_id"], {"concept_id": card["concept_id"], "title": card["title"]})
    return list(known.values())[-KNOWN_CONCEPTS:]


def keep_checked(notes, said, teaching_turns) -> int:
    """Drop, one by one, entries that do not quote the learner or cite turns not shown; returns how many."""
    dropped = 0
    for section in EVIDENCE_SECTIONS:
        kept = [item for item in notes[section] if item["evidence_quote"] in said]
        dropped += len(notes[section]) - len(kept)
        notes[section] = kept
    if "study_notes" in notes:
        kept = [card for card in notes["study_notes"] if set(card["source_turns"]) <= teaching_turns]
        dropped += len(notes["study_notes"]) - len(kept)
        notes["study_notes"] = kept
    return dropped


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
