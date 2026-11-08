# 架构第 0 阶段：清理遗留代码（修改方案）

日期：2026-11-08。状态：**方案，未实现**（cleanup-stage0-plan-001）。打算交给 Codex 实现，功能编号 `cleanup-stage0-001`。

## 1. 为什么先做这一项

- 架构梳理（[architecture_review.md](architecture_review.md) 第 6 节）的第 0 阶段原本有两项：
  - 清理遗留代码；
  - 去掉重复发送的工具清单。
- 第 1 阶段（单一循环）已经做完，网页也能选循环了（D-062～D-065）。下一个大步骤是把循环设为默认，但那要先用真实模型在网页上验证，需要用户授权，不适合交给 Codex。
- 清理遗留代码只删除没人用的文件，不改变智能体的行为，用确定性测试就能验证，适合现在交给 Codex。
- 去掉重复的工具清单会改变规划模式发给模型的内容，需要真实评测来判断好坏，所以**这次不做**。单一循环里已经去掉了这份清单（D-062）。

## 2. 开始之前（由用户决定）

- 2026-11-08 更新：用户已同意先提交，`loop-web-001`（含审查后的两处前端修复）已和本方案一起提交，Codex 可以从这次提交开始，工作区只剩用户自己的改动。下面的约定保留，以防工作区又有别的未提交内容。
- 原建议：工作区里还有 `loop-web-001` 没提交，包括 Codex 的实现和审查后的两处前端修复。建议先提交它，再开始这一项。这样两项改动不会混在一起，出问题也好分别回退。
- 如果用户不提交，Codex 必须在现有工作区上继续：不能覆盖、还原或一起提交 `loop-web-001` 的文件，并按 `docs/harness/baseline.md` 新开一份本轮清单。

## 3. 核对结果（2026-11-08，基于 `c15ffd9` 加未提交的 loop-web-001）

我（Claude）用全仓库搜索核对过每个对象的引用情况。范围包括 `app/`、`tests/`、`bench/`、`scripts/`、`pyproject.toml`、`init.ps1` 和文档，不含 `.venv`、`output`、`data`。

| 对象 | 谁在用 | 结论 |
|---|---|---|
| `app/agent/aiops/`（`__init__`、`executor`、`planner`、`replanner`、`state`、`utils` 共 6 个文件） | 没有任何代码或测试导入；文档里写的是"旧版工作流独立保留" | **删除** |
| `app/agent/mcp_client.py` | 只有 `aiops` 的三个文件在用（`get_mcp_client_with_retry`）；在用的 MCP 客户端是 `app/tools/mcp_client.py` | 随 `aiops` 一起**删除** |
| `app/agent/mcp_demo/registry_client.py` | 没有导入，也没有文档写到它的命令 | **删除** |
| `app/agent/mcp_demo/time_client.py` | 没有导入，也没有文档写到它的命令 | **删除** |
| `app/agent/mcp_demo/time_server.py` | `tests/tools/test_mcp_integration.py` 把它当测试用的服务启动 | **保留** |
| `app/agent/mcp_demo/web_search_client.py` | `tests/tools/test_web_search_integration.py` 导入，`README.md` 里有运行命令 | **保留** |
| `app/services/vector_index_service.py` | 没有代码导入，但它是唯一一段把文档切分后写进知识库的代码（`index_directory`、`index_single_file`）；知识检索工具只负责读 | **保留**；架构梳理把它算作遗留代码是错的，要改正 |
| `app/tools/__init__.py` 里的 `DEFAULT_LOCAL_AGENT_TOOLS` | `tests/agent/test_tool_boundaries.py` 在用 | **保留**，不要改 |
| `app/core/dependencies.py` 里的本地 Ollama 压缩器 | 命令行和一个测试在用 | **保留**，不在这次范围内 |

删完以后，下面这些依赖在源码里就没人用了：
- `langgraph`、`langgraph-checkpoint`、`langgraph-prebuilt`、`langgraph-sdk`：原来只有 `aiops/executor.py` 在用；
- `langchain-mcp-adapters`：原来只有 `app/agent/mcp_client.py` 在用。

按 AGENTS.md 的规矩，**不要改 `pyproject.toml` 或锁文件**，只把这一点记进交接，留给用户决定。

## 4. 要做的修改

### 4.1 删除文件（共 9 个）

```
app/agent/aiops/__init__.py
app/agent/aiops/executor.py
app/agent/aiops/planner.py
app/agent/aiops/replanner.py
app/agent/aiops/state.py
app/agent/aiops/utils.py
app/agent/mcp_client.py
app/agent/mcp_demo/registry_client.py
app/agent/mcp_demo/time_client.py
```

- 用 `git rm` 删除，`aiops/` 整个目录不留。
- 删之前，再用搜索确认一遍上表"谁在用"一栏仍然成立。如果工作区比 `c15ffd9` 加 loop-web-001 又有了变化，发现了新的引用，就停下来报告，不要连引用的代码一起删。
- `app/agent/mcp_demo/` 目录保留，里面剩下 `time_server.py` 和 `web_search_client.py`。这个目录没有 `__init__.py`，是按命名空间包导入的（`web_search_client` 的测试写的是 `from app.agent.mcp_demo import web_search_client`），不要给它加 `__init__.py`。

### 4.2 改文档里过时的说法

只改下面列出的句子，不重写整篇文档：

- `docs/architecture_review.md` 第 3 节问题 8（约第 123～131 行）：
  - 说明 `aiops/`、`app/agent/mcp_client.py` 和两个 `mcp_demo` 客户端已在第 0 阶段删除（cleanup-stage0-001）；
  - 改正 `vector_index_service.py`：它是知识库唯一的建库代码，没有调用方是因为项目没有建库命令，不是遗留代码；
  - 把"两个 `mcp_client.py` 名字重复，需要确认职责"改成结论：在用的是 `app/tools/mcp_client.py`，`app/agent/mcp_client.py` 只被 `aiops` 使用，已一起删除。
