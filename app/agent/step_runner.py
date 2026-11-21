"""Runner contract independent of the execution coordinator."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.tool_calling_model import ToolCallingModel
    from app.services.context_manager import ContextManager
    from app.tools.tool_registry import ToolRegistry

from app.agent.step_models import StepExecutionContext, StepRunOutput


class StepRunner(Protocol):
    runner_id: str

    async def run(self, context: StepExecutionContext) -> StepRunOutput:
        ...

class StepRunError(RuntimeError):
    """The current step could not be completed."""

    def __init__(
        self,
        message: str,
        source_refs: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)

        self.message = message
        self.source_refs = source_refs


class ToolRoundLimitError(StepRunError):
    """The tool-calling loop exceeded its allowed number of rounds."""

    pass


class StepBlockedError(StepRunError):
    """The step needs missing information before dependent work can proceed."""


class StepBudgetError(StepRunError):
    """The run's token or time budget was spent before the next model call (stage 4d-3).
    The message is the meter's host-authored reason; LoopRun ends the run with kind 'budget'."""


class StepFormatError(StepRunError):
    """Output formatting/validation failed; replanning cannot repair serialization."""


class StepContractError(StepFormatError):
    """Final answer failed the output schema contract after repair."""


class StepToolError(StepRunError):
    """A tool raised before producing a usable result."""


class StepBusinessError(StepRunError):
    """The model explicitly reported an unsuccessful business outcome."""


class StepModelError(StepRunError):
    """The provider request or response failed; not a new business plan."""


class StepTimeoutError(StepModelError):
    """A provider request timed out; tool side effects may already exist."""


class StepRunnerContractError(TypeError):
    """Runner data violates its interface contract, not a declared step failure."""


@dataclass(frozen=True, slots=True)
class RoleModel:
    """A member role's own model (stage 4d-2) and, when its window differs, its own context
    manager; None means the main loop's."""
    model: ToolCallingModel
    context_manager: ContextManager | None = None


@dataclass(frozen=True, slots=True)
class RunnerDependencies:
    model: ToolCallingModel
    tool_registry: ToolRegistry
    context_manager: ContextManager
    # Team members and explore members; None: they use the main model and context (stage 4d-2).
    member: RoleModel | None = None
    explorer: RoleModel | None = None


RunnerFactory = Callable[[RunnerDependencies], StepRunner]
