"""Host-owned team primitives shared by Team V2 (V1 was removed by DECISIONS D-025)."""

from app.agent.step_runner import StepModelError


class TeamError(StepModelError):
    """Terminal team/config/protocol failure; never replay a team by replanning."""


# Tools that change files or execute code; reviewers never receive them.
WORKER_ONLY_TOOLS = frozenset({'write_file', 'run_tests', 'run_python'})
RUN_TOOLS = frozenset({'run_tests', 'run_python'})


def has_passing_run_after_last_write(tool_results) -> bool:
    """True when a sandbox run with exit code 0 succeeded after the last successful write_file."""
    last_write = last_pass = -1
    for index, result in enumerate(tool_results):
        if not result.success:
            continue
        if result.tool_name == 'write_file':
            last_write = index
        elif result.tool_name in RUN_TOOLS and result.content.startswith('exit code 0;'):
            last_pass = index
    return last_pass > last_write
