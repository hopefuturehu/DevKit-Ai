# 本地开源 Agent 框架上下文管理对比

> 状态：本地源码静态分析快照
>
> 日期：2026-08-29；`bot` 文档核对：2026-08-29
>
> `bot` 代码基线：`db347644c1c7`

本文比较当前 `bot` 与本地 checkout 中的 Codex、OpenCode、Pi、Hermes Agent、DeepSeek
Harness 和 Nanobot，范围覆盖每次模型请求如何组装、reasoning 如何保存与回传、长对话如何
选择压缩边界，以及摘要如何生成、发布和失败恢复。KAT 是较早的 Nanobot 派生实现，放在文末
作为演进参照，不与当前 Nanobot 重复展开。

本文只描述上述 commit 的实现，不把项目宣传文案或未来计划当成已实现行为。当前 `bot` 的
修复前运行数据和卡游标案例见[上下文压缩历史快照](context-compaction-current-state.md)与
[长会话上下文压缩问题](context-compaction-failure-analysis.md)；本项目自身的请求层顺序见
[模型上下文分块与组装顺序](context-assembly.md)。

## 1. 结论摘要

1. 这些框架并不是都把 reasoning 当作普通 assistant 文本原样回传。Codex、Pi 和 DeepSeek
   Harness 使用结构化 reasoning/thinking block；OpenCode 同时保留 part 与 Provider metadata；
   Hermes 在发送边界按 Provider 决定保留、补位或删除；只有当前 `bot` 和 Nanobot 仍主要依赖
   扁平的 `reasoning_content` 字段。
2. “reasoning 是否回传给主模型”和“reasoning 是否进入压缩摘要”是两个独立问题。OpenCode、
   Pi 明确把可读 reasoning 序列化给摘要模型；Hermes、Nanobot 和当前 `bot` 明确不这样做；
   Codex 与 DeepSeek Harness 让 Provider adapter 决定结构化状态如何进入压缩请求，但压缩后的
   替换历史都不保留旧 reasoning 链。
3. 主流长对话方案是“用一个 checkpoint/summary 替换旧历史，再保留有界的近期锚点或连续
   原文尾部”，不是不断覆盖成最近 N 条消息。Codex 的本地路径保留近期 user 锚点；OpenCode、
   Pi、DeepSeek Harness 和当前 `bot` 保留近期消息 tail；Hermes 采用 head/summary/tail 并增加
   更强的降级和反抖逻辑。
4. Nanobot 是最明确的滑动窗口实现：每次请求先按消息数、再按 token 从尾部选择连续合法
   后缀，同时异步把被移出的前缀归档成记忆。它优先保证请求能继续，但语义连续性和溯源弱于
   checkpoint 系列。
5. 除 Nanobot/KAT 外，成熟实现通常不会在压缩失败后静默退化成任意最近消息窗口。Codex、
   OpenCode、Pi、DeepSeek Harness 会保留错误或停止；Hermes 可发布明确标记的确定性低保真
   handoff。当前 `bot` 的 Planner 虽会按优先级丢弃可选消息组，但结果未必是连续后缀，这比
   显式失败或显式降级更容易制造不可见的因果缺口。
6. 当前 `bot` 的优势是分层预算、Tool 原子组、不可变 Transcript、版本化摘要、来源哈希和
   fail-closed 发布；主要缺口是 reasoning 缺少 Provider/模型来源、近期轮次下限可以突破 token
   目标、活动长 Tool 链难以形成压缩边界，以及最终打包可能非连续地漏掉未摘要消息。

## 2. 比较口径

本文把容易混淆的状态拆成五层：

| 层 | 含义 | 不应混同的对象 |
|---|---|---|
| Canonical history | 可恢复、可审计的事实记录 | 本次请求里的临时裁剪结果 |
| Request view | 某一步真正送给模型的消息和 Tool schema | 数据库中的完整历史 |
| Reasoning replay state | Provider 为继续同一推理/Tool 链要求回传的字段、签名或加密 item | 给用户看的 reasoning 摘要 |
| Compaction input | 摘要模型本次看到的旧摘要与原始历史范围 | 压缩后主模型看到的 checkpoint |
| Compacted view | checkpoint/summary 与近期原文尾部组成的新请求视图 | 删除原始 Transcript |

因此，“框架保存了 reasoning”不等于“所有后续请求都回传它”，也不等于“摘要模型会读取它”。
安全的实现必须分别回答：哪一个 Provider 生成、哪一个目标 Provider 接收、需要保持到哪一个
Tool/turn 边界、模型切换时如何转换，以及压缩后是否还需要保留。

## 3. 源码快照

