# 第三项增强：按片段编辑文件、工具自己声明属性、单一循环的完成提醒（方案）

日期：2026-11-18。状态：**已实现并提交（2026-11-18，D-087）**。用户 2026-11-18“按推荐值实现”：提醒默认开，网页确认名单加 `edit_file` 并确认作用不明的工具，删掉旧的补丁版 `edit_file`。由 Claude 实施。

来源：这是用户 2026-11-18 同意的第三项功能增强，同样先出方案再实现。对应设计审查（`docs/agent_design_review.md`，暂未入库）第 9 节“先做的三件事”的第 3 件，包括 6.2 节第一行、2.4 节和 1.4 节。

## 1. 要解决的问题

1. **只能整文件覆盖**：文件系统服务只暴露 `write_file`（`app/tools/filesystem/server.py:63`）。大文件里改一行，模型也得把整个文件重新写一遍，这样费令牌，容易被输出上限截断，也会加重历史折叠的负担（`model_view.py` 专门折叠旧的 `write_file` 正文）。
   - `FileAccess` 里其实已经有一个 `edit_file`（`file_access.py:346-487`），但它有三个问题：
     - 它是统一差异格式（`@@ -a,b +c,d @@`）的补丁解析器，来自最早的提交 `4d54736`，没有暴露成工具，也没有任何测试，注释还是乱码；
     - 它从读根目录读、往写根目录写，两个根目录不同时，读的和写的不是同一个文件；
     - 原文件是 GB18030 编码时，会被悄悄改写成 UTF-8。
   - 另外，模型手写行号和行数准确的补丁，本来就容易出错。
2. **工具是只读还是有副作用，靠的是写死的名字**（审查 1.4）：`write_file`、`run_tests`、`run_python` 这几个名字分散在 8 个地方：
   - `team/models.py:11-12`（`WORKER_ONLY_TOOLS`、`RUN_TOOLS`）；
   - `side_effects.py:16`；
   - `agent_loop.py:419`（D-085 的判断）；
   - `hooks.py:410`（事件里的 `side_effect`）；
   - `route_selection.py:30`、`:62`；
   - `run_assembler.py:11-12`；
   - `delegation.py:65-108`、`:174`、`:321`；
   - `__main__.py:57-58`。

   以后新加一个能写或能删的工具，只要名字不在名单上，就会被当成只读，取消或超时后可能被重放。
3. **单一循环改完代码不跑就交付**（审查 2.4）：只有团队会装“交付前必须有一次通过的运行”这道门（`delegation.py:362`）。单一循环回一段纯文本就算最终答案（`agent_loop.py:312-330`），评测靠在题面后加一句话来弥补（`bench/run_bench.py:181`）。产品里用户不会加这句话。

## 2. 改什么

### 2.1 工具自己声明属性（新文件 `app/agent/tool_effects.py`）

- `ToolDefinition` 加一个字段 `effect: str | None = None`，取值有四种：
  - `read`：只读；
  - `write`：改工作区的文件；
  - `run`：在沙箱里运行，可能改工作区；
  - `external`：作用不明，或者会作用到外部。

  这个字段只给宿主用，不发给服务商。`openai_compatible_tool_calling_model.py:376` 只取名字、描述和参数结构，不受影响，实现时再核对一遍请求估算有没有把它算进去。
- `effect_of(definition)` 按顺序判断：
  1. 定义上声明了，就用声明的；
  2. 没声明时，查一张**内置工具表**，全项目只此一处：

     | 属性 | 工具 |
     | --- | --- |
     | `read` | `read_file`、`list_directory`、`search_files`、`get_current_time`、`retrieve_knowledge`、`search_web`、`read_context_artifact`、`run_python_code` |
     | `write` | `write_file`、`edit_file` |
     | `run` | `run_tests`、`run_python` |

     `run_python_code` 记为 `read`，因为它运行时工作区是只读挂载的（D-028）；
  3. 表里也没有，就记为 `external`。没有声明的一律按有副作用处理，宁可错拦，和 D-080 的思路一致。
