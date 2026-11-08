# 常用命令

都在项目根目录 `D:\tries\agent` 下，用 PowerShell 运行。

> 标着“真实调用”的命令会调用模型、产生费用。

## 网页

```powershell
# 启动本地网页（只读，地址 http://127.0.0.1:8000，令牌见 .env 的 WEB_ACCESS_TOKEN）
.venv\Scripts\python.exe -m app.api --filesystem-root . --no-knowledge

# 普通模式默认使用单一循环；也可在网页逐条消息选择“按计划 / 单一循环”
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
