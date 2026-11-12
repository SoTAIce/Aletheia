"""Task results and failure kinds, shared by LoopRun, the service and the entry points.

Until stage 4c this module also held AgentRuntime, the plan/execute/replan coordinator; plan mode and
that coordinator were removed (D-076) and every task now runs in app/agent/loop_run.py's LoopRun.
"""

from dataclasses import dataclass

from app.agent.step_models import StepExecutionReceipt
from app.models.taskstate import TaskStatus


# Why a task failed, for callers and for the user-facing message (D-033).
FAILURE_MESSAGES = {
    'limit': '已达到执行步数或重新规划次数的上限，任务停止。',
    'planning': '模型没有给出可用的计划，任务停止。',
    'format': '模型多次没有按要求的格式给出步骤结果，任务停止。',
    'model': '调用模型失败（服务出错或请求超时），任务停止，可以稍后重试。',
    'side_effect': '一个会修改文件或运行代码的操作没有正常结束，结果不确定。为避免重复执行，任务已停止，请先检查工作区。',
    'timeout': '执行超时，为避免重复已完成的操作，任务已停止。',
    'rejected': '任务结果没有通过完成检查，任务停止。',
    'business': '任务无法完成。',
    'budget': '已用完本次任务的令牌或时间预算，任务停止。',
    'internal': '运行时内部出错，任务停止。',
    'policy': '一次模型调用被规则拒绝（例如这个对话的令牌用量已达上限），任务停止。',
}


def _int_or_none(value):
    """A trace label takes only a real integer (D-073)."""
    return value if type(value) is int else None


def _policy_denial(exc):
    """The gate refusal behind an error, if any (D-060); its reason is host-authored."""
    from app.agent.hooks import PolicyDeniedError
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, PolicyDeniedError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    task_id: str
    status: TaskStatus
    content: str
    steps: tuple[StepExecutionReceipt, ...]
    execution_count: int
    replan_count: int
    failure_kind: str | None = None    # a FAILURE_MESSAGES key when status is FAILED
    usage: dict | None = None          # model calls, tokens and seconds of this run (D-034)
    step_log: tuple[dict, ...] = ()    # host-authored record of every step attempt (D-034)

    @property
    def user_message(self) -> str | None:
        """A Chinese explanation for the user; content keeps the English diagnostic."""
        return FAILURE_MESSAGES.get(self.failure_kind) if self.failure_kind else None
