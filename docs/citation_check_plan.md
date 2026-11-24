# 引用核对与可点开的来源（方案）

日期：2026-11-23。状态：**已实施，未用真实模型验证**（D-098）。第 8 节八条按推荐定（用户 2026-11-23）。由 Gemini（Antigravity 命令行，`gemini-4-neon-medium`）实施，Claude 写实施说明并核对，Gemini 结束前清理无关文件（用户 2026-11-20、2026-11-23 定）。

来源：三阶段收尾计划 3.5 的第二项（`docs/batch_run_plan.md` 第 1 节的排序），想法出自用户自己的 `docs/agent_feature_ideas.md` 第 2.3 节。那一节要求先补设计审查 `docs/agent_design_review.md` 5.1 节的检索链测试，所以这次两件一起做。下面的现状按当前代码（`68be969`）核实过。本文件没有加进 `docs/DOCUMENT_INDEX.md`，因为那个文件里有用户自己没提交的改动。

## 1. 要解决什么

知识库回答里的 `[ref:n]` 现在只是一串字：

- 用户看不到 `ref:2` 指的是哪个文件、哪一段；
- 模型写错编号，或者编了一个根本不存在的编号，宿主不知道；
- 检索链本身（稠密、关键词、混合检索、融合、重排序、上下文构建）没有任何专门的测试，改了也不知道有没有改坏。

这次要做到三件事：

- 宿主检查答案里的引用编号确实是本次任务检索返回过的，编错的要求改一次，还错就照常交付并标出来；
- 网页上能点开引用，看到文件、标题、行号和原文片段，命令行和批处理结果也列出来源；
- 补上检索链的测试。

按 `AGENTS.md`“不宣称可靠事实验证”：这只检查“引用的编号存在”，不检查“引用的内容支持这句话”。界面和文档都要这样写。

## 2. 现状（已核实）

- **编号**：引用编号由 `app/services/rag_context_builder.py` 的 `ContextBuilder.build` 生成，`ref_id = len(selected) + 1`，**每次检索都从 1 开始**。一个任务里调用两次 `retrieve_knowledge`，两次都有 `[ref:1]`，答案里的 `[ref:1]` 到底指哪个说不清。这是这次必须先解决的问题。
- **证据怎么交给模型**：`app/tools/knowledge_registry_tool.py` 把检索结果包成 JSON `{"status", "evidence", "citations"}` 交回模型。`evidence` 是一块块 `[ref:n]\nfile: ...\nsource: ...\nheading: ...\ncontent:\n...` 的文本（格式由宿主写死），`citations` 是 `Citation` 列表（`app/schemas/rag.py:44`：编号、来源、文件名、标题路径、起止行、几个分数）。整包登记进任务的 `Resources`（`rag://<摘要>`，类型 `RETRIEVED_DOCUMENT`），所以**任务记忆里已经存着每次检索的完整证据**，续跑时也在。
- **谁会检索**：单一循环、调研成员、团队成员用的是同一个注册表和同一个任务记忆（`app/tools/tool_loader.py:93`；`READ_TOOLS` 里有 `retrieve_knowledge`，`app/agent/delegation.py:24`）。所以成员检索到的证据也登记在同一个任务里，负责人转述成员报告时引用的编号也能核对。
- **完成前的检查**：`app/agent/agent_loop.py` 第 378～424 行是模型交最终答案时的检查链：空答案、完成门控、写了没跑的提醒（D-087）、输出契约（D-093，修一次，不合格就失败）。引用检查照这个写法加一环。完成门控的 `before_finish` 事件不带答案原文（D-059），所以检查只能在执行器里做，不能做成门控。
- **结果和界面**：`AgentRunResult`（`app/agent/runtime.py:68`）没有引用相关的字段；网页 `app.js`、命令行、批处理都没处理过 `[ref:n]`，只是原样显示文字。
- **检索链测试**：`rag_search_service`、`reciprocal_rank_fusion`、`rag_reranker_service`、`rag_query_rewrite_service`、`rag_context_builder` 在 `tests/` 里只被花费测量和服务商状态的测试顺带引用，没有测检索行为本身。
- **顺带发现的两处违反 D-033**（异常原文进了日志或模型）：
  - `app/tools/knowledge_tool.py:193` 用 `logger.exception`，第 197 行把 `str(exc)` 放进 `error`，交给了模型；
  - `app/services/rag_reranker_service.py:124` 把异常原文写进日志。
