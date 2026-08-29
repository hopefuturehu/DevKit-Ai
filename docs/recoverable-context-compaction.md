# 可恢复的单摘要上下文压缩

> 状态：当前实现说明
>
> 核对日期：2026-08-26

## 目标

运行时上下文压缩采用 Claude Code 风格的单摘要模型：任意时刻只向主模型注入一个活动
摘要，摘要之后拼接未覆盖的原始消息尾部。与一次性压缩不同，本实现保留完整 Transcript，
并为每个摘要记录来源范围、来源哈希、父版本和发布状态。

运行时视图为：

```text
Core / Project / Skills / USER.md
                  +
        一个 Active Compaction
                  +
        cursor 之后的原始消息
                  +
       Memory Router / Runtime Note；按需自动记忆作为 Tool Result
```

旧的 `ContextSnapshot` 数据仍可读取，但不参与默认运行时上下文装配。已有数据库中曾由
旧版本创建的语义记忆表不会在升级时被破坏性删除，新版本也不再访问这些表。

## 四个不变量

1. **事务发布**：新记录先以 `building` 写入。LLM 输出通过章节、来源范围和 Token
   预算校验后，才在一个 SQLite 事务里把旧 `ready` 改为 `superseded`、新记录改为
   `ready`。
2. **原始记录不删除**：压缩只推进派生视图的 `cursor`，不删除或改写 `messages`、
   Tool Run 和事件。
3. **来源可验证**：每个活动摘要保存连续覆盖范围、该范围消息的 SHA-256 和初始目标锚点。
   默认 `range` 模式下 `source_refs_json` 允许为空，不要求正文逐条引用；`item` 兼容模式才保存并
   校验摘要中的 `[m:N]` 引用。加载和恢复时按覆盖范围重新读取原文并计算哈希。
4. **失败不推进**：超时、Provider 错误、缺章节、越界引用或摘要超预算都只会把
   `building` 标记为 `failed`。旧活动摘要和游标保持不变。

## 数据与状态

`context_compactions` 保存所有版本：

- `covered_start_position` / `covered_end_position`：摘要覆盖的完整原始范围；
- `delta_start_position`：本次送给 LLM 的新增原始范围起点；
- `parent_id`：生成本版本时使用的活动摘要；
- `source_sha256`：覆盖范围内完整 SQLite 消息的来源证明，包含 `reasoning_content`；它校验
  消息内的 `context_ref`，不会展开后再重复哈希 blob 正文，blob 自身另以内容 SHA-256 寻址；
- `source_refs_json`：只保存摘要正文实际出现的 `[m:N]` 引用，默认 `range` 模式通常为空；
- `anchor_positions_json`：最早一条非空用户消息的位置，作为初始目标锚点；
- `status`：`building → ready → superseded`，失败进入 `failed`。

每个会话最多有一个 `ready` 和一个 `building` 记录，由 SQLite 部分唯一索引保证。发布时还
会再次检查 `parent_id` 是否仍是当前活动版本，避免并发压缩把旧结果覆盖到新结果之上。

## 压缩与恢复流程

压力触发时，Agent 保留最近的完整消息组，将更早的连续前缀交给 `ContextCompactor`。压缩器
还会按 `context.compaction_max_input_tokens` 对较早前缀二次分块，确保 System Prompt、旧摘要、
新增原文和 JSON 包装的总输入不超过压缩预算。两层切分都不会拆开 Assistant Tool Call 和
对应 Tool Result。长时间未成功压缩的 backlog 因此会按最老安全前缀逐步推进，而不会一次
生成超过模型窗口的追赶请求。

`recent_conversation_tokens=20000` 是软目标；系统会优先满足
`compaction_min_recent_user_turns=3`，所以三个用户轮次或一个完整 Tool 原子组本身很大时，近期
tail 可以超过 20K。普通压力每个 Agent step 只压缩一个分块；`/compact` 才会在空闲会话中循环
追赶多个分块。

摘要输入对每条消息正文先做 12,000 字符 head/tail 限制，Tool 参数在正常路径保持结构化原值；
若最早的完整原子组仍放不进 48K 规划目标，再依次尝试 2,000 和 512 字符的降级视图。摘要输入
保留 role、content、name、Tool Call/Result 字段，但不携带 `reasoning_content`；来源 SHA-256
仍覆盖原始持久消息的完整字段。若 512 字符降级视图仍放不下，返回
`source_group_exceeds_budget`，不调用模型也不推进 cursor。

首次压缩从原文生成摘要；后续通常用“上一份摘要 + 新增原文”做增量更新。每累计
`context.compaction_rebuild_every` 个成功版本，且完整原文仍能安全放入压缩模型上下文时，
系统从原文重建一次，降低多轮摘要漂移。也可执行：

```text
/compact
/compact rebuild
/compact rollback <compaction-id>
```

