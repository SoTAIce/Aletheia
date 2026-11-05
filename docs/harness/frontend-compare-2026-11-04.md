# single 与 team 真实模型对照：单文件前端页面（frontend-compare-001）

日期：2026-11-04。用户本轮授权真实模型调用。模型：`.env` 中的 `qwen3.8-flash`（JSON mode 开，thinking 关）。不改 `app/`、`.env`、模型默认值。

## 设置

- 任务：读取沙箱根目录的 `spec.md`，实现单文件 HTML 待办页面（新增/Enter/拒绝空输入/完成切换/删除/三种筛选/未完成计数/localStorage/viewport，不得引用外部资源），最终答案只输出完整 HTML。
- 两种模式用同一目标和同一 spec，各跑 **1 次**，不重试：
  `python -B -m app.agent "<目标>" --filesystem-root <TEMP 沙箱> --no-knowledge --execution-mode single|team`
- 两种模式都是只读（没有 `--write-root`；Team 的默认工具白名单本来就不含 `write_file`），HTML 只能放在最终答案里。web search 关闭（仅在子进程环境中把 `TAVILY_API_KEY` 置空）。
- 上限沿用默认：max-executions 16、max-replans 2；Team 48 次模型调用、180 秒。

## 结果

| 模式 | 状态 | 执行/重规划 | 耗时 | 失败类型（只记类型） | 产出 HTML |
| --- | --- | --- | --- | --- | --- |
| single | **failed**（exit 1） | 2 / 0 | 37 s | `StepModelError: Model generation failed`（`tool_calling_step_runner.py:130`，底层是非超时的 `ToolCallingModelError`，CLI 不输出具体子类） | 无 |
| team | **failed**（exit 1） | 1 / 0 | 45 s | `TeamError` → `ModelResponseError` → `JSONDecodeError`；模型调用 7 次、工具调用 4 次 | 无 |

**严格交付：single 0/1，team 0/1。** 两边都没有产出 HTML，所以 spec 中 7 条功能要求的浏览器验收都没有可检查的对象，全部记为未通过。

## 分析（基于代码，未重跑验证）

- team 的 `JSONDecodeError` 是 `ModelResponseError` 的成因：在 `openai_compatible_tool_calling_model.py` 中，tool call 参数不是合法 JSON 时会抛出这个异常（`_parse_arguments`，约 :218-250）。推测是模型在 tool call 参数里放大段 HTML 时 JSON 转义出错（诊断只记类型，没有记原文，所以无法直接确认）。
- single 失败在模型生成层，也不是超时。CLI 不打印异常子类，所以无法确认它和 team 是不是同一个原因。
- 两次失败都发生在模型响应/格式层，还没有到页面质量这一步。team 这一次与 2026-11-03 对照中内层 JSON 格式失败的现象一致。
- 样本量为 1，不能得出成功率结论。

## 可能的改进方向（未授权，仅记录）

1. 开放受限的 `--write-root`，并允许 Team 使用 `write_file`，让模型直接写文件，不必把 HTML 塞进 JSON 字符串。
2. 在 CLI 和诊断中输出 `ToolCallingModelError` 的子类型（不含原文），方便定位。
3. 换更大的模型，或者开启 thinking 后再对照。

## 清理

TEMP 沙箱 `aletheia-fe-*`（spec 和 stdout/stderr）已删除，并确认不存在；MCP 子进程生成的 `app/tools/__pycache__`、`app/tools/filesystem/__pycache__` 已删除。没有记录密钥或模型原文。
