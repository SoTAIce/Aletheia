"""Every limit a task runs under, in one place (stage 5b, docs/stage5_plan.md).

Each limit is still enforced by exactly one layer, which reads its own options:

- task deadline: LoopRun, once per execution, from the chosen runner's options (the single loop's
  LoopOptions.timeout_seconds, from AGENT_TASK_TIMEOUT_SECONDS; the team's DelegationOptions.timeout_seconds);
- member limits: the team's and the explorer's delegation, nested inside the task deadline;
- model request: the model profile; route choice and gates: their own constant;
- task budget: RunMeter (--max-tokens, --max-seconds).

Time spent waiting for the user's approval counts against none of the deadlines (D-061).
RunLimits only collects these values so they can be shown and checked together; it holds no key.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunLimits:
    loop: object                 # LoopOptions of the single loop
    team: object | None          # DelegationOptions, or None when no team can run
    explore: object              # ExploreOptions of --delegate members
    max_executions: int
    model_request_seconds: float
    route_seconds: float
    gate_seconds: float
    max_total_tokens: int | None = None
    max_seconds: float | None = None

    @classmethod
    def current(cls, *, loop=None, team=None, explore=None, max_executions=16,
                max_total_tokens=None, max_seconds=None, settings=None) -> 'RunLimits':
        from app.agent.agent_loop import LoopOptions
        from app.agent.delegation import ExploreOptions
        from app.agent.hooks import GATE_TIMEOUT_SECONDS
        from app.agent.route_selection import DEFAULT_TIMEOUT_SECONDS
        if settings is None:
            from app.config import config as settings
        return cls(loop or LoopOptions(timeout_seconds=settings.agent_task_timeout_seconds), team,
                   explore or ExploreOptions(), max_executions, settings.agent_model_profile.timeout_seconds,
                   DEFAULT_TIMEOUT_SECONDS, GATE_TIMEOUT_SECONDS, max_total_tokens, max_seconds)

    def deadlines(self) -> dict[str, float]:
        """The task deadline of each mode that can run."""
        found = {'single': float(self.loop.timeout_seconds)}
        if self.team is not None:
            found['team'] = float(self.team.timeout_seconds)
        return found

    def sandbox_problem(self, host_call_seconds: float) -> str | None:
        """A task deadline shorter than one sandbox call would cut the sandbox's own timeout result
        off (D-033); refused at start-up instead of widening the deadline behind the user's back."""
        short = [f'{mode} {seconds:g}s' for mode, seconds in self.deadlines().items()
                 if seconds <= host_call_seconds]
        if not short:
            return None
        return (f'task deadline ({", ".join(short)}) must be longer than one sandbox call '
                f'({host_call_seconds:g}s); raise AGENT_TASK_TIMEOUT_SECONDS or --team-timeout')

    def rows(self) -> list[tuple[str, str, str]]:
        """(层, 限额, 值) for the --show-limits table; host-authored text only."""
        def seconds(value):
            return '不限' if value is None else f'{value:g} 秒'
        loop, explore = self.loop, self.explore
        rows = [
            ('任务', '单一循环任务时限（AGENT_TASK_TIMEOUT_SECONDS）', seconds(loop.timeout_seconds)),
            ('任务', '单一循环工具轮数 / 调用次数', f'{loop.max_tool_rounds} / {loop.max_tool_calls}'),
            ('任务', '一个任务最多执行次数（含阻塞后续跑）', str(self.max_executions)),
            ('任务', '令牌预算（--max-tokens）', '不限' if self.max_total_tokens is None else str(self.max_total_tokens)),
            ('任务', '时间预算（--max-seconds）', seconds(self.max_seconds)),
        ]
        if self.team is not None:
            team = self.team
            rows += [
                ('团队', '团队任务时限（--team-timeout）', seconds(team.timeout_seconds)),
                ('团队', '每个成员的时限', seconds(team.member_timeout_seconds)),
                ('团队', '委派次数 / 负责人轮数', f'{team.max_delegations} / {team.max_lead_turns}'),
                ('团队', '成员工具轮数 / 调用次数', f'{team.worker_tool_rounds} / {team.worker_tool_calls}'),
            ]
        rows += [
            ('调研成员', '每个成员的时限（--delegate）', seconds(explore.member_timeout_seconds)),
            ('调研成员', '委派次数 / 每个成员的调用次数', f'{explore.max_delegations} / {explore.member_tool_calls}'),
            ('单次调用', '模型请求时限', seconds(self.model_request_seconds)),
            ('单次调用', '模型调用失败后的重试', retry_policy()),
            ('单次调用', '选择执行模式的调用时限', seconds(self.route_seconds)),
            ('单次调用', '每个门控的时限', seconds(self.gate_seconds)),
        ]
        return rows

    def table(self) -> str:
        lines = ['等待人工确认的时间不计入任何时限。']
        lines += [f'{layer}\t{name}\t{value}' for layer, name, value in self.rows()]
        return '\n'.join(lines)


def retry_policy() -> str:
    """The model call retry policy (D-085), from the adapter's own constants."""
    from app.agent.openai_compatible_tool_calling_model import (MAX_RETRIES, MAX_RETRY_AFTER_SECONDS,
                                                                MAX_RETRY_WAIT_SECONDS, RETRY_BASE_SECONDS)
    waits = '/'.join(f'{RETRY_BASE_SECONDS * 2 ** i:g}' for i in range(MAX_RETRIES))
    return (f'最多 {MAX_RETRIES} 次，等 {waits} 秒（带抖动，每次最多 {MAX_RETRY_WAIT_SECONDS:g} 秒），'
            f'遵守 Retry-After（最多 {MAX_RETRY_AFTER_SECONDS:g} 秒）')