- **MCP 工具读服务端的注解**：
  - `MCPToolAdapter` 遇到 `readOnlyHint=True` 时，记 `effect='read'`；其他情况不声明，交给上面的表和兜底规则。
  - 本项目自己的三个服务都补上注解（`mcp` 1.26 的 `@mcp.tool(annotations=ToolAnnotations(...))`，已确认支持）：
    - 读文件、列目录、搜索、`run_python_code`、联网搜索，标 `readOnlyHint=True`；
    - 写文件、改文件，标 `destructiveHint=True`；
    - `run_tests`、`run_python`，标 `readOnlyHint=False`。
  - 本地工具（时间、知识库、读回转存）在定义里直接写 `effect`。
- **原来按名字判断的地方都改为按属性判断**：
  - 副作用监视、D-085 的“写入或运行类出错立即结束”、钩子事件里的 `side_effect`：`effect_of(...) != 'read'`；
  - 只读任务要隐藏的工具、`run_assembler` 从团队选项里去掉的工具、审查成员拿不到的工具：属性不是 `read` 的那些；
  - “最后一次写入之后有通过的运行”（`has_passing_run_after_last_write`）和委派里记录写入、运行的证据：按 `write` 和 `run` 判断。这样 `edit_file` 自动算作写入。
  - `SIDE_EFFECT_TOOLS`、`WORKER_ONLY_TOOLS`、`RUN_TOOLS` 这几个常量保留，改成从表里推出来，供现有导入使用，代码路径里不再用它们判断。
- **钩子事件**多带一个 `effect` 字段。网页确认门（`app/api/approvals.py`）除了名单上的工具，遇到 `external` 的工具也要问一次（第 6 节第 2 条）。现在没有任何工具是 `external`，所以眼下行为不变，只是给以后接入的工具兜底。
- 不改的：
  - 调研成员能拿的只读工具（`delegation.py:21` 的 `READ_TOOLS`）仍然是显式的授权名单。授权属于权限决定，名单式更稳妥。以后要改，留给“用户自带 MCP 服务和按规则授权”那项新功能。
  - `run_python_code` 只给只读任务这条规则（`READ_ONLY_ONLY`）讲的是给谁用，不是属性，也保留。
- 已知限制：现在声明的属性优先，因为目前只接本项目自己的服务。接入用户自带的 MCP 服务时，要区分来源，不能让别人的服务自己声明“只读”就算数。这一点写进决策记录。

### 2.2 `edit_file`（`app/tools/filesystem/file_access.py`、`server.py`）

- **删掉旧的补丁解析器**，换成按原文替换的 `edit_file(path, old, new, replace_all=False)`（第 6 节第 3 条要用户同意）。规则如下：
  - 只在配置了写根目录时注册。文件按 `resolve_write_path` 解析，必须是已存在的普通文件，读和写都在写根目录下，是同一个文件。
  - 文件大小不超过写入上限 `max_write_bytes`，改完的结果也不超过。
  - 只改 UTF-8 文件；原来有 BOM 的保留 BOM。其他编码直接报错，并提示用 `write_file`，不会悄悄改编码。
  - `old` 不能为空，也不能和 `new` 相同。
  - 换行：原文找不到 `old`，而文件用的是 `\r\n`、`old` 里只有 `\n` 时，把 `old` 和 `new` 都换成 `\r\n` 再找一次。
  - `old` 出现 0 次时报错：“old text not found; read the lines again with read_file and copy them exactly”；出现多次且没有设 `replace_all` 时报错：“old text appears N times; include more surrounding lines or set replace_all=true”。这两种报错都是宿主写的英文，经 MCP 返回给模型，作为一次失败的工具结果，不是异常，所以不计入 D-085 的抛异常次数。
  - 写回时沿用 `write_file` 的临时文件加原子替换。把这段代码抽成 `_write_atomic`，两个工具共用。
  - 成功时返回：“Edited {path}: replaced N occurrence(s) starting at line L. Read it with read_file(path=..., start_line=...)”。
- **工具说明**里写清分工：改已有文件的一部分用 `edit_file`，新建文件或整体重写用 `write_file`。沙箱服务说明里的“先用 write_file 写代码”改为“write_file 或 edit_file”。
- **确认卡片**：
  - `hooks.py` 的参数预览加上 `edit_file`：`path`、`old`、`new`、`replace_all`，`old` 和 `new` 各最多保留 20000 字符；
  - `app/api/static/app.js` 加中文标签“修改文件”，卡片分别显示“替换前”“替换后”“全部替换”。
