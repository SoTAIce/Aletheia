"""Reuse the existing tool-calling model adapter for Planner's text contract."""

from app.agent.step_models import SystemMessage, UserMessage
from app.agent.tool_calling_model import ModelResponseError, ToolCallingModel


class PlanningModel:
    def __init__(self, model: ToolCallingModel) -> None:
        self._model = model

    @property
    def context_overflow_managed(self) -> bool:
        return getattr(self._model, "context_overflow_managed", False) is True

    async def generate(self, prompt: str) -> str:
        from app.agent.hooks import model_purpose
        with model_purpose('plan'):                      # labels the call in hook events (D-059)
            turn = await self._model.generate((
                SystemMessage("Follow the planning output contract. Return JSON only; do not execute tools."),
                UserMessage(prompt),
            ), ())
        if turn.tool_calls or not isinstance(turn.content, str) or not turn.content.strip():
            raise ModelResponseError("Planner requires a JSON text response without tool calls")
        return turn.content
