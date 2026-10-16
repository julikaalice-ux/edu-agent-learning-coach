"""Foundry Memory semantic mirror over the SQLite LearningStore.

SQLite stays authoritative for deterministic state (mastery, idempotency,
consent). This adapter mirrors validated notes into a Foundry Memory store
under one scope per memory key, and merges semantic recalls into snapshot().
Foundry Memory failures never propagate: every method degrades to the inner
SQLite behavior. Facts carry no evidence quotes (raw learner words stay in
SQLite only), and fact content never appears in logs.

The Foundry client is injected by server.py (real or fake); this module never
constructs credentials and imports azure.ai.projects lazily inside snapshot().
"""

import logging

from learning_coach.domain.note_contracts import ERROR_LABELS

LOG = logging.getLogger(__name__)

PROFILE_LABELS = {
    "learning_stage": "学习阶段",
    "age_band": "年龄段",
    "learning_goal": "学习目标",
    "prior_knowledge": "已有基础",
    "preferred_language": "偏好语言",
    "available_time": "可用时间",
    "accessibility_needs": "无障碍需求",
    "companion_role": "陪伴者角色",
    "preferred_method": "偏好方式",
    "learning_context": "学习场景",
}
ORIGIN_LABELS = {"self_report": "自述", "companion_report": "陪伴者转述"}
MEMORY_KIND_USER_PROFILE = "user_profile"
SEMANTIC_CONTENT_MAX = 300  # defensive cap before recalled facts enter a prompt


def facts_from_notes(notes) -> list[str]:
    """Render the four validated note sections into compact fact strings.

    Evidence quotes are deliberately left out: the learner's own words stay in
    SQLite only. Empty notes (quiz-evidence-only turns) produce no facts.
    """
    facts = []
    for item in notes.get("profile", []):
        label = PROFILE_LABELS.get(item.get("key"), item.get("key", "档案项"))
        origin = ORIGIN_LABELS.get(item.get("origin"), "")
        suffix = f"（{origin}）" if origin else ""
        facts.append(f"档案: {label}={item.get('value', '')}{suffix}")
    for item in notes.get("concepts", []):
        label = item.get("label") or item.get("concept_id") or "概念"
        text = f"概念: {label}"
        if item.get("concept_id"):
            text += f"（{item['concept_id']}）"
        if item.get("prerequisites"):
            text += "；前备: " + "、".join(item["prerequisites"])
        if item.get("related"):
            text += "；关联: " + "、".join(item["related"])
        facts.append(text)
    for item in notes.get("error_records", []):
        category = ERROR_LABELS.get(item.get("category"), item.get("category", "原因待确认"))
        facts.append(f"易错点: {item.get('concept_id', '')} — {category}")
    for item in notes.get("review_plan", []):
        text = f"复习安排: {item.get('topic', '')}"
        if item.get("deadline"):
            text += f"（截止: {item['deadline']}）"
        facts.append(text)
    return facts


class FoundryMemoryStore:
    """LearningMemory wrapper: inner LearningStore + a best-effort semantic mirror."""

    def __init__(self, inner, client, store_name, *, search_max=6):
        self._inner = inner
        self._client = client
        self._store = store_name
        self._search_max = search_max

    def is_enabled(self, session_id: str) -> bool:
        return self._inner.is_enabled(session_id)

    def set_enabled(self, session_id: str, enabled: bool) -> None:
        self._inner.set_enabled(session_id, enabled)

    def apply_notes(self, session_id: str, turn_id: str, notes: dict,
                    learner_text: str, quiz_evidence: dict | None = None) -> bool:
        accepted = self._inner.apply_notes(session_id, turn_id, notes, learner_text, quiz_evidence)
        if not accepted:
            return accepted
        # Mirror only after a successful inner write; idempotent replays (inner
        # returns False for an already-applied turn) never mirror again.
        facts = facts_from_notes(notes)
        mirrored = 0
        for fact in facts:
            try:
                self._client.create_memory(self._store, scope=session_id,
                                           content=fact, kind=MEMORY_KIND_USER_PROFILE)
                mirrored += 1
            except Exception as error:
                # Never log fact content; the exception type tells ops which
                # layer failed (e.g. HttpResponseError = rejected by the service).
                LOG.warning("foundry_memory_mirror_failed error=%s", type(error).__name__)
        if mirrored:
            # The scope key is the platform-assigned memory key; logged for
            # operations (project-private logs), never the fact content.
            LOG.info("foundry_memory_mirrored scope=%s facts=%d", session_id, mirrored)
        return accepted

    def snapshot(self, session_id: str) -> dict:
        memory = self._inner.snapshot(session_id)
        memory["semantic"] = []
        if not self._inner.is_enabled(session_id):
            # Records are off; searching would leak previously mirrored items.
            return memory
        try:
            from azure.ai.projects.models import MemorySearchOptions
            result = self._client.search_memories(
                self._store, scope=session_id, items=None,
                options=MemorySearchOptions(max_memories=self._search_max))
            memory["semantic"] = [
                {"id": item.memory_item.memory_id,
                 "content": (item.memory_item.content or "")[:SEMANTIC_CONTENT_MAX]}
                for item in getattr(result, "memories", []) or []
            ]
        except Exception:
            LOG.warning("foundry_memory_search_unavailable")
        return memory

    def clear(self, session_id: str) -> None:
        self._inner.clear(session_id)
        try:
            self._client.delete_scope(self._store, session_id)
        except Exception:
            LOG.warning("foundry_memory_scope_delete_failed")

    def close(self) -> None:
        # Not on the LearningMemory protocol, but demo.py closes the store.
        self._inner.close()
        try:
            self._client.close()
        except Exception:
            pass