- **网页确认名单**：`app/config.py:107` 的默认值 `WEB_APPROVAL_TOOLS` 加上 `edit_file`，`.env.example:106` 的注释同步改（第 6 节第 2 条）。你的 `.env` 里没有设这个变量，所以会用新的默认值。
- **团队模式**：`__main__.py:57` 给团队成员的写入工具加上 `edit_file`；`route_selection.py:62` 改成判断有没有任何写入工具。
- **历史折叠**：`model_view.py` 只折叠 `write_file` 的正文。`edit_file` 的参数本来就小，这次不改折叠规则。

### 2.3 单一循环的完成提醒（`app/agent/agent_loop.py`）

- `LoopOptions` 加一个开关 `remind_to_run: bool = False`。是否打开由 `RunAssembler` 决定：只在三个条件都满足时打开——单一模式、本次工具里有 `run` 类工具（也就是开了沙箱）、配置项 `AGENT_REMIND_TO_RUN` 为真。这个配置项的默认值由用户定（第 6 节第 1 条）。
- 模型第一次交答案时，宿主先检查：这次循环里成功写过文件，最后一次写入之后又没有通过的运行。两条都满足、而且还没提醒过，就把这段答案作为助手消息留在历史里，再追加一条宿主消息，然后继续循环：

  > HOST: you changed files in this task, but no run_tests or run_python passed after the last change. Run the relevant tests now. If you cannot run them, answer again and say that the change was not run.

  模型第二次交答案时照常结束，同时执行器记 `unverified = True`。
- 只提醒一次。提醒本身不占工具轮数，但多一次模型调用，调用前照常查预算。团队成员已经有自己的交付门，调研成员没有写入工具，所以两者都不受影响。
- **标记“未验证”**：
  - `LoopRun` 的运行日志条目加 `unverified: true`；
  - 命令行打印一句中文：“注意：这次改了文件，但最后一次修改之后没有通过的运行，结果未经验证。”；
  - 网页在服务商提示的同一位置显示这句话，传过去的只有一个布尔值，具体字段名实现时按网页现有的结构定；
  - 评测行从 `steps` 里读出 `unverified`。
- 评测题面后的 `SANDBOX_HINT` 保留，这样前后两次评测还能比较。

## 3. 黄金对照文件

预期一个都不变：

- 按属性判断出来的集合，和原来按名字判断的完全一样；
- 测试里直接构造 `LoopRun` 和 `AgentLoopRunner` 时，`remind_to_run` 默认关闭；
- 测试用的假工具大多直接用 `ToolDefinition(name, ...)`，没有声明属性，会走内置表，结果和原来相同。

如果有任何一个黄金文件变了，先停下来找原因，不重录。

可能要改的是断言了完整工具清单的测试：配了写根目录时，文件系统服务会多出 `edit_file`。

## 4. 测试

- `tests/test_tool_effects.py`（新建）：
  - 声明的属性优先，没声明的查表，表里没有的记为 `external`；
  - MCP 注解 `readOnlyHint=True` 记为 `read`，其他情况走表；
  - 本项目三个服务注册出来的每个工具，属性都和表一致（启动真实的 stdio 服务，不调模型）；
  - 原来那几个常量和表推出来的集合相同；
  - 一个声明为 `external` 的假工具：出错时立即结束任务（D-085）、副作用监视记为 `touched`、只读任务看不到它、网页确认门会问。
- `tests/test_filesystem_edit.py`（新建，只用临时目录）：
  - 替换一次，`replace_all`，0 次和多次的报错文字；
  - `\r\n` 文件的换行处理，保留 BOM，GB18030 文件报错；
  - 超出大小上限；
  - 读根目录和写根目录不同时，改的是写根目录下那个文件；
  - 链接、受保护路径、拒绝目录都和 `write_file` 一样被拒；
  - 经真实的 stdio 服务调用一次；
  - 写根目录为空时，工具清单里没有 `edit_file`。
