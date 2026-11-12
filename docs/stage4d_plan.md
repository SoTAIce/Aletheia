# 第 4 阶段 4d：模型档案（设计）

日期：2026-11-12。状态：**用户已回答第 7 节；4d-1、4d-3 已实现（第 9 节，D-078），4d-2、4d-4 等有第二个模型再做**（stage4d-001）。
来源：用户 2026-11-12“接下来来设计4d方案吧”。依据是当前 `main`（`e6973f3`）和 [第 4 阶段方案](stage4_plan.md) 第 3 节“4d 模型档案”。

## 1. 现状（以代码为准）

**对话模型只有一个，设置分散在各处。**

| 内容 | 现在在哪里 |
|---|---|
| 地址、密钥、模型名 | `config.agent_model_endpoint`：设了 `AGENT_MODEL_BASE_URL` 就用它和 `AGENT_MODEL_API_KEY`，否则用 DashScope 的地址和密钥；模型名是 `AGENT_MODEL`，没填就用 `RAG_MODEL`（D-050） |
| 请求参数 | `rag_agent_service._base_models` 构造 `OpenAICompatibleToolCallingModel`：`temperature=0`；`json_mode=RAG_JSON_MODE`；`max_output_tokens=CONTEXT_OUTPUT_TOKENS`；单次请求时限 `RAG_MODEL_TIMEOUT_SECONDS` |
| 按服务商推断的能力 | `enable_thinking` 只发给 DashScope（`None if config.agent_model_base_url`）；`prompt_cache_key` 只发给独立地址且 `AGENT_MODEL_PROMPT_CACHE_KEY=true`（D-057）。同样的推断在 `cache_probe.py` 里又写了一遍 |
| 上下文窗口 | `config.agent_model_window_tokens`（`AGENT_MODEL_CONTEXT_TOKENS`，没填用 `CONTEXT_WINDOW_TOKENS`），交给 `ContextManager`，工具结果预算随它缩放（D-052） |
| 价格 | `MODEL_INPUT_PRICE_PER_MILLION_TOKENS`、`MODEL_OUTPUT_PRICE_PER_MILLION_TOKENS`，`run_meter.estimated_cost` 按整次运行的总令牌一次算（D-034） |

**同一个模型对象用在所有角色上。** `RunAssembler` 把一个 `model` 交给：路由选择（`model_purpose('route')`）、单一循环、团队负责人、团队成员（`MemberSpawner`）、按需委派的调研成员（`ExploreDelegation`）。成员和主循环共用同一个 `ContextManager`。

**不在范围内的模型**（继续走 DashScope，不进档案）：嵌入、知识库查询改写（`RAG_QUERY_REWRITE_MODEL`）、长期记忆的抽取和合并（`RAG_MODEL`）、联网搜索演示（`DASHSCOPE_MODEL`）。

**计量。** `RunMeter.record` 记每次调用的令牌和响应里报的模型名（`response_models`，供追踪用），但不知道这次调用属于哪个角色、哪个档案；`estimated_cost` 只有一组价格。

## 2. 为什么现在做

[按需委派评测](loop_delegate_plan.md) 第 11 节的结论：真委派时令牌是不开时的 6.2 倍，每次成员请求都要付约 2 万令牌的固定开头。调研成员和团队成员做的多是读文件、搜索、汇总，换成便宜、快的模型可能就划算了；路由选择只输出一个词，也不需要主模型。但这一切的前提是**有第二个模型**。只有一个模型时，档案只是把分散的设置收拢，行为上没有好处（第 4 阶段方案当初建议推迟的理由）。

## 3. 目标

```
档案（ModelProfile）  名称、地址、密钥所在的环境变量名、模型名、窗口、输出上限、时限、
                      温度、JSON 模式、思考开关、是否发送缓存键、输入/输出单价
档案集（ModelProfiles）  若干档案 + 角色分配：main / route / member / explorer
                        没分配的角色用 main
运行装配（RunAssembler）  按角色取模型，交给路由选择、主循环、团队成员、调研成员
计量（RunMeter）         每次调用记档案名；用量里加 by_profile，费用按档案分别计价再相加
```

不变的东西：没有档案文件时，从现有 `.env` 变量合成唯一的 `default` 档案，所有角色都用它；发给模型的请求逐字节不变，用量字段只增不改。

## 4. 设计

### 4.1 档案结构（`app/models/model_profile.py`）