| 实现 | 本地 commit | 定位 |
|---|---|---|
| `bot` | `db347644c1c7` | 当前项目；分层请求 Planner + 可恢复单摘要 |
| `openai/codex` | `41ece455b7fa` | 结构化 Responses item + 本地/远端 compaction |
| `anomalyco/opencode` | `da4730e4a41d` | 消息 part + compaction summary/tail |
| `badlogic/pi-mono` | `a4453b79bb8d` | Provider-neutral thinking + append-only session tree |
| `NousResearch/hermes-agent` | `eac1e25127a7` | Provider 策略层 + 多级压缩降级 |
| `deepseek-ai/deepseek-harness` | `47f943859bef` | 事件表层替换 + 插件化 compaction capability |
| `HKUDS/nanobot` | `c78421cf1651` | 连续尾部窗口 + memory consolidation |
| `hopefuturehu/KAT` | `a93d3c262cae` | 较早 Nanobot 派生版本；仅作演进参照 |

## 4. 端到端架构全景

| 实现 | Canonical history | 请求组装核心 | 压缩后的主模型视图 | 原文恢复能力 |
|---|---|---|---|---|
| `bot` | SQLite 原始消息 + 独立 compaction 版本链 | typed `ContextItem` 按 retention、priority、atomic group 打包 | 单活动摘要 + cursor 后的原始消息 | 强：原文不删，摘要保存连续范围、哈希、目标锚点和父版本；逐条引用为可选兼容模式 |
| Codex | append-only rollout + replacement history | `base_instructions`、结构化 `ResponseItem`、tools 分离；请求前规范化 | 本地为近期用户原文 + summary；远端为原生 compaction item/过滤后的消息 | 强：rollout 保留 compaction 事件和 replacement history |
| OpenCode | session message/part 日志 | system 环境/指令/Skill/MCP + `filterCompacted()` 投影 + tools | 最新 compaction summary + `tail_start_id` 起的原始尾部 | 中强：旧消息仍在 session 存储，活动投影视图省略它们 |
| Pi | append-only JSONL session tree | 当前 leaf path + 最新 compaction + Provider compatibility transform | compaction entry + `firstKeptEntryId` 起的连续条目 | 强：树节点不因 compaction 删除，可分支和重建 active path |
| Hermes | DB/会话 transcript | 复制 canonical history，注入临时上下文，再执行 Provider sanitizer | 受保护 head + summary/handoff +近期 tail | 中强：旧消息软归档并可搜索，运行视图会重写中段 |
| DeepSeek Harness | append-only session events + derived surface | system-prompt registry + session surface + runtime contexts + canonical tools | 一个带来源的 checkpoint 节点替换旧 surface span，后接近期节点 | 强：`shadowedSeqs`、`sourceEventSeqs` 和原始事件都保留 |
| Nanobot | session JSONL + `last_consolidated` 游标 + memory history | system/memory/Skill + 未归档历史的连续合法尾部 + 当前消息 | 最近消息窗口；可额外注入最近一次 archive summary | 中：session 原消息仍在文件，但游标推进后不再进入活动历史 |

这些实现可以归为三类：

- **结构化事件投影**：Codex、Pi、DeepSeek Harness。先保留 append-only 事实，再构造活动投影。
- **摘要与尾部投影**：OpenCode、Hermes、当前 `bot`。Canonical history 与主请求视图分离，但
  数据模型和发布严格程度不同。
- **有界尾部与记忆归档**：Nanobot/KAT。每次请求直接限制尾部，旧内容靠较弱的记忆摘要补回。

## 5. 上下文组装机制

### 5.1 当前 `bot`

当前路径是：

```text
SQLite/文件事实源
  -> Core、AGENTS、Environment、Skill、Memory、Compaction、Conversation、Runtime ContextItem
  -> 预扣顶层 Tool schema token
  -> PINNED 优先，其他项按 priority + recency 选择，Tool Call/Result 整组处理
  -> 恢复稳定前缀和因果渲染顺序
  -> 精确计数后二次卸载
  -> 仅在请求副本上修复 Tool 协议
  -> ModelRequest(messages, tools)
```

最新用户消息和活动摘要是 `PINNED`；会话中用户消息 priority 为 700，assistant/Tool 为 600。
这能在多个上下文来源争用窗口时做细粒度预算，但选择阶段是“按组择优装箱”，不是“从最新
消息向前取连续后缀”。若一个较新的超大 Tool 组放不下，Planner 可以跳过它而保留更早的小组，
最终时间顺序虽然正确，语义时间线中间却可能出现未标记空洞。

实现入口为 [`AgentRuntime._build_context_items()`](../src/bot/core/agent.py)、
[`ContextPlanner.pack()`](../src/bot/core/context.py) 和
[`ChatMessage.to_openai()`](../src/bot/core/models.py)。

