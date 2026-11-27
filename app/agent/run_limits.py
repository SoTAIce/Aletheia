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
    summary_model: str | None = None
    blocked_task_ttl_seconds: float = 86400.0
    web_run_max_tokens: int | None = None
    web_run_max_seconds: float | None = None
    summary_profile: str | None = None
    summary_context_tokens: int | None = None
    accepted_models: tuple[str, ...] = ()
    on_model_mismatch: str = 'warn'

    @classmethod
    def current(cls, *, loop=None, team=None, explore=None, max_executions=16,
                max_total_tokens=None, max_seconds=None, settings=None) -> 'RunLimits':
        from app.agent.agent_loop import LoopOptions
        from app.agent.delegation import ExploreOptions
        from app.agent.hooks import GATE_TIMEOUT_SECONDS
        from app.agent.route_selection import DEFAULT_TIMEOUT_SECONDS
        from app.models.model_profile import ModelProfileError
        if settings is None:
            from app.config import config as settings
        try:
            summarizer = settings.model_profiles().summarizer_profile()
        except ModelProfileError:
            summarizer = None
        summary_profile = summarizer.name if summarizer is not None else None
        summary_model = (summarizer.model if summarizer is not None
                         else settings.summary_model.strip() or 'qwen3:4b-instruct')
        summary_context_tokens = (summarizer.context_tokens if summarizer is not None
                                  else settings.summary_model_context_tokens)
        default_profile = settings.agent_model_profile
        return cls(loop or LoopOptions(timeout_seconds=settings.agent_task_timeout_seconds), team,
                   explore or ExploreOptions(), max_executions, default_profile.timeout_seconds,
                   DEFAULT_TIMEOUT_SECONDS, GATE_TIMEOUT_SECONDS, max_total_tokens, max_seconds,
                   summary_model,
                   settings.blocked_task_ttl_seconds,
                   settings.web_run_max_tokens, settings.web_run_max_seconds,
                   summary_profile, summary_context_tokens,
                   default_profile.accepted_models, default_profile.on_model_mismatch)

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
        def hours_or_seconds(value: float) -> str:
            if value >= 3600 and value % 3600 == 0:
                return f'{int(value // 3600)} 小时'
            return seconds(value)
        loop, explore = self.loop, self.explore
        from app.services.project_instructions import MAX_INSTRUCTION_CHARS
        rows = [
            ('任务', '单一循环任务时限（AGENT_TASK_TIMEOUT_SECONDS）', seconds(loop.timeout_seconds)),
            ('任务', '单一循环工具轮数 / 调用次数', f'{loop.max_tool_rounds} / {loop.max_tool_calls}'),
            ('任务', '原地打转：连续几轮后提醒，再几轮后收尾', stall_text(loop)),
            ('任务', '一个任务最多执行次数（含阻塞后续跑）', str(self.max_executions)),
            ('任务', '阻塞任务保留时间（BLOCKED_TASK_TTL_SECONDS）', hours_or_seconds(self.blocked_task_ttl_seconds)),
            ('任务', '令牌预算（--max-tokens）', '不限' if self.max_total_tokens is None else str(self.max_total_tokens)),
            ('任务', '时间预算（--max-seconds）', seconds(self.max_seconds)),
            ('任务', '项目说明文件上限', f'{MAX_INSTRUCTION_CHARS} 字符'),
        ]
        if self.team is not None:
            team = self.team
            rows += [
                ('团队', '团队任务时限（--team-timeout）', seconds(team.timeout_seconds)),
                ('团队', '每个成员的时限', seconds(team.member_timeout_seconds)),
                ('团队', '委派次数 / 负责人轮数', f'{team.max_delegations} / {team.max_lead_turns}'),
                ('团队', '成员工具轮数 / 调用次数', f'{team.worker_tool_rounds} / {team.worker_tool_calls}'),
            ]
        import inspect
        from app.agent.batch import DEFAULT_ITEM_MAX_SECONDS, DEFAULT_ITEM_MAX_TOKENS
        from app.agent.tool_loading import DEFER_THRESHOLD, MAX_LOADED_TOOLS, MAX_VOLATILE_CHARS
        from app.services.memory_compression.llm_compressor import DEFAULT_MAX_OUTPUT_TOKENS
        from app.services.summary_scheduler import SummaryScheduler
        summary_params = inspect.signature(SummaryScheduler.__init__).parameters
        summary_timeout = summary_params['timeout_seconds'].default
        summary_attempts = summary_params['max_attempts'].default
        summary_cooldown = summary_params['cooldown_seconds'].default
        summary_model = self.summary_model
        if summary_model is None:
            from app.config import config
            summary_model = config.summary_model.strip() or 'qwen3:4b-instruct'
        summary_label = (f'档案 {self.summary_profile}，模型 {summary_model}'
                         if self.summary_profile is not None else f'本机 {summary_model}')
        summary_context = self.summary_context_tokens
        if summary_context is None:
            from app.config import config
            summary_context = config.summary_model_context_tokens
        summary_output_cap = min(DEFAULT_MAX_OUTPUT_TOKENS, summary_context // 4)
        rows += [
            ('调研成员', '每个成员的时限（--delegate）', seconds(explore.member_timeout_seconds)),
            ('调研成员', '委派次数 / 每个成员的调用次数', f'{explore.max_delegations} / {explore.member_tool_calls}'),
            ('单次调用', '模型请求时限', seconds(self.model_request_seconds)),
            ('单次调用', '模型调用失败后的重试', retry_policy()),
            ('单次调用', '认可的替换模型', ', '.join(self.accepted_models) if self.accepted_models else '无'),
            ('单次调用', '换模型时', '停止' if self.on_model_mismatch == 'stop' else '提示'),
            ('单次调用', '选择执行模式的调用时限', seconds(self.route_seconds)),
            ('单次调用', '每个门控的时限', seconds(self.gate_seconds)),
            ('批处理', '每项默认令牌预算（--item-max-tokens）', f'{DEFAULT_ITEM_MAX_TOKENS:,}'),
            ('批处理', '每项默认时间预算（--item-max-seconds）', seconds(DEFAULT_ITEM_MAX_SECONDS)),
            ('工具加载', '用户工具延迟加载阈值', f'超过 {DEFER_THRESHOLD} 个时启用'),
            ('工具加载', '每个任务最多加载的延迟工具', f'{MAX_LOADED_TOOLS} 个'),
            ('工具加载', '波动区定义总长度上限', f'{MAX_VOLATILE_CHARS:,} 字符'),
            ('后台摘要', '历史压缩模型（--compress-history）',
             f'{summary_label}，单次 {summary_timeout:g} 秒，最多试 {summary_attempts} 次'),
            ('后台摘要', '摘要模型窗口（SUMMARY_MODEL_CONTEXT_TOKENS 或档案）', f'{summary_context} 令牌'),
            ('后台摘要', '单次摘要输出上限', f'{summary_output_cap} 令牌'),
            ('后台摘要', '摘要失败后的冷却', f'{summary_cooldown:g} 秒'),
            ('网页', '每次运行的令牌上限（WEB_RUN_MAX_TOKENS）',
             '不限' if self.web_run_max_tokens is None else str(self.web_run_max_tokens)),
            ('网页', '每次运行的时限（WEB_RUN_MAX_SECONDS）', seconds(self.web_run_max_seconds)),
        ]
        return rows

    def table(self) -> str:
        lines = ['等待人工确认的时间不计入任何时限。']
        lines += [f'{layer}\t{name}\t{value}' for layer, name, value in self.rows()]
        return '\n'.join(lines)


def stall_text(loop) -> str:
    """The loop's going-round-in-circles rule (D-092); members of a team and --delegate use the same."""
    rounds = getattr(loop, 'stall_rounds', 0)
    if not rounds:
        return '关闭'
    after = f'再 {rounds} 轮后提前收尾' if getattr(loop, 'stall_wrap_up', False) else '不提前收尾'
    return f'{rounds} 轮后提醒一次，{after}'


def retry_policy() -> str:
    """The model call retry policy (D-085), from the adapter's own constants."""
    from app.agent.openai_compatible_tool_calling_model import (MAX_RETRIES, MAX_RETRY_AFTER_SECONDS,
                                                                MAX_RETRY_WAIT_SECONDS, RETRY_BASE_SECONDS)
    waits = '/'.join(f'{RETRY_BASE_SECONDS * 2 ** i:g}' for i in range(MAX_RETRIES))
    return (f'最多 {MAX_RETRIES} 次，等 {waits} 秒（带抖动，每次最多 {MAX_RETRY_WAIT_SECONDS:g} 秒），'
            f'遵守 Retry-After（最多 {MAX_RETRY_AFTER_SECONDS:g} 秒）')
