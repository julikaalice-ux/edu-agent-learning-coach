"""What each agent sees, within token budgets. Pure functions; no model calls.

The conversation is never simply cut off. Each turn the coordinator sees:

* recent turns, newest first until the window budget is used (at least the
  last few turns stay whole; a very long single message keeps its head and
  tail);
* an index of every earlier turn (turn number plus the start of what the
  learner said), so it can ask to look back at any of them;
* earlier turns recalled for this message: found automatically by text
  overlap, or requested by the coordinator by turn number or by a phrase;
* conversation notes it maintains itself (what was covered, where the
  learner got stuck, requests and corrections, open questions), checked and
  capped by the program;
* a learner card and a slice of the learning records, both built by the
  program.

Token counts use tiktoken's o200k_base encoding when it is installed and its
vocabulary is available; otherwise a conservative estimate (one token per
CJK character, one per four other characters). Real usage reported by the
model API is logged by the runner, so the estimate can be checked.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields
import json
import math
import os
import re


def object_schema(properties):
    # Same shape as contracts.object_schema; kept local so contracts can import this module.
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


# ---------------------------------------------------------------- budgets

@dataclass(frozen=True)
class Budgets:
    """Token budgets. Every value can be overridden by LEARNING_COACH_CTX_<NAME>."""

    decide_window: int = 4000      # recent turns shown when the coordinator decides
    respond_window: int = 2000     # recent turns shown when it writes the reply
    sub_window: int = 1000         # recent turns handed to quiz / visual agents
    min_turns: int = 3             # these newest turns always stay, whatever their size
    message_max: int = 1500        # one message longer than this keeps only head and tail
    recall: int = 2000             # earlier turns recalled into view
    index_entries: int = 40        # earlier-turn index lines (newest kept)
    memory: int = 1500             # slice of the learning records
    notes: int = 1000              # conversation notes
    notes_messages: int = 3000     # learner messages given to the notes agent

    @classmethod
    def from_env(cls, environ=None) -> "Budgets":
        environ = os.environ if environ is None else environ
        values = {}
        for item in fields(cls):
            raw = environ.get(f"LEARNING_COACH_CTX_{item.name.upper()}")
            if raw is None:
                continue
            try:
                number = int(raw)
            except ValueError:
                continue
            if number > 0:
                values[item.name] = number
        return cls(**values)


# ---------------------------------------------------------------- tokens

_ENCODER = None
_ENCODER_LOADED = False
_CJK = re.compile(r"[⺀-鿿豈-﫿＀-￯　-〿]")


def encoder():
    """tiktoken's o200k_base encoder, or None (then the estimate is used)."""
    global _ENCODER, _ENCODER_LOADED
    if not _ENCODER_LOADED:
        _ENCODER_LOADED = True
        if os.environ.get("LEARNING_COACH_TOKENIZER", "tiktoken") == "tiktoken":
            try:
                import tiktoken

                _ENCODER = tiktoken.get_encoding("o200k_base")
            except Exception:  # missing package or vocabulary download refused
                _ENCODER = None
    return _ENCODER


def tokenizer_name() -> str:
    return "tiktoken:o200k_base" if encoder() is not None else "estimate"


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)


def count_tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    enc = encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return estimate_tokens(text)


def trim_text(text: str, limit: int) -> str:
    """Keep head and tail of an over-long message, saying how much was left out."""
    total = count_tokens(text)
    if total <= limit:
        return text
    per_token = len(text) / max(total, 1)
    head = max(1, int(limit * 0.7 * per_token))
    tail = max(1, int(limit * 0.2 * per_token))
    omitted = len(text) - head - tail
    return f"{text[:head]}\n…（中间省略约 {omitted} 字）…\n{text[-tail:]}"


# ---------------------------------------------------------------- turns

def number_turns(messages) -> list[dict]:
    """Attach a turn number: each learner message starts a turn, replies join it."""
    numbered, turn = [], 0
    for item in messages:
        if item["role"] == "user" or turn == 0:
            turn += 1
        numbered.append({"turn": turn, "role": item["role"], "text": item["text"]})
    return numbered


def _group(numbered) -> dict[int, list[dict]]:
    turns: dict[int, list[dict]] = {}
    for item in numbered:
        turns.setdefault(item["turn"], []).append(item)
    return turns


def _shown(items, message_max):
    return [{"turn": m["turn"], "role": m["role"], "text": trim_text(m["text"], message_max)} for m in items]


def recent_window(numbered, budget: int, *, min_turns: int, message_max: int) -> list[dict]:
    """Newest turns first until the budget is used; the newest min_turns always stay."""
    chosen, used = [], 0
    for index, (_, items) in enumerate(sorted(_group(numbered).items(), reverse=True)):
        shown = _shown(items, message_max)
        size = count_tokens(shown)
        if index >= min_turns and used + size > budget:
            break
        chosen[:0] = shown
        used += size
    return chosen


