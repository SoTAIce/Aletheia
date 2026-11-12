from unittest.mock import AsyncMock
import pytest
from app.agent.step_models import ModelTurn
from app.services.rag_agent_service import RagAgentService


class _Requests:
    """Stands where the planner mock was: the text of the loop's last model request."""

    def __init__(self, model):
        self.model = model

    @property
    def text(self):
        return ' | '.join(m.content or '' for m in self.model.generate.call_args.args[0])


def service(root):
    model = AsyncMock()
    model.generate.return_value = ModelTurn('Archived evidence')
    return RagAgentService(model=model, memory_directory=root,
                           mcp_servers=(), include_knowledge=False), _Requests(model)


async def test_service_restart_restores_same_session_and_isolates_other(tmp_path):
    first, _ = service(tmp_path)
    await first.query('Original request', 'one')
    second, planner = service(tmp_path)
    await second.query('Follow-up', 'one')
    assert 'Archived evidence' in planner.text
    third, planner = service(tmp_path)
    await third.query('Different session', 'two')
    assert 'Archived evidence' not in planner.text
    assert len(second._conversations['one']) == 2

async def test_old_history_is_discoverable_and_readable_after_service_restart(tmp_path):
    from app.services.conversation_archive import ConversationArchive
    from app.models.conversation_memory import ConversationMemory,ConversationTurn,ConversationMessage,MessageRole
    archive=ConversationArchive(tmp_path,'one')
    memory=ConversationMemory('one',max_turns=1,archive=archive)
    original=ConversationTurn([ConversationMessage(MessageRole.USER,'UniqueAlpha original detail')])
    await memory.add(original)
    await memory.add(ConversationTurn([ConversationMessage(MessageRole.USER,'latest unrelated')]))
    summary=archive.save_summary([original.turn_id],'UniqueAlpha detailed stage summary','UniqueAlpha','important decision')
    restored,planner=service(tmp_path)
    await restored.query('Recall UniqueAlpha','one')
    prompt=planner.text
    assert summary.storage_ref in prompt
    assert 'turn:'+original.turn_id in prompt
