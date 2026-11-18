# 第二项增强：让评测能说明问题（方案）

日期：2026-11-18。状态：**已实现并提交（2026-11-18，D-086）**。用户 2026-11-18 确认方案，并让 Claude 实施。

来源：这是用户 2026-11-18 同意的第二项功能增强，同样一步一步做，先出方案再实现。对应设计审查（`docs/agent_design_review.md`，暂未入库）第 9 节“先做的三件事”的第 2 件，覆盖 3.2、4.1 两节，以及 4.3 的统计部分。

## 1. 要解决的问题

1. **评测走的不是产品的默认路径**（审查 3.2）：`bench/run_bench.py:199-201` 给子进程设了 `CONVERSATION_STORAGE_DIR=''`，所以评测里没有对话归档，后果有两个：
   - 超长的工具结果永远整段进上下文；
   - `read_context_artifact` 不会注册（`rag_agent_service.py:633-635`）。

   产品默认是开归档的（`app/config.py:75`）。所以 D-052 的转存、预览和按偏移读回，从来没在真实模型下评测过。
2. **一次运行分不清是噪声还是回退**（审查 4.1）：`--runs` 默认 1。11 题跑一次时，Wilson 95% 区间宽约 ±25 个百分点，9/11 和 11/11 之间没有统计差异。报告里既没有“k 次都过”这一项，也没有失败归因：服务商出错导致的失败，和智能体自己的失败算在同一个分母里。
3. **服务商状态没有写进结论**（审查 4.3 的统计部分）：`by_provider_state` 只按“档/模式”给出通过数，全报告的状态分布却没有一行说清楚，每次都要人工去拼。

## 2. 改什么

### 2.1 每次运行一个临时归档（`bench/run_bench.py`）

- `run_one` 在本次的临时目录里建 `work/archive`。它和智能体的根目录 `work/root` 是兄弟目录，不在根目录里面，智能体的文件工具看不到它。子进程的 `CONVERSATION_STORAGE_DIR` 指向这个目录，运行结束随 `work` 一起删除。不写 `data/`，也不留在仓库里。
- 新开关 `--no-archive`：恢复现在的做法（`CONVERSATION_STORAGE_DIR=''`），用来跑“无归档”对照。计划行里写出 `archive=on|off`。
- 每行新增三个字段，都只是宿主统计的计数，不含任何正文：
  - `archive`：`true` 或 `false`；
  - `archived_tool_results`：本次转存的工具结果个数，数的是 `work/archive/session-*/artifacts/` 下的文件，在删除目录之前数；
  - `artifact_reads`：工具记录里 `read_context_artifact` 被调用的次数，来自 `--result-json` 的 `steps`。
- 不受影响的部分：
  - 后台对话摘要只在 `--compress-history` 时才启用（`app/agent/__main__.py:298`），评测不传这个参数，所以不会多出模型调用；
  - 审计记录（`hooks_audit`）会写进这个临时归档，随目录一起删除；
  - 每次运行的会话编号本来就各不相同，不会读到别的运行留下的历史。
- 预期效果要说实话：调参集的文件都很小，大多数运行不会触发转存，`archived_tool_results` 多半是 0。这一项的作用是让评测和产品走同一条路径，模型看到的工具清单也一样（多了 `read_context_artifact`）。真正触发转存要等审查 4.2 补长上下文题，本方案不加题。

### 2.2 失败归因（`bench/run_bench.py`）

每行新增 `attribution`，通过的行没有这个字段。取值只看宿主给出的字段，不看模型原文：

| 取值 | 条件 |
| --- | --- |
| `provider` | `failure_kind == 'model'`，且失败行里的类型名不含 `ModelOutputLimitError`、`ModelToolArgumentsError`。也就是重试用完后服务商仍然出错、超时或回了无法解析的响应 |
| `grader` | 评分本身抛了异常。现在这种异常会让整次评测中断，改成在 `run_one` 里捕获，行里记 `grader_error`（只记类型名），本行判为不通过 |
| `unknown` | 评测在 `--timeout` 到点时杀掉了子进程（`status == 'timeout'`），或者结果只能从标准输出解析、失败行又认不出来（`failure == 'unrecognized'`） |
| `agent` | 其余所有不通过：`context`、`tool`、`timeout`、`budget`、`limit`、`business`、`side_effect`、`format` 这些失败类型；运行完成但严格评分没过；交付物没写 |

几点说明：

- `ModelOutputLimitError` 是模型自己写超了输出上限，`ModelToolArgumentsError` 是修复两次后参数仍然不对，这两种归智能体。
- 沙箱里 Docker 出问题时失败类型是 `tool`，会被归到 `agent`。这是已知的粗糙之处，在 README 里写明。本方案不靠解析文字去细分。
- 归因规则写成一个纯函数 `attribute(row)`，方便单测，以后也方便调整。

### 2.3 报告的统计（`bench/run_bench.py`）

- **`summary`**（按“题/模式”）每格新增：
  - `all_passed`：`passed == runs`；
  - `provider_failures`、`grader_failures`：两类失败各自的次数。
- **`by_split`**（按“划分/模式”）每格新增：
  - `runs_per_task`：`[最少, 最多]`，两个数不相等时下面的“k 次全过”没有意义，照样算，但会在 `headline` 里注明；
  - `tasks_all_passed`：k 次都过的题数，以及 `all_passed_rate` = `tasks_all_passed / tasks`；
  - `excluded`：`{'provider': n, 'grader': n}`；
  - `agent_runs`、`agent_passed`、`agent_pass_rate`、`agent_ci95`：分母去掉服务商和评分导致的失败之后的结果。原来的 `passed`、`pass_rate`、`ci95` 保留不变，两套数一起给。
