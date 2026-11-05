# 团队模式失败诊断 — 2026-11-04（team-diagnosis-001）

## 任务契约

```text
Feature: team-diagnosis-001
Scope: 新增安全执行轨迹 app/agent/team/trace.py；coordinator 记录每条命令的结果并在结束时写日志；models 增加 outcomes；
       bench/run_bench.py 把轨迹写进报告；tests/test_team_trace.py
Exclusions: 不改 .env、模型默认值、依赖；不改团队的修复预算或失败处理（只诊断）；不推送
Verification: 确定性测试证明轨迹不含模型文本；全量回归；经授权只重跑团队模式 2 次
Authorization: 用户 2026-11-04“授权 2 次”（parse_duration、fix_inventory 各 1 次，团队模式，沙盒，团队时限 400 秒）
Done when: 用轨迹证据说明失败的会话、动作和原因；输出文件已删除；无残留容器和 __pycache__
```

## 运行结果

| 任务 | 状态 | 耗时 | 模型调用 | 工具调用 | 失败类型 |
|---|---|---|---|---|---|
| parse_duration | failed | 107.8 秒 | 7 | 2 | ExceptionGroup → ModelToolArgumentsError → JSONDecodeError |
| fix_inventory | failed | 166.8 秒 | 15 | 9 | 评测报告里是 unrecognized；轨迹里是 ExceptionGroup（见下文“轨迹自身的缺陷”） |

两次都没有任何沙盒运行通过的记录，也都没有进入审查环节。

## 轨迹证据

**parse_duration**

- 负责人的动作：`create_worker worker_spec_extract`（成功）→ `run_member worker_spec_extract`（抛出异常，团队结束）。
- 负责人会话：模型调用 2 次，格式修复 **0/2**，没有失败。
- 成员 `worker_spec_extract`：只给了 `list_directory`、`read_file` 两个工具（**没有写入和运行工具**）。工具序列是 `list_directory` 成功 → `read_file` 成功，之后连续 3 次工具参数无法解码（`tool_arguments_rejected` = 3），格式修复 **2/2** 用完，第 3 次直接抛出。

**fix_inventory**

- 负责人的动作：`create_worker inv_fixer` → `run_member inv_fixer`（抛出异常，团队结束）。
- 负责人会话：格式修复 **2/2**（两次输出被拒，`output_rejected` = 2），还没失败，但已经没有余量。
- 成员 `inv_fixer`：有全部 5 个工具。工具序列：`list_directory` → `read_file` ×2 → `write_file` → `run_python` → `write_file` ×3 → `read_file`，全部成功；之后连续 3 次参数无法解码，修复 **2/2** 用完后抛出。

## 结论

1. **原假设只对了一半。** 原来认为负责人会先把修复预算用完。实际上两次都是**工作成员**先用完：每个成员在第一次 `run_member` 内就连续 3 次产生无法解码的工具参数。负责人在 fix_inventory 里也已经用完 2 次，说明“整个生命周期只有 2 次、从不重置”的预算确实很紧，但它不是这两次的直接原因。
2. **一个成员失败会直接终结整个团队。** `coordinator._advance` 记录失败后原样抛出，负责人没有机会换人、改指令或重试。普通模式下一步失败可以重新规划；团队模式没有这一层。这是团队比普通模式更脆弱的结构性原因。
3. **出错的工具调用很可能是 `submit_result`。** parse_duration 的成员只有两个只读工具，而且这两个都调用成功了；剩下能调用、且参数会很长的只有 `submit_result`（候选结果要把整段代码放进 JSON 字符串里，双重转义）。fix_inventory 的成员在写完文件之后才出错，同样符合“提交候选结果时出错”。普通模式交付时不需要把代码嵌进 JSON，所以没有这个问题。**这一点是推断**：本次轨迹没有记录被拒调用的工具名（已补，见下文），需要再跑一次才能确认。
4. **负责人的分工也有问题。** parse_duration 是写文件的任务，负责人却建了一个只读的“提取需求”成员，即使它成功了也交付不了文件。

## 轨迹自身的缺陷（已修复，确定性测试覆盖）

- 嵌套的 ExceptionGroup 只显示了最外层类型：去重逻辑碰到同名的内层组就停止深入。改为复用 `diagnostics.failure_types`。
- 被拒的工具参数没有记录元数据。现在每个会话增加 `argument_rejections`（工具名经正则校验、原因为枚举值、参数长度为整数，最多 8 条），不含任何参数内容。
- fix_inventory 在报告里的失败类型仍是 unrecognized：命令行首行没有通过安全过滤，原文已不可得（报告未保存原文是有意的）。轨迹已经能给出类型，这一项暂不处理。

## 建议的修复（未实施，需要用户同意）

按收益排序：

1. **成员失败不终结团队**：把成员的失败作为一个事件交回负责人（只含失败类型），让负责人决定换人、改指令或放弃；同时给每个团队设置总的成员失败上限，防止无限重试。
2. **修复预算按每次运行重置**：成员每次 `run_member`、负责人每条命令重置格式修复计数，同时保留整个团队的总上限，与普通模式的行为对齐。
3. **候选结果不再内嵌完整代码**：写文件任务里，`submit_result` 只提交文件路径和简短说明，代码以写入的文件为准，从源头减少超长 JSON 参数。
4. 负责人的提示里补一句：交付文件的任务，必须把写入和运行工具交给执行成员。

确认第 3 条推断需要再跑一次团队模式（建议 2 次，与本次相同的命令），新的轨迹会直接显示被拒调用的工具名和参数长度。

## 清理

报告和进度日志已删除；没有残留的沙盒容器；没有新增 `__pycache__`。
