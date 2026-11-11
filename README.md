# Aletheia

> 一个面向知识检索、工具编排和智能工作流的智能体 RAG 系统。

Aletheia 是用 Python 写的智能体项目，包括：

- 混合检索、重排序和 Milvus 向量库；
- 用 MCP 接入的文件、联网搜索和 Docker 沙箱工具；
- 分层的对话记忆和长期记忆；
- 一个有界的智能体运行时：所有执行都有次数、时间和令牌上限，状态由宿主程序掌握。

可以通过命令行、本地网页和评测脚本三种方式使用。

## 近期的主要更改（2026-11）

按时间从早到晚列出，每条后面括号里是对应的提交或决策编号（`D-0xx` 见 [决策记录](docs/DECISIONS.md)）。

### 运行时与执行方式

- **默认改用单一智能体循环**（`45291d9`，D-072）。命令行、网页和评测默认都用一个工具循环做完整个任务，不再先列计划再逐步执行。
  - 原来的“规划—执行—重规划”保留为 `--single-runner plan`。
  - 循环带两个控制工具：`todo` 记录进度，`ask_user` 向用户提问。
  - 任务被问题阻塞后，用户回答了，循环会在原来的消息上接着做（`ea5783b`，D-064）。
- **团队模式重写**（第 3 阶段，`cefcd2d`、`bab704a`，D-075）：
  - 负责人本身也是一个智能体循环，只有 `delegate` 和 `review` 两个宿主工具。
  - 每次委派都起一个新的成员循环。共享状态放在工作区的文件里，任务是否完成由宿主判定。
  - 旧的 `team_v2`、运行器注册表和按能力路由都已删除。
- **按需委派**（`a0aa489`，默认关闭）：单一循环可以把大范围的只读调研交给一个子循环去做，用 `--delegate` 开启。评测显示，现有题目上委派没有带来收益，所以默认关闭。
- **服务层拆分**（第 4 阶段 4a，`e6a9856`）：
  - `RagAgentService.query_result` 拆成若干小方法。
  - 选择执行方式和组装运行器的逻辑移到 `app/services/run_assembler.py` 的 `RunAssembler`。

  第 4 阶段后续还要让循环和团队脱离规划—执行骨架，再删除规划模式，计划见 [stage4_plan.md](docs/stage4_plan.md)。
- **省令牌**（`6199d90`、`7835fa2`，D-063）：
  - 循环在 `todo` 上花的调用减少了。
  - 规划模式的每一步不再重复发送固定的工具列表。
  - 评测记录改为逐次记下每次调用。

### 可观测性与安全

- **钩子机制**（`fe22af0`、`030bbd0`、`bd54ba8`，D-058～D-061）：
  - 观察者可以接收事件，门控只能拒绝，不能改写结果。
  - 内置审计记录和对话令牌上限。
  - 网页上可以对有副作用的工具调用逐次批准或拒绝。
- **本地追踪**（`db13d64`，D-073）：每次运行写一份带区间编号的本地轨迹。
  - 用 `python -m app.agent.trace FILE.jsonl` 或 `--session NAME` 只读查看。
  - 网页上的“执行记录”也能看到同样的内容。
  - 评测可以用 `--keep-traces` 保留每题的追踪。

### 工作目录与网页

- **本地网页**（`3933efe`、`081d1f3`）：运行 `python -m app.api` 启动。每条消息可以单独选执行方式、是否写文件、是否开沙盒。
- **每个会话选择工作目录**（`4409e97`、`28b3066`，D-074）：
  - 网页上可以手填路径，也可以用系统的“选择文件夹”窗口；命令行用 `/dir` 或启动参数。
  - 所有目录都经过同一套检查：系统盘（C 盘）上的目录必须确认；`WORKSPACE_DENY_DIRS`、项目 `data` 和对话归档目录对工具不可见。
  - 可以拖入 UTF-8 文本文件作为附件。

### 知识库

