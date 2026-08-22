# 模型上下文分块与组装顺序

本文描述主 Agent 每次调用模型时的实际请求视图。SQLite Transcript、压缩记录、Markdown
记忆和 Skill 文件是事实源；组装过程只生成本次 `ModelRequest`，不会为了排序或修复协议而改写
原始 Transcript。

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
       超过 target 时先压缩旧会话前缀
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

排序靠 layer；是否能进入请求则靠 retention、priority 和预算：

1. `PINNED` 项无条件先选，包括核心策略、项目指令、环境、最新用户消息和活动压缩；
2. 其余项按 atomic group 聚合，优先级高者先选；同优先级保留更新的会话组；
3. Tool schema 的 token 先从 target message budget 中扣除；
4. Provider 能精确计数且仍超过 hard limit 时，从低优先级、较旧的非 pinned 组开始二次卸载；
5. 最后才按上表恢复模型应看到的语义顺序。

因此，表中的“靠前”不代表更高保留优先级。例如 `SKILL_CATALOG` 排在会话前是为了形成稳定
前缀，但它的 priority 低于用户消息，压力下仍可能先被卸载。

## Tool schema 是独立请求字段

Tool 定义不转换成普通 `messages`，而是放入 `ModelRequest.tools`，由 OpenAI-compatible
Provider 序列化成顶层 `tools` 和 `tool_choice=auto`。所有可见 Tool 按名称排序，使注册扫描
顺序变化不会无意义地改变缓存前缀。

默认 schema budget 是 16K token。未超预算时发送完整且已排序的集合；超预算时先保留内部
恢复/激活工具，再加入模型显式激活且仍能放入预算的业务工具，其余工具生成
`TOOL_CATALOG`，模型可调用 `activate_tools` 加载。

## 压缩和 Tool 协议对请求视图的影响

压缩成功后，请求只包含：

```text
一个活动摘要 + covered_end_position 之后的原始消息
```

新摘要替换旧摘要会开启新的 cache epoch；这是保持单摘要、有界上下文和正确时间线的必要
代价。显式记忆位于摘要前，因此即使摘要变化，仍能复用更长的稳定前缀。

Planner 产出后，`repair_tool_protocol()` 只修复本次请求视图：把已有 Tool Result 移到所属
Assistant Tool Call 后；对已中断且没有结果的调用生成“结果未知”信封；丢弃没有 owner 的
孤儿结果。SQLite 原始消息保持不变，便于审计和重新压缩。

## 为什么采用这个顺序

本地调研的版本为 OpenCode `da4730e`、Codex `41ece455b7` 和 Pi `a4453b79b`：

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
