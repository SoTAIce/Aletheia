"""Shared frozen data models for step runners and execution coordination."""

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from app.models.taskstate import (
    Constraint,
    IntermediateResult,
    Observation,
    OpenQuestion,
    PinnedContext,
    PlanStep
)

@dataclass(frozen = True, slots = True)
class StepExecutionContext:
    task_id: str
    active_goal: str

    goal_revision: int
    plan_revision: int

    current_step: PlanStep

    constraints: tuple[Constraint, ...]
    pinned_contexts: tuple[PinnedContext, ...]

    intermediate_results: tuple[IntermediateResult, ...]
    last_observation: Observation | None

    open_question: tuple[OpenQuestion, ...]

    resource_contexts: tuple[str, ...]


@dataclass(frozen = True, slots = True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

@dataclass(frozen = True, slots = True)
class ToolCall:

    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SystemMessage:
    """Application instructions for the model."""

    content: str
    role: Literal["system"] = field(default="system", init=False)


@dataclass(frozen=True, slots=True)
class UserMessage:
    """The task input or additional user context."""

    content: str
    role: Literal["user"] = field(default="user", init=False)


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """Model text and/or tool requests retained in the turn history."""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    role: Literal["assistant"] = field(default="assistant", init=False)


@dataclass(frozen=True, slots=True)
class ToolMessage:
    """A tool reply correlated to an earlier ToolCall by its exact call_id.

    Content is model-visible text prepared by the runner, including failure
    details when appropriate. An empty tool reply is allowed.
    """

    call_id: str
    content: str
    role: Literal["tool"] = field(default="tool", init=False)


ModelMessage: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolMessage


@dataclass(frozen=True, slots=True)
class ModelTurn:
    """One model response, possibly containing text and tool requests."""

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    def to_assistant_message(self) -> AssistantMessage:
        """Preserve this normalized turn as history without provider conversion."""
        return AssistantMessage(content=self.content, tool_calls=self.tool_calls)


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """Business output; the caller attaches call identity to make ToolResult."""

    success: bool
    content: str
    source_refs: tuple[str, ...] = ()
    error: str | None = None

@dataclass(frozen = True, slots = True)
class ToolResult:
    call_id: str
    tool_name: str

    success: bool
    content: str

    source_refs: tuple[str, ...] = ()
    error: str | None = None

@dataclass(frozen = True, slots = True)
class StepRunOutput:
    content: str
    observation: str | None = None
    source_refs: tuple[str, ...] = ()

    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()

@dataclass(frozen = True, slots = True)
class StepExecutionReceipt:
    task_id: str
    step_id: str

    goal_revision: int
    plan_revision: int

    result_id: str
    result_summary: str

    state_version: int

    source_refs: tuple[str, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
