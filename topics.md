# 已讲知识点台账

格式：日期 | 栏目 | 知识点 | 关键词 | 来源

2026-11-24 | 前沿 | 离线记忆整理（dreaming）：对照原始记录纠错、顶层小索引往上提细节往下沉、按目标整理、单写者页面 | sleep-time compute, 记忆复核, 记忆层级, wiki, 出处回溯 | https://www.anthropic.com/research/components-of-agent-memory
2026-11-24 | 前沿 | 评测方法论：观察与因果设计、构念效度、逐题配对差值置信区间、评分器校准（原子标准、kappa、大声报错）、事先登记、多轮模拟用户 | paired comparison, McNemar, Cohen's kappa, preregistration, LLM judge 偏差 | https://www.anthropic.com/research/building-strong-safety-evaluations
2026-11-24 | 前沿 | Agent 隔离：环境层封顶影响范围、三种隔离形态、信任确认前执行配置、白名单即能力授予、持久记忆投毒、多 Agent 信任升级 | containment, egress allowlist, confused deputy, 符号链接先解析, trust escalation | https://www.anthropic.com/research/how-we-contain-claude
2026-11-24 | 面试 | MCP 工具注解不可信：信任来源是部署者而非工具，注解只能往严里用 | readOnlyHint, tool annotations, 信任传递 | MCP 规范 2025-06-18 server/tools
2026-11-24 | 面试 | 提示缓存前缀匹配：KV cache 依赖全部前缀、层级失效、回看窗口、动态内容后置、前缀稳定性测试 | prompt caching, KV cache, cache breakpoint, lookback | https://platform.claude.com/docs/en/build-with-claude/prompt-caching
2026-11-25 | 前沿 | Agent 被挡住后的“坚持”与绕路；权限分类器：三层放行、只看用户消息和工具调用、两段式判定、拒绝后继续（3 连拒/20 累计交人）、子 Agent 派出与返回双检 | persistence, overeager, auto mode, transcript classifier, deny-and-continue, reasoning-blind | https://www.anthropic.com/research/investigating-unintended-model-actions ; https://www.anthropic.com/research/claude-code-auto-mode
2026-11-25 | 前沿 | 生成—评估分离、事先约定验收条件（sprint contract）、评估者实际操作产品、上下文重置与压缩、逐个拆组件消融 | generator-evaluator, self-evaluation bias, context anxiety, sprint contract, harness ablation | https://www.anthropic.com/research/harness-design-long-running-apps
2026-11-25 | 基础 | 工具定义按需加载与工具搜索；即时上下文与 grep 代码搜索 | defer_loading, tool_reference, regex/BM25 工具检索, just-in-time context, 前缀追加 | https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool ; https://www.anthropic.com/research/advanced-tool-use
2026-11-25 | 面试 | 工具调用超时的重试语义：最多一次/至少一次、幂等、幂等键、条件写入与重试前查状态 | idempotency key, at-most-once, CAS, edit_file 条件写入 | 通用分布式系统原理；Aletheia D-032/D-085
2026-11-25 | 面试 | 约束解码与事后校验修复的取舍：令牌掩码、分布扭曲、Schema 限制、两者叠加 | constrained decoding, grammar, structured outputs, logit mask | https://platform.claude.com/docs/en/build-with-claude/structured-outputs
