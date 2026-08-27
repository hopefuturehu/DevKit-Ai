# 模型上下文分块与组装顺序

> 状态：当前实现说明
>
> 核对日期：2026-08-27

本文描述主 Agent 每次调用模型时的实际请求视图。SQLite Transcript、压缩记录、Markdown
记忆和 Skill 文件是事实源；组装过程只生成本次 `ModelRequest`，不会为了排序或修复协议而改写
原始 Transcript。

Codex、OpenCode、Pi、Hermes Agent、DeepSeek Harness 和 Nanobot 的端到端组装、reasoning
回传、长对话压缩与摘要差异见
[本地开源 Agent 框架上下文管理对比](context-framework-comparison.md)。

## 从事实源到模型请求

一次 Run 开始时，Agent 先探测环境并读取基础上下文，再读取当前活动压缩的游标。SQLite 只
加载 `position > cursor_position` 的消息；新用户输入持久化后加入这段近期会话。长期记忆、
活动 Skill 和运行时提示分别生成带类型的 `ContextItem`。

每个模型 step 按以下路径处理：

```text
基础上下文 + 记忆 + Skill + 活动压缩 + 近期会话 + 运行时状态
                         │
                         ▼
             ContextItem 候选集合
                         │
       超过 target 时尝试压缩旧会话前缀
                         │
                         ▼
       按 retention / priority / atomic_group 选入
                         │
                         ▼
              按稳定前缀顺序渲染 messages
                         │
                         ▼
       只在请求视图修复 Tool Call / Result 协议
                         │
                         ▼
              ModelRequest(messages, tools)
```

`_build_context_items()` 的列表追加顺序不是最终顺序。最终顺序由
`ContextPlanner._render_order()` 决定；预算选择发生在排序之前。

## `messages` 的最终顺序

| 顺序 | Layer | 典型角色 | 来源和用途 | 稳定性策略 |
|---:|---|---|---|---|
| 0 | `CORE_POLICY` | `system` | 内置安全、工具和完成规则 | `PINNED`，最稳定 |
| 1 | `PROJECT_INSTRUCTION` | `system` | 从仓库根到当前目录的 `AGENTS.md` | 父目录先、具体目录后；`PINNED` |
| 2 | `ENVIRONMENT` | `system` | OS、架构、workspace、可执行文件探测 | Run 内稳定；`PINNED` |
| 3 | `SKILL_CATALOG` | `system` | 可用 Skill 的精简目录 | 可从磁盘重建 |
| 4 | `TOOL_CATALOG` | `system` | Tool schema 超预算时的未加载工具目录 | 仅超预算时出现；稳定后进入前缀 |
| 5 | `ACTIVE_SKILL` | `system` | 已激活 Skill 的 header 和正文 | 激活后通常稳定；正文可卸载重载 |
| 6 | `MEMORY` | `user` | 用户显式确认的 `USER.md`/兼容 SQLite 记忆 | 放在会话前，参与稳定前缀复用 |
| 7 | `COMPACTION` | `user` | 唯一活动的 `context_compaction` 摘要 | `PINNED`；必须在它覆盖后的原始 tail 前 |
| 8 | `SNAPSHOT` | `user` | 旧 checkpoint 兼容层 | 默认主路径不注入 |
| 9 | `RECENT_CONVERSATION` / `TOOL_RESULT` | 原始角色 | 压缩游标之后的 SQLite 消息 | 按 `position` 恢复时间顺序 |
| 10 | `AUTOMATIC_MEMORY` | `user` | 异步提取生成的 Markdown 记忆索引 | 易在 Run 间变化，放在历史之后 |
| 11 | `RUNTIME_NOTE` | `system` | 后台进程、停滞恢复、终止及临时约束 | 最易变化，放在动态尾部 |

同一层内先按持久化 `position`，再按稳定 `id` 排序。Assistant 的 Tool Call 与其全部 Tool
Result 使用同一个 `atomic_group`，预算不足时整组保留或整组丢弃，不能拆开。

几个容易混淆的点：

