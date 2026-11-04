# Agent Team V1 文档—代码一致性审查（team-audit-001）

日期：2026-11-04。范围：`app/agent/agent_team_runner.py`、`app/agent/team/*`、`rag_agent_service._team_registry`、`runtime.py` 的 Team 超时分支。
对照对象：[PROJECT_HANDOVER §7](../PROJECT_HANDOVER.md)、[agent_team_v1.md](../agent_team_v1.md)。不改 `app/`，不调用模型/MCP/DB。

## 结论

**文档与代码基本一致。** Team 源码自冻结提交 `9138ac1` 起没有任何改动（`git diff 9138ac1 -- app/agent/team app/agent/agent_team_runner.py app/agent/runtime.py` 为空）。
只有 1 处表述不精确（修订上限），并有 4 处文档没写的细节；均已在总交接中更正或补充，原报告不改写。没有发现代码缺陷。

## 测试证据

| 测试 | 结果 | 说明 |
| --- | --- | --- |
| 历史 Team 测试（取自 git `9138ac1`：test_team_v1 / reliability / contract_review / session_poc） | **68 passed** | 在 TEMP 隔离目录中，针对当前工作区代码（含未提交的长期记忆源码）运行 |
| 历史路由测试（test_runner_registry / runner_routing / runtime_routing） | **42 passed** | 同上 |
| 新增常驻契约测试 [tests/test_team_contracts.py](../../tests/test_team_contracts.py) | **8 passed** | 固化下表中的默认值和门禁；由 init.ps1 运行（全套 10 passed） |

清理：TEMP 运行目录已删除（有超过 MAX_PATH 的路径，改用 `\\?\` 前缀删除并确认不存在）；init.ps1 现在会在 pytest 结束后自动删除 basetemp；历史测试启动的 MCP 子进程在 `app/tools/` 下生成了 2 个 `__pycache__`（子进程拿不到 `PYTHONDONTWRITEBYTECODE`），已删除；仓库内没有其他 tmp 产物。

## 逐条核对

| 文档声明 | 结论 | 代码证据 |
| --- | --- | --- |
| Team 是与单 Agent 平级的 Runner，不嵌套 Runtime | 一致 | `agent_team_runner.py:2-4` 只导入 coordinator/models；`:38-39` 以 `agent_team` 注册 |
| 独立 Lead + 确定性 Coordinator，按顺序调度 | 一致 | `coordinator.py:152-188` Lead 会话；`:243-326` 确定性执行命令；`:352-368` 每次只执行一条命令 |
| 宿主最终契约 `{"answer": 非空}`，Lead 不能覆盖 | 一致 | `models.py:56-58,72`；`coordinator.py:300-304` finish 时用宿主契约序列化，不合格返回 `host_rejected` |
| 工具 = 宿主允许 ∩ 实际可用 ∩ Lead 请求 | 一致 | `agent_team_runner.py:35-36`；`coordinator.py:205-207`；`_ToolGate` `:85-86` 执行时再查一次 |
| 默认只读工具 | 一致 | `models.py:70-71`：clock / read_file / list_directory / RAG / web search |
| 默认最多 4 成员、32 命令、48 模型调用、32 业务工具、180s | 一致 | `models.py:63-69` |
| “每个非 Lead Session 版本上限 12” | **不精确** | `session.py:189-191` 在递增前检查 `version > max_revisions`：允许 **12 次修订**，版本号可从 1 到 13。已在总交接中更正 |
| Lead 使用 actions 上限而非成员修订上限 | 一致 | `coordinator.py:125` |
| Reviewer 反馈 ≤ 2000 字符；超长时只允许 submit-only 修复 | 一致 | `coordinator.py:35-36,42`；`session.py:167-175`（默认最多 2 次修复，`:94`）；只有 Reviewer 开启截断修复（`coordinator.py:219`） |
| 修订 Worker 会使其审查失效；finish 需要当前版本的全部审查都 approve | 一致 | `coordinator.py:238-241,255-258,296-299` |
| 父任务所有权守卫必需，模型/工具调用前后和导入前都检查 | 一致 | `agent_team_runner.py:11-12`；`coordinator.py:63,73,83,96,305`；`rag_agent_service.py` `_team_registry.capture_guard` |
| 失败后不自动重放；外层超时不触发 Replan | 一致 | `coordinator.py:337-338`；`runtime.py:231-233` |
| 安全诊断不记录原始异常消息 | 一致 | `diagnostics.py` 只记录类型、嵌套结构与 token 数；`coordinator.py:384-386` |
| 预算在 rebind/恢复后不重置；用户思考时间不计入 | 一致 | 预算存在 `TeamState`（`models.py:115`）；`coordinator.py:347-391` 只累计运行时长 |
| 只能在同一进程内恢复 | 一致 | `TeamStore` 是内存 dict（`models.py:128-162`） |
| 成员各自拥有 WorkingMemory/ContextManager | 一致 | `coordinator.py:209`、`:114-116` |

## 文档未写明的细节（已补入总交接 §7）

1. 共享估算 token 预算默认为 **500,000**（`models.py:67`）。
2. 每个 Session 还有自己的上限：模型调用 48、工具调用 96（= 2×48）、单次运行超时 180s（`coordinator.py:121-127`）。团队级上限会先触发。
3. 被拒绝的 Lead 命令同样消耗 action/模型/时间预算（`coordinator.py:358-365`）。
4. 没有 Reviewer 的 approve，团队无法 finish；同一 service 最多保留 128 个团队状态（`models.py:130`）。

## 与 backlog 的关系

本审查没有改变 Team 的质量结论（真实任务严格交付 Team 0/3）。`team-delivery-reliability` 仍是 `authorized=false`。现在已有 68 个历史 Team 测试可以从 git 取出运行，加上 8 个常驻契约测试，做可靠性修复前的回归保护比之前更充分。
