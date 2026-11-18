"""Task results and failure kinds, shared by LoopRun, the service and the entry points.

Until stage 4c this module also held AgentRuntime, the plan/execute/replan coordinator; plan mode and
that coordinator were removed (D-076) and every task now runs in app/agent/loop_run.py's LoopRun.
"""

from dataclasses import dataclass

from app.agent.step_models import StepExecutionReceipt
from app.models.taskstate import TaskStatus


# Shown with a completed answer whose usage carries unverified_changes (D-087); app.js has the same text.
UNVERIFIED_NOTICE = '注意：这次改了文件，但最后一次修改之后没有通过的运行，结果未经验证。'

# Why a task failed, for callers and for the user-facing message (D-033).
FAILURE_MESSAGES = {
    'limit': '已达到工具调用次数或执行次数的上限，任务停止。',
    'tool': '工具调用反复出错或不可用，任务停止。可以检查工具服务（文件系统、联网搜索、沙箱）是否正常。',
    'context': '这次任务需要的上下文超过了模型窗口，重试也放不下，任务停止。可以缩小任务范围，或者换一个窗口更大的模型档案。',
    'format': '模型多次没有按要求的格式给出步骤结果，任务停止。',
    'model': '调用模型失败（服务出错或请求超时），任务停止，可以稍后重试。',
    'side_effect': '一个会修改文件或运行代码的操作没有正常结束，结果不确定。为避免重复执行，任务已停止，请先检查工作区。',
    'timeout': '执行超时，任务停止。已完成的修改不会重复执行。',
    'rejected': '任务结果没有通过完成检查，任务停止。',
    'business': '任务无法完成。',
    'budget': '已用完本次任务的令牌或时间预算，任务停止。',
    'internal': '运行时内部出错，任务停止。',
    'hooks': '这次运行没有装上钩子，权限确认、令牌上限和审计都无法生效，为安全起见拒绝了模型和工具调用，任务停止。这是程序接线的问题，不是模型的问题。',
    'policy': '一次模型调用被规则拒绝（例如这个对话的令牌用量已达上限），任务停止。',
}


def _int_or_none(value):
    """A trace label takes only a real integer (D-073)."""
    return value if type(value) is int else None


def _context_overflow(exc):
    """Whether an error comes from a request that does not fit the model's window: the host's own
    budget check, or the provider still refusing after the overflow retry (D-085)."""
    from app.agent.tool_calling_model import ModelContextOverflowError
    from app.services.request_fitter import ContextBudgetError
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, (ContextBudgetError, ModelContextOverflowError)):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False


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