- `docs/architecture_review.md` 第 6 节表格第 0 阶段那一行后面，补一句：遗留代码已清理；"去掉重复的工具清单"因为会改变规划模式发给模型的内容、需要真实评测，没有做。
- `docs/PROJECT_HANDOVER.md` 第 102 行附近：把 "`app/agent/aiops`、`mcp_demo` 是旧实现/示例/兼容与实验入口" 改成：`aiops` 已删除；`mcp_demo` 只剩测试用的 `time_server.py` 和 `web_search_client.py`。第 101 行提到的 `vector_index_service.py` 不用改。
- `docs/executor_v1.md` 第 6 行和 `docs/agent_runtime.md` 第 165 行附近，都提到了 `aiops`：改成"已于 2026-11-08 删除（cleanup-stage0-001）"。

`docs/harness/history/` 下的旧清单，以及 `docs/DECISIONS.md` 里已有的条目（例如 D-050 提到 `aiops`），都是历史记录，**不要改**。

### 4.3 不做的事

- 不改任何还在用的源码，包括 `app/tools/mcp_client.py`、`vector_index_service.py`、`document_splitter_service.py`、`dependencies.py`。
- 不改 `pyproject.toml` 和锁文件，不卸载依赖。
- 不去掉规划模式里重复的工具清单。
- 不新增建库命令，即使 `vector_index_service.py` 没有入口。这一点记成"另行决定的后续项"。

## 5. 测试

1. **补一个防回退的测试**，加在现有的 `tests/test_team_v2.py` 第 643 行附近那种"旧模块已移除"的测试旁边，或者新建 `tests/test_stage0_cleanup.py`：
   - 用 `importlib.util.find_spec` 断言 `app.agent.aiops`、`app.agent.mcp_client`、`app.agent.mcp_demo.registry_client`、`app.agent.mcp_demo.time_client` 都找不到；
   - 同时断言 `app.agent.mcp_demo.time_server`、`app.agent.mcp_demo.web_search_client`、`app.services.vector_index_service` 仍然存在，防止以后误删。
2. **跑 `init.ps1`**：
   - 语法检查的文件数会从 101 减少 9 个，变成 92；
   - 测试数会多出新增的那几项，其余测试必须全部照常通过。
   - 如果有测试失败，先查是不是漏掉了某个引用，不要为了通过去改测试的断言。
3. **单独确认两个保留文件的测试仍然通过**：
   - `tests/tools/test_mcp_integration.py`（用 `time_server.py`）；
   - `tests/tools/test_web_search_integration.py`（导入 `web_search_client`）。

   这两个测试可能需要真实服务，在 `init.ps1` 里被跳过。如果被跳过，就在记录里写明"没有覆盖到"，不要为了让它运行而去连真实服务。

## 6. 记录（按 AGENTS.md）

- `feature_list.json`：
  - 新增 `cleanup-stage0-001`，依赖 `cleanup-stage0-plan-001`，evidence 写清删除的文件、保留的文件和原因，以及 `init.ps1` 的结果；
  - 实现期间 `active_feature` 设为它，完成后改回 `null`。
- `docs/DECISIONS.md`：新增 D-066，内容包括：
  - 删除了什么；
  - 为什么保留 `vector_index_service.py`、`time_server.py`、`web_search_client.py`；
  - 为什么这次不去掉重复的工具清单；
  - 有哪些依赖已经没人用，但留给用户决定。
- `progress.md`、`session-handoff.md`：各加一条。交接的"下一步"里写上两件待用户决定的事：要不要从 `pyproject.toml` 去掉没人用的依赖，以及要不要给知识库加一个建库命令。
- `AGENTS.md`、`docs/PROJECT_HANDOVER.md`：更新测试数。
- `docs/harness/baseline.md`：按"清单是交接证据"的规则，记录本轮结束时的未提交状态。这次有被删除的已跟踪文件，要放进 `deleted_tracked`。

## 7. 给实现者的约束

- 先读 `AGENTS.md`、`session-handoff.md` 顶部最新交接，再读本方案。
- 不调用真实模型，不连真实服务，不安装或下载，不改 `.env`、模型默认值、依赖或业务数据。
- 不提交、不推送，除非用户在这一轮明确授权。
- 用户自己没提交的改动不要动，也不要一起提交：
  - `README.md`；
  - `app/agent/planner.py`、`app/agent/step_executor.py`、`app/agent/tool_calling_step_runner.py` 里的中文注释；
  - 删掉的 `scripts/benchmark_conversation_memory.py`、`scripts/validate_multi_runner_live.py`、`scripts/validate_summary_context.py`。
- 如果 `loop-web-001` 还没提交，它的文件也不要动，只在交接里说明两项改动叠在同一个工作区。
- 跑测试时用 `-p no:cacheprovider` 和指向临时目录的 `--basetemp`，不在项目里留下 `__pycache__` 或 `.pytest_cache`，不往 `data/` 写东西。

## 8. 之后的路线（这次不做，供参考）

1. 用真实模型在网页上验证单一循环：普通问答、写文件时人工确认的允许和拒绝、`ask_user` 阻塞后在网页上回答。需要用户授权。
2. 调参集和留出集各再跑 1～2 次。结果不差的话，由用户决定把默认值改成 `loop`（第 2 阶段）。
3. 第 0 阶段剩下的一项：去掉规划模式里重复的工具清单。只有规划模式还保留时才值得做，要配合一次调参集评测。
4. 第 3 阶段：团队模式改用循环里的 `delegate` 工具。