```python
@dataclass(frozen=True, slots=True)
class ModelProfile:
    name: str
    base_url: str
    api_key_env: str            # 存密钥的环境变量名，档案里永远不放密钥本身
    model: str
    context_tokens: int
    max_output_tokens: int
    timeout_seconds: float
    temperature: float | None = 0
    json_mode: bool = True
    enable_thinking: bool | None = None   # None = 不发这个参数
    prompt_cache_key: bool = False        # 是否发 prompt_cache_key
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
```

- 能力不再按“是不是 DashScope”推断，写在档案里。默认档案按今天的推断规则填：独立地址时 `enable_thinking=None`、`prompt_cache_key=AGENT_MODEL_PROMPT_CACHE_KEY`；DashScope 时 `enable_thinking=RAG_ENABLE_THINKING`、`prompt_cache_key=False`。
- `api_key_env` 保留 D-050 的安全约束：独立地址不能悄悄用 DashScope 的密钥。档案文件里每个档案都必须写明用哪个变量，变量为空就报错。
- `repr` 和日志只出现档案名、地址的主机名和模型名，不出现密钥。

### 4.2 档案从哪里来

- **默认（不建文件）**：`config.model_profiles()` 从现有变量合成 `default` 档案，四个角色都指向它。现有用户什么都不用改。
- **档案文件（可选）**：`MODEL_PROFILES_FILE` 指向一个 TOML 文件（默认 `models.toml`，不存在就走默认）。用标准库 `tomllib` 读，不加依赖。仓库提供 `models.example.toml`，`models.toml` 加进 `.gitignore`（里面没有密钥，但是个人设置）。

```toml
[profiles.main]
base_url = "https://<中转站>/v1"
api_key_env = "AGENT_MODEL_API_KEY"
model = "gpt-5.6-luna"
context_tokens = 128000
prompt_cache_key = true

[profiles.cheap]
base_url = "https://<另一个服务>/v1"
api_key_env = "CHEAP_MODEL_API_KEY"
model = "<便宜模型>"
context_tokens = 64000
input_price_per_million = 1.0
output_price_per_million = 4.0

[roles]
main = "main"
route = "cheap"
member = "cheap"
explorer = "cheap"
```

- 档案里没写的字段取默认档案的同名值（时限、输出上限、JSON 模式等），避免每个档案都抄一遍。
- 校验：未知字段、角色指向不存在的档案、`main` 角色缺失、密钥变量为空，都在启动时报错，并给出中文说明；错误信息里不带密钥。

### 4.3 模型的建立

- `_base_models` 改为按档案建模型：每个用到的档案一个 `OpenAICompatibleToolCallingModel`；地址和密钥相同的档案共用一个 `AsyncOpenAI` 客户端。只用一个档案时，结构和今天完全相同。
- 每个模型外面套一层很薄的标记（档案名），`HookedModel` 和计量据此记账。
- `prompt_cache_key` 仍按会话算，只发给 `prompt_cache_key=true` 的档案。不同模型之间本来就不共享缓存，这一点不变。
- `cache_probe.py` 改用档案，不再自己推断。

### 4.4 角色接线

| 角色 | 用在哪里 | 改动 |
|---|---|---|
| `main` | 单一循环、团队负责人 | 不变 |
| `route` | `route_selection.choose_mode` | `RunAssembler._choose_route` 取 `route` 模型 |
| `member` | 团队成员（`MemberSpawner`） | `DelegatingLoopRunner` 多收一个成员模型 |
| `explorer` | 按需委派的调研成员（`ExploreDelegation`） | 同上 |

- **成员的上下文窗口**：成员档案的窗口可能比主模型小。成员改用按自己档案窗口建的 `ContextManager`（共享同一个归档），避免按 12.8 万的预算给 6.4 万窗口的模型塞工具结果。角色都用 `main` 时仍共用原来那个，行为不变。
- 网页和命令行**不加**选模型的入口，角色分配只在档案文件里改。需要的话以后另做。

### 4.5 计量、费用和追踪

- `RunMeter.record` 多收一个档案名；`snapshot()` 现有字段不变，新增：
  - `by_profile`：`{档案名: {model_calls, prompt_tokens, completion_tokens, cached_prompt_tokens, estimated_cost}}`；
  - `estimated_cost` 改为各档案费用相加；有任何一个用到的档案没写价格就是 `None`（和今天“没配价格就不估”一致）。
- 默认档案的价格来自现有的两个价格变量，所以只有一个档案时 `estimated_cost` 和今天相同。
- 追踪（D-073）每次调用多记档案名；`app.agent.trace` 查看器显示它。评测输出的 `calls` 每项加 `profile`。
- 预算（`max_total_tokens`）仍按令牌，不按钱：不同模型令牌价格不同，但改成按钱算是另一个决定，见第 7 节第 4 问。