- 黄金对照文件（`tests/fixtures/`）里没有用到 `retrieve_knowledge` 的场景。

## 3. 设计

### 3.1 整个任务统一编号（第 8 节第 1 问）

- 在 `KnowledgeRegistryTool` 里重新编号：一个任务里第一次检索得到 1、2、3，第二次接着 4、5……
- 同一片段（来源、起止行、内容的摘要都相同）在同一个任务里再次被检索到时，**沿用原来的编号**，不另起新号。
- 重新编号时改两处，必须一致：
  - `citations` 里每项的 `ref_id`；
  - `evidence` 文本里每块开头那行 `[ref:n]`。这行的格式是宿主自己生成的，只按“整行就是 `[ref:数字]`”来改，正文里碰巧出现的 `[ref:..]` 不动。
- 编号表存在任务记忆里（和证据放在一起），续跑、换执行器都不丢。成员和负责人共用同一张表。
- 检索服务本身（`ContextBuilder`）不改，单独调用时照旧从 1 开始。

### 3.2 交付前检查（第 8 节第 2～5 问）

- 在单一循环的完成检查链里加一环，位置在“写了没跑的提醒”之后、输出契约之前。
- 从答案里找出所有 `[ref:n]`，也认 `[ref:1,2]`、`[ref:1][ref:2]` 这两种写法。有效编号就是 3.1 那张表里的编号。
- 规则：
  - 答案里没有任何引用：不检查（不强制必须引用）；
  - 引用的编号都有效：通过；
  - 有无效编号，第一次：把答案退回，追加一条英文宿主说明，列出无效的编号和本次任务有效的编号范围，要求只用有效编号改写，不准调用工具；
  - 改写后仍有无效编号：**照常交付**，结果里标出哪些编号无效，任务不算失败。
- 和输出契约的关系：引用检查在前。改写那一轮之后，答案还要照常过输出契约；JSON 字符串里的引用一样检查。
- 范围：单一循环（含开了 `--delegate` 的调研委派）。团队模式的负责人这次不加（团队完成由宿主判定，另有一套，D-075），以后再看。

### 3.3 结果里带上引用

- `AgentRunResult` 加两个字段，默认值让现有结果和黄金对照文件不变：
  - `citations`：答案里引用到的、有效的那些，每项包括编号、文件名、来源、标题路径、起止行、原文片段（最多 500 字，取自登记的证据）；
  - `citation_problems`：无效的编号列表。
- 只记答案里真正用到的引用，不把整个任务检索到的全部证据塞进结果。
- 原文片段来自知识库本身，不是模型写的，可以进结果；但不进钩子事件和轨迹（轨迹只记“引用了几个、无效几个”）。

### 3.4 怎么呈现（第 8 节第 6 问）

- 网页：
  - 答案里的 `[ref:n]` 渲染成可以点的小标签；
  - 点开是一张卡片：文件名、标题路径、行号、原文片段，底部一句“只核对了编号存在，没有核对内容是否支持这句话”；
  - 无效的编号标成另一种颜色，提示“这个编号不在本次检索结果里”；
  - 用 `textContent` 拼内容，不用 `innerHTML`。
- 命令行：答案后面列出“引用：[1] 文件 > 标题（第 a～b 行）”，有无效编号时再打一行提示。
- 批处理：每项结果行加 `citations` 和 `citation_problems`。

### 3.5 检索链测试（审查 5.1）

全部用内存替身，不连 Milvus、不调嵌入或重排序接口：

- 稠密、关键词、混合三条检索路径各自返回什么；
- 融合：并列时的排序规则、同一文档去重、分数计算；
- 重排序：正常时的排序、出错时回落到原排序并带上警告（只记异常类型名）、关掉时的行为；
- 改写：改写失败时回落到原问题；
- 上下文构建：令牌上限截断、跳过计数、编号连续、不编造元数据。

### 3.6 顺带修的（第 8 节第 7 问）

