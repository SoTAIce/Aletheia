"""Local time tool for ToolRegistry, without model or network dependencies."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.agent.step_models import ToolDefinition, ToolOutput


class CurrentTimeTool:
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="get_current_time",
            description="Get the current time in an IANA timezone, including its UTC offset.",
            input_schema={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone, for example Asia/Shanghai or UTC.",
                        "default": "Asia/Shanghai",
                        "minLength": 1,
                    },
                },
                "additionalProperties": False,
            },
        )

    async def invoke(self, arguments: dict[str, Any]) -> ToolOutput:
        """Return expected argument errors as business failures."""
        if not isinstance(arguments, dict):
            return ToolOutput(False, "", error="arguments must be an object")
        if any(key != "timezone" for key in arguments):
            return ToolOutput(False, "", error="only the timezone argument is supported")
        timezone = arguments.get("timezone", "Asia/Shanghai")
        if not isinstance(timezone, str) or not timezone.strip():
            return ToolOutput(False, "", error="timezone must be a non-empty string")
        timezone = timezone.strip()
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ToolOutput(False, "", error=f"Unknown or unavailable timezone: {timezone}")
        now = datetime.now(zone)
        return ToolOutput(
            success=True,
            content=f"{now.isoformat(timespec='seconds')} [{timezone}]",
        )
