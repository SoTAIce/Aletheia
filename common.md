# 常用命令

都在项目根目录 `D:\tries\agent` 下，用 PowerShell 运行。

> 标着“真实调用”的命令会调用模型、产生费用。

## 网页

```powershell
# 启动本地网页（只读，地址 http://127.0.0.1:8000，令牌见 .env 的 WEB_ACCESS_TOKEN）
.venv\Scripts\python.exe -m app.api --filesystem-root . --no-knowledge

# 启动时把普通模式的默认改成单一循环（不加时默认按计划）；网页上也可逐条消息选择“按计划 / 单一循环”
.venv\Scripts\python.exe -m app.api --filesystem-root . --no-knowledge --single-runner loop

# 允许写文件（只能写进指定目录）
.venv\Scripts\python.exe -m app.api --filesystem-root . --write-root .\output --no-knowledge

# 端口被占用时换一个端口
.venv\Scripts\python.exe -m app.api --filesystem-root . --no-knowledge --port 8001
```

- 关闭：在运行它的终端里按 `Ctrl+C`。
- 网页、写文件、长期记忆和团队模式的开关在页面上逐条消息选择。
- 要启用团队模式，启动时加 `--execution-mode team` 或 `--execution-mode auto`。

## 命令行（真实调用）

```powershell
# 交互模式
.venv\Scripts\python.exe -m app.agent --filesystem-root . --no-knowledge

# 单次提问，并把结果另存为 JSON
.venv\Scripts\python.exe -B -m app.agent "解释 app/agent/runtime.py 里一个任务是怎样被规划和执行的" --filesystem-root . --no-knowledge --max-tokens 400000 --result-json output\result.json
```

只想这一次换上下文窗口或对话存放位置、不改 `.env` 时，先在同一个终端里设置环境变量：

```powershell
$env:AGENT_MODEL_CONTEXT_TOKENS = '128000'
$env:CONVERSATION_STORAGE_DIR = "$env:TEMP\aletheia-run"
```

关掉终端后这两个变量就失效了。要长期生效，就在 `.env` 里加上 `AGENT_MODEL_CONTEXT_TOKENS=128000`（按所用模型填写）。

## 知识库建库

```powershell
# 预演：列出会处理的文件和各自切成几块，不调用嵌入接口，也不写入 Milvus
.venv\Scripts\python.exe -B -m app.knowledge docs

# 只读清理预演：连接 Milvus 列出过时来源，不调用嵌入接口、不删除
.venv\Scripts\python.exe -B -m app.knowledge docs --prune

# 真正写入（需要 Milvus 在运行、.env 里配好 DASHSCOPE_API_KEY；嵌入接口按量计费）
.venv\Scripts\python.exe -B -m app.knowledge docs --confirm

# 完整建库后清理过时来源（同样会调用嵌入接口并改动 Milvus）
.venv\Scripts\python.exe -B -m app.knowledge docs --confirm --prune
```

- 支持 `.md`、`.txt`、`.py`、`.json`，会跳过 `.git`、`.venv`、`node_modules`、`__pycache__` 等目录。
- 同一个文件再建一次，会先写新块，成功后再删除不属于新编号的旧块；嵌入或写入失败时保留旧块。删除失败时新旧块暂时并存，下次重跑可清掉；空文件只删除这个来源的旧块。
- `--prune` 只处理本目录下不在本次文件列表里的来源，不会误删相邻目录（如 `docs2/`）的块；排除目录和不再支持的扩展名所对应的旧来源也会被清理。不加该参数时不列来源；普通预演完全不连接服务。
- `--prune` 预演是唯一会连接服务的预演方式，只读列出来源；集合不存在时报告失败，不创建或重建集合。加 `--confirm` 后，只有所有文件都已尝试处理且没有提前停止，才逐个删除过时来源；单个文件失败仍计入退出码 1。
- 建库失败显示中文阶段和原始异常类型，不显示异常消息；前 3 个文件全失败时停止，并按阶段提示检查连接、密钥或接口。
- 建好以后，启动智能体或网页时不要加 `--no-knowledge`，才会用到知识库。

## 检查（不调用模型）

```powershell
# 项目健康检查（静态检查和全部离线测试）
powershell -NoProfile -ExecutionPolicy Bypass -File .\init.ps1

# 只跑测试；临时目录放到系统 TEMP，不在项目里留缓存
$env:PYTHONDONTWRITEBYTECODE = '1'
.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\aletheia-pytest"

# 基线核对：完整命令见 docs/harness/baseline.md
```

## 缓存测试（真实调用）

```powershell
# 向模型服务连发 3 次相同请求，看缓存是否稳定（2～5 次）
.venv\Scripts\python.exe -B -m app.agent.cache_probe --calls 3

# 同时测试服务是否接受 prompt_cache_key
.venv\Scripts\python.exe -B -m app.agent.cache_probe --calls 3 --cache-key aletheia-probe
```

中转站如果拒收 `prompt_cache_key`，在 `.env` 里加一行 `AGENT_MODEL_PROMPT_CACHE_KEY=false`。

## 评测

```powershell
# 只打印计划，不调用模型
.venv\Scripts\python.exe -m bench.run_bench --tasks all --modes single

# 调参集 11 题，普通模式各 1 次（真实调用）
.venv\Scripts\python.exe -B -m bench.run_bench --confirm-live --tasks all --modes single --runs 1 --max-runs 11 --out output\bench-tune-single.json

# 留出集 13 题，普通模式各 1 次（真实调用；改动定稿后才跑，不要根据它的失败去改提示词）
.venv\Scripts\python.exe -B -m bench.run_bench --confirm-live --tasks all --split holdout --modes single --runs 1 --max-runs 13 --out output\bench-holdout-single.json

# 调参集 11 题，普通模式改用单一循环（架构第 1 阶段试验，D-062；真实调用）
.venv\Scripts\python.exe -B -m bench.run_bench --confirm-live --tasks all --modes single --single-runner loop --runs 1 --max-runs 11 --out output\bench-tune-loop.json

# 单独跑某几题，普通模式和团队模式各 1 次（真实调用）
.venv\Scripts\python.exe -B -m bench.run_bench --confirm-live --tasks billing_qa todo_html --modes single team --runs 1 --out output\bench-some.json
```

- 默认最多跑 6 次（任务数 × 模式数 × 次数），超过要加 `--max-runs`。
- 单次超时 600 秒，用 `--timeout` 修改。
- 评测报告写在 `output\`，这个目录不进版本库。
- 更多选项见 `bench/README.md`：`--write`、`--sandbox`、`--keep-answers`。
