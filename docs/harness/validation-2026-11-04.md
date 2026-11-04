# Harness / Claude 交接验收 — 2026-11-04

## 范围与结果

管理文件、交接文档和只读健康入口；不是 Agent 新功能或可靠性修复。业务源码、原始 docs、配置和依赖保持原样。用户确认无额外目标/约束。

| 用户要求 | 交付与验收 |
| --- | --- |
| 参考指定 Harness Engineering 管理项目 | AGENTS + CLAUDE 共用入口、功能状态、当前交接、执行日志、init.ps1；维护手册链接课程原文并解释适配 |
| 汇总 docs 为交接文档 | PROJECT_HANDOVER 覆盖执行链、工具/MCP/RAG、四类记忆职责、Team、证据和风险；DOCUMENT_INDEX 覆盖全部原 31 篇文档 |
| 修订 AGENTS.md 不足 | 55 行短入口，通用 Done、全局不变量、权限边界、验证分级及渐进读取；移除旧单任务绑定 |
| 保留 docs 和当前核心代码 | 对整理前记录逐文件 SHA-256 比对；原 app/docs/.env/.env.example/pyproject.toml 未变；旧管理文件原文另存历史 JSON |

## 实际检查

1. `powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1`：通过，91 个 Python 源文件语法通过，CLI `--help` 展示 single/team 等参数并正常退出。
2. 初次入口确实失败：功能表 team-poc-001、team-poc-002、team-v1-001 缺少 description，抛 KeyError 后 PowerShell 非零退出；补齐历史记录描述后通过。没有绕过失败或把缺少测试当成功。
3. 保护检查：历史 snapshot 的 125 个 protected_hashes 全部一致；原 31 篇 docs 均被文档索引覆盖；不修改 app 或原报告来消除矛盾。
4. 新活动 Markdown 的 87 个相对链接检查全部有效：涵盖 AGENTS/CLAUDE/README/当前状态/总交接/索引/手册/本计划/本记录；不要求历史报告中的已归档 artifact 路径仍存在。
5. 外部备份只读检查：removed.zip 存在、可列目录，68,384 条，包含 tests/agent/test_team_v1.py；本轮不重新解压/全量重算内容哈希，不发送日志。
6. 独立只读代码/文档审查：主要状态、真实对照、长期记忆接入、宿主契约、MCP/恢复边界与源码一致；实际重复运行入口通过。采纳唯一精度修正：非 Lead Session 版本上限 12，Lead 用 actions 上限。
7. Git HEAD 与整理前一致；现有业务差异及清理删除项保留，没有新增 commit/push。

## 结构评分（不能替代以上验收）

运行安装的 `harness-creator/scripts/validate-harness.mjs --target D:/tries/agent --json`：整理前 80/100，整理后 84/100。
后者分项 instructions 5/5、state 5/5、verification 3/5、scope 5/5、lifecycle 3/5。
该启发式只识别 init.sh，不识别本项目实际运行的 init.ps1，并检查特定英文 restart 标记；不为取满分添加冗余入口或改写事实。
分数提高只表示部分文档/结构字段更符合启发式，不能证明应用成功率、事实准确性、安全或性能提高。

## 新会话接手检查

| 新维护者的问题 | 可直接找到的答案 |
| --- | --- |
| 系统是什么？ | 总交接 §1：Plan–Execute–Replan 的 Python Agent/RAG，single/team 平级 |
| 怎么组织？ | §3–7 的源码路径、职责与调用链 |
| 怎么运行？ | §2 和 README；实际 CLI help 检查通过，真实服务运行未重测 |
| 怎么验证？ | init.ps1 + 手册验证分级；tests 缺失与恢复位置明确 |
| 进度及下一步？ | session-handoff/feature_list；三组对照结束、未提交长期记忆部分接入、建议待办未授权 |

以上为仓库资料可回答性核对和独立只读审查，不声称完成多轮真实 Claude 使用效果对照。

## 没有执行与剩余限制

- 没运行真实模型、MCP、PostgreSQL/Milvus 或新 E2E，没有把旧 1093/2 写成当前 fresh regression。
- tests/验证脚本按用户要求仍未恢复；后续行为改动不能仅靠 init.ps1 宣称正确。
- TEMP 备份不是长期留存；未跟踪原始实验仍依赖它。10 个权限目录残留本轮不继续处理。
- 没安装依赖、下载模型、改默认配置、删业务数据、提交或推送。