- **建库命令**（`9902cb0`、`44d435c`、`64b2a49`、`a8c94b2`、`0f92d5c`）：运行 `python -m app.knowledge DIR`。
  - 默认只预览切块结果，加 `--confirm` 才真正写入。替换时先写新块再删旧块。
  - `--prune` 清理目录里已经不存在的来源，`--exclude` 排除路径。
  - 过大的块会被重新切分。
  - 列出文档改用 Milvus 的查询迭代器，不再受偏移窗口限制。

### 测试与评测

- 确定性测试已恢复（D-030）。`init.ps1` 除了管理文件和语法检查，还会运行全部 pytest；这些测试不调用模型、MCP 或数据库。
- `bench/` 提供调参集（11 题）和留出集（13 题），另有两道 `probe` 题，专门用来检验委派。只有加 `--confirm-live` 才会真正调用模型，见 [bench/README.md](bench/README.md)。

## 功能

- 稠密、BM25 和混合检索，倒数排名融合加交叉编码器重排序
- 兼容 OpenAI 接口的对话和嵌入客户端（配置项沿用 `DASHSCOPE_*` 的名字）
- Milvus 向量库，附本地 Docker Compose 配置
- MCP 工具：文件系统、联网搜索（Tavily）、Docker 沙箱
- 执行方式：single（默认单一循环，也可以按计划执行）、team、auto（宿主规则加一次模型判断，为每个任务选一种）
- 对话记忆（会话视图、任务状态、上下文投影、归档和后台摘要），长期记忆以 PostgreSQL 为准
- 钩子、预算计量、副作用保护、本地追踪

## 环境要求

- Python 3.11～3.13
- Docker 和 Docker Compose：本地 Milvus 和沙箱需要
- 一个兼容 OpenAI 接口的模型服务的 API 密钥

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

在 `.env` 里填好 `DASHSCOPE_API_KEY`、`DASHSCOPE_BASE_URL` 和 `RAG_MODEL`，然后启动 Milvus：

```powershell
docker compose -f deploy/milvus/compose.yaml up -d
```

Compose 文件里的账号密码只是开发用的默认值，部署到生产环境前必须修改。

## 使用

命令行运行单次任务，退出码 0 表示完成、1 表示失败、2 表示阻塞。不带目标时进入交互模式：

```powershell
.\.venv\Scripts\python.exe -m app.agent "阅读 README.md 并总结项目功能" --filesystem-root . --no-knowledge
```

启动本地网页（只监听回环地址；如果监听其他地址，必须设置访问令牌）：

```powershell
.\.venv\Scripts\python.exe -m app.api --write-root . --sandbox
```

常用参数：

- `--execution-mode {single,team,auto}`：执行方式
- `--single-runner {plan,loop}`：single 模式按计划执行还是用单一循环
- `--delegate`：开启按需委派
- `--write-root`：允许写入的目录
- `--sandbox`：开启 Docker 沙箱，需要同时给 `--write-root`

运行契约、记忆和续跑阻塞任务的说明见 [docs/agent_runtime.md](docs/agent_runtime.md)，常用命令汇总见 [common.md](common.md)。

## 项目结构

```text
app/
  agent/       运行时、单一循环、团队、钩子、计量、追踪
  api/         FastAPI 网页后端和静态页面
  knowledge/   建库命令
  models/      对话记忆和任务状态
  services/    智能体服务门面、执行组装、检索、记忆、工作目录
  tools/       工具注册表和 MCP 服务（文件系统、联网搜索、沙箱）
bench/         评测任务和评分
deploy/milvus/ 本地 Milvus 的 Docker Compose 配置
docs/          设计文档、决策记录和交接
tests/         确定性测试
```

## 检查

在仓库根目录运行健康检查。它会校验管理文件、编译 `app/`、加载 CLI，并运行确定性 pytest：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ./init.ps1
```

单独运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp $env:TEMP/aletheia-pytest
```

## 维护与交接

先读 [AGENTS.md](AGENTS.md)，再读 [当前会话交接](session-handoff.md) 和 [项目总交接](docs/PROJECT_HANDOVER.md)，其他文档按 [文档索引](docs/DOCUMENT_INDEX.md) 查找。

## 本地数据

虚拟环境、API 密钥、下载的模型、生成的输出和 Milvus 数据卷都不进 Git，完整列表见 `.gitignore`。
