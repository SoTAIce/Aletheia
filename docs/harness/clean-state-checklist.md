# Clean-State 退出清单

课程 L12：会话完成 = 任务通过验证 **且** clean-state 检查通过。逐项勾选，并写进 progress.md 的本会话记录。任何一项为“否”，都要在 session-handoff.md 中写明原因和恢复方法。

- [ ] **标准启动路径可用**：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1` 通过，输出的启动命令仍然有效。
- [ ] **所有测试通过**：init.ps1 通过（包含 pytest `tests/`）；本轮改了行为，就要在 tests/ 中有对应测试。零收集不能算通过。
- [ ] **progress.md 已更新**：Current Verified State 和本会话记录（Goal / Completed / Verification / Evidence / Commits / Risks / Next）。
- [ ] **功能表与实际一致**：本轮没有 fresh verification 的条目不能升为 done；`active_feature` 与 `in_progress` 一致；新证据写明命令和记录位置。
- [ ] **没有未记录的半成品**：未完成的改动要么已回滚，要么在 handoff 的 “Still broken or unverified” 中逐条写明文件和状态。
- [ ] **没有过时产物**：本轮没有新增 debug 输出、临时文件、注释掉的代码或 TODO；临时文件放在会话 scratchpad 或被 gitignore 的目录中。测试用的 TEMP 目录测完即删（长路径用 `\?\` 前缀），并确认已不存在；仓库内没有新的 `__pycache__`。
- [ ] **新增决策已写入** [DECISIONS.md](../DECISIONS.md)；新增文档已写入 [DOCUMENT_INDEX.md](../DOCUMENT_INDEX.md)。
- [ ] **Git 状态已检查**：`git status --short` 中只有本轮预期的变化；用户原有差异保留；没有授权就不 commit/push。
- [ ] **已提交 / 基线已刷新**：已授权的工作已提交；未提交的用户差异记录在 [基线清单](history/baseline-2026-11-04.json)（或当天的新清单）中，运行 [baseline.md](baseline.md) 中的核对命令，结果应为 drift none。
- [ ] **下个会话无需手工修复**：按 AGENTS.md 启动流程能直接继续。

周期性清理（课程建议每周或每个大阶段一次）：复核 [质量文档](quality-document.md) 的评级，用 [evaluator-rubric](evaluator-rubric.md) 评一次最近完成的任务。
