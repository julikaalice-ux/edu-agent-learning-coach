"""Only a validated final response crosses the Responses hosting boundary.

The learner-facing text is the wire response's ``message`` plus any ``next_step``
prompt (quiz question faces included). The full four-field wire stays in session
state (``learning_coach.last_response``) for evaluation and debugging; it is
never serialized as chat text.
"""

from copy import deepcopy
from uuid import uuid4

from agent_framework import AgentResponse, AgentResponseUpdate, BaseAgent, Content, Message, ResponseStream


def learner_text(output):
    """Compose the learner-facing text: message plus any next-step prompt.

    Quiz question faces and follow-up questions live in ``next_step``; without
    appending them the learner would never see the question.
    """
    text = output["message"]
    next_step = output.get("next_step")
    if next_step:
        text = f"{text}\n\n{next_step['text']}"
    return text


class LearningCoachAgent(BaseAgent):
    def __init__(self, pipeline):
        super().__init__(name="learning-coach", description="Five-role learning coach with validated output")
        self.pipeline = pipeline

    def run(self, messages=None, *, stream=False, session=None, options=None,
            function_invocation_kwargs=None, client_kwargs=None, **kwargs):
        active_session = session if session is not None else self.create_session()

        async def execute():
            if isinstance(messages, str):
                inputs = [{"role": "user", "text": messages}]
            elif isinstance(messages, Message):
                inputs = [{"role": messages.role, "text": messages.text}]
            else:
                inputs = [{"role": message.role, "text": message.text} for message in (messages or [])]
            output, state = await self.pipeline.run(
                inputs, session_id=active_session.session_id,
                state=deepcopy(active_session.state.get("learning_coach", {})),
            )
            active_session.state["learning_coach"] = {**state, "last_response": output}
            return AgentResponse(messages=[Message("assistant", [Content.from_text(learner_text(output))])],
                                 agent_id=self.id)

        if not stream:
            return execute()

        async def updates():
            response = await execute()
            yield AgentResponseUpdate(role="assistant", contents=[Content.from_text(response.text)],
                                      agent_id=self.id, message_id=str(uuid4()))

        return ResponseStream(updates(), finalizer=AgentResponse.from_updates)
