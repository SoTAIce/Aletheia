# 个性化人设第三步：记忆分区（方案）

日期：2026-11-18。状态：**已实现（2026-11-18，D-089），未提交**；实现时的补充见第 10 节。原状态：方案已定，未实施。用户 2026-11-18“按推荐值定”，随后把第 1 条改成“删除人设时直接删除它的记忆”；其余三条：“默认”人设下记的算公共的；去掉人设的 `memory` 键；Milvus 不改结构。由 Claude 实施。

来源：[个性化人设方案](persona_plan.md) 第 7 节。第一步和第二步已经在 `43e6a39`（D-088）实现。用户 2026-11-18 说“要不先做 AI 档案吧”，所以先做剩下的这一步，JEPA 式“预测再核对”放到后面。

## 1. 要解决的问题

现在长期记忆只按用户分开。学 agent 时记下的“注意力机制还没弄懂”，学后端时也会被召回，反过来也一样。人设分开了指令和默认设置，记忆还混在一起。

目标：每条记忆要么是**公共的**（名字、语言偏好这类，所有人设都用），要么**属于某个人设**。召回时只查公共的和当前人设的。

## 2. 现状（已核实）

- 记录模型 `MemoryRecord`（`app/models/longterm_memory.py`）只有 `user_id`，没有别的归属字段。
- PostgreSQL 是权威数据（D-006）。`memories` 表在 `PostgresMemoryStore.initialize` 里用 `CREATE TABLE IF NOT EXISTS` 建（`postgres_memory_store.py:36`）。关键词检索 `search` 用 `WHERE user_id = $1 AND STRPOS(...)`。
- Milvus 索引 `MemoryVectorIndex` 的结构固定为 `id`、`user_id`、`content`、`vector` 四个字段，建好以后会校验，多一个、少一个都报错（`memory_vector_index.py:99`）。仓库里**没有**从 PostgreSQL 重建索引的工具，D-006 说“可重建”，但实际没写。
- 召回 `MemoryRetriever.retrieve`（`memory_retriever.py`）：关键词和向量各取 `max(limit*3, 10)` 个候选，按排名融合（RRF），**然后逐条从 PostgreSQL 读权威记录**，过期的跳过。所以按归属过滤可以放在这一步做，不用动 Milvus。
- 写入只有两条路：
  - `remember_explicit`：`/remember` 和网页记忆面板，用户自己说要记的；
  - `remember`：模型自动抽取，**生产代码里没接上**（设计审查 3.4 节）。
- 召回只在 `RagAgentService._prepare_task` 里调一次：`recall(user_id=..., query=..., limit=...)`。
- 测试 `tests/test_postgres_memory_store.py` 等都用替身，不需要真实数据库。

## 3. 设计

### 3.1 归属

- `MemoryRecord` 加一个字段 `persona_id: str | None = None`。`None` 表示公共的，现有记录全部是公共的。
- 在“默认”人设下记的算**公共的**（第 8 节第 2 条）。这样不建人设的用户，行为和现在完全一样。
- 去掉人设文件里原来计划的 `memory` 键（第 8 节第 3 条）：每个人设都看“公共的 + 自己的”。一个人设没有自己的记忆时，效果就是只看公共的，不需要单独的开关。

### 3.2 PostgreSQL

- `initialize` 在建表之后加：
  - `ALTER TABLE memories ADD COLUMN IF NOT EXISTS persona_id TEXT`；
  - `CREATE INDEX IF NOT EXISTS idx_memories_user_persona ON memories(user_id, persona_id)`。

  两句都能重复执行，已有数据不用迁移，原有的行 `persona_id` 是空的，也就是公共的。
- `add` 写入 `persona_id`；`_row_to_record` 读它（老库在加列之前读不到，也按空处理）。
- `update` **不改**归属：只改内容，归属沿用原记录。
- `search(user_id, query, limit, persona_scope=ALL)`：给了范围时加条件 `AND (persona_id IS NULL OR persona_id = $4)`；范围是“只看公共的”时加 `AND persona_id IS NULL`。
- `list_by_user` 不过滤，全部列出来，每条带归属，给用户自己看和删。
- 新方法 `count_by_persona(user_id, persona_id) -> int`：删除人设前告诉用户会删掉几条。
- 新方法 `delete_by_persona(user_id, persona_id) -> list[str]`：`DELETE FROM memories WHERE user_id = $1 AND persona_id = $2 RETURNING memory_id`，返回删掉的编号。删除人设时用（第 8 节第 1 条）。

### 3.3 Milvus：不改结构（第 8 节第 4 条）

- 向量索引照旧只按 `user_id` 检索。
- `MemoryRetriever.retrieve` 加参数 `persona_scope`。给了范围时，候选数从 `max(limit*3, 10)` 加到 `max(limit*6, 20)`（上限仍是 100）。融合排序后从 PostgreSQL 读记录时，归属不在范围里的跳过，和现在跳过过期记录是同一个位置。
- 代价：如果某个用户别的人设的记忆特别多，相关的记忆可能挤不进候选。个人使用的规模下，几十到几百条记忆，影响很小。真遇到了，再给 Milvus 加字段并写重建工具。

