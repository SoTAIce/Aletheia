"""Watch one step's tool calls for side effects whose outcome is unknown (DECISIONS D-032).

AGENTS.md forbids automatic retries when an external side effect is uncertain. The runtime
wraps the registry it hands to runners; before each step it resets the watch, and after a
timeout or a raised tool call it asks whether a side-effecting tool may have run.
It also keeps a host-authored record of the step's calls for the run log (D-034).
"""

import time

from app.agent.step_models import ToolCall, ToolResult
from app.agent.team.models import WORKER_ONLY_TOOLS
from app.tools.tool_registry import ToolRegistry

# Tools that change the workspace or run code there. run_python_code mounts it read-only.
SIDE_EFFECT_TOOLS = WORKER_ONLY_TOOLS      # write_file, run_tests, run_python


class SideEffectWatch(ToolRegistry):
    """The same tools as the wrapped registry, plus two flags for the current step.

    touched: a side-effecting tool was started in this step.
    uncertain: a side-effecting call raised or was cancelled, so its effect is unknown.
    calls: one record per call in this step: tool name, success, seconds and, when it raised,
    the exception type. Never arguments, output or error text.
    """

    def __init__(self, inner: ToolRegistry) -> None:
        super().__init__()
        self.inner = inner
        self.touched = self.uncertain = False
        self.calls: list[dict] = []

    def reset(self) -> None:
        self.touched = self.uncertain = False
        self.calls = []

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
        known = {d.name for d in self.inner.list_definitions()}
        name = call.tool_name if isinstance(call, ToolCall) else None
        record = {'tool': name if name in known else 'unknown', 'success': False}   # never model text
        self.calls.append(record)
        started = time.monotonic()
        try:
            result = await self.inner.invoke(call)
        except BaseException as exc:
            if side:
                self.uncertain = True
            record['error'] = type(exc).__name__
            raise
        finally:
            record['seconds'] = round(time.monotonic() - started, 3)
        record['success'] = bool(getattr(result, 'success', False))
        return result
