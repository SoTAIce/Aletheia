# Aletheia 评测集

用固定任务和自动评分，衡量 single 与 team 模式的真实交付能力（capability-phase1-001）。
目的：每次改进后用同一批任务重跑，用通过率说话，而不是凭印象。

## 任务

| id | 类型 | 需要的能力 | 评分 |
| --- | --- | --- | --- |
| `todo_html` | 单文件前端页面 | 读 spec、长代码输出 | 11 项静态检查（结构、id、筛选、localStorage、无外部资源等）；交互行为另在浏览器中人工检查 |
| `parse_duration` | Python 函数 | 精确遵守规则、边界处理 | 21 项隐藏用例，子进程执行 |
| `fix_inventory` | 读代码修 bug | 多文件阅读、对照规格修复 | 16 项隐藏用例，子进程执行 |

每个任务目录里：`task.json`（目标、公开文件、评分器）、公开文件（复制进 agent 的只读根目录），以及 `checker.py`（隐藏用例，不会暴露给 agent）。
目前两种模式都是只读，代码只能放在最终答案里；开放写目录属于阶段 2。

## 运行

```powershell
# 只打印计划，不调用模型
.venv/Scripts/python.exe -m bench.run_bench
# 真实运行（会产生 API 费用，须经用户本轮授权）
.venv/Scripts/python.exe -m bench.run_bench --confirm-live --tasks all --modes single team --runs 1 --out output/bench-report.json
```

- 默认上限 `--max-runs 6`（任务数 × 模式数 × 次数），超过直接拒绝；单次超时 `--timeout 600`。
- 每次运行使用新的 TEMP 根目录，结束后删除；子进程关闭 web search；运行后删除 MCP 子进程新生成的 `app/tools/**/__pycache__`。
- 报告只含状态、次数、耗时、异常类型名和检查结果，不含模型原文。`--keep-answers <dir>` 会保存完成的答案，供浏览器人工检查；用完自行删除。
- `python_checks` 先做 AST 守卫（只允许少量标准库导入，禁止 open/exec/eval/dunder 访问），再用 `python -I` 子进程加超时执行。它只防止误伤，**不是安全沙箱**。

## 文件交付与沙盒模式

- `--write`：智能体把交付物写进临时根目录里的指定文件（`task.json` 的 `deliverable`），评分读取该文件；文件没写或没改动都算失败。
- `--sandbox`：在 `--write` 基础上开放 `run_tests` / `run_python`（Docker 沙盒，需要先构建 `aletheia-sandbox:py313`），并提示智能体先测试再交付；团队模式结束任务前必须有通过的运行。`--team-timeout` 可以加长团队的时间预算。
- 第一版沙盒只支持 Python，`todo_html` 在沙盒模式下仍然只做静态评分。

## 判定口径

- `passed` = 运行状态为 completed，且该任务的全部检查都通过。部分得分只作诊断，不能代替严格交付。
- 样本量小，单次结果不能当作成功率；比较改进时至少用同样的任务、模式和次数。