### 4.6 可选：每次模型调用前检查预算（D-076、D-077 记下的遗留）

现在循环只在一步开始前和结束后查预算，一步里的多次模型调用之间不查（`tests/test_loop_run_guarantees.py` 固定了这一点）。成员换成便宜模型后调用次数会更多，所以把它放在这里一起决定：在计量包装里，每次请求发出前查 `budget_problem()`，超了就以预算失败结束，不再发请求。这会改变一个固定下来的行为，要改那项测试，所以单列，见第 7 节第 4 问。

## 5. 分步（每步单独测试、单独提交）

| 步 | 内容 | 行为变化 | 验证 |
|---|---|---|---|
| 4d-1 | 档案结构；从 `.env` 合成默认档案；`_base_models`、`estimated_cost`、`cache_probe` 改读档案 | 无 | 默认档案与旧设置逐项对照（DashScope、独立地址、开关缓存键、有无价格的组合）；67 个期望值文件对照仍全部通过；请求参数抓取对照 |
| 4d-2 | 档案文件和角色；按角色接线；成员按自己的窗口建 `ContextManager`；计量、追踪、评测输出按档案记 | 只有建了档案文件才有 | 文件解析和校验；角色回落；成员确实用成员模型；`by_profile` 和费用相加；没有文件时与 4d-1 完全一致 |
| 4d-3（可选） | 每次模型调用前检查预算 | 有：一步内超预算会提前停 | 改 `test_loop_run_guarantees.py` 那一项，并补成员循环里超预算的测试 |
| 4d-4（有第二个模型才做） | 真实评测 | — | 见第 6 节 |

4d-1 是纯整理，可以先做，也可以等第 7 节第 1 问的答案再决定做不做。

## 6. 评测

- 4d-1、4d-2 在没有档案文件时行为不变，由测试保证，不需要真实模型评测。
- 有第二个模型时（4d-4，每次都要你当轮授权）：
  1. 调参集团队 11 次，成员用便宜模型（`--write --sandbox`，同 4b），对照 4c 的团队 10/11：通过数不低于 4c，同时比较令牌、费用和耗时；
  2. 探针题 `probe_config_keys`、`probe_event_flow` 开委派各 1 次，调研成员用便宜模型，对照 [按需委派评测](loop_delegate_plan.md) 第 11 节（6.2 倍令牌）；
  3. 可选：调参集单一循环 11 次，只把路由换成便宜模型（单一循环不走路由时可跳过；`--modes auto` 才用到）。
- 按 D-035 不看留出集调提示；留出集只在最后确认时跑。
- 服务商的少开销状态仍可能出现，评测时照例记录每题第一次请求的提示令牌。

## 7. 需要你确认的问题

1. **有没有第二个模型？** 有的话，是哪个服务、什么模型（地址和模型名，不要发密钥）。
   - (a) 有：做 4d-1、4d-2，然后评测（4d-4）；
   - (b) 暂时没有：只做 4d-1，把设置收拢、能力不再靠推断，行为不变；4d-2 等有了第二个模型再做；
   - (c) 继续推迟整个 4d。

   建议：有现成的便宜模型就选 (a)，否则选 (b)。(b) 的好处是 `enable_thinking`、缓存键这些按服务商推断的规则写明在档案里，以后换中转站不容易踩坑；代价是一次纯整理的改动。
2. **档案放在哪？** (a) 单独的 `models.toml`（建议：多个档案用环境变量写会很长，TOML 能表达角色分配，标准库能读）；(b) 全用 `.env` 变量，例如 `MODEL_PROFILE_CHEAP_BASE_URL`。
3. **哪些角色允许单独换模型？** 建议 `route`、`member`、`explorer` 三个都允许，没配就用主模型。主循环和团队负责人始终用 `main`。
4. **预算两件事**：
   - 每次模型调用前检查预算（4d-3）要不要并入？建议并入，单独提交；
   - 预算继续按令牌（建议），还是增加按钱的上限？
5. **由谁实现？** Codex 额度 11 月 15 日恢复。可以现在由 Claude 做，也可以等 Codex。4d-1 规模小，建议 Claude 现在做；4d-2 可以交给 Codex。

