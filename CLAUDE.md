# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 入口

请先阅读并遵循 [AGENTS.md](AGENTS.md)，它是本仓库唯一的公共工程指令入口（全局不变量、完成定义、会话收尾都在那里，这里不重复）。
随后读取当前会话交接 `.handoff/session-handoff.md`（在独立的 `handoff` 分支上，检出方法见 AGENTS.md 启动第 2 步）和 [项目总交接](docs/PROJECT_HANDOVER.md)。
按 [文档索引](docs/DOCUMENT_INDEX.md) 加载相关主题，不必一次读取全部历史文件。代码和文档注释里的 `D-0xx` 指 [决策记录](docs/DECISIONS.md) 中的条目。
历史报告中的测试与 API 授权不可直接沿用；以当前文件、用户要求和实际验证为准。

## 回复语言

默认用中文回复用户，不要中英夹杂。用户能看到的所有文字都用中文：正文、每轮结束时下一步框里的总结、问题和按钮、任务列表的步骤名、工作途中的进度说明。只有文件路径、命令、代码里的名字和提交号保留原样，放在代码格式里。

## 分工和会话

- **Codex 实施，Claude 写方案和审查**（用户 2026-11-08 定）：要改代码的任务，Claude 先在 `docs/` 写方案（要改的文件、测试、记录和约束），用 Agent 工具派 `codex:codex-rescue` 子代理实现，做完逐项审查（范围、用户未提交改动、`init.ps1`、基线核对）。派出去后挂后台监视，任务日志 20 分钟没更新就当作卡住，先用 `git status` 收下已完成的部分。Codex 没额度、没登录或报错时，告诉用户，由 Claude 自己实施。一两行的小改动、文档和只读排查由 Claude 直接做。
- **可以自己开多个会话**（用户 2026-11-06 定）：任务确实能拆成互不依赖、不改同一批文件的几块时，可以分头开新会话，每份说明写清范围、文件和完成标准，并要求遵守 `AGENTS.md`。这不扩大其他授权：真实模型调用、提交、推送仍要用户逐项同意。

## 常用命令

Windows + PowerShell，解释器优先用 `.venv/Scripts/python.exe`（Python 3.11–3.13）。

```powershell
# 健康检查：管理文件/JSON 校验、app/ 语法编译、CLI 加载、确定性 pytest（不调模型、MCP、数据库）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1

# 单独跑测试（basetemp 放到 TEMP 下的新目录，不要写进仓库）
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp $env:TEMP/aletheia-pytest-x
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/test_loop_run.py::test_name
# 真实容器测试默认跳过，需 Docker 和 aletheia-sandbox:py313 镜像：$env:ALETHEIA_DOCKER_TESTS='1'
```

以下入口会调用真实模型或服务，需要本轮用户授权后才能运行：

- `python -m app.agent "<目标>" --filesystem-root . --no-knowledge`：CLI 单次任务；不带目标进入交互模式。退出码 0 完成、1 失败、2 阻塞。
- `python -m app.api`：FastAPI Web 后端（`--write-root`、`--sandbox`；非回环地址必须带令牌）。
- `python -m app.knowledge`：从目录建 Milvus 知识库（默认只切块，确认后才替换）。
- `python -m bench.run_bench`：基准任务；`--confirm-live` 才真正调用模型，见 [bench/README.md](bench/README.md)。
- `python -m app.agent.trace FILE.jsonl` 或 `--session NAME`：只读查看运行轨迹，不连接任何服务。

pytest 配置在 `pyproject.toml`（`asyncio_mode = "auto"`，测试只在内存中运行）。仓库没有 lint/格式化配置。

## 架构概览

**任务运行（`app/agent/`）**：每个任务是一个循环（D-076）。`loop_run.py` 的 `LoopRun` 负责宿主保证：锁、重新绑定后旧执行器作废、取消后不提交、版本校验、预算、单步时限、副作用监视、执行次数上限、失败分类，以及 `plan`/`step` 事件和追踪区间；任务状态在 `app/models/run_state.py` 的 `RunState`/`RunMemory`。执行器只返回输出，由 `LoopRun` 提交。结果和失败分类在 `runtime.py`（`AgentRunResult`、`FAILURE_MESSAGES`）。规划模式和规划—执行骨架已在第 4 阶段删除。

**执行模式**：`route_selection.py` 每个任务开始前选一次模式（先走宿主规则，再一次有界模型调用，失败回落到 single），同时判断任务是否写文件，只读任务看不到写入和运行类工具。`app/services/run_assembler.py` 的 `RunAssembler` 按模式选执行器工厂并建 `LoopRun`。
- single：`agent_loop.py` 的 `AgentLoopRunner`，整个任务一个工具循环，纯文本即最终答案，自带 `todo` / `ask_user` 控制工具，阻塞后在内存中接着原来的消息续跑；`--delegate` 时用 `delegation.py` 的 `ExploreLoopRunner`，可以把只读调研交给子循环（默认关闭）。
- team：`delegation.py` 的 `DelegatingLoopRunner`，负责人也是一个循环，只有宿主工具 `delegate`/`review`，每次委派都起一个全新上下文的子循环成员；共享状态在工作区文件里，完成由宿主判定（D-075）。
- `step_runner.py` 是执行器接口（`RunnerDependencies`、`RunnerFactory`、错误类型）；能力需求来自应用代码，不来自模型。

**横切机制**：`hooks.py`（观察者 + 只能拒绝的门控，通过上下文变量挂到 `HookedTool`/`HookedModel` 上，事件不带工具输出或模型原文）；`run_meter.py`（预算计量，每次模型调用前查预算，D-078）；`provider_state.py`（服务商报告的提示令牌与本机估算之比分档、模型名是否被替换，异常时提示，D-079）；`side_effects.py`（外部副作用不确定时不重试）；`trace.py` / `trace_view.py`（本地轨迹）。

**服务层**：`app/services/rag_agent_service.py` 是面向会话的门面（CLI 和 Web 都经过它），持有工具注册表和每个任务的 `LoopRun`，选择和组装交给 `run_assembler.py`。记忆分三层：`app/models/conversation_memory.py`（会话视图）、`app/models/run_state.py`（任务真值）、`app/services/context_manager.py`（只做投影）；`conversation_archive.py` 归档原文，`summary_scheduler.py` 做后台摘要。长期记忆以 PostgreSQL（`postgres_memory_store.py`）为准，Milvus（`memory_vector_index.py`）是可重建的索引。RAG 检索链：`rag_search_service.py`（稠密 / BM25 / 混合）→ `reciprocal_rank_fusion.py` → `rag_reranker_service.py`。

**工具（`app/tools/`）**：都注册进 `tool_registry.py`。文件系统、联网搜索（Tavily）和沙箱（Docker）都是 stdio MCP 子进程（各子目录下的 `server.py`），经 `mcp_client.py` + `mcp_tool_adapter.py` 接入；MCP 连接的进入和退出必须在同一个 asyncio task 里，并覆盖整个执行期。

**配置**：`app/config.py`（pydantic-settings，读取 `.env`，参考 `.env.example`）；对话模型的地址、窗口、能力和价格由 `Settings.agent_model_profile` 收成一个 `ModelProfile`（`app/models/model_profile.py`，D-078）。模型走 DashScope 兼容接口；Milvus 本地栈在 `deploy/milvus/compose.yaml`。

## 注意

- 改动行为时，在 `tests/` 中补充或恢复对应测试。
