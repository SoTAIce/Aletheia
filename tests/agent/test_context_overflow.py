import json
import httpx
import pytest
from openai import AsyncOpenAI
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_models import UserMessage
from app.agent.tool_calling_model import ModelContextOverflowError, ModelRequestError


@pytest.mark.parametrize('status,code,expected',[(400,'context_length_exceeded',ModelContextOverflowError),
    (413,'prompt_too_long',ModelContextOverflowError),(400,'invalid_request',ModelRequestError),
    (401,'context_length_exceeded',ModelRequestError)])
async def test_sdk_error_codes_distinguish_overflow_from_other_failures(status,code,expected):
    def handler(request):
        return httpx.Response(status,json={'error':{'code':code,'message':'test'}})
    async with AsyncOpenAI(api_key='test',base_url='https://example.test/v1',max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
        model=OpenAICompatibleToolCallingModel(client,'test')
        with pytest.raises(expected) as caught:
            await model.generate((UserMessage('goal'),),())
        assert type(caught.value) is expected
