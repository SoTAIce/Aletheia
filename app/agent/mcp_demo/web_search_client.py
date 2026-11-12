"""Answer one question with web search in an agent loop: python -m app.agent.mcp_demo.web_search_client QUERY."""

import argparse
import asyncio
import os
from pathlib import Path
import sys

from mcp import StdioServerParameters
from openai import AsyncOpenAI

from app.agent.agent_loop import AgentLoopRunner, LOOP_STEP, LoopResumeStore
from app.agent.loop_run import LoopRun
from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.config import config
from app.models.run_state import RunMemory, RunState
from app.tools.mcp_client import MCPClient
from app.tools.mcp_tool_loader import register_mcp_tools
from app.tools.tool_registry import ToolRegistry


async def main(query: str) -> None:
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key.strip():
        raise ValueError("TAVILY_API_KEY must be set to a non-empty value")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.tools.web_search.server"],
        cwd=str(Path(__file__).resolve().parents[3]),
        env={"TAVILY_API_KEY": api_key},
    )
    memory = RunMemory(RunState("user", query))
    registry = ToolRegistry()
    async with AsyncOpenAI(
        api_key=config.dashscope_api_key, base_url=config.dashscope_base_url
    ) as model_client:
        model = OpenAICompatibleToolCallingModel(model_client, config.dashscope_model)
        async with MCPClient.connect(parameters) as client:
            await register_mcp_tools(client, registry)
            # The whole run stays inside the connection: the registry's adapters use the live client.
            run = LoopRun(memory, model, registry, step_description=LOOP_STEP, runner_factory=lambda deps:
                          AgentLoopRunner(deps.model, deps.tool_registry, None, deps.context_manager, LoopResumeStore()))
            result = await run.run()
            print(result.content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Answer a question with Aletheia web search")
    parser.add_argument("query")
    asyncio.run(main(parser.parse_args().query))
