"""A blocked question reaches the user as asked and the answer reaches the resumed loop (D-043).

The plan-mode step prompt tests went with ToolCallingStepRunner (stage 4c); the loop's own ask_user
contract is in tests/test_agent_loop.py."""

import json
from unittest.mock import AsyncMock

from app.agent.step_models import ModelTurn
from app.services.rag_agent_service import RagAgentService


async def test_the_blocked_reason_reaches_the_user_and_the_answer_reaches_the_resumed_step(tmp_path):
    from loop_fakes import loop_reply as outcome
    from app.agent.step_models import ToolMessage
    model = AsyncMock()
    model.generate.side_effect = [outcome('need the sentence', 'blocked', '要翻译哪一句？'), outcome('Hello, world.')]
    async with RagAgentService(model=model, mcp_servers=(), include_knowledge=False,
                               memory_directory=tmp_path) as service:
        blocked = await service.query_result('翻译一句话，先问我是哪一句', 's')
        assert blocked.content == '要翻译哪一句？'
        done = await service.query_result('你好，世界。', 's', resume_task_id=blocked.task_id)
    assert done.content == 'Hello, world.'
    # The loop resumes its own messages: the answer arrives as the reply to its ask_user call.
    resumed = model.generate.call_args_list[-1].args[0]
    [answer] = [m for m in resumed if isinstance(m, ToolMessage) and m.call_id == 'ask']
    assert '你好，世界。' in answer.content
