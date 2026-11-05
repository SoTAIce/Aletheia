# 能力增强阶段 1：可恢复的参数错误 + 评测集（capability-phase1-001）

日期：2026-11-04。用户授权“先按照这个开始”。本轮只改代码和确定性测试，**没有调用真实模型**。

## 改动

| 文件 | 改动 |
| --- | --- |
| `app/agent/tool_calling_model.py` | 新增 `ModelToolArgumentsError`（只保存 reason / 过滤后的工具名 / 参数长度）和 `tool_argument_repair_instruction()`（宿主撰写的修复文字，不含模型输出） |
| `app/agent/openai_compatible_tool_calling_model.py` | `_parse_arguments` 改为抛出上述错误；消息文本保持不变 |
| `app/agent/team/session.py` | 遇到参数错误时：发出 `tool_arguments_rejected` 事件，追加修复指令后重试；共用 `max_format_repairs`；新增 `argument_repairs` 计数 |
| `app/agent/team/diagnostics.py` | 失败详情加入 reason / tool_name / argument_chars |
| `app/agent/tool_calling_step_runner.py` | 主循环中同样做有界修复（`argument_repair_count`）；`StepModelError` 消息附带异常类型链。用户未提交的中文 docstring 原样保留 |
| `bench/` | 评测集：`todo_html`、`parse_duration`、`fix_inventory`，评分器 `graders.py`，运行器 `run_bench.py`，说明见 [bench/README.md](../../bench/README.md) |

## 验证

| 测试 | 结果 |
| --- | --- |
| 新增 `tests/test_tool_argument_repair.py` | 9 passed：适配器 3 种 reason 且不含原文、可疑工具名被丢弃、single 修复后成功 / 次数耗尽 / 其他错误不修复、Team 修复后完成 / 次数耗尽 |
| 新增 `tests/test_bench_graders.py` | 11 passed：参考答案满分；内置的有缺陷 inventory 不通过；外部资源和 localStorage 键检查；AST 守卫；运行器未确认时不启动、超出上限时拒绝 |
| 历史 `tests/agent`（git 9138ac1 → TEMP） | **450 passed**（包括适配器、runner、Team、路由、runtime），TEMP 已删除 |
| init.ps1 | PASS，pytest 30 passed（2 smoke + 8 Team 契约 + 9 参数修复 + 11 评测评分） |

## 还没有验证

- 修复逻辑对真实模型是否有效：需要用评测集做一轮真实运行（6 次运行 = 3 任务 × 2 模式 × 1 次），须用户授权。
- 评分器对前端页面只做静态检查；交互行为要用 `--keep-answers` 保存答案后在浏览器中人工检查。

## 第一轮真实评测（2026-11-04，用户授权 6 次运行）

命令：`python -m bench.run_bench --confirm-live --tasks all --modes single team --runs 1`（qwen3.8-flash，只读根目录，web search 关闭）。

| 任务 / 模式 | 状态 | 执行 | 耗时 | 严格判定 | 宽松诊断（提取代码后） |
| --- | --- | --- | --- | --- | --- |
| fix_inventory / single | completed | 5 | 69.5 s | 失败：答案前后有说明文字，无法作为源码解析 | **16/16** |
| parse_duration / single | completed | 3 | 39.5 s | 失败：同上 | 20/21（没有拒绝 `1 h`） |
| todo_html / single | completed | 3 | 66.6 s | 失败：同上（10/11） | **11/11**；浏览器实测 9 项交互全部通过（含刷新后保留、375px 无横向滚动、无控制台错误） |
| fix_inventory / team | failed | 1 | 13.2 s | 失败（模型调用 1 次，工具调用 0 次） | — |
| parse_duration / team | failed | 1 | 87.8 s | 失败（模型调用 37 次，工具调用 32 次） | — |
| todo_html / team | failed | 1 | 109.4 s | 失败（模型调用 8 次，工具调用 3 次） | — |

**严格交付 0/6。** 宽松诊断只用来定位问题，不算通过。

### 结论

- **single 模式三项任务都完成了，代码质量其实不错**（同一个页面任务，frontend-compare-001 中 single 在模型生成层就失败了）。单次样本无法确认这次的变化来自参数修复，因为报告里没有记录修复次数。
- **single 的主要问题是交付格式**：三个答案都在代码前加了“已读取……”之类的说明；HTML 答案中整份文档出现了两次（11.6k 字符，其中有效文档 5.5k）。也就是说，“最终答案只输出源码”这条要求没有被执行。
- **team 三项全部失败**，失败类型这次没有采集到（见下一节）。其中 `parse_duration` 用掉 37 次模型调用、32 次工具调用，说明存在重复工具调用或循环。

### 评测工具自身的问题（已修复，并有测试覆盖）

1. 失败原因过滤正则不允许第二个冒号，`TeamError: team execution failed: ...` 被记成了 `unrecognized`。已放宽为只允许字母、空格、冒号、括号、`->` 等字符。这一轮 team 的失败类型因此丢失，需要再跑才能拿到。
2. HTML 评分器把 `localStorage.getItem(KEY)` 加 `const KEY='todos'` 判成不合格，这种写法符合 spec。已改为同时接受常量形式。
3. 新增宽松诊断：`extract_code` 从带说明文字的答案中提取代码再评分，记为 `lenient_*` 字段。严格判定 `passed` 的规则没有改。

### 下一步建议

1. **交付格式**：让最终答案只包含交付物。可选做法：宿主侧加“交付物契约”（例如要求 final content 为纯代码），或者在最终汇总阶段只输出最后一个步骤的交付内容；属于新的功能条目。
2. **team 失败诊断**：用修复后的运行器只跑 team 三项（3 次运行），拿到失败类型链，并记录修复次数。
3. 在报告中加入 `argument_repair_count` 等修复计数，用来判断参数修复是否起作用。