- 显式记忆和自动记忆故意不是同一层。显式记忆由用户控制，通常长期不变；自动索引可能在
  后台提取完成后改变。如果把自动索引放在历史前，它的一次更新会让整段长会话前缀失效。
- 压缩摘要不能移到近期会话之后。摘要代表被替换的旧时间段，必须先于游标后的原始消息，
  否则模型看到的因果顺序会反转。
- 动态尾部本身通常不能跨请求获得最大复用，这是为了保护前面的长会话不受高频变化影响。
- 历史中由旧版本写入的 `system` 消息会降级成名为 `historical_context` 的 `user` 消息，
  不会重新获得当前系统策略权限。

## 预算选择和排序是两套规则

主请求硬输入上限按下式计算：

```text
hard = min(
  context.max_input_tokens,
  model.context_window_tokens
    - max(context.output_reserve_tokens, model.max_output_tokens or 0)
    - context.protocol_reserve_tokens
    - context.safety_margin_tokens
)
target = floor(hard * context.auto_compact_threshold)
```

默认 `context_window=131072` 且未配置 `model.max_output_tokens` 时，`hard=120000`、
`target=96000`。模型请求的输入计数包括选中的 `messages` 和顶层 Tool schema；输出、协议和
安全预留不进入输入内容，而是在计算 `hard` 时先扣除。

各子预算的默认值和性质如下：

| 配置 | 默认值 | 性质 |
|---|---:|---|
| `memory_tokens` | 8K | 显式与自动记忆共享的总预算；自动索引另外最多使用 `memory.index_tokens=2K` |
| `active_skill_tokens` | 16K | 活动 Skill 正文累计预算 |
| `tool_schema_tokens` | 16K | 业务 Tool schema 的选择预算；内部恢复 Tool 始终先保留 |
| `tool_result_inline_tokens` | 4K | 外置内容在请求中的摘录预算 |
| `recent_conversation_tokens` | 20K | 自动压缩时近期原文的软目标，不是硬上限 |
| `compaction_min_recent_user_turns` | 3 | 近期原文的用户轮次下限，可使 tail 超过 20K |
| `compaction_summary_target_tokens` | 3K | 摘要软目标；未显式配置时运行时取 `min(3000, compaction_summary_tokens)` |
| `compaction_summary_tokens` | 4K | 摘要可见正文硬限制 |
| `compaction_max_input_tokens` | 60K | 压缩请求输入硬上限；默认按 80% 即 48K 规划 |

排序靠 layer；是否能进入请求则靠 retention、priority 和预算：

1. `PINNED` 项无条件先选，包括核心策略、项目指令、环境、最新用户消息和活动压缩；
2. 其余项按 atomic group 聚合，优先级高者先选；同优先级保留更新的会话组；
3. Tool schema 的 token 先从 target message budget 中扣除；
4. Provider 能精确计数且仍超过 hard limit 时，从低优先级、较旧的非 pinned 组开始二次卸载；
5. 最后才按上表恢复模型应看到的语义顺序。

因此，表中的“靠前”不代表更高保留优先级。例如 `SKILL_CATALOG` 排在会话前是为了形成稳定
前缀，但它的 priority 低于用户消息，压力下仍可能先被卸载。可选组采用按优先级和新旧程度
择优装箱，不保证一定是连续的最近后缀；被跳过的组写入 `dropped_items` 和
`context.packed` 事件，但当前不会在模型消息中插入 gap 标记。

Planner 的精确计数和二次卸载是能力接口，不等于当前 Provider 已经提供精确计数。基类
`ModelProvider.count_tokens()` 默认返回 `None`，当前 `OpenAICompatibleProvider` 没有覆盖它，
所以普通生产路径仍以 `TokenEstimator` 的启发式估算为主；若估算漏判，依靠 Provider 的首次
上下文长度错误进入一次恢复路径。

## 不调用 LLM 时，当前 `bot` 如何减载

自动摘要不是当前 `bot` 唯一的上下文控制手段。应区分两件事：LLM 压缩负责把旧历史做
**语义整合**；下列确定性机制负责让每次请求的**实际输入视图**变小。它们都不会删除 SQLite
中的原始 Transcript。