### 5.2 其他框架

| 实现 | 稳定/系统上下文 | 会话投影 | Tool 与协议处理 | Cache 取向 |
|---|---|---|---|---|
| Codex | `base_instructions` 独立于 input | append-only `ResponseItem` 经 `History.for_prompt()` 规范化 | 缺失 Call/Result 配对修复；只确定性截断 Tool output | session/window id 驱动 cache key；尽量保持结构化前缀 |
| OpenCode | Environment、项目指令、MCP、Skill 组成 system 数组 | `filterCompacted()` 选最新 summary 和 tail，随后按目标模型转换 part | pending Tool 生成中断结果；旧 Tool output 可单独清空 | Tool 排序，same-model metadata 原样保留 |
| Pi | 项目上下文与 Skill 组成稳定 system prompt | 沿当前 session tree leaf 构造 active path | `transformMessages()` 修复 Tool 协议并做 Provider 兼容转换 | 支持的 Provider 上设置 system/Tool/尾部 cache boundary |
| Hermes | 系统提示、Skills、Memory 与临时用户上下文分层组装 | canonical transcript 的请求副本 | 单一发送边界清理空消息、Tool 对和 Provider reasoning 字段 | 压缩/裁剪按批次发生，避免每轮破坏前缀；有反抖逻辑 |
| DeepSeek Harness | 插件向 registry 贡献有序 section、runtime context、变量与 Tool | append-only event log 投影成可替换 surface | Tool schema canonical order；压缩边界验证 Call/Result 平衡 | 摘要请求逐字重放旧请求前缀，优先复用热 KV cache |
| Nanobot | Identity、AGENTS/SOUL/USER、Memory、Skill、recent history 合成 system | `last_consolidated` 后先按数量、再按 token 取连续尾部 | 起点重新锚定到 user 并去掉 orphan Tool result；超限时缩 Tool result | 设计目标是有界生存性，不强调长前缀缓存稳定性 |

与当前 `bot` 相比，Codex/Pi/DeepSeek Harness 更倾向“先得到唯一活动历史投影，再整体发送”；
`bot` 则先把所有来源变成可竞争的 item。前者更容易保证会话连续性，后者更容易给 Memory、
Skill、Tool schema 分别设预算，但必须额外证明被丢弃 item 不会造成因果缺口。

### 5.3 Role 分配与最终消息位置

这里必须区分两层：**框架内部的逻辑消息类型**与**Provider 最终收到的 wire role/item**。
Chat Completions 通常使用 `system`、`developer`、`user`、`assistant`、`tool`；Responses API 还把
`instructions`、reasoning、function call/output、compaction 等放在独立顶层字段或专用 item 中，
不能把它们都等价描述成一串 Chat Completions messages。Tool schema 在这些实现中通常也位于
请求顶层，而不是一条对话消息。