### 3.4 范围的表示

- 一个哨兵值 `ALL_PERSONAS`（不过滤，所有旧调用方的默认）。
- `None`：只看公共的（“默认”人设）。
- 人设编号：公共的加这个人设的。

`LongTermMemoryService.recall`、`remember`、`remember_explicit` 都加对应的参数，默认值保持旧行为：`recall` 默认 `ALL_PERSONAS`，两个写入默认 `persona_id=None`（公共的）。

`LongTermMemoryService` 另加 `forget_persona(user_id, persona_id) -> int`：先在 PostgreSQL 删掉这个人设的全部记忆，再逐条删 Milvus 里的向量。和现在的 `forget` 一样，数据库先删；索引删失败时抛 `MemoryIndexSyncError`（数据库已经删了），残留的向量不会被召回，因为召回要从 PostgreSQL 读记录。

### 3.5 服务层（`rag_agent_service.py`）

- `_prepare_task` 召回时传范围：有人设而且不是“默认”就传它的编号，是“默认”就传 `None`，人设功能关闭时传 `ALL_PERSONAS`。人设关闭时行为和现在一样。
- 续跑的任务不重新召回（现在就是这样），不受影响。

### 3.6 命令行

- `/remember 内容`：记到当前会话的人设下（“默认”人设下就是公共的）。回复里写明归属，例如“已记住（属于「学 agent」），编号 …”。
- `/remember --shared 内容`：记成公共的。
- `/memories`：每条前面标出归属，`[公共]` 或 `[学 agent]`；属于已删除人设的记录标 `[已删除的人设]`（见第 4 节）。
- `/persona delete 名字`：长期记忆开着而且这个人设有记忆时，先问一句：“人设「学 agent」有 3 条记忆，会一起删除，删了找不回来。输入 y 确认，其他任意输入取消：”。确认后先删记忆，再删人设文件，回复“已删除人设「学 agent」和它的 3 条记忆。”删记忆失败就不删人设，提示失败的类型名。没有记忆时不问，直接删。
- `memory_command` 和 `persona_command` 都要拿到长期记忆服务和人设存储；交互循环已经有这两样，只是要传进去。

### 3.7 网页

- `POST /api/memories` 的请求体加 `session_id`（可选）和 `shared`（可选，布尔值）。给了 `session_id` 而且不是 `shared` 时，记到这个会话的人设下。不直接收人设编号，免得记到一个已经删掉的人设名下。
- `GET /api/memories` 每条多 `persona_id` 和 `persona_name`（公共的为空，人设已删除时 `persona_name` 为空、`persona_id` 保留）。
- `GET /api/personas/{id}` 多一个 `memories`（这个人设的记忆条数，长期记忆没开时为空），给页面的确认框用。
- `DELETE /api/personas/{id}` 和命令行一样，先删记忆再删人设；删记忆失败时人设不删，返回错误。
- 页面删除人设的确认框写明：“删除人设「学 agent」？它的 3 条记忆会一起删除，删了找不回来。用它的会话会回到‘默认’。”
- 页面：
  - 记忆面板的输入框下面加一个“只给当前人设”的勾选框。当前是“默认”人设时隐藏，因为那时记的本来就是公共的；
  - 列表每条显示归属。

## 4. 边界情况

- **人设关闭（`PERSONAS_DIR` 留空）**：召回不过滤，`/remember` 记成公共的。以前在人设下记的记忆这时也会被召回，因为没有当前人设可比。
- **长期记忆没开时删除人设**：数据库连不上，删不了记忆。照样删人设，确认框里说明“长期记忆没开，它在数据库里的记忆（如果有）不会删”。这些记忆成了“已删除的人设”的记忆：不会被任何人设召回，`/memories` 和网页里仍然列出，标“已删除的人设”，用户可以 `/forget` 掉。
- **手动删掉人设文件**：同上，这些记忆按“已删除的人设”处理。不自动删，因为宿主不知道文件是用户有意删的还是一时改坏了。
- **自动抽取 `remember`**：参数加上、默认公共的，生产代码里仍然不调用。以后接上时，归到当前人设。
- **记忆内容**：归属只是一个编号，不改变召回后发给模型的格式（`longterm_context` 不变），所以黄金对照文件不受影响。

## 5. 不做的

- Milvus 加字段、写重建工具（第 3.3 节，等真有需要）。
- 把已有的公共记忆批量归到某个人设，比如 `/memories move`。用户可以 `/forget` 后在对应人设下重新 `/remember`。
- 接上自动记忆。

## 6. 测试

- `tests/test_postgres_memory_store.py`（替身连接）：
  - `initialize` 执行了加列和加索引，两句都带 `IF NOT EXISTS`；
  - `add` 写了 `persona_id`；
  - `update` 的语句不碰 `persona_id`；
  - `search` 在三种范围下的语句和参数；
  - `count_by_persona` 和 `delete_by_persona` 的语句、参数和返回值；
  - 没有 `persona_id` 列的老行读成公共的。