| 机制 | 触发或预算 | 对请求视图的处理 | 原文恢复 |
|---|---|---|---|
| 大消息外置 | 普通消息默认超过约 5K token；Tool Result 摘录预算默认 4K | 完整正文写入内容寻址 blob，只内联 head/tail 和 `context_ref` | `load_context_reference` 先按 `query` 检索，或按 `offset/limit` 分块读取 |
| 引用结果一次性交付 | 成功检索或读取外置内容 | 原始命中片段只进入紧随其后的单次模型请求，之后换回短回执，不创建嵌套 blob | 原始 `context_ref` 保持可再次检索/读取 |
| Tool schema 渐进披露 | 全部 schema 超过 `tool_schema_tokens=16K` | 只保留内部恢复 Tool 和已激活业务 Tool，其余降为短目录 | `activate_tools` 按名称重新加载 |
| Skill 正文限额 | 活动 Skill 正文累计超过 `active_skill_tokens=16K` | 保留 header；放不下的正文不注入 | `activate_skill` / `load_skill_resource` 重载 |
| Memory 限额和短索引 | 显式与自动记忆共享 `memory_tokens=8K`；自动索引最多 2K | 省略较旧显式记忆，只注入自动记忆索引 | `search_memory` / `load_memory_evidence` 回读 |
| 分层预算装箱 | 任意主请求组装时都执行 | `PINNED` 必留；其余按 priority、recency 和 atomic group 选择，放不下的组不进入本次请求 | Transcript 保留；`search_session_history` 可检索 |
| Reasoning 作用域收窄 | Assistant reasoning 不属于仍需回放的 Tool Call 消息 | 普通请求不重放该 reasoning；压缩输入也不携带 reasoning 正文 | SQLite 仍持久化，来源哈希仍覆盖 |
| 协议清理 | 组装后的请求副本存在孤儿 Tool 或缺失结果时 | 修复 Call/Result 配对，丢弃孤儿结果；不把历史伪 `system` 恢复为特权消息 | 不改写持久 Transcript |
| Provider 溢出应急外置 | Provider 首次报 context-length error，且强制压缩没有推进 | 将本次内存视图中超过 2,000 字符的正文缩成约 1,500 字符前缀和引用，再重试一次 | blob 中保留完整正文 |

其中“预算装箱”是有损的请求投影，但不是持久删除。它目前可能跳过中间原子组后保留更早的
小组，形成没有 gap 标记的非连续视图；这与“连续最近窗口”不是同一种策略。大消息外置、按需
Tool/Skill/Memory 加载则属于可恢复的渐进披露，优先级应高于不可恢复裁剪。

## 大消息和 reasoning 如何计入

普通 Tool Result 都先把完整 `model_content()` 写入内容寻址 blob，再把带 `context_ref` 的请求
视图写入 SQLite；短结果保留全文，长结果只保留受 `tool_result_inline_tokens` 限制的 head/tail。
`load_context_reference` 的成功结果是例外：它已经有原始 blob，不再把读取结果写成第二层 blob。
SQLite 只保存包含原始 `context_ref`、操作和范围的短回执；内存请求视图临时保存正文，并以
`DISPOSABLE`、priority 800 参与装箱。只在该原子 Tool Call/Result 组实际进入模型请求且 Provider
完成响应后，内存视图才降回短回执；如果 Planner 本次没有选中该组，则不会提前过期。

同一个 `load_context_reference` Tool 提供两种模式，避免为了“先搜索再加载”永久增加一个内部
Tool schema：传 `query` 时在 blob 存储侧做大小写可选的字面量检索，默认最多返回 8 个命中和
每处前后 240 个字符，并附可直接用于后续范围读取的 `load_offset/load_limit`；不传 `query` 时
沿用 `offset/limit` 字节范围读取。命中片段已经足够回答时只需一次 Tool 调用，只有确实需要更大
邻域时才继续按返回范围读取。

这项机制优化的是**跨轮重复回放 token**，不是保证 Tool 调用数永远更少。如果模型立即需要完整
大结果，先外置再读取会多一次往返；如果证据还要跨多个后续模型步骤反复使用，一次性交付也可能
导致再次检索。合理默认是让源 Tool 优先返回有信息量的有界结果，未知位置时用 `query` 一次定位，
只有需要连续全文时才分页读取；不能把外置本身当成无成本压缩。

