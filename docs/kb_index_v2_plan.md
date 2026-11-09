# 建库命令改进：安全替换、失败分步说明、清理已删除文件（修改方案）

日期：2026-11-09。状态：**方案，未实现**（kb-index-cli-002-plan）。打算交给 Codex 实现，功能编号 `kb-index-cli-002`。

## 1. 背景

建库命令 `python -m app.knowledge`（kb-index-cli-001，D-068）第一次真实使用时（2026-11-08），Milvus 没有启动，三个文件都只报了 `RuntimeError`，用户看不出是哪一步出错。复查代码时还发现两个问题。三个问题按重要性排：

1. **失败时会丢掉已有的块（数据安全）。**`VectorIndexService.index_single_file` 的顺序是：切块 → `delete_by_source` 删掉这个文件的旧块 → `add_document` 嵌入并写入新块。嵌入或写入只要失败，旧块已经删了，新块又没写进去，这个文件就从知识库里消失了。Milvus 正常、只有嵌入接口出问题时（密钥错误、接口限流），`--confirm` 会一个文件接一个文件地清掉知识库，直到前 3 个都失败才停下。
2. **失败提示看不出是哪一步。**`index_single_file` 把所有异常都包成 `RuntimeError`，命令行只打印类型名。
3. **目录里删掉的文件，它的旧块会一直留在知识库里。**检索时还会被找出来。

## 2. 现状（HEAD `7835fa2`）

- `app/services/vector_index_service.py`：`supported_files`、`_split`、`count_chunks`、`index_single_file`（先删后写，所有异常包成 `RuntimeError(f"failed to index {path}: {exc}")`）、`index_directory`、`close`。
- `app/services/vector_store_manager.py`：
  - `add_document(documents)`：生成 uuid，调用 LangChain `Milvus.add_documents`，在里面先调用嵌入，再插入；
  - `delete_by_source(path)`：先 `connect()`，再按 `metadata["_source"] == path` 删除；
  - `list_documents()`：分页读出整个集合。
- `app/core/milvus_client.py`：`connect()` 失败时抛 `RuntimeError("Milvus initialization failed: ...")`。
- `app/services/vector_embedding_service.py`：
  - 密钥没配：抛 `ValueError("DASHSCOPE_API_KEY is not configured")`；
  - 接口失败：抛 `RuntimeError("batch embedding request failed: ...")`；
  - 返回的条数或维度不对：抛 `ValueError`。
- `app/knowledge/__main__.py`：预演或 `--confirm`；`_failure` 只取异常类型名；前 3 个文件都失败时停止。
- `tests/test_knowledge_cli.py`：10 项，用真实的切块器和假的向量库。

## 3. 要做的修改

### 3.1 先写新块，成功后再删旧块（修复问题 1）

- 在 `VectorStoreManager` 加一个方法 `replace_source(source, documents) -> int`：
  1. 有新块时，先调用 `add_document(documents)` 写入，拿到新的 id；
  2. 写入成功后，删掉这个来源下**不属于新 id** 的块：`metadata["_source"] == "<source>" and id not in [新 id...]`，转义沿用 `_escape_filter_string`；
  3. 没有新块（文件是空的）时，直接删掉这个来源的旧块，和现在一样；
  4. 返回写入的块数。
- `VectorIndexService.index_single_file` 改为调用 `replace_source`，不再先调 `delete_by_source`。
- 效果：嵌入或写入失败时，这个文件原来的块原样保留。写入成功、删除失败时，新旧块会暂时并存，下次重跑会清掉，这比丢数据好。在 D-070 里写明这一点。
- `index_directory` 也会自动用上新的顺序。

### 3.2 失败时说明是哪一步（修复问题 2）

- 在 `vector_embedding_service.py` 加三个异常类。都继承原来的类型，所以现有的捕获和测试不受影响：
  - `EmbeddingConfigError(ValueError)`：没配密钥；
  - `EmbeddingRequestError(RuntimeError)`：接口调用失败（批量和单条都用它）；
  - `EmbeddingResponseError(ValueError)`：返回的条数或维度不对。

  原来抛 `ValueError` 或 `RuntimeError` 的地方改抛这几个子类，消息文字不变。