| 实现 | 高权限/稳定提示 | `user` 中的真实与合成内容 | `assistant` | Tool 在 wire 上 | 典型请求顺序或例外 |
|---|---|---|---|---|---|
| `bot` | Core、`AGENTS.md`、环境、Skill 目录/正文、Memory Router 和 runtime note 均为 `system` | 真实用户；显式记忆、压缩摘要、旧 snapshot 为带边界的 synthetic `user`；`automatic_memory(user)` 仅在 `eager` 兼容模式出现；旧版历史 `system` 会降为带边界的 `user(name=historical_context)` | 原始 Assistant 回复和 Tool Call | `role=tool`；schema 在顶层 `tools`；记忆检索/证据正文为一次性 Tool Result | 默认 `system* -> explicit-memory(user) -> compaction(user) -> transcript -> router(system) -> assistant(memory Tool Call) -> memory(tool)`；`REQUIRE_*` 在 Provider 支持时使用 named choice，否则由 Agent fail-closed 校验 Tool 名称 |
| Codex | 常规 Responses 请求的模型基础提示位于顶层 `instructions`；开发者指令、Memory 摘要、Skill、协作/权限/模型状态等为 `developer` item；Responses Lite 把基础提示也转成 `developer` item | 真实用户；`AGENTS.md`、环境和部分 App/插件提示为 contextual `user`；本地压缩保留的用户锚点与最终 summary 都是 `user` | 模型消息为 `assistant` item | Function/custom tool call/output、reasoning、native compaction 是专用 `ResponseItem`，不是 `role=tool` | 初始上下文通常按 `developer* -> contextual-user` 进入 history，再接会话；远端压缩可返回无普通 role 的原生 compaction item |
| OpenCode | Agent/provider prompt、环境、项目指令、MCP、Skill 和 per-user system 合成 system 数组；一般变成前置 `system`，OpenAI OAuth 路径改用 `instructions` | 真实用户；压缩 marker 会渲染为 synthetic user “What did we do so far?”；媒体兼容提示和自动继续提示也可生成 `user` | 真实回复；压缩摘要保存为 `assistant(summary=true)` | AI SDK 从 assistant Tool part 生成 Tool Call/Result；Provider adapter 再落到目标协议 | 正常为 `system* -> projected history`；压缩投影明确重排为 `compaction-user -> summary-assistant -> retained tail -> continue-user` |
| Pi | Core、Tool 指南、项目 context files、Skill、CWD 合成一个 `systemPrompt`；Chat Completions 上按模型能力发 `system` 或 `developer`，标准 Responses 发 `system/developer` message，Codex Responses 路径使用顶层 `instructions` | 真实用户；bash/custom extension 消息、branch summary、compaction summary 均转换为 synthetic `user` | 真实回复；thinking 与 Tool Call 是 assistant content block | 内部是独立 `toolResult` 角色；Chat Completions 转成 `tool`，Responses 转成 function/custom tool output item | `systemPrompt + active session-tree path`；最新 compaction 先作为 `user`，再接 `firstKeptEntryId` 起的连续尾部 |
| Hermes | Core、workspace/context files、Skill、内置 Memory/`USER.md`、外部 memory 的系统块、日期和 ephemeral system 合成一个前置 `system` | 真实用户；外部 recall、插件 user context、gateway turn note 和 MoA context 直接拼入当前 user 的 API 副本；另有少量恢复 nudge | 真实回复；压缩摘要也可能取 `assistant` | Chat Completions 上为 `role=tool`；发送边界按 Provider 修复配对和 reasoning 字段 | `system -> history/current-user-with-injections`；压缩摘要不是固定 role：在 `user`/`assistant` 间选以满足相邻角色，仍冲突时合并进 tail 消息 |
| DeepSeek Harness | 有序 system-prompt section 渲染到 `GenerateOptions.system`，DeepSeek adapter 将其作为首条 `system` | 真实用户；动态 runtime context 是追加在本步 claimed input 后的可持久化 `user` snapshot；压缩 checkpoint 也是 `user` | 模型输出为 `assistant` | 内部 Tool Result 是含 `tool-result` block 的 `user` message；DeepSeek wire 展开成 `role=tool` | `system -> session surface -> claimed input -> changed runtime-context user`；首步 claimed input 通常是真人 user，后续也可含 Tool 追加 context；surface replace 用 checkpoint-user 替换旧 span |
| Nanobot | Identity、`AGENTS.md`/`SOUL.md`/`USER.md`、Tool contract、`MEMORY.md`、Skill、recent memory history 和 archived summary 全部拼进单个 `system` | 真实用户；当前时间/channel/sender/goal 等 runtime metadata 拼在当前 user 尾部；注入、恢复和重试提示也是 `user` | 真实回复和 Tool Call | `role=tool` | `system -> bounded legal history suffix -> current user + runtime metadata`；若尾部已经是同角色则合并内容 |

逐框架看，结论并不是“上下文全部用 user 名义注入”：

- **全放 system 的代表是 Nanobot 的长期记忆路径**。`MEMORY.md`、recent memory history 和 archived
  session summary 都取得与核心提示相同的 wire role，隔离主要依赖文本边界而不是 role。
- **显式拆出 developer/user 的代表是 Codex**。Memory summary 与 Skill 是 `developer`，而
  `AGENTS.md` 和环境是 contextual `user`；其基础提示在常规 Responses 请求中甚至不属于
  `input[]`，而是顶层 `instructions`。
- **大量使用 synthetic user 的代表是 Pi 和 DeepSeek Harness**；`bot` 仍用它承载显式记忆和
  Compaction，但默认自动记忆已经改成按需 Tool Result，避免把派生索引追加在真人输入之后。
- **Hermes 说明压缩摘要的 role 不能只按语义决定**。它会根据严格模板看到的前后角色选择
  `user` 或 `assistant`，必要时把摘要并入 tail；同时用明确的 reference-only 前缀避免旧任务被
  当成新请求。

Role 也不等于来源或信任级别。当前 `bot` 的 `ContextTrust`、Codex/DeepSeek Harness 的 source
provenance、OpenCode/Pi 的 synthetic/summary 标记主要在框架内部携带来源或形态信息，并供审计、
投影或 UI 使用；最终序列化时 Provider 未必能看到这些元数据。模型真正能稳定利用的是 wire
role、相对位置、文本边界和
目标 API 保留下来的专用字段。因此，只把不可信自动记忆标成内部 `UNTRUSTED` 并不足够；`bot`
现在同时改变默认注入时机、相对位置、文本边界和 Tool wire role，并对高风险归因使用
capability-aware Tool 门禁与原始证据门禁。