摘要候选先做本地规范化；空响应可按同一生成请求重试一次，格式仍不合法时只携带候选摘要
发起一次修复，长度截断则进入候选凝练。transport 错误同范围重试，只有 Context overflow 才
缩小 Tool 原子范围；鉴权、支付、配置和持久化错误立即停止。每个压缩 Provider 请求有 90 秒
墙钟；一次 `compact()` 默认最多消耗 8 个请求，并以 `$0.25` 为请求间费用停止阈值，这两项门禁
也用于自动压力压缩。费用在一次请求完成后才可得，因此最终一次请求可能使累计值略微越过
阈值。显式 `/compact` 还以 600 秒总墙钟循环多个分块，并按剩余请求数和累计费用停止。普通
Agent Run 另外受 `agent.max_cost_usd` 约束；费用门禁要求配置模型的输入、输出单价。普通压力下
相同 parent/delta 失败后默认退避 300 秒；显式压缩、强制 Provider 恢复和 rebuild 不走这条
退避。

恢复会话时只加载 `ready` 版本。若哈希或摘要结构校验失败，该版本转为 `failed`，系统沿
`parent_id` 自动恢复最近的有效父版本。初始用户目标位置作为锚点保存，并以原文逐字放入
活动压缩消息，避免目标只依赖派生摘要。

模型还可按需调用：

- `search_session_history`：在当前会话完整 Transcript 中检索；
- `load_compaction_source`：按压缩 ID 和消息范围读取原文。

## 摘要过大时

系统不会叠加多份摘要。每次生成的新摘要必须替换旧摘要，并受
`context.compaction_summary_tokens` 可见正文硬限制；软目标由
`context.compaction_summary_target_tokens` 控制。长度截断只会凝练候选，不会重发完整原文；
仍无法通过时发布失败、游标不推进。随着任务增长，
低价值已完成步骤应在下一版中合并，目标、约束、未完成事项、失败、决定和关键文件继续
保留。

压缩失败只保证“不发布、不推进 cursor”，不保证当前 Run 一定停止。随后 Context Planner 会
先卸载非 pinned 的 Skill、Memory、历史消息组等可选项；这些组按优先级和新旧程度装箱，可能
形成非连续会话视图。只有 Core/Project/Environment、活动摘要、最新用户消息等 pinned 内容与
已选 Tool schema 仍超过硬窗口时，Planner 才报告 `context_limit` 并停止主请求。此时可从原文
重建更紧凑摘要、降低摘要预算、卸载可重载 Skill/Tool schema，或换用更大上下文模型。

## 配置

```toml
[context]
# 留空时冻结启动时的 model.name，后续 /model 不影响压缩
# compaction_model = "low-cost-summary-model"
recent_conversation_tokens = 20000
compaction_summary_target_tokens = 3000
compaction_summary_tokens = 4000
compaction_max_output_tokens = 8192
compaction_max_input_tokens = 60000
compaction_input_target_ratio = 0.8
compaction_repair_attempts = 1
compaction_condense_attempts = 1
compaction_empty_retries = 1
compaction_transport_retries = 1
compaction_transport_retry_backoff_seconds = 1
compaction_range_attempts = 2
compaction_failure_backoff_seconds = 300
compaction_request_timeout_seconds = 90
compaction_command_max_requests = 8
compaction_command_max_seconds = 600
compaction_command_max_cost_usd = 0.25
compaction_min_recent_user_turns = 3
compaction_source_refs = "range"
compaction_thinking = "auto"
compaction_max_message_chars = 12000
compaction_rebuild_every = 5
```

`compaction_thinking = "auto"` 会在 DeepSeek 官方端点上仅为压缩请求发送
`thinking.type = "disabled"`，避免结构化摘要消耗大量不可见推理 token；其他 OpenAI-compatible
端点沿用 Provider 默认行为。可按端点能力显式改为 `provider_default`、`enabled` 或
`disabled`，普通 Agent 请求不受此配置影响。

## 测试

`tests/unit/test_context_compaction.py` 验证事务发布、原文保留、有界分块、分类恢复、候选凝练、
请求超时、范围缩小、失败退避、回滚、模型隔离、范围来源和损坏自动降级。

`tests/integration/test_context_compaction_benchmark.py` 以 10 阶段长任务验证运行时只注入一个
摘要、保留近期原文、Tool 原子性、原始消息摘要不变和 snapshot-free。

`scripts/run_compaction_effectiveness.py` 使用确定性或显式开启的真实 Provider 回放 success、
length、format、429、Context overflow 和 authentication 场景，产出请求级 JSONL 与质量门禁。

`tests/integration/test_long_context_cache_benchmark.py` 使用离线确定性 Provider，让同一个任务
经历多次“增长 → 压缩 → 再增长”，并与不压缩反事实比较缓存折算后的单轮成本。生产窗口规模
的 `tests/soak/test_context_cache_soak.py` 由 `RUN_CONTEXT_CACHE_SOAK=1` 显式开启。完整指标、
产物和控制变量方法见 [长任务上下文缓存评测](context-cache-benchmark.md)。