- `tests/test_agent_loop.py`：
  - 提醒开着：写过文件但没跑就交答案，收到一次提醒；第二次交答案时结束，结果标记为未验证；
  - 写入之后有通过的运行，不提醒；
  - 写入失败了，不算写过；
  - 开关关着时行为不变；
  - `edit_file` 算作写入。
- `tests/test_hooks.py` 或现有的确认测试：`edit_file` 的预览字段和长度上限。
- 委派和团队：`edit_file` 之后有通过的运行，团队交付门放行；只有 `edit_file` 没有运行，团队交付门拒绝。
- `RunAssembler`：只在单一模式加沙箱、并且配置项打开时，`remind_to_run` 才为真。
- `init.ps1` 必须通过。

## 5. 文档和记录

- `docs/DECISIONS.md` 新增 D-087：工具属性的判断顺序、`edit_file` 的规则、完成提醒，以及被否方案：
  - 保留补丁格式的编辑：模型写行号容易错，这段代码也从没测过；
  - 未验证时直接判失败：会丢掉已经写好的改动，而用户可能本来就没打算跑；
  - 未声明属性的工具按只读处理：一旦判错，就会重放有副作用的调用。
- `docs/agent_runtime.md` 加上提醒和“未验证”标记；`docs/PROJECT_HANDOVER.md` 补一段。
- `bench/README.md`：说明行里的 `unverified` 字段。
- `docs/modules/`：03 循环、05 路由和组装、08 工具、09 钩子、10 副作用这几篇，同步代码摘录。这个目录照旧不提交。

## 6. 要用户定的三件事

1. **完成提醒默认开还是关**：推荐**开**。它只在开了沙箱时生效，而沙箱本身就要你显式打开；代价是每个任务最多多一次模型调用。默认关的话，就要在 `.env` 里打开才有。
2. **网页确认名单加上 `edit_file`，`external` 类工具也要确认**：推荐**加**。不加的话，网页上用 `edit_file` 改文件就不会弹确认卡片，比现在 `write_file` 的安全性还低。这一条会改 `app/config.py` 里的一个默认值。
3. **删掉 `FileAccess` 里旧的补丁版 `edit_file`**：推荐**删**，换成按原文替换的新版本。旧代码没暴露、没测试，读写的目录也不一致。删之前我会确认没有任何调用者（目前查到的唯一定义就是它本身）。

## 7. 约束

- 不改 `.env` 和依赖，不调用真实模型，不跑评测。除第 6 节第 2 条那个默认值外，不改其他默认值。实现只用离线测试验证，真实 stdio 服务的测试不调模型。
- 用户自己删掉的 `scripts/benchmark_conversation_memory.py`，以及还没提交的 `docs/modules/`、`docs/DOCUMENT_INDEX.md`、两份审查报告，提交时都不带。
- 提交和推送要用户同意。

## 8. 实现时的补充

- **未知工具名不算副作用**：模型编造的工具名，注册表根本不会执行，所以 `is_side_effect` 对注册表里没有的名字返回假，和原来一样。否则“不可用工具”的场景会被误判为动过工作区。
- **网页确认卡片**：`replace_all` 加进了参数摘要保留原值的键（它只是一个布尔值），卡片才能显示“是/否”。
- **提醒和阻塞续跑**：循环在 `ask_user` 之后续跑时，这次循环的工具结果是从空开始的（原来就是这样，`LoopPause` 不存结果）。所以写入发生在阻塞之前、回答之后直接交答案，不会被提醒。这种情况少，这次不改。
- **工具的 MCP 调用测试**：`edit_file` 经 FastMCP 在进程内调用（`server.call_tool`）。真实的 stdio 子进程测试（`tests/tools/test_filesystem_server.py`）覆盖了它的注册和属性。
- 另外改的测试：
  - 几处假工具的名字不在内置表里（`read`、`list_dir`、MCP 的 `web_search`），现在要声明 `effect='read'` 或带上注解，否则按作用不明处理；
  - 钩子事件多了 `effect` 字段；
  - 团队的写入工具多了 `edit_file`；
  - 文件系统服务的工具清单多了 `edit_file`。
- 黄金对照文件一个都没变。
