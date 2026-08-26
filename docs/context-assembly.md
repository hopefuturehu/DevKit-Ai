# 模型上下文分块与组装顺序

> 状态：当前实现说明
>
> 核对日期：2026-08-26

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

## 大消息和 reasoning 如何计入

每个 Tool Result 都先把完整 `model_content()` 写入内容寻址 blob，再把带 `context_ref` 的请求
视图写入 SQLite；短结果保留全文，长结果只保留受 `tool_result_inline_tokens` 限制的 head/tail。
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
