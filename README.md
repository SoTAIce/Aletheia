# Aletheia

> An agentic RAG system for knowledge retrieval, tool orchestration, and intelligent workflows.

Aletheia is a Python-based AI agent project built around RAG, tool calling, and agent workflows. It combines hybrid retrieval, reranking, Milvus vector storage, MCP tools, and turn-based conversation memory. Run its bounded plan/execute/replan loop with `python -m app.agent "Your goal"`.

## Features

- Dense, BM25, and hybrid retrieval
- Reciprocal-rank fusion and cross-encoder reranking
- DashScope-compatible chat and embedding clients
- Milvus vector storage with a local Docker Compose setup
- MCP tool integration
- Turn-based short-term conversation memory

## Requirements

- Python 3.11 through 3.13
- Docker and Docker Compose for the local Milvus stack
- A DashScope API key

## Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Create the local configuration file:

```powershell
Copy-Item .env.example .env
```

Set `DASHSCOPE_API_KEY` in `.env`, then start the local Milvus services:

```powershell
docker compose -f deploy/milvus/compose.yaml up -d
```

The credentials in the Compose file are development defaults and must be changed before any production deployment.

## Project layout

```text
app/
  agent/       Agent and MCP orchestration
  core/        Infrastructure clients
  models/      Conversation-memory models
  schemas/     Request and response schemas
  services/    RAG, vector-store, and memory services
  tools/       Agent tools
deploy/
  milvus/      Local Milvus Docker Compose configuration
```

## Development checks

Compile the application modules:

```powershell
.\.venv\Scripts\python.exe -m compileall app
```

Run tests after adding or updating them:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

## Agent runtime

Configure `DASHSCOPE_API_KEY` and `RAG_MODEL` in `.env`. The runtime always registers
the local time tool and, unless disabled, the existing RAG service. Filesystem MCP
is enabled by `MCP_FILESYSTEM_ROOT` (or `--filesystem-root`); writing requires the
additional `MCP_FILESYSTEM_WRITE_ROOT` (or `--write-root`). WebSearch MCP is enabled
when `TAVILY_API_KEY` is configured. The service reads `.env` through the existing
settings module and passes the Tavily key to its stdio subprocess.

```powershell
$env:TAVILY_API_KEY = "your-tavily-api-key"
.\.venv\Scripts\python.exe -m app.agent "Read README.md and summarize the project's capabilities" --filesystem-root . --no-knowledge
```

Omit `--no-knowledge` to include RAG when the existing Milvus/embedding/reranker
services are configured. Runtime defaults: 16 step attempts, 2 replans, 60 seconds
per planning/execution attempt, 4 tool rounds and 8 calls per step. Failed work
does not become a completed step. Blocking questions pause the task. CLI exit
codes are 0 for completed, 1 for failed, and 2 for blocked.

`RagAgentService.query_result()` returns status, successful-step receipts and
counters; `query()` returns text. `query_stream()` emits the final content and
terminal status rather than token streaming. See [runtime contracts, memory,
resuming blocked tasks and limitations](docs/agent_runtime.md).

Run the complete offline regression suite (the workspace temp path avoids host
temporary-directory permission issues):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/agent tests/models tests/tools -q --basetemp=tmp/pytest-agent-runtime
```

## Web search through MCP

Set `TAVILY_API_KEY` in the environment of the process launching the MCP server.
The server reads this variable directly; it does not load `.env` automatically.
The agent demo uses the existing DashScope settings from `.env`:

```powershell
$env:TAVILY_API_KEY = "your-tavily-api-key"
.\.venv\Scripts\python.exe -m app.agent.mcp_demo.web_search_client "Search for Aletheia MCP documentation"
```

The demo starts `python -m app.tools.web_search.server` as a stdio subprocess,
discovers `search_web` with `register_mcp_tools`, and runs `StepExecutor` with
`ToolCallingStepRunner`. Keep both registration and the entire step execution
inside `async with MCPClient.connect(...)`; registry adapters use that live client.
Search results are JSON objects containing `title`, `url`, and `snippet`.

Run the offline integration tests (real stdio MCP; simulated HTTP and model responses):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/tools/test_web_search_integration.py tests/tools/test_mcp_integration.py tests/agent/test_tool_calling_step_runner.py -q
```

## Local data

Virtual environments, API credentials, downloaded models, generated output, and Milvus data volumes are intentionally excluded from Git. See `.gitignore` for the complete list.