其他角色的消息从 SQLite 加载或新写入会话时，只要正文估算超过
`max(tool_result_inline_tokens, recent_conversation_tokens / 4)`，也会把完整正文写入 blob，并只
在内存请求视图中保留 head/tail 和 `context_ref`，不改写 SQLite 原消息。默认通用触发线为
5K token，摘录上限为 4K token。Provider 首次报告上下文超限且强制压缩没有推进时，还会对
本次内存视图中超过 2,000 字符的正文做一次更激进的 1,500 字符前缀外置。

Assistant 的 `reasoning_content` 独立持久化，但只有同一条 Assistant 消息还带 Tool Call 时才
序列化回普通 Agent 请求，`TokenEstimator` 也只在这一条件下计入它。压缩模型的
`new_messages/raw_messages` 不包含 reasoning；不过 `source_sha256` 对完整持久消息计算，因此
reasoning 的任何变化仍会使来源校验失败。压缩请求收到的 reasoning delta 只计入诊断和 usage，
不会成为摘要正文。

## Tool schema 是独立请求字段

Tool 定义不转换成普通 `messages`，而是放入 `ModelRequest.tools`，由 OpenAI-compatible
Provider 序列化成顶层 `tools` 和 `tool_choice=auto`。所有可见 Tool 按名称排序，使注册扫描
顺序变化不会无意义地改变缓存前缀。

默认 schema budget 是 16K token。未超预算时发送完整且已排序的集合；超预算时先保留内部
恢复/激活工具，再加入模型显式激活且仍能放入预算的业务工具，其余工具生成
`TOOL_CATALOG`，模型可调用 `activate_tools` 加载。内部 Tool 的总 schema 若自身已经超过 16K，
不会在这里被删除；最终仍由主请求 `hard` 门禁兜底。

## 压缩和 Tool 协议对请求视图的影响

压缩成功后，会话历史层只包含：

```text
一个活动摘要 + covered_end_position 之后的原始消息
```

新摘要替换旧摘要会开启新的 cache epoch；这是保持单摘要、有界上下文和正确时间线的必要
代价。显式记忆位于摘要前，因此即使摘要变化，仍能复用更长的稳定前缀。

Planner 产出后，`repair_tool_protocol()` 只修复本次请求视图：把已有 Tool Result 移到所属
Assistant Tool Call 后；对缺少结果的调用按“运行中断、结果未知”生成信封；丢弃没有 owner
的孤儿结果。SQLite 原始消息保持不变，便于审计和重新压缩。压缩边界更保守：当前活动 Run
中缺少结果的 Tool Call 会阻止游标越过；非活动 Run 才允许在压缩输入中生成逻辑关闭信封。

## 达到触发线或硬上限时的实际动作

| 场景 | 当前动作 | 最终状态 |
|---|---|---|
| 未规划候选输入超过 `target` | 同步尝试压缩一个最旧、Tool-safe 的连续前缀；普通压力保留约 20K tail 且至少 3 个用户轮次 | 压缩成功则推进 cursor；失败则旧摘要/cursor 不变，继续交给 Planner |
| 压缩失败或没有安全前缀 | 不删除原文；普通同增量失败默认退避 300 秒 | Planner 仍可丢弃非 pinned 组并发送，因此压缩失败不等于 Run 立即失败 |
| Planner 估算超过 target | 丢弃放不下的非 pinned 原子组；Provider 有精确计数且超过 hard 时再从低优先级、较旧组开始卸载 | 能降到 hard 内则继续请求，可能形成非连续历史视图 |
| pinned messages + 已选 Tool schema 仍超过 hard | 发出 `context.limit_reached`，不发送该次主模型请求 | Run 为 `limit_reached/context_limit`，并禁用模型收尾，使用确定性收尾文本 |
| Provider 首次返回上下文长度错误 | 整个 Run 只允许一次恢复：强制压缩到仅保留至少 3 个用户轮次；压缩未推进则激进外置正文，然后重建并重试 | 成功则继续 Run |
| Provider 再次返回上下文长度错误 | 不再循环压缩或重试 | Run 为 `failed/provider_error`；终止协调器会尝试无 Tool 模型收尾，失败则使用确定性收尾 |

