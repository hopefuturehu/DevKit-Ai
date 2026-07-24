# LLM Episode 记忆整合

> 状态：融合版终态
> 适用范围：运行时上下文压缩、跨会话语义记忆、回溯与治理
> 参考模式：[Harness Engineering Guide 6.4 Memory Consolidation](https://yeasy.gitbook.io/harness_engineering_guide/di-er-bu-fen-harness-he-xin-zi-xi-tong/06_memory/6.4_memory_consolidation)

## 1. 核心决策

生产运行不再用 `ContextSnapshot` 生成或恢复压缩上下文。消息、Tool Run 和事件是不可变
System of Record；LLM 对不可变消息区间生成 Episode 摘要，摘要和经过确定性验证的
Memory Card 构成发送给模型的压缩视图。

旧 `context_snapshots` 表、`ContextSnapshot` 和 `compact_messages()` 仅用于读取旧数据库
及兼容旧调用方。新运行不会创建、复制或消费 snapshot。

## 2. 三层记忆

| 层 | 数据 | 生命周期 | 用途 |
|---|---|---|---|
| Working | 游标之后的原始消息、当前 Tool 原子组 | 当前会话 | 保持正在执行任务的精确上下文 |
| Episodic | 不可变 Episode 和 LLM 结构化摘要 | 会话、分叉继承 | 压缩历史、保留因果和验证链 |
| Semantic | 版本化 Memory Card | session 或 workspace | 跨会话复用稳定事实、偏好和经验 |

完整原始消息不会因压缩而删除。模型可先调用 `search_memory` 检索，再通过
`load_memory_source` 按 Episode ID 回溯原文或读取 Memory Card 版本历史。

## 3. 触发机制

以下条件采用或逻辑：

1. Time Gate：距上次成功整合超过 `memory.time_gate_hours`；从未成功整合时，以最旧
   pending Episode 的年龄计算。
2. Session Gate：pending Episode 数达到 `memory.session_gate`。
3. Explicit Lock：`/consolidate`、`/compact`，或用户明确说“保存进度/整合记忆”等。
4. Context Pressure：未整合内容达到 `memory.context_utilization_gate`，或运行时上下文
   达到 `context.auto_compact_threshold`。

`/consolidate` 处理所有 pending Episode；`/compact` 同时推进压缩游标。单次模型请求受
`max_episodes_per_run` 和 `max_source_chars` 限制，多批处理受
`max_consolidation_batches` 限制。

## 4. 四阶段管道

### Orient

LLM 为每个 Episode 输出 `title`、`objective`、`topics`、`keywords`、`depth` 和
`summary`。摘要必须保留任务目标、关键决定、原因、执行结果和验证关系。

### Gather

LLM 只能从选中 Episode 提取候选操作。候选类型包括 preference、project、lesson、
decision、error、constraint、artifact、verification 和 task。候选必须带：

- `memory_key`：稳定语义键，且以类型开头；
- `source_positions`：本批 Episode 内真实消息位置；
- `evidence_refs`：消息、成功 Tool 或已有 blob 引用；
- `confidence`、`scope` 和 `operation`。

### Consolidate

LLM 输出只具有候选资格。Harness 在写入前确定性验证：

- preference/constraint 必须包含用户来源；
- verification/artifact 必须引用成功 Tool Run；
- source、blob、目标 Card、类型、语义键和作用域必须真实且匹配；
- 置信度必须达到配置门槛；
- resolve/retract 必须引用当前可访问的有效 Card。

同一 `memory_key` 的 upsert 更新现有 Card；每次变更写入不可修改的版本记录。Card 保存
`episode:<id>:message:<position>` 限定来源，跨会话后不会混淆消息位置。

### Prune

Prune 只做软失效，不删除原始消息、Episode、候选审计或 Card 版本。过期的 project、
task、error、artifact 和 verification 按最后访问时间失效；容量超限时综合访问次数、
置信度和时间选择低价值临时 Card。constraint、preference、decision 和 lesson 不会仅因
时间自动丢弃。

## 5. 上下文投影

`consolidated_memory_cursor()` 只跨越连续、已成功发布的 Episode。运行恢复时：

1. 游标前历史由相关 Episode 摘要表示；
2. 游标后消息按原文加载，并保持 assistant Tool Call 与 Tool Result 原子性；
3. workspace/session Card 经倒排关键词召回和确定性重排；
4. Episode 摘要和 Card 分别受独立 Token 预算控制；
5. 所有派生记忆以 `role=user` 注入，不能提升为 System 指令。

上下文压力触发时，Runner 只在 Tool 原子组边界切分旧前缀。整合成功并原子发布后才推进
游标；失败时 Episode 恢复 pending，原始消息和当前游标保持不变。

## 6. 一致性与故障恢复

- Episode 通过 `(session_id, start_position, end_position)` 唯一，支持同一 run 分段。
- 开始整合时以事务把 Episode 从 pending claim 为 consolidating，避免重复并发处理。
- 摘要、候选审计、Card/版本、索引、Prune 和 ready 状态在同一事务发布。
- LLM/校验/写入失败会将 claimed Episode 恢复 pending，下次可重试。
- 超过一小时的孤立 building 运行在启动迁移时恢复为 failed/pending。
- 连续失败达到 `failure_warning_threshold` 时，失败事件标记需要人工检查。
- 分叉复制已整合 Episode 摘要和游标，不复制旧 snapshot；原始消息仍按既有规则复制。

## 7. 检索、索引和指标

Memory Card 写入或更新时同步维护 SQLite 倒排词项。召回结合关键词覆盖、类型权重、
session 作用域、置信度、访问次数和当前请求；Episode 结合主题覆盖、深浅范围和时序。

SQLite 持久化：

- 整合输入/输出字符数、Token、耗时和压缩率；
- 检索候选数、命中 Card/Episode、耗时、零命中率；
- Card 的访问次数和最近访问时间；
- 被接受和拒绝的候选及拒绝原因；
- 每个 Card 的完整版本链。

`/status` 展示游标、pending/ready 数量、连续失败和聚合指标；`/memory-cards` 查看有效
Card，`/memory-history <id>` 查看版本，`/memory-forget <id>` 以可审计版本执行用户撤销。

## 8. 验收不变量

1. 新运行不会写入或读取 snapshot。
2. 压缩后原始消息数量和内容不变。
3. 只有连续 ready Episode 能推进上下文游标。
4. LLM 无效 JSON、遗漏 Episode、伪造 Tool、越权目标或低置信候选不能污染有效 Card。
5. 同一稳定键不会产生多个有效冲突 Card。
6. workspace Card 可跨会话检索；session Card 不越界。
7. Episode 原文回溯受工作区边界约束。
8. 并发整合不会对同一 Episode 重复发布。
9. Prune 可逆且保留版本审计。
10. 数据库 v7 升级到 v8 时保留已有 Episode 和 Card。
