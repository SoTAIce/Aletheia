# 摘要角色测试不再依赖本机 `.env`（方案）

日期：2026-11-26。状态：**已实施**。第 5 节三条按推荐定（用户 2026-11-26）。只改测试，不需要新决策编号（记在 D-103 的记录下）。

## 1. 现象

在没有 `.env` 的干净检出里跑全部测试，`tests/test_summarizer_role.py` 有 3 个失败：

- `test_open_history_compressor_without_summarizer_uses_local_unchanged`
- `test_open_history_compressor_profile_unavailable_degrades_cleanly`
- `test_web_serve_with_summarizer_profile_passes_profile_compressor`

在你的工作区里这 3 个都能通过。干净检出里，`2298d44`（D-105）、`995b036`（D-106）、`309c5b1`（D-107）都是这 3 个失败，其余全部通过。这些测试是 D-103（`d7d7d9c`）加的。

## 2. 原因（已在干净检出里复现）

- 三个测试写的临时档案文件里，`main` 档案的密钥变量是 `AGENT_MODEL_API_KEY`（文件第 23–27 行的 `MAIN_PROFILE`）。
- `load_profiles` 会检查用到的每个档案的密钥都不为空（`app/models/model_profile.py:216`）。它从设置字段 `agent_model_api_key` 读取，这个字段的值来自 `.env`。
- 你的 `.env` 里有这个密钥，所以检查通过；干净检出里它是空的，于是报 `ModelProfileError: 档案 'main' 的密钥变量 AGENT_MODEL_API_KEY 是空的`。
- 附带的问题：在你的机器上，这三个测试其实读到了你真实的密钥（只用来判断“不为空”，没有发出去，也没有打印）。测试本不该碰到真实密钥。

## 3. 修法

- 这三个测试各加一行 `monkeypatch.setattr(<config>, "agent_model_api_key", "main-test-key")`，`<config>` 用测试里已经在改的那个对象（`cli.config` 或 `web_cli.config`）。这样有没有 `.env` 都一样，也不会再读到真实密钥。
- 不改产品代码，不改其他断言。
- 顺手检查同一文件里其他用 `MAIN_PROFILE` 的测试：不检查密钥的不用改；会检查但已经自己设了密钥的（例如第 58 行那种）也不用改。

## 4. 验证

- 在你的工作区跑 `init.ps1`，应该还是 2535 passed、28 skipped。
- 再在 `%TEMP%` 下建一个临时检出（`git worktree add --detach`），里面没有 `.env`，跑全部测试，应该 0 失败；跑完删掉这个检出和它的测试临时目录。删除时只删这次新建的目录，不用通配符。

## 5. 要用户定的

1. **修法**：推荐**只在这三个测试里设一个假密钥**。不推荐另一种做法：给整个测试套件加一个自动清空所有密钥的夹具。那样改动面大，可能牵动其他依赖设置的测试。
2. **谁来改**：推荐**Claude 直接改**。改动只有三行，写实施说明、派 Gemini 的开销比改动本身还大。按惯例也可以交给 Gemini。
3. **验证**：推荐按第 4 节**在工作区和干净检出里各跑一遍**。

## 6. 实施记录（2026-11-26）

- Claude 直接改了 `tests/test_summarizer_role.py`：三个测试各加一行 `agent_model_api_key = "main-test-key"`，其他断言和产品代码都没动。
- 工作区 `init.ps1` 通过；`309c5b1` 加这次改动的干净检出（没有 `.env`）：2534 passed、29 skipped、0 failed，比工作区多跳过的 1 个是依赖本机环境的跳过项，总数相同。临时检出和它的测试目录已按名字删除。