自动压力路径每个 step 最多推进一个压缩分块；backlog 仍很大时，下一 step 再继续推进。显式
`/compact` 则在空闲会话中循环处理分块，直到目标位置、无进展或请求数/时间/费用预算之一
到达上限。

## 本地开源实现中的非 LLM 减载策略

以下结论来自 `/Users/huyang/codespace` 的本地源码快照，只统计不依赖生成式模型产出摘要的
路径。对比基线为 Codex `41ece455b7fa`、OpenCode `da4730e4a41d`、Pi
`a4453b79bb8d`、Hermes Agent `eac1e25127a7`、DeepSeek Harness `47f943859bef`、
Nanobot `c78421cf1651` 和 LightRAG `441a3e4872c2`。更完整的端到端压缩与 reasoning 对比见
[本地开源 Agent 框架上下文管理对比](context-framework-comparison.md)。

| 实现 | 不调用 LLM 的减载方式 | 关键边界或特点 |
|---|---|---|
| Codex | 写入历史时按模型策略确定性截断 Function/Custom Tool output；请求前移除孤儿输出和目标模型不支持的图片/音频；把可延迟工具放入索引，由 `tool_search` 用 BM25 只取匹配 schema | Tool Call/Result 成对处理；延迟 Tool 让大规模 MCP/App schema 不必每轮全量发送 |
| OpenCode | 启用 `compaction.prune` 时，从后向前保护近期约 40K Tool output，旧完成结果只发送固定占位并移除附件；预计回收不足 20K 时不提交 | 至少跨过两个用户轮次，遇到旧摘要或已剪枝边界停止；原 output 仍在 session part 中 |
| Pi | 默认发送路径会移除 error/aborted Assistant turn、跨模型不可移植的 opaque thinking 和不支持的图片；`context` extension hook 可在每次 LLM 调用前非破坏性过滤消息 | 默认没有通用的旧 Tool result prune；2K Tool result 截断只用于摘要输入，不能算普通请求减载 |
| Hermes Agent | 对大 Tool result 做内容哈希去重，只保留最新完整副本；把旧结果改成按 Tool 类型生成的确定性单行说明；截断超大 Tool 参数且保持 JSON 合法；移除旧截图/多模态主体 | 可独立于完整压缩提前触发，并按最小回收量和 re-arm 阈值防抖；压力过大时可进入受保护 tail，但尽量保留最新 Tool 结果 |
| DeepSeek Harness | 可选 pruner 将超大文本 Tool result 改成 head + 明确 marker + tail，并以带来源的 surface replacement 持久化；若已回到安全阈值则跳过摘要；Code Mode 只向模型暴露一个 `run_code` transport schema；Skill 只注入目录、正文按需加载 | 剪枝记录 `shadowedSeqs` 和来源事件；glob/grep 等有界结果还可把完整列表写入 spill store 后返回定位符 |
| Nanobot | 超大 Tool result 落盘并返回稳定路径/预览；活动 Tool 链溢出时把可重放结果降为明确占位；每次请求按 `window - output - 1024` 从尾部选择连续合法后缀；近期 Memory History 另受 50 条/8K token 双限制 | 后缀重新锚定到 user，并再次修复孤儿 Tool；策略优先保证请求可继续，但被滑出窗口的语义只能靠外部记忆找回 |
| LightRAG | 对候选片段去重、向量 top-k、可选 rerank 与最低分过滤，再分别按实体、关系和文本块 token 预算截断；先扣除 system/query/KG/引用安全开销，再分配 chunk 预算 | 它不是会话 Agent，但展示了“先检索相关证据、再组装”而不是“把全部历史塞入窗口”的路线 |

关键源码定位如下：

