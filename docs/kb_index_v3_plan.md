# 建库命令：超长块再切、按路径排除（修改方案）

日期：2026-11-09。状态：**方案，未实现**（kb-index-cli-003-plan）。打算交给 Codex 实现，功能编号 `kb-index-cli-003`。

## 1. 背景

2026-11-09 第一次真实运行 `python -m app.knowledge docs --confirm --prune`（kb-index-cli-002 的真实验证）：68 个文件里 67 个成功，写入 859 块。暴露出两个问题：

1. **超长块写不进去。**`docs/harness/history/2026-11-04-state-before.json` 有 74,247 字符，切成 23 块，其中 3 块超过 Milvus 内容字段的上限（`MilvusClientManager.CONTENT_MAX_LENGTH = 8000`），最长的一块 26,491 字符，写入时 Milvus 拒绝，报成"写入 Milvus 失败，MilvusException"。原因是 `RecursiveJsonSplitter` 遇到很长的单个值切不开，就把它整块交了出来。普通文本切块器按 800 字符切（`chunk_max_size`），离线核对 `docs/` 下其他文件，最大的块是 786 字符、2,027 字节，远低于上限。所以目前只有 JSON 会超长，但长段落的 Markdown 或纯文本理论上也可能碰到，修法应该是通用的。
2. **不该建库的目录没法排除。**`docs/harness/history/` 放的是基线清单，内容基本是文件哈希值，建进知识库只会干扰检索。`EXCLUDED_DIRS` 按目录名匹配，任何叫 `history` 的目录都会被排除，不适合放在这里；需要一个按相对路径排除的办法。

## 2. 要做的修改

### 2.1 超长块再切（修复问题 1）

- `DocumentSplitterService` 加一个构造参数 `max_chunk_bytes`，默认值等于 `MilvusClientManager.CONTENT_MAX_LENGTH`（8000）。按 **UTF-8 字节数** 计算：这样无论 Milvus 按字符还是按字节限制长度，都不会超；中文每个字占 3 字节，按字节算更保守。
  - 默认值写成数字 8000，不要为此在切块器里导入 `app.core.milvus_client`（避免切块器依赖 Milvus 模块）。加一个测试断言两者相等，以后改一边时测试会报错。
- 在 `split_document` 返回之前统一处理（四种文件类型都经过这里，预演的块数也会随之准确）：凡是 `len(page_content.encode('utf-8')) > max_chunk_bytes` 的块，用现有的 `self.text_splitter`（`RecursiveCharacterTextSplitter`，800 字符，最后一级分隔符是空字符串，所以一定能切开）再切一次：
  - 新块复制原块的 `metadata`，保留 `_source`、`_extension`、`_file_name` 以及 Markdown 的标题字段；
  - 顺序保持不变：原来第 k 块被切成的几块，按原顺序放回第 k 块的位置。
- 再切之后，如果仍有块超过上限（按上面的切法理论上不会出现），抛 `ValueError`，消息写明文件和"块超过长度上限"。`index_single_file` 会把它归为 `read` 阶段，命令行显示"读取或切块失败"，而不是写入 Milvus 以后才失败。
- 对 JSON 再切，得到的片段不再是合法的 JSON，这是可以接受的：知识库只拿它们做检索文本。在 D-071 里写明这一点。

### 2.2 按相对路径排除（修复问题 2）

