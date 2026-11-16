"""Stage 5b: every limit in one table; each enforced by one layer (docs/stage5_plan.md)."""

from app.agent.agent_loop import LoopOptions
from app.agent.delegation import DelegationOptions
from app.agent.run_limits import RunLimits
from app.config import Settings


def _settings(**kw):
    return Settings(_env_file=None, dashscope_api_key='k', agent_model_base_url='', agent_model='', **kw)


def test_the_limits_come_from_the_settings_and_the_options():
    limits = RunLimits.current(team=DelegationOptions(timeout_seconds=200), max_executions=3,
                               max_seconds=60.0, settings=_settings(agent_task_timeout_seconds=300,
                                                                     rag_model_timeout_seconds=90))
    assert limits.loop.timeout_seconds == 300 and limits.model_request_seconds == 90
    assert limits.deadlines() == {'single': 300.0, 'team': 200.0}
    rows = {name: value for _, name, value in limits.rows()}
    assert rows['单一循环任务时限（AGENT_TASK_TIMEOUT_SECONDS）'] == '300 秒'
    assert rows['每个成员的时限'] == '120 秒' and rows['时间预算（--max-seconds）'] == '60 秒'
    assert rows['一个任务最多执行次数（含阻塞后续跑）'] == '3' and rows['令牌预算（--max-tokens）'] == '不限'
    assert RunLimits.current(settings=_settings()).deadlines() == {'single': 480.0}


def test_the_old_step_timeout_setting_is_no_longer_read():
    s = _settings(AGENT_STEP_TIMEOUT_SECONDS=1)        # extra settings are ignored
    assert not hasattr(s, 'agent_step_timeout_seconds') and s.agent_task_timeout_seconds == 480


def test_a_sandbox_call_must_fit_inside_every_deadline():
    limits = RunLimits.current(loop=LoopOptions(timeout_seconds=100), team=DelegationOptions(timeout_seconds=90),
                               settings=_settings())
    assert limits.sandbox_problem(80) is None
    problem = limits.sandbox_problem(90)
    assert 'team 90s' in problem and 'single' not in problem


def test_the_command_line_prints_the_table_without_a_model_call(capsys, monkeypatch):
    import asyncio
    from app.agent import __main__ as cli
    import app.services.rag_agent_service as service
    monkeypatch.setattr(service, 'RagAgentService', None)      # building a service would fail loudly
    args = cli.parse_args(['--show-limits', '--execution-mode', 'team', '--team-timeout', '200'])
    assert asyncio.run(cli.main(args)) == 0
    out = capsys.readouterr().out
    assert '等待人工确认的时间不计入任何时限' in out and '团队任务时限（--team-timeout）\t200 秒' in out
