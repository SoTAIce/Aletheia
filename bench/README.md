# Aletheia 评测集

用固定任务和自动评分，衡量 single 与 team 模式的真实交付能力（capability-phase1-001）。
目的：每次改进后用同一批任务重跑，用通过率说话，而不是凭印象。

## 任务

共 24 个任务：调参集 11 个，留出集 13 个（D-035）。

| 划分 | 类别 | 任务 |
| --- | --- | --- |
| `tune` | 只读问答 | `billing_qa`、`config_merge_qa`、`log_errors_qa`、`sales_csv_qa` |
| `tune` | 按规格实现 | `merge_intervals`、`parse_duration`、`semver_compare` |
| `tune` | 对照规格修复 | `fix_inventory`、`fix_lru_cache`、`fix_rate_limiter` |
| `tune` | 前端页面 | `todo_html` |
| `holdout` | 只读问答 | `call_chain_qa`、`deps_doc_qa`、`edge_eval_qa`、`failing_tests_qa`、`role_perms_qa`、`side_effects_qa`、`spec_mismatch_qa` |
| `holdout` | 按规格实现 | `business_days`、`csv_line`、`expr_eval` |
| `holdout` | 对照规格修复 | `fix_split_amount`、`fix_topo_sort`、`fix_word_wrap` |

- 只读问答：答案是一个 JSON 对象，每个键一项检查，并检查根目录没有被改动。
- 实现和修复：隐藏用例在子进程中执行；修复类的原始文件本身不能通过。
- `todo_html`：11 项静态检查，交互行为另在浏览器中人工检查。

每个任务目录里：`task.json`（目标、公开文件、评分器）、公开文件（复制进 agent 的只读根目录），以及隐藏的评分文件（`checker.py`、`expected.json`、`reference.py`，不会暴露给智能体）。
默认交付方式是最终答案；`--write` 和 `--sandbox` 改为写文件交付，见下文。

## 运行

```powershell
# 只打印计划，不调用模型
.venv/Scripts/python.exe -m bench.run_bench
# 真实运行（会产生 API 费用，须经用户本轮授权）
.venv/Scripts/python.exe -m bench.run_bench --confirm-live --tasks all --modes single team --runs 1 --out output/bench-report.json
```

- 默认上限 `--max-runs 6`（任务数 × 模式数 × 次数），超过直接拒绝；单次超时 `--timeout 600`。
- 每次运行使用新的 TEMP 根目录，结束后删除；子进程关闭 web search；运行后删除 MCP 子进程新生成的 `app/tools/**/__pycache__`。
- 每次运行让命令行用 `--result-json` 把结果写到智能体根目录之外，评测优先读这份结果；读不到或格式不对时回退到解析标准输出（行里的 `result_source` 说明来源）。报告每行附带失败类型、令牌数、模型调用次数和工具调用次数（D-034）。
- 报告只含状态、次数、耗时、异常类型名和检查结果，不含模型原文。`--keep-answers <dir>` 会保存完成的答案，供浏览器人工检查；用完自行删除。
- `python_checks` 先做 AST 守卫（只允许少量标准库导入，禁止 open/exec/eval/dunder 访问），再用 `python -I` 子进程加超时执行。它只防止误伤，**不是安全沙箱**。

## 文件交付与沙盒模式

- `--write`：智能体把交付物写进临时根目录里的指定文件（`task.json` 的 `deliverable`），评分读取该文件；文件没写或没改动都算失败。
- `--sandbox`：在 `--write` 基础上开放 `run_tests` / `run_python`（Docker 沙盒，需要先构建 `aletheia-sandbox:py313`），并提示智能体先测试再交付；团队模式结束任务前必须有通过的运行。`--team-timeout` 可以加长团队的时间预算。
- 第一版沙盒只支持 Python，`todo_html` 在沙盒模式下仍然只做静态评分。

## 调参集与留出集（D-035）

- 每个任务的 `task.json` 有 `split`：`tune` 是调参集，改进智能体时可以反复看它的结果和失败原因；`holdout` 是留出集，只用来在改进定稿后验证一次，不要根据它的失败去改提示词或规则，否则它就不再能说明泛化能力。
- `--tasks all` 默认只取调参集；留出集要显式加 `--split holdout`（或 `--split all`）。按 id 点名的任务不受划分限制。
- `probe` 是按需委派的探针题（`probe_config_keys`、`probe_event_flow`，几十个文件分在子目录里），只用 `--split probe` 运行，`--split all` 不包含它。`--delegate` 让单一循环可以委派只读调研（docs/loop_delegate_plan.md），报告每行多出 `delegations`、`delegate_requests`、`delegate_calls`。
- 每个任务还有 `category`（`qa`、`implement`、`fix`、`frontend`）。代码类任务带隐藏的参考解 `reference`，`tests/test_bench_tasks.py` 会检查参考解能通过全部检查、修复类的原始文件至少失败一项，且隐藏文件都不在 `files` 里。
- 服务商状态（D-079，[方案](../docs/provider_state_plan.md)）：每行有 `provider_state`（第一次调用的档：`dropped` 报告的比发出的少、`plain` 相当、`padded` 加了少量内容、`heavy` 加了大量内容、`unknown`）、`provider_states`、逐次的 `provider_ratios`（报告值 ÷ 本机估算）和 `model_mismatch`；报告的 `by_provider_state` 按“档/模式”汇总通过数，`model_mismatch_runs` 是回答模型和请求不同的运行数。中转站的隐藏内容会改变结果，比较两次评测时只在同一档里比。
- 报告的 `summary` 每格附 `ci95`（Wilson 95% 区间）；`by_split` 按“划分/模式”汇总全部运行的通过率和区间。不同任务难度不同，跨任务的区间只能粗略反映抽样误差；比较两个版本时用同一批任务、同样次数，区间重叠时不能断言谁更好。

## 判定口径

- `passed` = 运行状态为 completed，且该任务的全部检查都通过。部分得分只作诊断，不能代替严格交付。
- 样本量小，单次结果不能当作成功率；比较改进时至少用同样的任务、模式和次数。