| 实现 | 本地源码 |
|---|---|
| Codex | `codex-rs/core/src/context_manager/history.rs`；`codex-rs/core/src/tools/handlers/tool_search.rs` |
| OpenCode | `packages/opencode/src/session/compaction.ts`；`packages/opencode/src/session/message-v2.ts` |
| Pi | `packages/ai/src/api/transform-messages.ts`；`packages/coding-agent/docs/extensions.md` |
| Hermes Agent | `agent/context_compressor.py` |
| DeepSeek Harness | `packages/compaction/compaction-tool-result-pruner/src/index.ts`；`docs/tool-catalog.md` |
| Nanobot | `nanobot/agent/context_governance.py`；`nanobot/utils/helpers.py` |
| LightRAG | `lightrag/utils.py::process_chunks_unified()`；`lightrag/operate.py::_build_context_str()` |

这些实现可以归纳为六类、且都早于“最后再调用 LLM 做摘要”：

1. **从源头限制增长**：Tool 自身分页、采样或设置输出上限，完整结果写 spill/blob，只返回引用；
2. **确定性瘦身**：head/tail 截断、旧结果占位、重复结果去重、Tool-aware 单行降级和超大参数裁剪；
3. **渐进披露**：Tool schema、Skill 正文和大文件默认不进窗口，通过搜索或显式激活按需加载；
4. **有界重放**：只保留满足 Tool 协议的连续近期后缀，并为系统提示、输出和安全余量预留预算；
5. **检索式组装**：按当前任务检索历史、记忆和项目证据，执行去重、top-k、相关性过滤和分层 token 配额；
6. **Provider-aware 清理**：不回放已失效 reasoning、opaque metadata、错误/中断 turn 和不支持的模态。

Prompt cache、稳定前缀和 cache boundary 能降低重复计算成本与延迟，但不会减少送入模型的逻辑
上下文长度，因此不计入本节的“减载方式”。同样，停止 Tool runaway 或限制每轮 Tool 数主要是
防止上下文继续增长，而不是缩减已经存在的历史。

### 对当前 `bot` 的直接启示

当前 `bot` 已覆盖“blob 外置、固定预算、渐进披露、Provider-aware reasoning 作用域和按需回读”。
与本地实现相比，仍有四项非 LLM 增强值得优先评估：

1. 在进入 LLM 压缩前增加 Hermes/DeepSeek Harness 风格的**旧 Tool result 确定性降级**：先去重，
   再把旧摘录降为 Tool-aware 单行说明，同时保留 `context_ref`、来源位置和明确剪枝 marker；
2. 把当前按名称激活 Tool 升级为 Codex 风格的**查询相关 Tool schema 检索**，目录只保存 namespace
   和短描述，按任务一次加载少量匹配 schema；
3. 为压缩失败后的请求视图提供 Nanobot 风格的**连续、Tool-safe 后缀**，或至少为 Planner 的
   非连续丢组插入 gap marker，避免模型不知道中间证据已经缺失；
4. 在 `search_session_history` 和 Markdown Memory 之上增加 LightRAG 风格的**自动相关性选择**：
   去重、top-k、分数门槛和独立 token 配额，只把与当前用户请求有关的历史证据注入。

这些增强中，第 1 项通常收益最高：Agent 长任务的上下文膨胀主要来自重复文件读取、搜索、命令
和网页结果；它们比用户目标和关键决定更适合用确定性规则压缩，而且失败模式比生成式摘要更
容易验证和回滚。第 3 项解决正确性，第 2、4 项解决 Tool/知识规模增长；三者不应被 prompt
cache 指标替代。

`/status` 的 `context_manifest` 只列基础 Core/AGENTS/Skill Catalog 的来源和字符数；
`context` 字段另外给出 hard/target、活动摘要、cursor 后消息数和估算 token、活动 Tool，以及
最近一次 pack 的逐层 token 与 `dropped_items`。在尚未发生 pack 时，`last_pack` 为空，因此它
不是任意时刻都完整的逐层实时 token 清单。

## 为什么采用这个顺序

本节只解释组装顺序的直接来源；完整源码对比另见
[本地开源 Agent 框架上下文管理对比](context-framework-comparison.md)。本地调研的直接参照版本为
OpenCode `da4730e`、Codex `41ece455b7` 和 Pi `a4453b79b`：

