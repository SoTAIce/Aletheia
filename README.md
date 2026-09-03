# Super Biz Agent

Super Biz Agent is a Python business assistant project built around RAG, tool calling, and agent workflows. The repository currently contains the core retrieval, reranking, Milvus, MCP, and conversation-memory components; the application entry point is still under development.

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
tools/         Development and documentation utilities
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

## Local data

Virtual environments, API credentials, downloaded models, generated output, and Milvus data volumes are intentionally excluded from Git. See `.gitignore` for the complete list.