- `VectorIndexService.supported_files(directory, exclude=())`：`exclude` 是相对于 `directory` 的路径（目录或文件，用 `/` 分隔），按**路径段前缀**匹配。比如 `harness/history` 排除 `harness/history/a.json` 和 `harness/history/x/y.md`，但不排除 `harness/history2/a.md`，也不排除 `other/harness/history/a.md`。
  - 规范化输入：去掉首尾的 `/`，把 `\` 换成 `/`。空字符串、绝对路径，或者含 `..` 的路径，一律视为参数错误，抛 `ValueError`。
  - `EXCLUDED_DIRS` 不变，两者同时生效。
- 命令行加 `--exclude 路径`，可以重复给多个。参数错误时输出中文提示，退出码 2，什么也不做。
- 预演和 `--confirm` 都按排除后的文件列表处理；输出里加一行，说明排除了几个文件。
- **和 `--prune` 的关系**：被排除的文件不在本次文件列表里，所以加 `--prune` 时，它们已经建进知识库的块会被当成过时来源清掉。这正是想要的效果（以后用 `--exclude harness/history --prune` 一次就能把清单从知识库里清掉），但要在帮助信息和 `common.md` 里写清楚，免得用户以为排除只是"这次不处理"。
- 不加 `--exclude` 时，行为和现在完全一样。

### 2.3 不改的地方

- 切块参数（`chunk_max_size`、`chunk_overlap`）、`EXCLUDED_DIRS`、支持的扩展名。
- `replace_source`、失败阶段说明、`--prune` 的判断逻辑（kb-index-cli-002）。
- 集合结构和 `CONTENT_MAX_LENGTH`、嵌入模型、依赖、`.env`。

## 3. 测试（确定性测试，不连 Milvus，不调用嵌入接口）

放在 `tests/test_knowledge_cli.py`，或者新建 `tests/test_document_splitter_limits.py`：

1. 一个 JSON 文件里有一个约 3 万字符的长字符串值：切块后每块都不超过 8000 字节，块数比原来多，`metadata` 里有 `_source`，顺序保持。
2. 中文长文本（按字符数不超，按字节数超过 8000）也会被再切。
3. 普通 Markdown 的切块结果和现在**完全相同**，防止改动影响已有文件的块数。用 `docs/` 下一两个真实文件或固定内容断言即可。
4. `max_chunk_bytes` 的默认值等于 `MilvusClientManager.CONTENT_MAX_LENGTH`。
5. 再切后仍超长时抛 `ValueError`，`index_single_file` 把它归为 `read` 阶段（可以把 `max_chunk_bytes` 设得很小来构造）。
6. `supported_files(..., exclude=['harness/history'])`：路径段前缀匹配正确，包括 `history2` 不被排除、不在开头的同名路径不被排除；`\` 和首尾 `/` 能正确规范化。
7. 非法的 `--exclude`（空、绝对路径、含 `..`）：退出码 2，不切块，也不调用向量库。
8. `--exclude` 加 `--prune` 预演：被排除文件已有的来源会被列为将清理的来源；加 `--confirm` 后会被清掉（用假向量库）。
9. 预演输出里有排除了几个文件的那一行；不加 `--exclude` 时输出不变。

再对 `docs/` 跑一次预演（不加 `--confirm`，不加 `--prune`，只切块），确认 `harness/history/2026-11-04-state-before.json` 现在能切块、没有超长块，并且不连接任何服务。另外跑一次 `--exclude harness/history` 的预演，确认文件数正确减少。

完成标准：`init.ps1` 通过，测试数在 1697 passed、16 skipped 的基础上增加。

## 4. 真实验证（Codex 不做，审查后由 Claude 向用户申请）

```powershell
.venv\Scripts\python.exe -B -m app.knowledge docs --exclude harness/history --confirm --prune
```

预期：`harness/history/` 下已经建进去的来源被清掉；其余文件照常替换；不再有写入失败。这一步会调用嵌入接口（计费）。

## 5. 记录（按 AGENTS.md）

- `feature_list.json`：新增 `kb-index-cli-003`，依赖 `kb-index-cli-003-plan`；实现期间 `active_feature` 设为它，完成后改回 `null`。
- `docs/DECISIONS.md`：新增 D-071，写明：按字节限制的理由、为什么在切块器里统一处理、JSON 再切以后不再是合法 JSON、`--exclude` 和 `--prune` 的关系，以及为什么不把 `history` 加进 `EXCLUDED_DIRS`。
- `common.md` 的"知识库建库"一节：加上 `--exclude` 的用法，以及推荐的命令 `--exclude harness/history`。
- `progress.md`、`session-handoff.md`：各加一条；交接的下一步写真实验证待用户授权。
- `AGENTS.md`、`docs/PROJECT_HANDOVER.md`：更新测试数。
- `docs/harness/baseline.md` 和本轮清单：按现有做法记录本轮结束时的未提交状态。

## 6. 给实现者的约束

- 先读 `AGENTS.md`、`session-handoff.md` 顶部最新交接，再读本方案。
- 用户自己没提交的改动不要动，也不要暂存或还原：
  - `README.md`；
  - `app/agent/planner.py`、`app/agent/step_executor.py`、`app/agent/tool_calling_step_runner.py` 里的中文注释；
  - 删掉的 `scripts/benchmark_conversation_memory.py`、`scripts/validate_multi_runner_live.py`、`scripts/validate_summary_context.py`。
- 这次要改的文件里没有用户的未提交改动。
- 不连接真实的 Milvus，不调用嵌入接口或任何真实模型，不运行 `--confirm` 或 `--prune`；不安装或下载，不改 `.env`、模型默认值和依赖。
- 不提交、不推送。
- 跑测试时用 `-p no:cacheprovider` 和指向系统 TEMP 的 `--basetemp`，不在项目里留下 `__pycache__` 或 `.pytest_cache`，不往 `data/` 写东西。
- 记录、文档和命令行输出里给人看的文字都用中文，和现有的一致。
