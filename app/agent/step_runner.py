"""Runner contract independent of the execution coordinator."""

from typing import Protocol

from app.agent.step_models import StepExecutionContext, StepRunOutput


class StepRunner(Protocol):
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


class StepFormatError(StepRunError):
    """Output formatting/validation failed; replanning cannot repair serialization."""


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