- `knowledge_tool.py`：`logger.exception` 改成只记异常类型名；交给模型的 `error` 改成宿主写的固定说明加异常类型名。
- `rag_reranker_service.py`：日志只记异常类型名。

### 3.7 要改的文件

- 修改：
  - `app/tools/knowledge_registry_tool.py`：重新编号、编号表；
  - `app/agent/agent_loop.py`：交付前检查；
  - `app/agent/runtime.py`、`app/agent/loop_run.py` 和结果组装的地方：两个新字段；
  - `app/agent/__main__.py`、`app/agent/batch.py`：命令行和批处理的呈现；
  - `app/api/runs.py`（如果结果要多传字段）、`app/api/static/app.js`、`app.css`：网页；
  - `app/tools/knowledge_tool.py`、`app/services/rag_reranker_service.py`：D-033；
  - `app/agent/trace_view.py`：轨迹里显示引用计数（如果轨迹有这一项）。
- 新增：
  - 一个小模块放编号表和引用解析，比如 `app/agent/citations.py`；
  - `tests/test_citations.py`、`tests/test_rag_chain.py`。
- 不改：`ContextBuilder` 的编号规则、检索参数、团队模式、黄金对照文件、模型默认值、`.env`、依赖。

## 4. 测试（离线）

- 编号：
  - 两次检索，第二次的编号接着第一次；`evidence` 文本里的编号行和 `citations` 一致；
  - 同一片段第二次被检索到时沿用原编号；
  - 正文里碰巧出现的 `[ref:9]` 不被改；
  - 续跑后编号接着来；
  - 调研成员检索的编号，负责人引用时算有效。
- 交付前检查：
  - 没有引用时不检查，请求和以前逐字相同；
  - 编号都有效时通过，`citations` 里有对应的文件和片段；
  - 有无效编号时退回一次，改对后通过；
  - 改写后仍无效时照常交付，`citation_problems` 列出编号，状态是完成；
  - 改写那一轮不能调用工具；
  - 和输出契约一起用时，两个检查的顺序和次数都对；
  - 三种写法（`[ref:1]`、`[ref:1,2]`、`[ref:1][ref:2]`）都能识别。
- 记录：钩子事件和轨迹里没有原文片段和答案。
- 网页：结果里带两个新字段；页面文字用 `textContent`（接口测试加上手动看一次）。
- 命令行和批处理：来源行和结果字段。
- 检索链：3.5 列的各项。
- D-033：异常原文不进日志，也不进交给模型的 `error`。
- 黄金对照文件不变。

## 5. 不做的

- 检查引用的内容是否支持那句话（要模型判断，不在宿主能可靠做的范围）；
- 强制答案必须带引用；
- 团队模式的负责人检查；
- 引用网页搜索结果（`search_web` 有自己的来源格式，以后看需要）。

## 6. 以后可以接着做

- 评测加一道知识库题，检查引用编号都有效（要真实模型调用，另外问）；
- 团队模式也加上；
- 网页端知识库管理（排序第 8 项）可以复用这次的卡片。

## 7. 风险

- **模型看到的证据文字变了**：编号从“每次从 1 开始”变成“整个任务连续”，模型第二次检索看到的是 `[ref:4]` 起的编号。模型本来就该照抄编号，影响应该不大，但没有用真实模型验证。
- **改写一次会多花一轮模型调用**：只在有无效编号时发生。
- **误判**：答案里本来就要写 `[ref:n]` 这样的字面文字（比如讨论这个功能本身）时会被当成引用。只在有无效编号时才退回一次，最坏情况是多一轮、结果里多一个标记，不会让任务失败。

## 8. 要用户定的

1. **编号**：推荐**整个任务统一编号，同一片段沿用原编号**。另一个做法是每次检索各自编号，让模型写成“第几次检索的第几条”，但模型更容易写错，界面也难对应。
2. **检查什么**：推荐**只检查编号存在**，符合“不宣称可靠事实验证”。
3. **不合格时**：推荐**退回改一次，还错就照常交付并标出**，不让任务失败。另一个做法是像输出契约一样失败，但引用错一个编号就丢掉整个答案太重。
4. **没有引用时**：推荐**不检查**。另一个做法是“用了知识库就必须引用”，但很多回答只是顺带查了一下。
5. **范围**：推荐**这次只做单一循环**（含调研委派），团队模式以后再加。
6. **呈现**：推荐**网页可点开的卡片，命令行列出来源，批处理结果带上字段**。
7. **顺带修 D-033 的两处**：推荐**一起修**，都很小。
8. **记录**：推荐新开 **D-098**。

