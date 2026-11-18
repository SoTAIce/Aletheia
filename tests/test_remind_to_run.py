"""D-087: with the sandbox, the single loop is reminded once to run the tests when it answers after
changing files without a passing run; the second answer finishes and is marked unverified."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.agent_loop import LOOP_STEP, REMIND_TO_RUN, AgentLoopRunner, LoopOptions
from app.agent.loop_run import LoopRun
from app.agent.run_meter import RunMeter, metering
from app.agent.runtime import UNVERIFIED_NOTICE
from app.agent.step_models import ModelTurn, SystemMessage, ToolCall, ToolDefinition, ToolOutput
from app.models.run_state import RunMemory, RunState
from app.models.taskstate import TaskStatus
from app.tools.tool_registry import ToolRegistry

PASS, FAIL = ToolOutput(True, 'exit code 0; 1s\n1 passed'), ToolOutput(True, 'exit code 1; 1s\n1 failed')


def call(call_id, name):
    return ModelTurn(tool_calls=(ToolCall(call_id, name, {'path': 'a.py'}),))


class Scripted:
    def __init__(self, *turns):
        self.turns, self.requests = list(turns), []

    async def generate(self, messages, tools):
        self.requests.append(list(messages))
        return self.turns.pop(0)


def tool(name, *outputs):
    return SimpleNamespace(definition=ToolDefinition(name, name, {'type': 'object'}),
                           invoke=AsyncMock(side_effect=list(outputs)))


def loop_run(model, *tools, remind=True):
    registry = ToolRegistry()
    for item in tools:
        registry.register(item)
    options = LoopOptions(remind_to_run=remind)
    return LoopRun(RunMemory(RunState('user', 'fix the bug')), model, registry,
                   runner_factory=lambda deps: AgentLoopRunner(deps.model, deps.tool_registry, options),
                   step_description=LOOP_STEP)


def reminders(model):
    """Reminders in the last request: the history carries every one sent so far."""
    return [m for m in model.requests[-1] if isinstance(m, SystemMessage) and m.content == REMIND_TO_RUN]


async def test_an_answer_after_an_unrun_edit_is_reminded_once_then_marked_unverified():
    model = Scripted(call('e', 'edit_file'), ModelTurn('done'), ModelTurn('done, not run'))
    with metering(RunMeter()):
        result = await loop_run(model, tool('edit_file', ToolOutput(True, 'Edited')),
                                tool('run_tests', PASS)).run()
    assert result.status is TaskStatus.COMPLETED and result.content == 'done, not run'
    assert len(model.requests) == 3 and model.requests[2][-1].content == REMIND_TO_RUN
    assert model.requests[2][-2].content == 'done'        # the first answer stays in the history
    assert result.step_log[-1]['unverified'] is True and result.usage['unverified_changes'] is True


async def test_running_the_tests_after_the_reminder_verifies_the_answer():
    model = Scripted(call('w', 'write_file'), ModelTurn('done'), call('t', 'run_tests'), ModelTurn('tested'))
    with metering(RunMeter()):
        result = await loop_run(model, tool('write_file', ToolOutput(True, 'written')),
                                tool('run_tests', PASS)).run()
    assert result.content == 'tested' and len(reminders(model)) == 1
    assert 'unverified' not in result.step_log[-1] and 'unverified_changes' not in result.usage


@pytest.mark.parametrize('turns, outputs', [
    # a passing run after the last write: no reminder
    ([call('w', 'write_file'), call('t', 'run_tests'), ModelTurn('done')], ['written', PASS]),
    # the write failed, so nothing changed
    ([call('w', 'write_file'), ModelTurn('done')], ['failed']),
    # reads only
    ([ModelTurn('done')], []),
])
async def test_no_reminder_without_an_unrun_change(turns, outputs):
    write = tool('write_file', *[ToolOutput(False, '', error='refused') if o == 'failed'
                                 else ToolOutput(True, o) for o in outputs if o != PASS])
    run = tool('run_tests', *[o for o in outputs if o == PASS])
    model = Scripted(*turns)
    result = await loop_run(model, write, run).run()
    assert result.status is TaskStatus.COMPLETED and reminders(model) == []
    assert 'unverified' not in result.step_log[-1]


async def test_a_failing_run_after_the_write_still_gets_the_reminder():
    model = Scripted(call('w', 'write_file'), call('t', 'run_tests'), ModelTurn('done'), ModelTurn('again'))
    result = await loop_run(model, tool('write_file', ToolOutput(True, 'written')), tool('run_tests', FAIL)).run()
    assert len(reminders(model)) == 1 and result.step_log[-1]['unverified'] is True


async def test_off_by_default():
    model = Scripted(call('w', 'write_file'), ModelTurn('done'))
    result = await loop_run(model, tool('write_file', ToolOutput(True, 'written')), remind=False).run()
    assert result.content == 'done' and reminders(model) == [] and 'unverified' not in result.step_log[-1]


def test_the_option_must_be_a_bool():
    with pytest.raises(ValueError, match='remind_to_run'):
        LoopOptions(remind_to_run=1)


# ---- who turns it on ----

def _registry(*names):
    registry = ToolRegistry()
    for name in names:
        registry.register(tool(name))
    return registry


@pytest.mark.parametrize('names, read_only, setting, on', [
    (('read_file', 'write_file', 'run_tests'), False, True, True),
    (('read_file', 'edit_file', 'run_python'), False, True, True),
    (('read_file', 'write_file'), False, True, False),          # no sandbox
    (('read_file', 'run_python_code'), False, True, False),     # read-only runs only
    (('read_file', 'write_file', 'run_tests'), True, True, False),
    (('read_file', 'write_file', 'run_tests'), False, False, False),
])
def test_the_assembler_turns_it_on_for_writes_with_the_sandbox(monkeypatch, names, read_only, setting, on):
    from app.config import config
    from app.services.run_assembler import _with_reminder
    monkeypatch.setattr(config, 'agent_remind_to_run', setting)
    assert _with_reminder(LoopOptions(), _registry(*names), read_only).remind_to_run is on


def test_the_setting_defaults_to_on(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv('AGENT_REMIND_TO_RUN', raising=False)
    assert Settings(_env_file=None).agent_remind_to_run is True


def test_the_command_line_explains_an_unverified_answer():
    from app.agent.__main__ import report
    lines = []
    result = SimpleNamespace(content='done', user_message=None, status=TaskStatus.COMPLETED, execution_count=1,
                             replan_count=0, usage={'unverified_changes': True})
    report(result, lines.append)
    assert f'\n{UNVERIFIED_NOTICE}' in lines
    lines.clear()
    report(SimpleNamespace(**{**vars(result), 'usage': {}}), lines.append)
    assert f'\n{UNVERIFIED_NOTICE}' not in lines


def test_the_web_page_shows_the_same_text():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / 'app' / 'api' / 'static' / 'app.js').read_text(encoding='utf-8')
    assert UNVERIFIED_NOTICE in script and 'usage.unverified_changes === true' in script


def test_the_bench_row_carries_the_flag():
    from bench.run_bench import usage_fields
    assert usage_fields({'steps': [{'tools': [], 'unverified': True}]})['unverified'] is True
    assert 'unverified' not in usage_fields({'steps': [{'tools': []}]})