## 6. Reasoning 的保存、回传与压缩作用域

### 6.1 对比表

| 实现 | 内部表示 | 普通请求回传作用域 | 模型/Provider 切换 | 是否作为摘要材料 | 压缩后旧 reasoning |
|---|---|---|---|---|---|
| `bot` | assistant 上单个 `reasoning_content: str` | 只有同一条消息带 Tool Call 时才序列化 | 无来源字段，无法判断是否来自当前目标 | 否；摘要 payload 不含该字段 | cursor 覆盖后消失；摘要只保留可见结论 |
| Codex | `ResponseItem::Reasoning`，含 summary/content/encrypted content | Responses 常规模式让服务端使用 `current_turn` 默认；Lite 显式 `all_turns`，并请求 encrypted content | 主要依赖 Responses 原生 item，不把 plaintext CoT 当普通文本 | 不拼成普通文本；压缩 Provider 可接收结构化/加密状态 | replacement history 过滤旧 reasoning 与 Tool 链 |
| OpenCode | `reasoning` part + Provider metadata | 同模型保留结构化 part 和 metadata | 切到不同模型时，可读 reasoning 降级为 text，opaque metadata 删除 | 是，显式写成 `[Assistant reasoning]` | 旧 part 被 summary 替代，近期 tail 仍原样保留 |
| Pi | `ThinkingContent`，含 text、signature、redacted | 精确 provider/API/model 匹配时保留签名或 opaque block | 跨目标时丢弃 redacted block；可读 thinking 转 text | 是，显式写成 `[Assistant thinking]` | summary 取代旧 thinking；保留 tail 继续按兼容规则回传 |
| Hermes | `reasoning`、`reasoning_content`、`reasoning_details` 多种 sidecar | DeepSeek/Kimi/MiMo require-side 保留或补位；strict Provider 删除；Codex replay 只留活动 user turn 后的链 | 每次发送和 fallback 后重新执行目标 Provider 策略 | 否；只序列化可见 content，并去掉 inline think tag | 旧 replay sidecar 被清理，native compaction checkpoint 例外 |
| DeepSeek Harness | Provider-neutral `reasoning` content block | adapter 决定；DeepSeek adapter 只在 assistant Tool Call turn 回传 `reasoning_content` | adapter 负责目标 wire format；Pi adapter 可保留 thinking/signature 语义 | 源消息按 adapter 重放，不转成普通 reasoning 文本；摘要输出只收 text | checkpoint 替换旧 surface，旧 reasoning 留在事件日志但不在活动面 |
| Nanobot | `reasoning_content` 与 `thinking_blocks` 并存 | 只要消息仍在近期尾部就随消息回放 | 有部分 Provider adapter，但 canonical 字段缺少统一来源/兼容信封 | 否；memory formatter 只读取 `content` | 随滑动窗口或 consolidation 游标一起退出活动上下文 |

### 6.2 “只保留 Provider 要求的作用域”具体指什么

合理的 reasoning 生命周期通常不是“保存后永远回放”，而是下面的组合：

1. **同一 Tool 链作用域**：某些 thinking API 要求 assistant Tool Call 以及后续 Tool Result 继续
   携带它生成的 reasoning。此时只能保留完整 Tool 原子组，不能单独截掉 reasoning。
2. **当前 turn 作用域**：Codex 通过 Responses 的 `current_turn` reasoning context 表达服务端
   作用域；Hermes 会对 Codex Responses sidecar 清理最后一个真实 user 边界之前的旧 replay
   item，同时保留该 user 之后跨多次 Tool Call 的活动链。
3. **精确 Provider/API/model 作用域**：Pi、OpenCode 只有在目标与产生 reasoning 的源匹配时
   才保留签名或 opaque metadata；切模型时把可读内容降级为普通文本，或直接丢弃不可移植块。
4. **压缩 checkpoint 作用域**：一旦旧 turn 已由摘要或原生 checkpoint 取代，旧 reasoning
   不再进入活动请求；它可以留在 append-only 日志供审计，但不继续占用模型窗口。
5. **摘要作用域**：摘要需要保留决定、证据、未完成事项和失败结论，不必保留逐 token 的内部
   思考轨迹。OpenCode/Pi 选择把可读 thinking 当素材；Hermes、Nanobot 和当前 `bot` 选择排除。

当前 `bot` 的“assistant 有 Tool Call 才回传”已经体现了第 1 条，但 `ChatMessage` 没有
`provider/model/api/mode` 来源，也没有 `opaque/readable/replay_required_until` 之类的语义，
所以模型切换、fallback 和未来非 DeepSeek reasoning 协议只能靠同一个字段猜测。

## 7. 长对话的触发、切分和失败退路