- **`provider_state_counts`**：全报告按第一次调用的状态（`dropped`、`plain`、`padded`、`heavy`、`unknown`）分别计数。
- **`headline`**：每个“划分/模式”一行中文，文字全由宿主拼出，只含数字。例如：

  > `tune/single`：33 次运行通过 29 次（88%，95% 区间 72%～95%）；每题 3 次，全过 9/11 题；不计服务商失败 1 次、评分失败 0 次后为 29/32（91%，区间 76%～97%）；服务商状态 heavy 30、padded 3；回答模型和请求不同 33 次。

  每题次数不一致时，“每题 3 次”改成“每题 1～3 次，全过比例仅供参考”。
- 保持不变的：`--runs` 默认值仍是 1，`--max-runs` 默认值仍是 6，所以不会悄悄多花钱。README 里另写一条验收用的命令：调参集单一模式，`--runs 3 --max-runs 33`。

### 2.4 不做的

- 审查 4.3 的第 1 条（直连服务商对照）：要第二个服务商，也要花钱，留到以后。
- 审查 4.3 的第 2 条（`model_mismatch` 只在和上次不同时才提示）：这是产品命令行和网页上的提示，要记住“上次”是什么，不属于评测统计，放进以后的可观测性改进。
- 审查 4.2 的补题（长上下文、两轮、追问）：工作量是中等，单独做。
- 比较两份报告的工具（读两份报告、判断区间是否重叠）：有用，但不在本步范围内。`headline` 里已经有区间，可以人工比较。
- 失败行的识别正则（`_SAFE_FAILURE`）不改：业务失败的消息里夹带模型写的原因，放宽规则就可能把模型原文带进报告。归因只看 `failure_kind`，用不着放宽。

## 3. 测试

新建 `tests/test_bench_report.py`，只用假的 `launch_agent`，不调模型。测试覆盖：

1. **归档目录**：
   - 子进程拿到的 `CONVERSATION_STORAGE_DIR` 在本次临时目录里，不在根目录里面，运行完被删除；
   - 加 `--no-archive` 时这个变量是空字符串；
   - 假子进程在 `artifacts/` 下写两个文件，行里 `archived_tool_results == 2`；
   - `steps` 里有两次 `read_context_artifact`，行里 `artifact_reads == 2`。
2. **`attribute`**：按上表逐条用参数化测试覆盖，包括 `ModelOutputLimitError` 归智能体、`status == 'timeout'` 归 `unknown`、通过的行没有这个字段。
3. **评分异常**：评分函数抛 `RuntimeError` 时，`run_one` 返回不通过的行，行里有 `grader_error == 'RuntimeError'` 和 `attribution == 'grader'`，不会把异常往外抛。
4. **统计**：
   - 两题各跑 3 次，一题全过、另一题过 2 次，`tasks_all_passed == 1`；
   - 混入一次服务商失败，`agent_runs` 少 1，`agent_ci95 == wilson(...)`；
   - 每题次数不一致时，`runs_per_task` 写出最少和最多两个数，`headline` 带上“仅供参考”。
5. **`headline`**：检查它只由数字和固定中文拼成，行里塞进带特殊字符的字段时，原样不进 `headline`。
6. **`provider_state_counts`**：计数正确，无法识别的状态记为 `unknown`。

要改的现有测试：

- `tests/test_archive_default.py::test_bench_agent_processes_do_not_archive` 改名为 `test_bench_agent_processes_archive_under_their_temp_dir`，断言换成上面第 1 条。`--no-archive` 的情况另写一个测试。
- `tests/test_bench_qa.py:92-95`、`tests/test_bench_tasks.py:95-103`：这两处对 `summary` 和 `split_summary` 的结果做了整体相等的断言，要补上新增的键。

`init.ps1` 必须通过。

## 4. 文档和记录

- `bench/README.md`：写清临时归档和 `--no-archive`、`attribution` 的取值表和已知的粗糙之处、新增的统计字段和 `headline`、验收命令（`--runs 3 --max-runs 33`）。
- `docs/DECISIONS.md`：新增 D-086，写起因、改了什么、归因规则、不做的部分和被否方案。被否的两个方案：
  - 把 `--runs` 默认改成 3：会悄悄多花钱；
  - 服务商失败自动重跑：用户以前决定过不自动重跑。
- `docs/PROJECT_HANDOVER.md`：评测相关的那一段补一句。
- `docs/modules/16-bench.md`：同步代码摘录。这个目录本身还没提交，照旧不提交，但内容保持同步。

## 5. 约束

- 不改 `.env`、模型默认值和依赖，不调用真实模型，不跑评测。实现只用离线测试验证。
- 智能体本身的代码不改，只改 `bench/` 和测试。所以黄金对照文件一个都不应该变；如果变了，说明改错了地方。
- 用户自己删掉的 `scripts/benchmark_conversation_memory.py`，以及还没提交的 `docs/modules/`、`docs/DOCUMENT_INDEX.md`、两份审查报告，提交时都不带。
- 真正的 k=3 调参集评测（33 次运行，要花令牌费用）是单独的一步，实现完再请用户授权，不在本方案里自动跑。
- 提交和推送要用户同意。

## 6. 实现时的补充

- 除了严格评分，宽松评分（`lenient`）和失败行上对已写文件的诊断评分也会调用评分器。这两处出异常时，只记 `lenient_error` 或 `file_checks_error`（类型名），不改变这一行的严格结果，也不影响归因。
- 每次运行结束时，`headline` 的几行也会打印到标准错误输出，方便直接看。
