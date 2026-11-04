# Harness 合规审查 — 2026-11-04（Claude 接手审查）

对照 [Learn Harness Engineering](https://walkinglabs.github.io/learn-harness-engineering/en/) 的讲义（L03–L12）与 [Templates](https://walkinglabs.github.io/learn-harness-engineering/en/resources/templates/)，检查本仓库能否让 Claude 只凭仓库内容完成接手。
本审查改了管理文档和入口 `init.ps1`，并按用户授权新增最小测试、在本地提交 harness 文件；未改 `app/`、原始专题报告、配置、依赖，未调用模型/DB/MCP，未推送。

## 结论

**可以交接。** Claude 只读仓库就能回答“是什么、怎么跑、怎么验证、做到哪、下一步做什么、什么不能碰”（问答见下文“交接演练”）。
初审时有两项不合规，已在用户授权（2026-11-04）后关闭：

1. **All tests pass**：新增最小测试 `tests/test_harness_smoke.py`（WorkingMemory：plan → commit → 完成；已消费 token 被拒且状态不变），init.ps1 运行 pytest，2 passed。覆盖面很窄，原 1093 个测试仍在不可访问的备份中（见剩余限制）。
2. **Everything is committed**：harness 管理文件、docs 和最小测试已在本地提交（未推送）。工作区里仍有的差异，是用户选择暂不提交的未验收工作（长期记忆源码、README、旧 tests/scripts 删除项），记录在 [基线清单](history/baseline-2026-11-04.json) 中，可用 [baseline.md](baseline.md) 的命令核对（DECISIONS D-010）。

## 逐项对照

| 课程要求 | 来源 | 审查前 | 本轮处理 | 现状 |
| --- | --- | --- | --- | --- |
| 根指令文件：启动流程、工作规则、Definition of Done、收尾 | Templates/AGENTS.md, L04 | 符合；55 行短入口，渐进披露 | 收尾指向 clean-state 清单；启动读决策记录 | 符合 |
| CLAUDE.md 不另立规则 | L04 | 符合 | — | 符合 |
| 仓库是唯一事实来源 | L03 | 基本符合；但设计决策散落在专题报告中 | 新增 [DECISIONS.md](../DECISIONS.md) | 符合 |
| 功能表：id / 可观察行为 / verification / status / evidence；同时只有一个 active | L08, Templates | 字段齐全，init 会校验 active 唯一；状态词汇与课程不同，`done` 的证据是历史结果 | 在 [手册](README.md) 增加状态映射：`done` ≠ 课程 `passing`，除非有 fresh verification；新增 harness-002 | 符合（状态映射写明 done 与 passing 的区别） |
| 功能粒度：一个会话可完成 | L08 | backlog 两项偏大，需拆分 | 在手册中写明：启动前先拆成可在一个会话内完成的条目 | 规则已写明 |
| 进度日志：Current Verified State + 每会话记录（Goal / Completed / Verification / Evidence / Commits / Risks / Next） | Templates/claude-progress.md, L05 | 有日志，但缺 Current Verified State 块和 Commits 字段 | 重排 [progress.md](../../progress.md) | 符合 |
| 交接：Currently verified / Changes / Still broken / Next best action（含不要碰什么）/ Commands（startup、verification、debug） | Templates/session-handoff.md | 缺 Commands 节和明确的 do-not-touch 清单 | 重写 [session-handoff.md](../../session-handoff.md) | 符合 |
| 决策记录：日期 / 决策 / 理由 / 被否方案 / 约束 | L05 | 缺失 | 新增 DECISIONS.md，内容取自现有报告，并标注来源 | 符合 |
| 初始化脚本：install → verify → 打印/运行 start | Templates/init.sh, L06 | 只做 verify；不打印启动路径 | init.ps1 增加打印标准启动/验证路径（只打印不运行），并校验新管理文件 | 部分符合：有意不自动安装（AGENTS 禁止），已记入 DECISIONS |
| 至少一个测试通过 | L06, L12 | 不满足（tests 已删除） | 新增 `tests/test_harness_smoke.py`，init.ps1 运行 pytest，2 passed | 符合；覆盖面窄 |
| 已提交的干净 git 基线 | L05, L06 | 不满足 | 用户授权后本地提交 harness 文件；用户未验收差异记入哈希清单（[baseline.md](baseline.md)） | 符合（剩余差异由用户决定暂不提交） |
| Clean-state 清单：startup 能跑、verification 能跑、progress 已更新、功能表无虚假 passing、没有未记录的半成品、下个会话无需手工修复 | Templates, L12 | 规则分散在 AGENTS | 新增 [clean-state-checklist.md](clean-state-checklist.md) | 符合（测试项标为 N/A 并说明原因） |
| 没有过时产物（debug、临时文件、TODO） | L12 | `app/` 中没有 TODO/FIXME/pdb；`tmp/pytest-harness-*` 有 10 个权限残留（已被 gitignore） | 记录，不改 ACL | 已知限制 |
| 评估 rubric：6 维 0–2 分，Accept/Revise/Block | Templates, L09/L11 | 缺失 | 新增 [evaluator-rubric.md](evaluator-rubric.md)，并给本轮打分 | 符合 |
| 质量文档：按领域和层评 A–D | Templates | 缺失 | 新增 [quality-document.md](quality-document.md) | 符合 |
| 验证分级 / E2E / 不提前宣布完成 | L09, L10 | 符合；手册有 static→E2E 四级，证据矩阵区分了历史与当前 | — | 符合 |
| 可观测性：生命周期、trace、完整错误上下文 | L11 | 未建档；应用有 logging/loguru 与 Team diagnostics，但没有统一 trace/OTel | 在质量文档中记为缺口和建议，不改代码 | 已建档，未实现 |
| 任务契约（scope / verification / exclusions，写代码前定好） | L11 | 手册写了“明确目标、涉及文件和验收”，但没有模板 | 手册增加任务契约模板 | 符合 |

## 交接演练（Claude 只读仓库作答）

| 问题 | 能否回答 | 依据 |
| --- | --- | --- |
| 怎么启动 / 运行 | 能 | session-handoff Commands；PROJECT_HANDOVER §2；init.ps1 输出 |
| 怎么验证 | 能（init.ps1：static/import + pytest） | init.ps1；手册的验证分级 |
| 当前状态 / 最优先未完成项 | 能 | progress Current Verified State；feature_list.backlog |
| 不能碰什么 | 能 | AGENTS 全局不变量；handoff 的 do-not-touch |
| 过去为何这样设计 | 能 | DECISIONS.md（新增） |
| 外部备份是否可用 | **本轮无法确认** | 本会话沙箱访问 `removed.zip` 被拒（Access denied），既无法证明它存在，也无法证明它丢失 |

## 剩余限制（不影响交接，需授权才能继续）

- 旧的完整回归：要恢复 1093 个测试，需要能访问备份并获得授权。
- 用户未验收的源码/README/删除项：按用户选择暂不提交。
- 没有清理 `tmp/` 权限残留，也没有移动 TEMP 备份。
- 没有改功能表中历史条目的 status 词汇：改动会让历史证据含义漂移，所以改用映射说明。
- 结构评分工具（harness-creator）本会话不可用，没有重新打分。

## 验证结果

- `init.ps1`：PASS，exit 0；91 个源码文件语法 + CLI help + 新管理文件存在 + pytest 2 passed；输出 START/VERIFY 路径，启动命令未执行。
- 基线核对命令：HEAD OK，drift none，deleted-restored none（清单在会话结束时重新生成）。
- 受保护文件：126 个（app 源码、原始 docs、README、.env.example、pyproject.toml）的 SHA-256 与本轮开始时一致。
- 活动管理文档的 133 个相对链接全部有效；HEAD 仍为 `9138ac1`；没有提交。
- 评审打分见 [evaluator-rubric](evaluator-rubric.md)：Accept（11/12），Verification 1 分：测试覆盖很窄。