**用户回答（2026-11-12）**：
- 第 1 问：暂时没有第二个模型；以后可能加入 DeepSeek、Qwen、Claude，但先不用。按建议只做 4d-1；4d-2（档案文件和角色）、4d-4（评测）等真要加模型时再做。
- 第 2～4 问：按建议。以后的档案放单独的 `models.toml`；`route`、`member`、`explorer` 三个角色都可以单独换模型；每次模型调用前检查预算（4d-3）并入、单独提交，它不依赖第二个模型，所以现在就做；预算继续按令牌。
- 第 5 问：由 Claude 实现。

**为以后加模型记下的一点**：DeepSeek 和 Qwen 都有 OpenAI 兼容接口，加进档案即可。Claude 也提供 OpenAI 兼容接口，但提示缓存、思考参数的写法和 OpenAI 不同；到时候要么只用兼容接口能表达的部分，要么给档案加一个 `api` 字段并写一个 Anthropic 原生适配器。这要等真要接入时再定。

## 8. 不改的

- 嵌入、知识库查询改写、长期记忆、联网搜索演示继续走 DashScope 的现有设置。
- `.env` 现有变量的含义和默认值都不变；不自动建 `models.toml`，也不自动改 `.env`。
- 请求开头的缓存友好结构（D-057）、hooks、副作用保护、失败分类、团队完成门槛（D-075）、按需委派默认关闭。
- 网页和命令行不加选模型的入口。

## 9. 实现记录

### 4d-1 档案结构（2026-11-12，Claude）

- 新增 `app/models/model_profile.py`：`ModelProfile`（字段同第 4.1 节；`api_key_env` 只记密钥所在的设置或环境变量名，`api_key(settings)` 用时才读；`estimated_cost` 按档案单价算）。
- `Settings.agent_model_profile`：从现有设置合成 `default` 档案，原来按“是不是 DashScope”推断的规则写进这里（独立地址不发 `enable_thinking`、按开关发缓存键；DashScope 相反）。`agent_model_endpoint`、`agent_model_window_tokens` 保留，改为从档案取。
- 改为读档案的地方：`rag_agent_service._base_models`（新函数 `profile_model` 按档案建模型）、`_prompt_cache_key`、`_prepare_task` 的 `ContextManager` 窗口和输出上限、`run_meter.estimated_cost`、`cache_probe`。
- 温度保留整数 `0`：改成 `0.0` 会让请求里的 `"temperature": 0` 变成 `0.0`，对照测试会抓到（试过，24 项失败）。
- 测试 `tests/test_model_profile.py`（38 项）：新旧两种构造在 24 种设置组合下（DashScope / 独立地址 × 思考开关三种 × JSON 模式 × 缓存键开关）发出的请求逐字节相同，包括地址和认证头；默认档案逐项对应设置；档案的 `repr` 不含密钥；密钥变量回落到环境变量；非法档案被拒；缓存键只发给支持的档案。
- 全部测试（Linux）：1760 通过、22 跳过、4 失败（只在 Windows 上成立的路径断言，改动前同样）。

### 4d-3 每次模型调用前检查预算（2026-11-12，Claude）

- `AgentLoopRunner._generate` 每次发请求前先查 `RunMeter.budget_problem()`（令牌和时间预算），超了就抛新的 `StepBudgetError`（`app/agent/step_runner.py`），不再发请求；`LoopRun` 收到后以 `budget` 结束运行，说明文字仍是计量器原来的 “Agent token budget reached (… of … tokens)”。
- 单一循环、团队负责人、团队成员、按需委派的调研成员都是 `AgentLoopRunner`，所以都在每次调用前检查。成员超预算时，`delegate` 工具照原样把它当成员失败报给负责人；负责人下一次调用同样被拦下，整个运行以 `budget` 结束。路由选择不经过循环，不受影响（它在运行开始时调用，此时预算还没用）。
- 行为变化：以前一步里的所有调用都会发出，一步结束后才以预算失败；现在超预算后的那次调用不发。已经执行的工具不撤销，这一步没有结果（`steps` 为空），步骤日志里照常记下已执行的工具。
- 测试：`tests/test_loop_run_guarantees.py` 里固定旧行为的那一项改为 `test_the_token_budget_is_checked_before_every_model_call`（3 次调用变 2 次）；新增 `test_a_spent_budget_stops_a_member_loop_before_its_first_call`。67 个期望值对照全部通过（没有一项在一步内超预算）。
- 全部测试（Linux）：1761 通过、22 跳过、4 失败（同上，只在 Windows 上成立）。
- 没有真实模型评测：预算默认不设上限（命令行和网页都不传 `max_total_tokens`，评测也不设），默认运行不受影响。

