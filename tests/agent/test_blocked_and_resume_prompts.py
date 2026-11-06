"""Step prompts ask blocked questions directly and use resolved answers without tools (D-043)."""

import json
from unittest.mock import AsyncMock

from app.agent.step_executor import StepExecutor
from app.agent.step_models import ModelTurn
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
from app.services.rag_agent_service import RagAgentService
from app.tools.tool_registry import ToolRegistry


def _system(require_step_outcome):
    wm = WorkingMemory(TaskState('user', '翻译一句话'))
    wm.task_state.start_planning()
    wm.install_plan(['翻译'])
    step = wm.task_state.plan[0]
    wm.begin_step(step.step_id)
    context = StepExecutor(wm, AsyncMock()).build_context(step.step_id)
    runner = ToolCallingStepRunner(AsyncMock(), ToolRegistry(), require_step_outcome=require_step_outcome)
    return ' '.join(runner._build_initial_messages(context)[0].content.split())


def test_blocked_reason_is_a_direct_question_shown_as_is():
    system = _system(True)
    assert 'reason is shown to the user as-is' in system
    assert 'one direct question to the user in the language of active_goal' in system
    assert 'explain the blocker' not in system and 'explain the blocker' not in _system(False)


def test_resolved_answers_are_used_without_searching():
    for require in (True, False):
        system = _system(require)
        assert "resolved_questions are the user's answers" in system
        assert 'Never search files, archives or the conversation for an answer that is already in the context' in system


async def test_the_blocked_reason_reaches_the_user_and_the_answer_reaches_the_resumed_step(tmp_path):
    def outcome(text, status='succeeded', reason=None):
        return ModelTurn(json.dumps({'status': status, 'content': text, 'reason': reason}))

    planner, model = AsyncMock(), AsyncMock()
    planner.generate.side_effect = ['{"steps":["Ask which sentence","Translate"],"reason":null}',
                                    '{"steps":["Translate the supplied sentence"],"reason":null}']
    model.generate.side_effect = [outcome('need the sentence', 'blocked', '要翻译哪一句？'), outcome('Hello, world.')]
    async with RagAgentService(model=model, planner_model=planner, mcp_servers=(), include_knowledge=False,
                               memory_directory=tmp_path) as service:
        blocked = await service.query_result('翻译一句话，先问我是哪一句', 's')
        assert blocked.content == '要翻译哪一句？'
        done = await service.query_result('你好，世界。', 's', resume_task_id=blocked.task_id)
    assert done.content == 'Hello, world.'
    context = json.loads(model.generate.call_args_list[-1].args[0][1].content)
    assert [(q['content'], q['answer']) for q in context['resolved_questions']] == [('要翻译哪一句？', '你好，世界。')]