- `tests/test_memory_retriever.py`：
  - 给了范围时，别的人设的记录被跳过，公共的和当前人设的留下；
  - 候选数在给了范围时变大；
  - 不给范围时和现在完全一样。
- `tests/test_memory_service.py`：`recall`、`remember_explicit` 的新参数和默认值；`forget_persona` 先删数据库再删索引，索引失败时抛 `MemoryIndexSyncError`。
- 服务层：
  - 有人设时召回带它的编号，“默认”人设带 `None`，人设关闭时不过滤；
  - 续跑不重新召回。
- 命令行：`/remember`、`/remember --shared` 的归属；`/memories` 的标记；删除有记忆的人设时先确认，确认后记忆和人设都删掉，取消时都不删，删记忆失败时不删人设；没有记忆时不问。
- 网页：`POST /api/memories` 的 `session_id` 和 `shared`；列表的归属字段；人设详情的记忆条数；删除人设时记忆一起删，删记忆失败时人设还在。
- `app.js` 通过 `node --check`；`init.ps1` 通过；黄金对照文件一个都不变。

## 7. 文档和记录

- `docs/DECISIONS.md` 新增 D-089：归属字段、默认人设下记的算公共的、在 PostgreSQL 过滤而不改 Milvus、删除人设时先确认再连记忆一起删。被否方案：
  - Milvus 加字段：要改结构、要重建，仓库里还没有重建工具；
  - 每个人设一套独立的记忆，不要公共部分：名字、语言偏好这类要在每个人设里各记一遍；
  - 删除人设时记忆转成公共的：这个人设的私有记忆会混进所有人设，正是分区要避免的（用户 2026-11-18 否掉）；
  - 有记忆时不让删人设：用户得先一条条 `/forget`。
- [个性化人设方案](persona_plan.md) 第 7 节和第 11 节注明以这份为准；`docs/PROJECT_HANDOVER.md`、`docs/modules/13-longterm-rag.md`（照旧不提交）补上说明。

## 8. 要用户定的四件事

用户 2026-11-18 先按推荐值定了四条，随后把第 1 条改成**直接删除**：删除人设时，它的记忆一起删掉。删了找不回来，所以有记忆时先确认一次（第 3.6、3.7 节）。其余三条按推荐值。

1. **删除人设时它的记忆怎么办**：**用户定了直接删除**。原来推荐转成公共的，不丢数据，回复里说明转了几条，用户不想要可以 `/forget`。另外两种：一起删掉（找不回来），或者有记忆时不让删（用户得先一条条处理）。
2. **“默认”人设下记的算不算公共的**：推荐**算**。这样不建人设的用户什么都不变；要是“默认”也有自己的私有记忆，就没有地方能记公共的东西了，除非每次都加 `--shared`。
3. **去不去掉人设的 `memory` 键**：推荐**去掉**。每个人设都看公共的加自己的，没有自己的记忆时就只看公共的，不需要这个开关。
4. **Milvus 改不改结构**：推荐**不改**，在从 PostgreSQL 读记录时过滤，候选数加大一倍。另一种是给索引加 `persona_id` 字段，召回更准，但要改结构、写重建工具，已经在用的索引要重建一次。

## 9. 约束

- 不改 `.env`、模型默认值和依赖，不调用真实模型。
- 数据库结构的变化只有加一列、加一个索引，都可以重复执行，不删、不改已有数据。
- 用真实的 PostgreSQL 和 Milvus 验证，要用你本机的数据库，而且写入时会调用向量化接口。这一步要你同意后才做；不做的话，在记录里写明“只用替身测过”。
- 用户自己删掉的 `scripts/benchmark_conversation_memory.py`，以及还没提交的 `docs/modules/`、`docs/DOCUMENT_INDEX.md`、两份报告，提交时都不带。
- 提交和推送要用户同意。

## 10. 实现时的补充

- **长期记忆没开时删除人设**：方案说确认框里写“长期记忆没开，它在数据库里的记忆不会删”。实现时没有加这句：命令行只要配置了数据库就会打开长期记忆，网页也一样，所以“配了数据库却没开”的情况实际上不会出现；没配数据库的用户每次删人设都看到这句，只会添乱。
- **“默认”人设的删除**：网页和命令行在碰记忆之前就拒绝，不会先删掉记忆再发现人设不能删。
- **网页人设详情**：`memories` 是删除时会一起删掉的条数；“默认”人设和长期记忆没开时为空。
- **命令行的替身服务**：交互循环只把真正的 `PersonaStore` 当作“人设开启”，测试里用的替身服务不会被误认。
- **`/persona` 和 `/btw` 改成异步**：删除时要问用户、要调长期记忆服务。
- 用假的长期记忆在本机起网页看过：勾选框显示“只给「学 agent」”，记下的记忆标着“学 agent”，删除确认框写“它的 1 条记忆会一起删除，删了找不回来”。控制台没有报错。
- 没有连真实的 PostgreSQL 和 Milvus。黄金对照文件一个都没变。