- 在 `vector_index_service.py` 加 `IndexStageError(RuntimeError)`，带一个 `stage` 属性，取值是下面五个之一。`index_single_file` 不再统一包成 `RuntimeError`，而是按阶段抛它：
  - `read`：读文件或切块失败；
  - `connect`：连接 Milvus 失败。判断方法：`replace_source` 或 `delete_by_source` 抛出的异常是 `milvus_client` 的初始化失败。可以在 `MilvusClientManager.connect` 里也改抛一个子类 `MilvusConnectError(RuntimeError)`，消息不变；
  - `embed_config`：异常链（`__cause__`、`__context__`）里有 `EmbeddingConfigError`；
  - `embed`：异常链里有 `EmbeddingRequestError` 或 `EmbeddingResponseError`；
  - `write`：其他写入或删除失败。
- `IndexStageError` 的消息沿用现在的格式（`failed to index {path}: ...`），所以 `index_directory` 记的内容不变。原始异常用 `raise ... from exc` 保留。
- 命令行 `_failure` 改成"阶段说明 + 原始异常类型"，例如 `失败：intro.md（连接 Milvus 失败，MilvusException）`。原始异常类型取 `__cause__` 的类型名，**仍然不输出任何异常消息或接口原文**（D-033）。阶段说明：

  | stage | 说明 |
  |---|---|
  | `read` | 读取或切块失败 |
  | `connect` | 连接 Milvus 失败 |
  | `embed_config` | 没有配置嵌入密钥 |
  | `embed` | 调用嵌入接口失败 |
  | `write` | 写入 Milvus 失败 |

- 提前停止时的提示，按失败的阶段给出对应建议，替换现在那句笼统的话：
  - 全是 `connect`：请先启动 Milvus，例如 `docker compose -f deploy/milvus/compose.yaml start`；
  - 全是 `embed_config`：请在 `.env` 里配置 `DASHSCOPE_API_KEY`；
  - 全是 `embed`：嵌入接口调用失败，请检查密钥、网络和额度；
  - 混合或其他：沿用现在的笼统提示。
- 预演里"无法切块"的提示不变（只做切块，只会是 `read`）。

### 3.3 `--prune`：清掉目录里已不存在的文件的块（修复问题 3）

- 在 `VectorStoreManager` 加 `list_sources() -> set[str]`：分页读出集合里所有块的 `metadata["_source"]`，做法和 `list_documents` 一样，但只取 `metadata` 字段。
- 命令行加 `--prune` 选项，只处理"在这个目录下、但已经不在本次文件列表里"的来源：
  - 判断"在目录下"用 `source.startswith(root.as_posix() + '/')`，`root` 取 `Path(directory).resolve()`。这样 `docs2/` 不会被当成 `docs/` 下的文件，其他目录建的块也不会被碰；
  - 本次的文件列表就是 `supported_files(directory)` 的结果，所以已经被 `EXCLUDED_DIRS` 排除的目录，或者不再支持的扩展名，它们的旧块也会被清掉。在说明里写明这一点。
- **预演 + `--prune`**：要只读地连一次 Milvus，列出会被清掉的来源和数量，不删除，也不调用嵌入接口。这是预演里唯一会连接服务的情况，要在帮助信息和输出里写明。连接失败时报 `连接 Milvus 失败`，退出码 1。
- **`--confirm` + `--prune`**：先逐个文件建库，**全部处理完、并且没有提前停止**时，再对每个过时的来源调用 `delete_by_source`，逐个输出 `已清理：路径，N 块`。提前停止时跳过清理，并说明原因。
- 汇总行加上清理的数量，例如 `清理：3 个已删除文件，共 41 块`。
- 不加 `--prune` 时，行为和现在完全一样，不连接 Milvus 来列来源。

### 3.4 不改的地方

- 支持的扩展名、`EXCLUDED_DIRS`、切块参数、集合结构、嵌入模型和配置默认值。
- 知识检索的读路径（`knowledge_tool.py`、`rag_search_service.py`）。
- 依赖、`.env`。

## 4. 测试（确定性测试，不连 Milvus，不调用嵌入接口）

