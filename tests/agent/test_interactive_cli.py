"""The interactive CLI keeps one session open and answers blocked tasks in place (D-040). No model calls."""

from types import SimpleNamespace

import pytest

import app.agent.__main__ as cli
from app.agent.run_meter import current_meter
from app.agent.runtime import AgentRunResult
from app.models.taskstate import TaskStatus


def result(task_id, status, content, usage=None):
    return AgentRunResult(task_id, status, content, (), 1, 0, usage=usage)


class FakeService:
    """Records each request and replies from a script; a reply may be an exception to raise."""

    def __init__(self, replies):
        self.replies, self.calls, self.meters = list(replies), [], []

    async def query_result(self, question, session_id, *, resume_task_id=None, **_):
        self.calls.append((question, session_id, resume_task_id))
        self.meters.append(current_meter())
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def lines(*inputs):
    queue = list(inputs)

    async def read_line(prompt):
        lines.prompts.append(prompt)
        if not queue:
            raise EOFError
        return queue.pop(0)
    lines.prompts = []
    return read_line


def args(**overrides):
    values = dict(goal=None, session_id='cli', max_tokens=None, max_seconds=None, interactive=True)
    return SimpleNamespace(**(values | overrides))


async def test_a_blocked_task_is_answered_by_the_next_line():
    service = FakeService([
        result('t1', TaskStatus.BLOCKED, 'Which format?'),
        result('t1', TaskStatus.COMPLETED, 'done', usage={'model_calls': 3, 'total_tokens': 120, 'seconds': 1.5}),
        result('t2', TaskStatus.COMPLETED, 'second'),
    ])
    out = []
    code = await cli.interactive(service, args(), lines('write a report', 'Markdown', 'another goal'), out.append)
    assert code == 0
    assert service.calls == [('write a report', 'cli', None), ('Markdown', 'cli', 't1'), ('another goal', 'cli', None)]
    assert lines.prompts == ['> ', '回答> ', '> ', '> ']
    text = '\n'.join(out)
    assert 'Which format?' in text and '任务在等待你的回答' in text
    assert '\nStatus: completed; executions: 1; replans: 0' in text
    assert '用量：模型调用 3 次，令牌 120，耗时 1.5 秒' in text


async def test_commands_and_blank_lines_never_reach_the_service():
    service = FakeService([result('t1', TaskStatus.BLOCKED, 'Which format?'),
                           result('t2', TaskStatus.COMPLETED, 'fresh')])
    out = []
    code = await cli.interactive(service, args(), lines(
        'goal', '   ', '/help', '/what', '/new', '/new', 'fresh goal', '/exit', 'never read'), out.append)
    assert code == 0
    assert service.calls == [('goal', 'cli', None), ('fresh goal', 'cli', None)]   # /new dropped the wait
    text = '\n'.join(out)
    assert '未知命令：/what' in text
    assert '已放弃正在等待回答的任务。' in text and '当前没有等待回答的任务。' in text
    assert text.count(cli.INTERACTIVE_HELP) == 3   # banner, /help and the unknown command


async def test_a_refused_request_reports_only_the_type_and_keeps_the_session():
    service = FakeService([result('t1', TaskStatus.BLOCKED, 'Q?'),
                           ValueError('SECRET detail'),
                           result('t2', TaskStatus.COMPLETED, 'ok')])
    out = []
    await cli.interactive(service, args(), lines('goal', 'answer', 'next'), out.append)
    assert service.calls[2] == ('next', 'cli', None)   # the failed resume cleared the wait
    text = '\n'.join(out)
    assert '请求没有执行：ValueError' in text and 'SECRET' not in text


async def test_the_command_line_goal_is_the_first_turn_and_each_turn_has_its_own_budget():
    service = FakeService([result('t1', TaskStatus.COMPLETED, 'a'), result('t2', TaskStatus.FAILED, 'b')])
    await cli.interactive(service, args(goal='first', max_tokens=500, max_seconds=60.0), lines('second'), [].append)
    assert [c[0] for c in service.calls] == ['first', 'second']
    first, second = service.meters
    assert first is not second
    assert first.max_total_tokens == second.max_total_tokens == 500 and first.timeout_seconds == 60.0


def test_parser_selects_the_interactive_mode():
    assert cli.parse_args([]).goal is None
    assert cli.parse_args(['-i', 'goal']).interactive is True
    assert cli.parse_args(['goal']).interactive is False
    with pytest.raises(SystemExit):
        cli.parse_args(['--result-json', 'out.json'])
    with pytest.raises(SystemExit):
        cli.parse_args(['-i', 'goal', '--result-json', 'out.json'])


async def test_main_opens_one_service_for_the_whole_interactive_session(monkeypatch):
    opened = []

    class Service(FakeService):
        def __init__(self, **kwargs):
            super().__init__([result('t1', TaskStatus.COMPLETED, 'a'), result('t2', TaskStatus.COMPLETED, 'b')])
            opened.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            self.closed = True

    monkeypatch.setattr(cli, 'RagAgentService', Service)
    monkeypatch.setattr(cli, 'local_mcp_servers', lambda **kwargs: ())
    monkeypatch.setattr(cli, '_read_line', lines('one', 'two'))
    parsed = cli.parse_args(['--no-knowledge', '--session-id', 'work'])
    assert await cli.main(parsed) == 0
    assert len(opened) == 1 and opened[0].closed
    assert opened[0].calls == [('one', 'work', None), ('two', 'work', None)]
