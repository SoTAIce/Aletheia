"""Run a search step: python -m app.agent.mcp_demo.web_search_client QUERY."""

import argparse
import asyncio
import os
from pathlib import Path
import sys

from mcp import StdioServerParameters
from openai import AsyncOpenAI

from app.agent.openai_compatible_tool_calling_model import OpenAICompatibleToolCallingModel
from app.agent.step_executor import StepExecutor
from app.agent.tool_calling_step_runner import ToolCallingStepRunner
from app.config import config
from app.models.taskstate import TaskState
from app.models.working_memory import WorkingMemory
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
    memory = WorkingMemory(TaskState("user", query))
    memory.task_state.start_planning()
    memory.install_plan([f"Search the web and answer using the results: {query}"])
    registry = ToolRegistry()
    async with AsyncOpenAI(
        api_key=config.dashscope_api_key, base_url=config.dashscope_base_url
    ) as model_client:
        model = OpenAICompatibleToolCallingModel(model_client, config.dashscope_model)
        async with MCPClient.connect(parameters) as client:
            await register_mcp_tools(client, registry)
            runner = ToolCallingStepRunner(model, registry)
            executor = StepExecutor(memory, runner)
            result = await executor.execute(memory.task_state.plan[0].step_id)
            print(result.result_summary)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run an Aletheia web-search step")
    parser.add_argument("query")
    asyncio.run(main(parser.parse_args().query))
