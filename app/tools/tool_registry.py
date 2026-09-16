"""In-memory tool registration, lookup, and validated invocation."""


from app.agent.step_models import ToolDefinition, ToolCall, ToolResult, ToolOutput
from app.tools.tool import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a valid tool once; its definition name must remain stable."""
        definition = getattr(tool, "definition", None)
        if not isinstance(definition, ToolDefinition):
            raise TypeError("tool.definition must be ToolDefinition")
        if not callable(getattr(tool, "invoke", None)):
            raise TypeError("tool.invoke must be callable")

        name = definition.name
        if not isinstance(name, str) or not name.strip():
            raise ValueError("tool name must be a non-empty string")
        if name != name.strip():
            raise ValueError("tool name must not contain surrounding whitespace")

        description = definition.description
        if not isinstance(description, str) or not description.strip():
            raise ValueError("tool description must be a non-empty string")
        if not isinstance(definition.input_schema, dict):
            raise TypeError("tool input_schema must be a dict")

        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def list_definitions(self) -> tuple[ToolDefinition, ...]:
        """Return tool definitions in registration order."""
        definitions = []
        for name in self._tools:
            definitions.append(self.get(name).definition)
        return tuple(definitions)

    def definition(self) -> tuple[ToolDefinition, ...]:
        """Compatibility alias for list_definitions()."""
        return self.list_definitions()

    @staticmethod
    def _normalize_name(value: str, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        if not value.strip():
            raise ValueError(f"{field} must not be empty")
        return value.strip()

    def get(self, name: str) -> Tool:
        """Return a registered tool; reject names changed after registration."""
        name = self._normalize_name(name, "tool name")
        try:
            tool = self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None
        definition = getattr(tool, "definition", None)
        if not isinstance(definition, ToolDefinition) or definition.name != name:
            raise ValueError(f"registered tool definition changed: {name}")
        return tool

    async def invoke(self, call: ToolCall) -> ToolResult:
        """Dispatch one call and attach identity to validated business output.

        Tool exceptions and cancellation propagate unchanged. This layer checks
        data shape, not JSON Schema semantics or WorkingMemory resource ownership.
        Raw content and argument values retain their whitespace.
        """
        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        call_id = self._normalize_name(call.call_id, "call_id")
        tool_name = self._normalize_name(call.tool_name, "tool_name")
        if not isinstance(call.arguments, dict) or not all(
            isinstance(key, str) for key in call.arguments
        ):
            raise TypeError("call.arguments must be a dict with string keys")
        tool = self.get(tool_name)
        output = await tool.invoke(call.arguments)

        if not isinstance(output, ToolOutput):
            raise TypeError("tool.invoke() must return ToolOutput")
        if not isinstance(output.success, bool):
            raise TypeError("output.success must be a bool")
        if not isinstance(output.content, str):
            raise TypeError("output.content must be a string")
        if not isinstance(output.source_refs, tuple):
            raise TypeError("output.source_refs must be a tuple of resource IDs")
        source_refs = tuple(
            self._normalize_name(ref, f"output.source_refs[{index}]")
            for index, ref in enumerate(output.source_refs)
        )
        error = output.error
        if error is not None:
            if not isinstance(error, str):
                raise TypeError("output.error must be a string or None")
            error = error.strip()
        if output.success and error is not None:
            raise ValueError("successful output must not contain an error")
        if not output.success and not error:
            raise ValueError("failed output must contain a non-empty error")

        return ToolResult(
            call_id=call_id,
            tool_name=tool_name,
            success=output.success,
            content=output.content,
            source_refs=source_refs,
            error=error,
        )
