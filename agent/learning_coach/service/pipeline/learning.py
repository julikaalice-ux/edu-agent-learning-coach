"""Session-scoped UI actions over the existing authenticated Responses transport."""
import json
from learning_coach.service.pipeline.texts import reply
from learning_coach.service.pipeline.state import memory_enabled
from learning_coach.service.ports import StorageError

PREFIX = "/learning "
RESPONSE_PREFIX = '{"learning_protocol":'

async def handle(pipeline, turn):
    try:
        request = json.loads(turn.text[len(PREFIX):])
        if not isinstance(request, dict):
            raise ValueError("invalid_action")
        action = request.get("action")
        store, key = pipeline.store, turn.memory_key
        message, result = "", None
        if action == "records":
            if type(request.get("enabled")) is not bool:
                raise ValueError("invalid_enabled")
            response, _ = pipeline.commands.set_records(turn, request["enabled"])
            message = response["message"]
            if response.get("next_step"):
                message += "\n" + response["next_step"]["text"]
        elif action == "compile":
            response, _ = await pipeline.notes.compile(turn)
            message = response["message"]
        elif action == "edit":
            if not memory_enabled(turn.state, store, key):
                raise ValueError("records_disabled")
            store.edit_study_note(key, request.get("card_id"), request.get("fields"))
            message = "笔记修改已保存，已标注为学习者编辑。"
        elif action in ("retry", "reveal"):
            if not memory_enabled(turn.state, store, key):
                raise ValueError("records_disabled")
            result = store.retry_card(key, request.get("card_id"), request.get("event_id"),
                                      request.get("choice"), action == "reveal")
        elif action != "read":
            raise ValueError("unknown_learning_action")
        view = store.learning_view(key) if memory_enabled(turn.state, store, key) else {"enabled": False, "mistakes": [], "study_notes": []}
        envelope = {"learning_protocol": 1, "ok": True, "view": view, "message": message, "result": result}
    except (ValueError, TypeError, StorageError, OSError):
        envelope = {"learning_protocol": 1, "ok": False, "message": "记录操作未完成，请检查是否已开启记录，或刷新后重试。"}
    return reply(json.dumps(envelope, ensure_ascii=False), "E"), turn.state