| 实现 | 触发与默认预算 | 近期原文 | 边界和单轮超大输入 | 非 LLM 剪枝 | 摘要失败/仍超限 |
|---|---|---|---|---|---|
| `bot` | hard input 由窗口减输出/协议/安全预留后再与 120K 取小；默认 80% 触发 | 20K token，且至少 3 个 user turn | 不拆 Tool 原子组；user turn 下限可令 tail 超过 20K | 大消息外置、Tool schema 卸载、Planner 可丢可选组 | compaction fail closed、游标不动；最终 Planner 仍可能非连续丢组 |
| Codex | 模型元数据和当前 context-window 状态触发 auto/native compaction | 本地 replacement 最多约 20K 原始 user message；远端由 checkpoint 结果决定 | Tool output 可截断；压缩请求自身溢出时逐个移除最旧输入 item | Tool output 定长截断 | 正常 sampling overflow 会返回错误并进入明确 compaction 路径，不使用无摘要滑窗 |
| OpenCode | usage 达到 usable input；默认最多预留约 20K output/buffer | usable 的 25%，限制在 2K–15K | 可以在一个超大 turn 内从 assistant 消息边界保留 suffix | 保护近期约 40K 后清空更旧 Tool output，累计收益不足 20K 不提交 | compaction 仍返回 overflow 时写 `ContextOverflowError` 并停止 |
| Pi | `contextTokens > contextWindow - 16,384` | 默认约 20K | 可对超大单 turn 的 prefix 单独摘要；不从 Tool Result 开始 tail | 摘要输入中的 Tool result 截至约 2K 字符 | overflow 最多 compact-and-retry 一次，失败后显式报错 |
| Hermes | 默认在有效窗口约 50% 触发，可按模型覆盖或设 token cap | tail 预算默认约阈值的 20%，同时保护有限 head/tail 和最近 user | Tool-safe 切分；活动 turn reasoning 计费与旧 turn 区分 | 主动/溢出 Tool result prune、输入 head+tail bound | 可生成明确的确定性 fallback handoff 并丢中段；也可配置 abort；有 cooldown/anti-thrash |
| DeepSeek Harness | 默认窗口 80% 触发，Provider overflow 也可强制恢复 | 默认窗口 16% | 选择最旧、完整、Tool 配对平衡的 surface 单元；未闭合尾部拒绝压缩 | 可选 Tool result pruner | 默认 1 次额外收敛重试和 1 次 overflow retry；失败保持最新 durable surface，不滑窗 |
| Nanobot | 每次请求都受 `context - output - 1024` replay budget；达到预算时 consolidate 到约 50% | 按消息数和 token 从尾部取连续后缀 | 对齐最近 user 与合法 Tool 起点；不能安全切分时保留可见尾部 | in-flight Tool result compact + `snip_history()` | 摘要失败写有界 `[RAW]` breadcrumb 并推进 cursor；下次不重复攻击同一前缀 |

这里最值得区分的是三种“退路”：

- **失败并停止**：不制造未知信息丢失，Codex/OpenCode/Pi/DeepSeek Harness 主要采用这一类。
- **显式低保真 checkpoint**：Hermes/Nanobot 会告诉后续模型摘要不完整或保存 raw breadcrumb，
  再推进边界，优先避免压缩重试循环。
- **隐式请求裁剪**：当前 `bot` 的 Planner 会记录 dropped item，但模型请求中没有同等醒目的
  gap marker。若被裁掉的是尚未进入摘要的中间 Tool 组，模型无法知道缺口存在。

## 8. 摘要机制