- OpenCode 将环境、项目指令和 Skill 合并为 system 前缀，再追加模型消息，并按名称排序
  Tool；
- Codex 将 `base_instructions`、append-only `input` 和 `tools` 分开构造，并默认使用 session
  id 作为 `prompt_cache_key`；
- Pi 将项目上下文和 Skill 合入稳定 system prompt，并在支持的 Provider 上为 system、最后
  一个 Tool 和会话尾部设置 cache boundary。

本项目没有照搬某一个实现：它还需要处理异步自动记忆、单活动压缩摘要、可卸载 Tool schema
和不改写 Transcript 的协议修复。因此采用“稳定前缀 → 因果历史 → 易变尾部”的三段式，
并把显式记忆和自动记忆拆层。对应的受控缓存结果见
[长任务上下文缓存评测](context-cache-benchmark.md#组装顺序优化实验2026-08-23)。

## 压缩和大结果外置的综合验收

`context-efficiency` case 用同一个 12 轮确定性任务贯穿真实 `AgentRunner`、组装器、SQLite
Transcript、压缩器、大 Tool Result 外置、`load_context_reference` 和后续继续执行。每轮 Tool
返回约 32K 字符；其中 3 轮必须读取正文深处的事实，另外 9 轮禁止无意义回读。第 5、9 轮固定
触发压缩，使不同版本的门禁不会依赖偶然越过 token 阈值。

```bash
.venv/bin/python scripts/run_context_efficiency_benchmark.py \
  --output .bot/benchmarks/context-efficiency
```

一次执行比较五种变体。`raw` 是不压缩、不外置的全量基线；`compact-inline` 单独测压缩；
`query-persistent` 模拟已回读正文继续留在当前 Run 后续请求中的旧行为；`range-one-shot` 使用
4KB 盲分页和一次性交付；`current` 启用压缩、外置、`query` 和一次性交付。任务目标、fixture
输出和 seed 完全相同，只有待测的上下文策略不同，`workload.sha256` 因而必须一致。

离线门禁同时统计 Agent 与压缩请求，不能只看主模型最后一个请求：

| 比较 | 门禁 | 说明 |
|---|---:|---|
| `compact-inline` / `raw` | 累计输入 token ≤ 75% | 证明摘要请求自身成本计入后，压缩仍有净收益 |
| `current` / `raw` | 累计输入 token ≤ 50% | 证明完整生产组合降低总输入，而不只是降低峰值 |
| `current` / `range-one-shot` | 引用调用至少减少 3 倍，Agent 请求更少 | 证明已知 key 时检索优于从头盲分页 |
| `current` / `query-persistent` | 重复交付 token 为 0，累计输入更少 | 证明读取正文只在紧随其后的一次请求中出现 |

所有变体还必须完成任务、精确找回并校验 3 个事实、在最终轮保留全部事实、不产生虚构事实、
保持 Transcript 不可变、保持 Tool 协议平衡、只保留一个活动摘要，并且不出现上下文超限或
压缩失败。`comparison.json` 明确报告 `current` 相对 `raw` 新增的引用 Tool 调用；外置优化的
收益是减少重复 token，不应被描述成相对全量内联也能无条件减少调用。调用次数下降的结论只来自
`query` 与盲分页的受控比较。

每个变体写出 `summary.json`、`requests.jsonl` 和 `turns.csv`，根目录写出
`comparison.json` 与 `aggregate.json`。`turns.csv` 可定位累计输入 token 的 break-even 轮次；
默认要求不晚于第 4 轮。

真实 Provider 复验沿用同一 case 和确定性事实评分，要求至少重复 3 次并配置模型价格：

```bash
RUN_CONTEXT_EFFICIENCY_LIVE=1 \
  .venv/bin/python scripts/run_context_efficiency_benchmark.py \
  --provider live --variants raw current --repeat 3 --max-cost-usd 0.50 \
  --output .bot/benchmarks/context-efficiency-live
```

Live 模式要求每次请求都有 Provider usage，比较 `current` 与 `raw` 的累计输入 token 中位数；
固定的 50% 比例只作为离线可控工作负载门禁，不用于约束存在采样波动的真实模型。