def earlier_index(numbered, window, limit: int) -> dict:
    """One line per turn that is out of view, so any of them can be looked up."""
    first_shown = window[0]["turn"] if window else math.inf
    lines = []
    for turn, items in sorted(_group(numbered).items()):
        if turn >= first_shown:
            break
        learner = next((m["text"] for m in items if m["role"] == "user"), "")
        lines.append({"turn": turn, "learner": re.sub(r"\s+", " ", learner).strip()[:40]})
    return {"count": len(lines), "index": lines[-limit:]}


def _bigrams(text: str) -> set[str]:
    text = text.casefold()
    grams = set()
    for run in re.findall(r"[一-鿿]+", text):
        grams.update(run[i:i + 2] for i in range(len(run) - 1))
    grams.update(word for word in re.findall(r"[a-z0-9]{3,}", text))
    return grams


def overlap(focus: str, text: str) -> int:
    return len(_bigrams(focus) & _bigrams(text))


def related_turns(numbered, focus: str, exclude: set[int], *, limit: int, minimum: float = 1.0) -> list[int]:
    """Earlier turns that share distinctive wording with focus.

    Each shared word pair counts 1/(number of earlier turns containing it),
    so a word used once ("披萨") weighs more than one used everywhere
    ("分数"). The newest turn (the message being answered) is not counted.
    """
    groups = _group(numbered)
    newest = max(groups) if groups else 0
    wanted = _bigrams(focus)
    shared = {turn: _bigrams(" ".join(m["text"] for m in items)) & wanted
              for turn, items in groups.items() if turn != newest}
    frequency = Counter(gram for grams in shared.values() for gram in grams)
    scored = []
    for turn, grams in shared.items():
        if turn in exclude:
            continue
        score = sum(1 / frequency[gram] for gram in grams)
        if score >= minimum:
            scored.append((score, turn))
    scored.sort(key=lambda pair: (-pair[0], -pair[1]))
    return [turn for _, turn in scored[:limit]]


def recall(numbered, turns, budget: int, *, message_max: int) -> list[dict]:
    """Full messages of the requested turns, in turn order, within the budget."""
    groups = _group(numbered)
    chosen, used = [], 0
    for turn in turns:
        if turn not in groups:
            continue
        shown = _shown(groups[turn], message_max)
        size = count_tokens(shown)
        if used + size > budget:
            continue
        chosen.append((turn, shown))
        used += size
    return [message for _, shown in sorted(chosen) for message in shown]


def learner_messages(numbered, *, after_turn: int, budget: int, message_max: int) -> list[dict]:
    """The learner's own words since after_turn, newest kept first when over budget."""
    chosen, used = [], 0
    for item in reversed(numbered):
        if item["role"] != "user" or item["turn"] <= after_turn:
            continue
        shown = {"turn": item["turn"], "text": trim_text(item["text"], message_max)}
        size = count_tokens(shown)
        if chosen and used + size > budget:
            break
        chosen.insert(0, shown)
        used += size
    return chosen


# ---------------------------------------------------------------- conversation notes

NOTE_LIMITS = {"covered": 6, "stuck": 4, "requests": 3, "open_questions": 3}
_NOTE_TEXT = {"type": "string", "minLength": 1, "maxLength": 300, "pattern": r"\S"}
_TURN = {"type": "integer", "minimum": 1}
_NOTE_ITEMS = {
    "covered": object_schema({"point": _NOTE_TEXT, "turn": _TURN}),
    "stuck": object_schema({"quote": _NOTE_TEXT, "issue": _NOTE_TEXT, "turn": _TURN}),
    "requests": object_schema({"text": _NOTE_TEXT, "turn": _TURN}),
    "open_questions": object_schema({"text": _NOTE_TEXT, "turn": _TURN}),
}
# The schema is generous on purpose: a few extra or long notes must not fail
# the whole decision. The program trims to NOTE_LIMITS afterwards.
CONTEXT_NOTES_SCHEMA = object_schema({
    name: {"type": "array", "items": item, "maxItems": 12} for name, item in _NOTE_ITEMS.items()
})
LOOKBACK_SCHEMA = object_schema({
    "turns": {"type": "array", "items": _TURN, "maxItems": 6},
    "query": {"anyOf": [{"type": "string", "minLength": 1, "maxLength": 200}, {"type": "null"}]},
})
CONTEXT_UPDATE_SCHEMA = {"anyOf": [{"type": "null"}, object_schema({
    "notes": {"anyOf": [{"type": "null"}, CONTEXT_NOTES_SCHEMA]},
    "lookback": {"anyOf": [{"type": "null"}, LOOKBACK_SCHEMA]},
})]}
_SHORT = {"point": 80, "quote": 120, "issue": 80, "text": 80}


def empty_notes() -> dict:
    return {name: [] for name in NOTE_LIMITS}


