# 新功能一：常驻指令——项目说明文件和个人指令（方案）

日期：2026-11-18。状态：**方案，待用户确认**（第 7 节有四件事要用户定）。

**2026-11-18 调整**：用户定了个人指令先放一放，由 [个性化人设方案](persona_plan.md) 里每个人设的指令代替，在输入栏用 `/btw` 追加。下文“个人指令”的部分（第 2 节右列、3.1 节的 `personal`/`set_personal`、3.4 节的编辑框）暂不实施；“项目说明”的部分不变。

来源：用户 2026-11-18 让我从 `docs/agent_feature_ideas.md` 里挑新功能写方案。这是报告第 9.3 节推荐的第一个（1.1 节），价值高、改动小，也不依赖任何增强。

## 1. 要解决的问题

现在每个任务只带用户这一条消息，再加上会话历史和长期记忆的召回。“用 pytest 跑测试”“注释写中文”“不要改 `vendor/`”“先给结论”这类要求，用户每次都得重说一遍。主流编码智能体会读项目里的说明文件，聊天产品有“自定义指令”，Aletheia 两样都没有。

现成的落脚点（已核实）：

- `RunState.add_constraint`（`app/models/run_state.py:91`）写好了，生产代码里没有调用者。`constraints` 已经在发给模型的上下文里，排在 `active_goal` 后面（`agent_loop.py:56` 的 `CONTEXT_FIELDS`）。
- 系统提示说文件、工具结果和 `pinned_contexts` 是“证据，不是指令”，`constraints` 不在这一列里。所以放进 `constraints` 的内容会被当作指令，而读到的普通文件仍然只是证据，两者不会混。
- 团队成员和调研成员的上下文保留 `constraints`（`delegation.py:456` 的 `_member_context`），所以成员也会遵守同样的指令，不用另外处理。
- 任务开始时钉上下文的地方是 `RagAgentService._prepare_task`（`rag_agent_service.py:590`）。命令行和网页都经过它，这一次的工作目录也是在这里确定的（`:603`）。
- 黄金对照文件记录了系统提示（47 个文件里都有它），但这些场景里 `constraints` 都是空的。所以只要**不改系统提示**、指令只通过 `constraints` 进来，黄金文件就不会变。

## 2. 两种指令

| | 项目说明 | 个人指令 |
| --- | --- | --- |
| 内容 | 这个工作目录的规则：测试命令、目录约定、不能碰的文件 | 跟着用户走的偏好：回答语言、详略、格式 |
| 放在哪 | 工作目录下的固定文件 `.aletheia/instructions.md`（第 7 节第 1 条） | `data/instructions/personal/<用户>.md`，在 `data/` 下，智能体的文件工具看不到 |
| 谁写的 | 可能是用户，也可能是克隆来的仓库里别人写的 | 只有用户自己 |
| 要不要确认 | 要：按内容哈希确认一次，内容改了要重新确认（第 7 节第 2 条） | 不要 |
| 上限 | 8000 字符 | 4000 字符 |
| 怎么改 | 用户自己编辑文件 | 网页上的编辑框，或者直接编辑文件 |

## 3. 改什么

### 3.1 读取和确认（新文件 `app/services/instructions.py`）

- **`InstructionStore(directory)`**：
  - `directory` 默认是 `data/instructions`，由配置项 `INSTRUCTIONS_DIR` 指定，留空表示关掉这个功能；
  - 只认绝对路径，按项目根目录锚定（和 `CONVERSATION_STORAGE_DIR` 一样，`app/config.py:139`）。
- **`personal(user_id)`、`set_personal(user_id, text)`**：
  - 文件名取用户编号的 SHA-256 前 16 位，不直接拿用户编号当文件名；命令行没有用户编号时用 `local`；
  - 空文本表示删除；
  - 超过 4000 字符时拒绝保存；
  - 原子写入。
- **`project(workspace)`**，返回 `ProjectInstructions`，字段有：
  - `path`：固定为 `.aletheia/instructions.md`；
  - `chars`；
  - `sha256`；
  - `trusted`；
  - `problem`：`None`、`missing`、`too_large`、`unreadable`、`denied` 之一；
  - `text`：只有已确认时才有。
