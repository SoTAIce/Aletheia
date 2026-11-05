# 按任务动态选择执行模式（route-auto-001，决策 D-026）

日期：2026-11-05。来源：用户“先按如此写”（采用“规则 + 模型”方案的第一阶段）。

## 做了什么

- 新模块 `app/agent/route_selection.py`：
  - `allowed_modes(team_options)`：宿主规则层。没有团队选项（`team_unavailable`）、只读（`read_only`）、没有沙盒（`no_sandbox`）时只允许普通模式；允许写文件且开启沙盒时两种都允许（`code_delivery_with_sandbox`）。
  - `choose_mode(goal, team_options, model)`：规则只剩一种时直接采用；否则向模型发一次不带工具的 JSON 询问，回复必须是 `{"route": ..., "reason": ...}`，且模式在规则允许的范围内。出错、超时（20 秒）或格式不对一律退回普通模式；取消照常向上传递。任务文本最多发送 4000 字。
  - 每次决策记录 `Route decision: {...}`：模式、来源（`user`、`rule`、`model`、`fallback`）、规则名和异常类型链，不含模型原文；平时是 INFO，`ALETHEIA_TEAM_TRACE=1` 时是 WARNING。
- 服务 `RagAgentService`：
  - 接受 `execution_mode='auto'`；每个新任务在创建计划器之前选一次模式，选团队时才使用单步计划器、团队注册表和 `agent_team` 能力要求。
  - 被阻塞的任务恢复时沿用原来的选择；任务结束后清除记录。
  - 固定模式行为不变，只多记一条“用户指定”的决策。
- 命令行：`--execution-mode auto`；只有同时给了 `--write-root` 和 `--sandbox` 才会生成团队选项，否则规则直接定为普通模式。
- 评测器：`--modes` 支持 `auto`，团队相关参数（时限、独立验收）也传给 `auto`；结果行新增 `route`。
- 顺带修正：删除评测器里 V1 删除后遗留、已不起作用的 `--team-version` 参数；项目交接文档的调用链图更新为 V2 并加入路由选择。

## 没做什么

- 按步骤选择运行器（第二阶段）：需要团队能只接管某一步，并修改计划器输出格式（`planner.py` 有用户未提交的改动）。
- 路由准确率的真实评测：需要用户授权；评测任务也还需要补充只读问答类任务，才能检验“该选普通模式”的情况。

## 验证

- `tests/test_route_selection.py` 20 passed：规则、模型选择、各种失败退回、超时、取消、日志不含模型原文、命令行、评测器、服务按任务绑定运行器、阻塞任务保留选择、固定模式。
- 当前 tests 128 passed、12 skipped；init.ps1 PASS。
- 历史回归（9138ac1 快照）：1022 passed、2 skipped，3 项已知失败和 4 个 V1 测试文件无法收集，与上次基线相同。