| 实现 | 摘要输入 | 摘要输出约束 | 发布与溯源 |
|---|---|---|---|
| `bot` | `previous_summary + new_messages`，周期性可从 raw rebuild；消息含 role/content/Tool 信息，不含 reasoning | Goal、Constraints、Progress、Key Decisions、Relevant Files、Failures、Next Steps、Critical Context 八段；默认目标 3K、正文硬限 4K，支持格式修复和候选凝练 | `building -> ready -> superseded/failed`；记录连续覆盖范围、SHA-256、目标锚点和父版本，默认 `source_refs` 可为空；SHA 覆盖含 reasoning 的完整持久消息，但校验通过才推进 cursor |
| Codex | 本地 compaction 在现有结构化 prompt 上追加压缩指令；远端调用原生 compact endpoint | 本地提示重在简洁 handoff，没有逐项来源/固定标题 parser；远端结果可为 opaque compaction checkpoint | append-only rollout 记录 compaction 与 replacement history；本地还重放最多约 20K user 原文作为目标锚点 |
| OpenCode | 旧 summary + 被选 head；reasoning 显式标记，Tool output 每项约 2K 字符 | Objective、Important Details、Work State、Next Move、Relevant Files 等固定 Markdown；没有当前 `bot` 的逐段/来源严格门禁 | compaction user marker + summary assistant message + `tail_start_id`；旧 session parts 仍可读取 |
| Pi | 旧 summary + 待摘要条目；thinking 显式标记，Tool result 每项约 2K 字符；超大 turn prefix 可第二次摘要 | Goal、Constraints & Preferences、Progress、Key Decisions、Next Steps、Critical Context；输出上限约为 `min(0.8 × reserve, model max)` | append compaction entry，保存 `firstKeptEntryId`、tokens、文件读写 sidecar；session tree 保留原条目 |
| Hermes | 旧 summary + 有界新 turns；只取可见 content/Tool，剥离 native 和 inline reasoning；总输入约 160K 字符 head+tail | Active Task、Goal、Completed Actions、Active State、Blocked、Key Decisions、Relevant Files 等；动态目标为输入约 20%，限制 2K–10K | LLM、主模型/辅助模型、确定性 fallback 与 cooldown 多层处理；旧消息软归档，handoff 明确低保真状态 |
| DeepSeek Harness | 逐字重放被遮蔽区域原请求的 system、tools、messages，再追加压缩指令；reasoning 的 wire replay 由 adapter 决定 | 八段结构化 checkpoint；默认 `maxTokens=8192`，只接受返回 text，reasoning-only/Tool output 不进入 checkpoint | `compaction/start/summary/end` bracket；记录 `compactionId`、`shadowedRange`、精确 `shadowedSeqs`、usage，replacement 的 `sourceEventSeqs` 指回原事件 |
| Nanobot | Memory formatter 仅选有 content 的消息，因此 Tool-only assistant 和 reasoning 不进入摘要；输入按摘要模型预算截断 | SNIP 事实标记列表；持久摘要最多约 8K 字符，不是完整 Agent handoff | 写入 `history.jsonl` 并把最近摘要存 session metadata；失败写最多约 16K 字符 raw archive 后仍推进 `last_consolidated` |

摘要是否包含 reasoning 没有唯一答案，但可以看出两种成立条件：

- OpenCode/Pi 把 reasoning 当成可帮助恢复决定依据的可读材料，代价是摘要输入更大，也可能把已
  推翻的假设凝固成“事实”。
- Hermes/当前 `bot`/Nanobot 把 reasoning 当作瞬时 scratch work，只总结可见结论、Tool 证据
  和任务状态。这个方向更省窗口，也更适合跨模型；前提是重要决定和证据不能只存在于 reasoning。

Codex 与 DeepSeek Harness 采取第三种方式：reasoning 先是 Provider-native structured state，
是否在压缩调用中重放由 adapter 决定；无论如何，发布后的 checkpoint 不继续携带旧推理链。

## 9. 对当前 `bot` 的结论

### 9.1 应保留的设计

- **不可变原始 Transcript + 派生投影**：与 Codex/Pi/DeepSeek Harness 的方向一致，比直接删除
  旧消息更适合恢复、审计和重新压缩。
- **单活动摘要和事务发布**：失败不覆盖旧 `ready`，来源 SHA-256 与父版本可验证，这是本地
  对比中最完整的摘要发布机制之一。
- **Tool 原子组和独立 Tool schema 预算**：避免从 Tool Result 中间切开协议，同时允许在业务
  Tool 过多时卸载 schema。
- **压缩 reasoning 与普通 Agent reasoning 分开配置**：官方 DeepSeek 端点上为摘要调用关闭
  thinking，可以直接避免“隐藏 reasoning 吃满摘要输出额度”。

### 9.2 需要补齐的设计

1. **Reasoning provenance**：把单个字符串升级为带 `provider`、`model`、`api_mode`、
   `representation`、`replay_scope` 的信封；Provider serializer 只发送目标端要求的字段。
2. **连续因果尾部**：常规请求应使用“checkpoint + 连续合法 suffix”。如果紧急预算仍不足，
   应显式失败或插入 gap/checkpoint 标记，而不是跳过一个新 Tool 组后再保留更旧消息。
3. **双重有界 tail**：`compaction_min_recent_user_turns=3` 不应无限覆盖 20K token 目标。至少要
   分开“当前活动 Tool 链必须完整”和“历史 user turn 尽量保留”，并给后者硬上限。
4. **活动 turn 上限**：压缩只能处理安全前缀，不能在 Provider 仍要求 replay 的 Tool/reasoning
   链内部推进游标。应限制每个 turn 的 Tool step、reasoning token、墙钟和累计输出，并在触顶
   时先生成显式进度 checkpoint。