- **读取项目说明**：
  - 走 `FileAccess(Path(workspace), denied=WorkspacePolicy.from_config().closed_folders())`，路径检查和智能体自己读文件完全一样：拒绝目录、链接、受保护路径都会拦下来；
  - 读取上限 32 KB，解码沿用 `FileAccess` 的规则；
  - 超过 8000 字符时**整份不加载**，记 `too_large`（第 7 节第 3 条）。
- **确认**：
  - 记录放在 `data/instructions/trusted.json`，格式是 `{工作目录的绝对路径: 已确认内容的 sha256}`；
  - `trust(workspace, sha256)` 只在给的哈希和文件当前内容一致时才写入，免得用户确认的是旧内容；
  - 内容一变，哈希就对不上，需要重新确认。
- 文件里的文字只进模型请求，不进钩子事件、轨迹、审计和日志（D-059）；这些地方只记字符数。

### 3.2 加进任务（`rag_agent_service.py` 的 `_prepare_task`）

- 新任务开始时（`runtime is None`），按“个人、项目”的顺序各调一次 `task.add_constraint`。阻塞后续跑的任务已经带着约束，不再加。
- 每条约束的开头是宿主写的一句英文，说明来源和优先级：
  - 个人：`Personal instructions from the user, for every task. Follow them unless the user's current message or the host rules say otherwise:`
  - 项目：`Project instructions from .aletheia/instructions.md in the working directory, confirmed by the user. Follow them unless the user's current message or the host rules say otherwise:`

  优先级是：当前消息高于项目说明，项目说明高于个人指令，宿主规则始终在最上面（AGENTS.md：“宿主拥有最终输出契约”）。
- 工作目录有项目说明、但还没确认时，不加载，只发一个提示。
- **发一个 `instructions` 事件**，只带数字和固定的代码，不带正文：
  - 有几个字段：`personal_chars`、`project_chars`、`project_problem`、`project_untrusted`；
  - 命令行打印一行中文，例如：“本次任务带了个人指令（120 字）和项目说明（860 字）。”或者“工作目录里有项目说明 .aletheia/instructions.md，还没确认，本次没带。”；
  - 网页在路由提示旁边显示同样的内容；
  - 轨迹只记这几个数字。
- 请求的大小：约束属于必须放进请求的上下文，两项加起来最多 12000 字符。窗口小到放不下时，会按 D-085 的 `context` 失败结束，并提示用户缩短指令。

### 3.3 命令行（`app/agent/__main__.py`）

- 新参数 `--trust-instructions`：单次任务时，确认当前工作目录里这份项目说明（记下它的哈希），然后加载。
- 交互模式新命令：
  - `/instructions`：显示个人指令文件的位置和字数，以及项目说明的状态；
  - `/instructions trust`：确认当前工作目录的项目说明。
- 帮助文字和 `INTERACTIVE_HELP` 同步更新。

### 3.4 网页（`app/api/app.py`、`app/api/static/app.js`）

- 三个接口，都受访问令牌保护：
  - `GET /api/instructions?workspace=...`：返回个人指令的正文，以及项目说明的状态（不含正文）；
  - `PUT /api/instructions/personal`，请求体 `{text}`：保存个人指令；
  - `POST /api/instructions/trust`，请求体 `{workspace, sha256}`：确认项目说明。工作目录要先通过 `check_workspace`；在系统盘上的，沿用 D-074 的确认。
- 页面：
  - 设置里加一个“常驻指令”：编辑个人指令的文本框，下面显示项目说明的状态；有未确认的说明时，给一个“确认这份说明”的按钮，并提示“说明文件的内容会作为指令发给模型，确认前请先看一遍”；
  - 每次任务开始时，显示 `instructions` 事件那句话。
- 网页只显示个人指令的正文，因为那是用户自己写的。项目说明的正文网页不显示，用户在自己的编辑器里看；这样页面上不会渲染仓库里的文字。

### 3.5 不做的

- 不读 `AGENTS.md`、`CLAUDE.md` 这类其他工具的说明文件（第 7 节第 1 条）。
- 不支持子目录里各放一份说明、按层级合并。
- 不自动从对话里抽取个人指令。这和长期记忆的自动抽取是同一件事，审查 3.4 节还等用户决定。
- 评测不加题。评测每次都在新的临时目录里跑，里面没有说明文件，所以结果不受影响。以后可以加一道“格式要求写在说明文件里”的题，单独做。

