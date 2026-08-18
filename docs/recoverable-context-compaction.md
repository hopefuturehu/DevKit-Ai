# 可恢复的单摘要上下文压缩

## 目标

运行时上下文压缩采用 Claude Code 风格的单摘要模型：任意时刻只向主模型注入一个活动
摘要，摘要之后拼接未覆盖的原始消息尾部。与一次性压缩不同，本实现保留完整 Transcript，
并为每个摘要记录来源范围、摘要哈希、父版本和发布状态。

运行时视图为：

```text
Core / Project / Skills / USER.md / 自动记忆短索引
                  +
        一个 Active Compaction
                  +
        cursor 之后的原始消息
```

旧的 `ContextSnapshot` 数据仍可读取，但不参与默认运行时上下文装配。已有数据库中曾由
旧版本创建的语义记忆表不会在升级时被破坏性删除，新版本也不再访问这些表。

## 四个不变量

1. **事务发布**：新记录先以 `building` 写入。LLM 输出通过章节、来源范围和 Token
   预算校验后，才在一个 SQLite 事务里把旧 `ready` 改为 `superseded`、新记录改为
   `ready`。
2. **原始记录不删除**：压缩只推进派生视图的 `cursor`，不删除或改写 `messages`、
   Tool Run 和事件。
3. **来源可验证**：每个活动摘要保存覆盖范围和该范围原始消息的 SHA-256；摘要事实使用
   `[m:N]` 或 `[m:N-M]` 引用。加载和恢复时重新计算哈希并校验引用。
4. **失败不推进**：超时、Provider 错误、缺章节、越界引用或摘要超预算都只会把
   `building` 标记为 `failed`。旧活动摘要和游标保持不变。

## 数据与状态

`context_compactions` 保存所有版本：

- `covered_start_position` / `covered_end_position`：摘要覆盖的完整原始范围；
- `delta_start_position`：本次送给 LLM 的新增原始范围起点；
- `parent_id`：生成本版本时使用的活动摘要；
- `source_sha256` / `source_refs_json` / `anchor_positions_json`：来源证明和初始目标锚点；
- `status`：`building → ready → superseded`，失败进入 `failed`。

每个会话最多有一个 `ready` 和一个 `building` 记录，由 SQLite 部分唯一索引保证。发布时还
会再次检查 `parent_id` 是否仍是当前活动版本，避免并发压缩把旧结果覆盖到新结果之上。

## 压缩与恢复流程

压力触发时，Agent 保留最近的完整消息组，将更早的连续前缀交给 `ContextCompactor`。压缩器
还会按 `context.compaction_max_input_tokens` 对较早前缀二次分块，确保 System Prompt、旧摘要、
新增原文和 JSON 包装的总输入不超过压缩预算。两层切分都不会拆开 Assistant Tool Call 和
对应 Tool Result。长时间未成功压缩的 backlog 因此会按最老安全前缀逐步推进，而不会一次
生成超过模型窗口的追赶请求。

首次压缩从原文生成摘要；后续通常用“上一份摘要 + 新增原文”做增量更新。每累计
`context.compaction_rebuild_every` 个成功版本，且完整原文仍能安全放入压缩模型上下文时，
系统从原文重建一次，降低多轮摘要漂移。也可执行：

```text
/compact
/compact rebuild
/compact rollback <compaction-id>
```

摘要首次校验失败时，系统只携带候选摘要和校验错误发起小型格式修复请求，不会重发整段
原文。修复仍失败时，本次覆盖范围会缩小后重试。所有范围尝试均失败后，相同父摘要和增量
起点进入持久化退避，避免每个 Agent step 重复消耗模型额度。压缩调用的 Token 和费用计入
所属主 Run，并受 `agent.max_cost_usd` 约束。

恢复会话时只加载 `ready` 版本。若哈希或摘要引用校验失败，该版本转为 `failed`，系统沿
`parent_id` 自动恢复最近的有效父版本。初始用户目标位置作为锚点保存，并以原文逐字放入
活动压缩消息，避免目标只依赖派生摘要。

模型还可按需调用：

- `search_session_history`：在当前会话完整 Transcript 中检索；
- `load_compaction_source`：按压缩 ID 和消息范围读取原文。

## 摘要过大时

系统不会叠加多份摘要。每次生成的新摘要必须替换旧摘要，并受
`context.compaction_summary_tokens` 硬限制；超出时发布失败、游标不推进。随着任务增长，
低价值已完成步骤应在下一版中合并，目标、约束、未完成事项、失败、决定和关键文件继续
保留。

如果“Core/Project/Skills + 单摘要 + 必须保留的最近消息”本身仍超过硬窗口，Context
Planner 会报告不可压缩层，而不是静默删除目标或伪造成功。此时可从原文重建更紧凑摘要、
降低摘要预算、卸载可重载 Skill/Tool schema，或换用更大上下文模型。

## 配置

```toml
[context]
# 留空时复用 model.name
# compaction_model = "low-cost-summary-model"
compaction_summary_tokens = 8000
compaction_max_output_tokens = 8192
compaction_max_input_tokens = 60000
compaction_input_target_ratio = 0.8
compaction_repair_attempts = 1
compaction_range_attempts = 2
compaction_failure_backoff_seconds = 300
compaction_max_message_chars = 12000
compaction_rebuild_every = 5
```

## 测试

`tests/unit/test_context_compaction.py` 验证事务发布、原文保留、有界分块、格式修复、范围缩小、
失败退避、回滚、原文重建、来源读取、检索和损坏自动降级。

`tests/integration/test_context_compaction_benchmark.py` 以 10 阶段长任务验证运行时只注入一个
摘要、保留近期原文、Tool 原子性、原始消息摘要不变和 snapshot-free。