## 9. 实施记录（2026-11-23）

- 新增模块 `app/agent/citations.py`（`CITATION_PATTERN`、`cited_refs`、`CitationLedger`、`citation_repair_instruction`），实现任务级统一编号（同一片段 `(source, start_line, end_line, sha256(content))` 沿用原编号，仅替换整行块头 `[ref:n]`，块数与引用数不一致时不改证据并记警告后按原编号登记）。
- 编号表挂在 `RunMemory.citations` 惰性属性上，续跑和调研成员共用；`RunAssembler` 将 `getattr(memory, 'citations', None)` 显式传给 `AgentLoopRunner` / `ExploreLoopRunner`，未显式传入时执行器回退读取注册表 `retrieve_knowledge.citation_ledger`；`MemberSpawner` 和 `DelegatingLoopRunner` 通过 `check_citations=False` 关闭成员与团队负责人的引用检查。
- `AgentLoopRunner.run` 在 `remind_to_run` 之后、`output_contract` 之前加入引用核对：无引用时不检查、不发事件；有无效编号时退回改写一次（`ToolsShownOnly` 禁工具、`format_repair_count += 1`、发只含计数的 `citation_check` 事件）；改写轮若仍无效、调用工具（拒绝执行）或参数畸形/空回复，均不让任务失败，交付结果并标出 `citation_problems`。
- `StepRunOutput`、`AgentRunResult`、`LoopRun`、`RagAgentService` 归档与 `_restore_result`、`app/api/runs.py`、`app/agent/__main__.py`（`result_record` 与 `report`）、`app/agent/batch.py`、`app/agent/trace_view.py` 已接通 `citations` 和 `citation_problems`；默认值在归档、网页事件和 `--result-json` 中不出现，保持黄金对照不变。
- 网页端 `app/api/static/app.js`、`app.css` 在现有基于 `createElement`/`textContent` 的 `renderMarkdown` / `inline` 上扩展 `[ref:n]` / `[ref:1,2]` 可点 `<button>` 和来源卡片（有效展示文件/标题/行号/片段及固定说明，无效展示提示），全程不使用 `innerHTML`。
- D-033 顺带修复 5 处：`app/tools/knowledge_tool.py` 的 `search_knowledge` 与 `asearch_knowledge` 各 2 处（日志改 `logger.warning` 仅记异常类型名，返回 `error` 改宿主固定文案加类型名），以及 `app/services/rag_reranker_service.py` 第 124 行日志仅记异常类型名。
- 新增离线测试 `tests/test_citations.py`（17 项）、`tests/test_rag_chain.py`（11 项），并在 `tests/test_batch.py`、`tests/test_web_api.py` 各补 1 项（共新增 30 项，未调用真实模型或外部服务）。
- Claude 核对（2026-11-23）：
  - Gemini 进程跑完了，但没写出最后的报告（停在“正在运行 `init.ps1`”）；改动和本节上面的记录是完整的，由 Claude 逐项核对；
  - 看过编号表、交付前检查、结果字段的传递、命令行、批处理和网页的改动，没有要改的地方；网页只用 `createElement`/`textContent`；
  - 自己重跑 `init.ps1`：2339 passed、28 skipped（基线 2307，新增 32 个），黄金对照文件没有变化，新文件和改过的文件都是 LF；
  - 用户没提交的文件没有被碰过（按校验值核对）；`data/` 下没有新文件，仓库里没有遗留的无关文件；
  - 记下两个限制：
    - 网页上可点开的引用只在任务刚完成时显示；重新打开会话、从历史里看答案时，引用还是普通文字（会话历史接口不带 `citations`）；
    - 证据块和引用条数对不上时（正常不会发生），这次检索的引用按原编号登记，可能和前面的编号重叠；
  - 没有调用真实模型。