def accept_notes(proposed, previous, numbered, budget: int) -> tuple[dict, list[str]]:
    """Check the coordinator's notes; keep the previous ones when nothing usable came back.

    Turn numbers must exist. A "stuck" quote must be something the learner
    actually said; its turn is corrected to where it was said. Each category
    keeps its newest items within NOTE_LIMITS, then the whole set is cut to
    the token budget, dropping covered points first (they are the easiest to
    rebuild from recent turns).
    """
    previous = previous or empty_notes()
    if not isinstance(proposed, dict):
        return previous, []
    last_turn = numbered[-1]["turn"] if numbered else 0
    learner = [(m["turn"], m["text"]) for m in numbered if m["role"] == "user"]
    problems, notes = [], empty_notes()
    for name, limit in NOTE_LIMITS.items():
        for item in proposed.get(name, []):
            item = {key: (value.strip()[:_SHORT[key]] if isinstance(value, str) else value)
                    for key, value in item.items()}
            if not 1 <= item["turn"] <= last_turn:
                problems.append(f"{name}_turn_unknown")
                continue
            if name == "stuck":
                found = [turn for turn, text in learner if item["quote"] in text]
                if not found:
                    problems.append("stuck_quote_not_said")
                    continue
                item["turn"] = found[-1]
            notes[name].append(item)
        notes[name] = notes[name][-limit:]
    for name in ("covered", "open_questions", "requests", "stuck"):
        while count_tokens(notes) > budget and notes[name]:
            notes[name].pop(0)
    return notes, problems


def note_slice(notes, *names) -> dict:
    notes = notes or empty_notes()
    return {name: notes.get(name, []) for name in names}


# ---------------------------------------------------------------- learner card and records

def learner_stage(state, memory) -> str | None:
    stage = (state.get("onboarding") or {}).get("stage")
    if stage and stage != "不想说":
        return stage
    profile = (memory or {}).get("profile", {}).get("learning_stage")
    return profile.get("value") if isinstance(profile, dict) else None


def learner_card(state, memory, *, memory_enabled: bool) -> dict:
    """Who is learning and where the session stands. Built by the program only."""
    context = state.get("context") or {}
    onboarding = state.get("onboarding") or {}
    return {
        "stage": learner_stage(state, memory),
        "who": {"self": "learner", "companion": "companion"}.get(onboarding.get("who")),
        "records": "on" if memory_enabled else "off",
        "goal": context.get("goal"),
        "active_task": state.get("active_task"),
        "paused_tasks": list(state.get("pending_tasks", [])),
        "finished_tasks": list(context.get("finished", [])),
        "quiz_results": list(context.get("quiz_results", [])),
    }


def brief_card(state, memory, participant: str | None = None) -> dict:
    """The part of the learner card every sub-agent receives."""
    context = state.get("context") or {}
    goal = context.get("goal") or {}
    return {"stage": learner_stage(state, memory), "goal": goal.get("text"),
            "participant": participant or (state.get("quiz") or {}).get("participant") or "unknown"}


def _strip(item, *drop):
    return {key: value for key, value in item.items() if key not in drop}


def memory_slice(memory, focus: str, budget: int) -> dict:
    """Profile, the concepts most related to the focus (then the weakest),
    the latest errors and review items; evidence quotes are left out."""
    memory = memory or {}
    profile = {key: _strip(value, "evidence_quote", "turn_id")
               for key, value in memory.get("profile", {}).items()}
    concepts = sorted(memory.get("concepts", []), key=lambda item: (
        -overlap(focus, f"{item.get('label', '')} {item.get('concept_id', '')}"),
        item.get("mastery_level") if item.get("mastery_level") is not None else 0.5))
    result = {
        "schema_version": memory.get("schema_version", "1.0"),
        "profile": profile,
        "concepts": [_strip(item, "evidence_quote") for item in concepts[:8]],
        "error_records": [_strip(item, "evidence_quote", "turn_id") for item in memory.get("error_records", [])[-5:]],
        "review_plan": [_strip(item, "evidence_quote", "turn_id") for item in memory.get("review_plan", [])[-5:]],
    }
    for name in ("review_plan", "error_records", "concepts"):
        while count_tokens(result) > budget and result[name]:
            result[name].pop()
    totals = {name: len(memory.get(name, [])) for name in ("concepts", "error_records", "review_plan")}
    result["not_shown"] = {name: totals[name] - len(result[name]) for name in totals}
    return result


def research_digest(research):
    """Conclusions and where they come from, without the raw page excerpts."""
    if research is None:
        return None
    return {
        "overall": research.get("overall"),
        "claims": [{"text": claim["text"], "status": claim["status"],
                    "evidence": [{"source_id": e["source_id"], "quote": e["quote"][:300], "relation": e["relation"]}
                                 for e in claim.get("evidence", [])[:2]]}
                   for claim in research.get("claims", [])],
        "sources": [{key: source.get(key) for key in ("id", "title", "url", "publisher")}
                    for source in research.get("sources", [])],
        "warnings": list(research.get("warnings", [])),
    }