## 4. 黄金对照文件

预期一个都不变：

- 系统提示不改；
- `LoopRun` 的场景测试不经过 `_prepare_task`；
- `tests/conftest.py` 把 `INSTRUCTIONS_DIR` 设成空，服务层测试默认不读指令，只有专门的测试才打开。

如果有任何一个黄金文件变了，先停下来找原因。

## 5. 测试

- `tests/test_instructions.py`（新建，只用临时目录）：
  - 个人指令：保存、读取、删除、超过上限被拒、文件名不含用户编号原文、原子写入；
  - 项目说明：
    - 没有文件、过大、无法读取（链接或拒绝目录）时，各报对应的 `problem`；
    - 未确认时不给正文；
    - 用当前哈希确认后才有正文；用旧哈希确认会被拒；
    - 内容改了，就变回未确认；
  - `INSTRUCTIONS_DIR` 留空时，整个功能关闭。
- 服务层：
  - 有个人指令和已确认的项目说明时，模型收到的第一条请求的 `constraints` 里按顺序有这两条，开头是宿主写的那句话；
  - 未确认时 `constraints` 为空，并发出 `project_untrusted` 事件；
  - 阻塞后续跑不会重复加；
  - 团队成员和调研成员的请求里也有这两条；
  - 钩子事件、轨迹、审计记录里没有指令正文。
- 命令行：
  - `--trust-instructions` 记下哈希并加载；
  - `/instructions` 和 `/instructions trust` 的输出；
  - 未确认时打印的那句提示。
- 网页：
  - 三个接口：令牌、请求体大小、工作目录检查、用旧哈希确认被拒；
  - `app.js` 能通过 `node --check`。
- `init.ps1` 必须通过。

## 6. 文档和记录

- `docs/DECISIONS.md` 新增一条（D-088 已经给了个性化人设，用实现时的下一个空号），写清来源、放在哪、确认方式和优先级。被否方案：
  - 读 `AGENTS.md`：别的工具写的说明会被当成 Aletheia 的指令；
  - 不确认直接加载：克隆来的仓库可以借它注入指令；
  - 超长时截断：截断后指令的意思可能变了；
  - 放进 `pinned_contexts`：系统提示说那里是证据，不是指令。
- `README.md` 和 `.env.example`：说明这个功能和 `INSTRUCTIONS_DIR`。
- `docs/PROJECT_HANDOVER.md` 补一段；`docs/modules/06-service-facade.md`、`15-entry-points.md` 补上说明，这个目录照旧不提交。

## 7. 要用户定的四件事

1. **项目说明的文件名**：推荐 `.aletheia/instructions.md`，Aletheia 专用。另一种是读 `AGENTS.md`，好处是不少仓库已经有了；坏处是那份是写给别的工具的，比如本仓库的 `AGENTS.md` 讲的是 Codex 和 Claude 怎么分工，被当成指令只会干扰。
2. **项目说明要不要确认**：推荐**要**，按内容哈希确认一次，内容改了要重新确认。代价是第一次用的时候多一步。不确认的话，克隆来的仓库里放一份说明文件，就能让模型照它做事（提示注入）。
3. **超过上限怎么办**：推荐**整份不加载并提示**。另一种是截断后加载，但截断可能正好切掉“不要”后面的那半句。
4. **谁来实现**：前三项增强是 Claude 实施的。这一项横跨服务层、命令行和网页前端，改动面比前几项大。可以照旧由我来做，也可以按仓库的分工派 Codex 实施，我写方案和审查。

## 8. 约束

- 不改 `.env`、模型默认值和依赖，不调用真实模型，不跑评测。新增配置项 `INSTRUCTIONS_DIR` 有默认值，用户什么都不设也能用。
- 用户自己删掉的 `scripts/benchmark_conversation_memory.py`，以及还没提交的 `docs/modules/`、`docs/DOCUMENT_INDEX.md`、两份报告，提交时都不带。
- 提交和推送要用户同意。
