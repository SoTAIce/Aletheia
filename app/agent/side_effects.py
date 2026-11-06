"""Watch one step's tool calls for side effects whose outcome is unknown (DECISIONS D-032).

AGENTS.md forbids automatic retries when an external side effect is uncertain. The runtime
wraps the registry it hands to runners; before each step it resets the watch, and after a
timeout or a raised tool call it asks whether a side-effecting tool may have run.
"""

from app.agent.step_models import ToolCall, ToolResult
from app.agent.team.models import WORKER_ONLY_TOOLS
from app.tools.tool_registry import ToolRegistry

# Tools that change the workspace or run code there. run_python_code mounts it read-only.
SIDE_EFFECT_TOOLS = WORKER_ONLY_TOOLS      # write_file, run_tests, run_python


class SideEffectWatch(ToolRegistry):
    """The same tools as the wrapped registry, plus two flags for the current step.

    touched: a side-effecting tool was started in this step.
    uncertain: a side-effecting call raised or was cancelled, so its effect is unknown.
    """

    def __init__(self, inner: ToolRegistry) -> None:
        super().__init__()
        self.inner = inner
        self.touched = self.uncertain = False

    def reset(self) -> None:
        self.touched = self.uncertain = False

    def register(self, tool) -> None:
        raise TypeError('register tools on the wrapped registry')

    def list_definitions(self):
        return self.inner.list_definitions()

    def get(self, name: str):
        return self.inner.get(name)

    async def invoke(self, call: ToolCall) -> ToolResult:
        side = isinstance(call, ToolCall) and call.tool_name in SIDE_EFFECT_TOOLS
        if side:
            self.touched = True
        try:
            return await self.inner.invoke(call)
        except BaseException:
            if side:
                self.uncertain = True
            raise
