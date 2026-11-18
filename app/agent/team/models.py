"""Host-owned team primitives shared by Team V2 (V1 was removed by DECISIONS D-025)."""

from app.agent.step_runner import StepModelError
from app.agent.tool_effects import effect_of_name, names_with


class TeamError(StepModelError):
    """Terminal team/config/protocol failure; never replay a team by replanning."""


# Built-in tools that change files or execute code; reviewers never receive them. Derived from the
# effects table for older imports; the host decides by effect (D-087).
WORKER_ONLY_TOOLS = names_with(('write', 'run'))     # write_file, edit_file, run_tests, run_python
RUN_TOOLS = names_with(('run',))                    # run_tests, run_python


def has_passing_run_after_last_write(tool_results) -> bool:
    """True when a sandbox run with exit code 0 succeeded after the last successful write (write_file,
    edit_file: any tool whose effect is write, D-087)."""
    last_write = last_pass = -1
    for index, result in enumerate(tool_results):
        if not result.success:
            continue
        effect = effect_of_name(result.tool_name)
        if effect == 'write':
            last_write = index
        elif effect == 'run' and result.content.startswith('exit code 0;'):
            last_pass = index
    return last_pass > last_write
