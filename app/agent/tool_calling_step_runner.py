"""Future tool-calling runner; execution is intentionally not implemented."""

from app.agent.step_models import StepExecutionContext, StepRunOutput


class ToolCallingStepRunner:
    async def run(self, context: StepExecutionContext) -> StepRunOutput:
        raise NotImplementedError("Tool-calling execution is not implemented yet")