5. **结构化工作状态代替 raw CoT**：继续不把完整 reasoning 放入摘要，但把 decisions、evidence、
   rejected hypotheses、current plan 作为可见的短结构化状态保存；这比直接摘要 CoT 更稳定。
6. **区分 compaction failure 与 request degradation**：监控需要同时报告 cursor、待压缩 prefix、
   retained tail、reasoning replay token、Planner dropped groups 和是否存在非连续 gap，避免只看
   最终请求“能放下”就误判成功。

一个更稳妥的目标请求路径是：

```text
不可变 Transcript
  -> 最新已验证 checkpoint
  -> 连续、Tool-safe、Provider-replay-safe 的近期 suffix
  -> 按目标 Provider 转换 reasoning/Tool wire 字段
  -> 对系统层、Skill、Memory、Tool schema 做独立预算
  -> 精确 token 门禁；因果 suffix 不做非连续装箱
```

这不是简单照搬某一个仓库：可以保留当前 `bot` 的版本化摘要与 typed layers，引入 Hermes 的
Provider 发送边界、Pi/OpenCode 的 tail 与超大 turn 切分、DeepSeek Harness 的精确 surface
来源，以及 Nanobot 仅作为最终生存性退路的“连续合法 suffix + 明确低保真 breadcrumb”。

## 10. 关键源码索引

### 当前 `bot`

- [`src/bot/core/agent.py`](../src/bot/core/agent.py)：cursor 投影、ContextItem 组装、近期尾部边界；
- [`src/bot/core/context.py`](../src/bot/core/context.py)：token 预算、Planner 和 Tool 协议修复；
- [`src/bot/core/models.py`](../src/bot/core/models.py)：`reasoning_content` wire 条件；
- [`src/bot/providers/openai_compatible.py`](../src/bot/providers/openai_compatible.py)：流式 reasoning
  捕获与 OpenAI-compatible 请求；
- [`src/bot/compaction/service.py`](../src/bot/compaction/service.py)：摘要输入、验证、发布与恢复；
- [`src/bot/sessions/store.py`](../src/bot/sessions/store.py)：原始消息和压缩版本持久化。

### 本地开源仓库

| 实现 | 关键路径 |
|---|---|
| Codex | `codex-rs/core/src/{client.rs,compact.rs,compact_remote.rs}`；`codex-rs/core/src/context_manager/{history.rs,updates.rs}`；`codex-rs/core/src/context/{user_instructions.rs,world_state/}`；`codex-rs/ext/{memories,skills}/src/extension.rs` |
| OpenCode | `packages/opencode/src/session/{prompt.ts,message-v2.ts,compaction.ts,instruction.ts}`；`packages/opencode/src/session/llm/request.ts`；`packages/opencode/src/provider/transform.ts` |
| Pi | `packages/ai/src/{types.ts,api/transform-messages.ts,api/openai-completions.ts,api/openai-responses-shared.ts,api/openai-codex-responses.ts}`；`packages/coding-agent/src/core/{messages.ts,system-prompt.ts,session-manager.ts,agent-session.ts}`；`packages/coding-agent/src/core/compaction/{compaction,utils}.ts` |
| Hermes | `agent/{system_prompt.py,turn_context.py,conversation_loop.py,context_compressor.py,chat_completion_helpers.py,message_sanitization.py}` |
| DeepSeek Harness | `packages/core/system-prompt/src/index.ts`；`packages/core/agent-loop/src/{agent.ts,runtime-context.ts,tool-calls.ts}`；`packages/compaction/compaction-basic/{README.zh.md,src/region.ts,src/summarizer.ts}`；`packages/llm/llm/src/message.ts`；`packages/llm/llm-deepseek/src/serialize.ts` |
| Nanobot | `nanobot/agent/context.py`；`nanobot/session/manager.py`；`nanobot/agent/context_governance.py`；`nanobot/agent/memory.py`；`nanobot/agent/loop.py` |

## 11. KAT 演进参照

KAT `a93d3c262cae` 中的 `nanobot` 已具备 JSONL session、`last_consolidated`、合法近期后缀、
token 压力 consolidation 和 LLM 失败 raw archive。`Session.get_history()` 的默认上限是 500
条，但 Agent loop 在 consolidation 后以 `max_messages=0` 读取全部未归档尾部，因此实际主边界
来自 consolidation，而不是无条件的 500 条滑窗。它会随保留消息回传 `reasoning_content`，但
摘要 formatter 仍只读取普通 `content`。与当前 Nanobot 相比，它还没有每次请求的显式 replay
token budget、`thinking_blocks` 的完整历史复制和更细的 in-flight context governance。因此它
证明了 Nanobot 路线的演进重点一直是“让游标前进并保持请求有界”，而不是构建 Codex/Pi 式的
高保真 checkpoint 投影。