在 `tests/test_knowledge_cli.py` 里扩充，`FakeStore` 加上 `replace_source`、`list_sources`，并能按阶段注入失败。另外，`VectorStoreManager.replace_source` 用假的向量库和假的集合单独测。至少覆盖：

1. **安全替换**：`replace_source` 先写后删；写入抛错时不删除，旧块保留；删除表达式只排除新 id，来源路径里的引号和反斜杠被正确转义；空文件只删除旧块。
2. **`index_single_file` 用 `replace_source`**，不再先调用 `delete_by_source`。
3. **五种阶段各自的提示**：读失败、连接失败（`MilvusConnectError`）、密钥未配置、接口失败、其他写入失败，分别输出对应的中文说明和原始类型名；输出里没有异常消息原文（沿用现有测试里 `secret`、`private` 的写法）。
4. **新的异常子类仍然是原来的类型**：`EmbeddingConfigError` 是 `ValueError`，`EmbeddingRequestError` 是 `RuntimeError`，`MilvusConnectError` 是 `RuntimeError`；消息文字不变。
5. **提前停止的提示按阶段变化**：全是连接失败时提示启动 Milvus，全是密钥问题时提示配置 `DASHSCOPE_API_KEY`。
6. **`--prune` 预演**：只调用 `list_sources`，不写也不删；只列出目录下已不存在的来源；`docs2/` 下的来源不算；连接失败时退出码 1。
7. **`--prune` + `--confirm`**：建库完成后删除过时的来源，汇总行带上清理数量；提前停止时不清理，并输出原因。
8. **不加 `--prune`**：不调用 `list_sources`，现有 10 项测试照常通过（期望值随新的失败提示格式调整）。

完成标准：`init.ps1` 通过，测试数在 1644 passed、16 skipped 的基础上增加。另外对 `docs/` 跑一次预演（不加 `--prune`），确认仍然不连接任何服务。

## 5. 真实验证（Codex 不做，审查后由 Claude 向用户申请）

- 预演 + `--prune`：只读连接 Milvus，列出要清理的来源。
- 可选：改一个 `docs/` 下的文件，或者临时删一个测试用的文件，然后跑 `--confirm --prune`，确认块被替换、旧来源被清掉。这一步会调用嵌入接口（计费），由用户决定。

## 6. 记录（按 AGENTS.md）

- `feature_list.json`：新增 `kb-index-cli-002`，依赖 `kb-index-cli-002-plan`；实现期间 `active_feature` 设为它，完成后改回 `null`。
- `docs/DECISIONS.md`：新增 D-070，写明三个问题和各自的做法，尤其是"先写后删"的取舍（删除失败时新旧块会暂时并存）。
- `common.md` 的"知识库建库"一节：加上 `--prune` 的用法，并删掉"从目录里删掉的文件，它原来的块不会自动删除"这句，改成说明 `--prune`。
- `progress.md`、`session-handoff.md`：各加一条；交接的下一步写真实验证待用户授权。
- `AGENTS.md`、`docs/PROJECT_HANDOVER.md`：更新测试数。
- `docs/harness/baseline.md` 和本轮清单：按现有做法记录本轮结束时的未提交状态。

## 7. 给实现者的约束

- 先读 `AGENTS.md`、`session-handoff.md` 顶部最新交接，再读本方案。
- 用户自己没提交的改动不要动，也不要暂存或还原：
  - `README.md`；
  - `app/agent/planner.py`、`app/agent/step_executor.py`、`app/agent/tool_calling_step_runner.py` 里的中文注释；
  - 删掉的 `scripts/benchmark_conversation_memory.py`、`scripts/validate_multi_runner_live.py`、`scripts/validate_summary_context.py`。
- 这次要改的文件里没有用户的未提交改动。
- 不连接真实的 Milvus，不调用嵌入接口或任何真实模型，不安装或下载，不改 `.env`、模型默认值和依赖。
- 不提交、不推送。
- 跑测试时用 `-p no:cacheprovider` 和指向系统 TEMP 的 `--basetemp`，不在项目里留下 `__pycache__` 或 `.pytest_cache`，不往 `data/` 写东西。
- 记录和文档里给人看的文字都用中文，和现有记录一致。
