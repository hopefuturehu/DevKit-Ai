# 本地开源 Agent 框架上下文管理对比

> 状态：本地源码静态分析快照
>
> 日期：2026-08-31；`bot` 文档核对：2026-08-31
>
> `bot` 代码基线：`2035795`

本文比较当前 `bot` 与本地 checkout 中的 Codex、OpenCode、Pi、Hermes Agent、DeepSeek
Harness 和 Nanobot，范围覆盖每次模型请求如何组装、reasoning 如何保存与回传、长对话如何
选择压缩边界、摘要如何生成、发布和失败恢复，以及跨会话自动长期记忆如何提取、管理、检索和
注入。KAT 是较早的 Nanobot 派生实现，放在文末作为演进参照，不与当前 Nanobot 重复展开。

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
   原文尾部”，不是不断覆盖成最近 N 条消息。但“保留 user”有三种不同强度：Codex 只保留
   user-only 原文锚点，OpenCode/Pi 以 turn 为首选切点但允许拆超大单轮，Hermes/Nanobot 才显式
   把至少一个真实 user 作为尾部锚点；DeepSeek Harness 只保证 token 与 Tool 配对边界。不能把
   这些策略统称为“保留最近若干完整用户轮次”。
4. Nanobot 是最明确的滑动窗口实现：每次请求先按消息数、再按 token 从尾部选择连续合法
   后缀，同时异步把被移出的前缀归档成记忆。它优先保证请求能继续，但语义连续性和溯源弱于
   checkpoint 系列。
5. 除 Nanobot/KAT 外，成熟实现通常不会在压缩失败后静默退化成任意最近消息窗口。Codex、
   OpenCode、Pi、DeepSeek Harness 会保留错误或停止；Hermes 可发布明确标记的确定性低保真
   handoff。当前 `bot` 的 Planner 虽会按优先级丢弃可选消息组，但结果未必是连续后缀，这比
   显式失败或显式降级更容易制造不可见的因果缺口。
6. 当前 `bot` 的优势是分层预算、Tool 原子组、不可变 Transcript、版本化摘要、来源哈希和
   fail-closed 发布；20K tail 已改为有界目标并支持 Assistant-safe 切分。剩余缺口是 reasoning
   缺少 Provider/模型来源、最新单个原子组仍可突破 tail 目标、活动中未闭合 Tool 链难以形成
   压缩边界，以及最终打包可能非连续地漏掉未摘要消息。
7. 真正具有可比自动长期记忆流水线的是当前 `bot`、Codex、Hermes 和 Nanobot。OpenCode、Pi 和
   DeepSeek Harness 当前快照中的 summary/checkpoint 主要服务当前会话恢复，不能等同于跨会话
   事实记忆。
8. 四种长期记忆路线的核心差异是信任与召回位置：`bot` 把自动记忆作为低信任的一次性 Tool
   Result；Codex 总是把短索引放入 `developer` 上下文再渐进搜索；Hermes 把内置记忆放进
   `system`、把外部 recall 拼入当前 user 的 API 副本；Nanobot 把 `SOUL.md`、`USER.md`、
   `MEMORY.md` 和 recent memory history 一并放进单个 `system`。
9. 当前 `bot` 选择“宁可漏召回，也不让派生文本单独证明用户说过什么”。它的证据和冲突边界最
   保守，代价是显式历史任务多一次模型往返、隐式语义召回较弱，并且尚未实现基于实际使用的排序、
   自动衰减、Git 版本恢复和跨 Run 全局综合。
10. `/compact` 不是各框架之间统一的协议。Hermes 支持 focus、用户指定边界和 dry-run，Pi 支持
    自定义摘要指令；Codex、OpenCode 和 DeepSeek Harness 只接受无参数命令；Nanobot 当前没有
    `/compact`，其 `/dream` 是长期记忆整理而不是会话上下文压缩。
11. 当前 `bot` 的手动压缩在可恢复性上最完整：既能从不可变原文重建，也能切换回已验证的历史
    摘要版本。它的代价是没有 focus/preview/用户指定边界，追赶可能受请求数、墙钟或费用门禁影响而
    部分完成；空闲检查也尚未像 DeepSeek Harness 那样与后续 Run 接纳组成一个原子 maintenance
    reservation。

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
| `bot` | `2035795` | 分层请求 Planner + 可恢复单摘要 + 原子证据型自动记忆 + Assistant-safe 有界 tail |
| `openai/codex` | `41ece455b7fa` | 结构化 Responses item + 两阶段全局 memory consolidation |
| `anomalyco/opencode` | `da4730e4a41d` | 消息 part + compaction summary/tail |
| `badlogic/pi-mono` | `a4453b79bb8d` | Provider-neutral thinking + append-only session tree |
| `NousResearch/hermes-agent` | `eac1e25127a7` | Provider 策略层 + 多级压缩降级 |
| `deepseek-ai/deepseek-harness` | `47f943859bef` | 事件表层替换 + 插件化 compaction capability |
| `HKUDS/nanobot` | `c78421cf1651` | 连续尾部窗口 + memory consolidation |
| `hopefuturehu/KAT` | `a93d3c262cae` | 较早 Nanobot 派生版本；仅作演进参照 |

## 4. 端到端架构全景

| 实现 | Canonical history | 请求组装核心 | 压缩后的主模型视图 | 原文恢复能力 |
|---|---|---|---|---|
| `bot` | SQLite 原始消息 + 独立 compaction 版本链 | typed `ContextItem` 按 retention、priority、atomic group 打包 | 原始 user 锚点 + 单个 Assistant 活动摘要 + cursor 后的原始消息 | 强：原文不删，摘要保存连续范围、哈希、活动用户锚点和父版本；逐条引用为可选兼容模式 |
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

最新用户消息、活动压缩的原始 user 锚点和 Assistant 摘要是 `PINNED`；会话中用户消息
priority 为 700，assistant/Tool 为 600。这能在多个上下文来源争用窗口时做细粒度预算，但选择
阶段是“按组择优装箱”，不是“从最新消息向前取连续后缀”。若一个较新的超大 Tool 组放不下，
Planner 可以跳过它而保留更早的小组，最终时间顺序虽然正确，语义时间线中间却可能出现未标记
空洞。

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
| `bot` | 只有版本化 Core Policy 为 `system`；组装器拒绝其他 System 来源 | 真实用户；`AGENTS.md`、环境、Skill 目录/正文、Tool 目录、显式/自动记忆、Memory Router 和 runtime note 都是带 `bot.context.v1` 信封、`can_authorize=false` 的 synthetic `user`；压缩范围内的真实用户锚点按原始 `user` 回放；旧版历史 `system` 降为 synthetic `user(name=historical_context)` | 原始 Assistant 回复和 Tool Call；压缩摘要为 `assistant(name=context_compaction)` | `role=tool`；schema 在顶层 `tools`；记忆检索/证据正文为一次性 Tool Result | 默认 `core(system) -> project/environment/skill/memory(synthetic user) -> raw-anchor(user)* -> compaction(assistant) -> transcript -> runtime(synthetic user) -> assistant(memory Tool Call) -> memory(tool)`；`REQUIRE_*` 使用 named choice 或 Agent fail-closed 校验 Tool 名称 |
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
- **大量使用 synthetic user 的代表是 Pi、DeepSeek Harness 和当前 `bot`**；`bot` 只保留单条
  内置 Core System，把项目、环境、Skill、记忆和 runtime 全部降到带统一来源信封的 `user`，
  Compaction 则拆成原始 user 锚点与 Assistant 派生摘要。默认自动记忆改成按需 Tool Result，
  避免把派生索引伪装成真人输入。
- **Hermes 说明压缩摘要的 role 不能只按语义决定**。它会根据严格模板看到的前后角色选择
  `user` 或 `assistant`，必要时把摘要并入 tail；同时用明确的 reference-only 前缀避免旧任务被
  当成新请求。

Role 也不等于来源或信任级别。当前 `bot` 的 `ContextTrust`、Codex/DeepSeek Harness 的 source
provenance、OpenCode/Pi 的 synthetic/summary 标记主要在框架内部携带来源或形态信息，并供审计、
投影或 UI 使用；最终序列化时 Provider 未必能看到这些元数据。模型真正能稳定利用的是 wire
role、相对位置、文本边界和
目标 API 保留下来的专用字段。因此，只把不可信内容标成内部 `UNTRUSTED` 并不足够；`bot`
现在还强制非 Core 内容不得映射为 `system`，并用模型可见的 `bot.context.v1` 信封携带来源、
作用域和不可授权标记；自动记忆同时改变默认注入时机和 Tool wire role，并对高风险归因使用
capability-aware Tool 门禁与原始证据门禁。

### 5.4 自动长期记忆：生成、管理、召回与取舍

#### 5.4.1 比较边界

长期记忆与上下文压缩不是同一个状态：压缩 summary/checkpoint 的首要目标是让当前会话继续；自动
长期记忆的目标是从已经结束或已经归档的会话中提取可跨会话复用的信息，并在未来任务中召回。按此
口径，当前 checkout 中真正可直接比较的是 `bot`、Codex、Hermes 和 Nanobot：

| 实现 | 自动生成入口 | 持久化形态 | 默认读取路径 | 主要定位 |
|---|---|---|---|---|
| `bot` | 新 Root Run 启动时异步处理此前已完成的 Root Run；也可手动 `/memory extract` | SQLite 保留原始 Transcript/提取状态；`topics/<kind>/*.md` 保存带精确证据位置的原子记忆 | 确定性 Router；自动正文只作为一次性 `role=tool` 交付 | 保守的证据型事实库 |
| Codex | Root session 启动时，Phase 1 领取已 idle 的近期 rollout；Phase 2 全局巩固 | DB 中每 rollout 的 `raw_memory`/summary + Git 管理的 `MEMORY.md`、`memory_summary.md`、skills 和 rollout summaries | `memory_summary.md` 进入 `developer`；模型按 prompt 做 quick pass，再搜索具体文件 | 渐进披露的全局知识手册 |
| Hermes | 内置后台 Review 默认按轮次周期触发；外部 Provider 可在每轮结束后异步同步 | 有字符上限的内置 `MEMORY.md`/`USER.md`，或 Honcho/Hindsight/Mem0 等外部后端 | 内置冻结快照进入 `system`；外部 prefetch 拼入当前 user 的 API 副本 | 可插拔 Memory Bus |
| Nanobot | 上下文压力先把旧消息归档到 `history.jsonl`；定时或手动 Dream 处理未消费 history | Git 管理的 `SOUL.md`、`USER.md`、`memory/MEMORY.md`、skills 和 cursor | 长期记忆、recent history 与归档 summary 一起进入单个 `system` | 自编辑人格与知识库 |

OpenCode、Pi 和 DeepSeek Harness 在当前快照中没有同等级的自动跨会话事实提取流水线。它们的
compaction summary、session tree entry 或 checkpoint 主要是当前会话投影，不应仅因名称含
“summary”就算作长期记忆。

#### 5.4.2 当前 `bot` 的硬边界

`bot` 把自动提取分成模型建议和程序裁决两部分：

```text
SQLite 已完成 Root Run
  -> LLM 最多建议 5 条 kind/key/content/confidence/evidence_positions
  -> 程序验证置信度、长度、敏感信息、证据位置和 User preference 来源
  -> 确定性拒绝“用户曾说过/贴出/否认/同意/授权”一类会话事件
  -> 同 key 同正文合并；同 key 不同正文进入 conflicts/；FORGET.md key 被抑制
  -> 默认不注入 MEMORY.md，由 Router 决定 NONE/SUGGEST/REQUIRE/EVIDENCE
  -> search_memory / load_memory_evidence 以一次性 Tool Result 交付
```

每条自动记忆保留 `session_id/run_id/message positions`，用户历史归因只能由
`load_memory_evidence` 返回的原始 `role=user` 支撑。Provider 支持 named `tool_choice` 时直接指定
必需 Tool；不支持时 Agent 在持久化 Assistant 正文和执行 Tool 前 fail-closed 校验，提前回答或错误
Tool 都会被丢弃。完整 Tool 正文只对下一次模型请求可见，SQLite 和后续窗口只留 receipt。

这使 `bot` 的优势集中在：

- 自动内容不默认取得 `system`/`developer`/`user` 的话语权，无关请求也不回放自动索引；
- 事实可追溯到原始消息位置，高风险用户归因有结构化证据门禁；
- 冲突不会被另一个 LLM 静默覆盖，用户 `/forget` 后同 key 也不会被重新学习；
- 原子 key、固定状态转换和一次性交付可以通过确定性测试逐项证伪。

对应代价是：

- 词法 Router 不是语义检索器，关键词不同的隐式相关请求可能漏召回；
- 显式历史任务至少多一次模型往返，模型还可能做非最短的重复搜索；
- 提取器按 Run 顺序处理，没有 Codex 的并发 job lease，也不会跨多个 Run 做全局语义综合；
- `STALE`/`SUPERSEDED` 虽已进入数据模型，但当前没有按时间或实际使用自动迁移状态；
- 自动提取只生成事实、偏好、决策、流程和坑点，不会自动生成 Skill。

#### 5.4.3 与 Codex 两阶段知识手册的差异

Codex Phase 1 在 Root session 启动时领取符合来源、年龄和 idle 条件的 rollout，使用 DB lease、并发
上限和失败 backoff 生成 `raw_memory`、`rollout_summary` 和可选 slug。当前默认每次启动最多处理
2 个、rollout 最大年龄 10 天、至少 idle 6 小时。Phase 2 取得全局锁，从最多 256 个 Stage 1 输出中
优先选择使用次数多、最近使用或生成时间新的记录；默认超过 30 天未使用的不再进入选择，然后让一个
无网络、无审批、不可递归委派的内部 Agent 维护 Git 工作区中的 `MEMORY.md`、
`memory_summary.md`、rollout summaries 和可选 Skill。

| 维度 | `bot` | Codex | 直接结果 |
|---|---|---|---|
| 记忆粒度 | 一个稳定 key 对应一条原子事实 | 每 rollout 原始记忆，再聚合成 task-group handbook | Codex 更擅长跨任务综合；`bot` 更容易去重、审计和定点遗忘 |
| 证据粒度 | 每条事实绑定具体消息 position | thread/rollout path/summary file 级来源 | `bot` 更适合严格归因；Codex 更适合导航和复用完整工作流 |
| 巩固方式 | 程序确定性合并和冲突隔离 | 第二阶段 Agent 根据 diff 全局改写 | Codex 能主动重组、删旧和生成 Skill；也增加一次 LLM 失真面 |
| 过期策略 | 用户 forget；尚无自动 decay | usage count、last usage/generated time、选择窗口 | Codex 能自然淘汰低价值旧记忆；`bot` 可能长期积累未使用条目 |
| 请求常驻内容 | 自动记忆为零，除非 Tool 检索 | `memory_summary.md` 总在 `developer` prompt | `bot` 无关轮次便宜且权限低；Codex 隐式召回入口更强 |
| 检索决策 | 确定性规则 + 可解释词法候选 | developer prompt 指导模型“除明显自包含外默认 quick pass” | `bot` 行为可预测但可能漏召回；Codex 更灵活但搜索步数和判断更依赖模型 |
| 读取审计 | Router/Tool event、证据位置 | memory citation、rollout ID、usage telemetry | Codex 能形成“实际使用→排序”的闭环；`bot` 尚无读取反馈排名 |
| 人工更新 | `/remember` 与 `/forget` 直接、物理信任隔离 | 读路径只允许用户明确要求时写 ad-hoc note | 两者都限制普通自动过程提升权限；Codex 的更新再经过下一轮全局 consolidation |

Codex 的主要优势是规模化生命周期：并发领取、跨进程 lease、使用反馈、自动衰减、Git diff、渐进披露
和 Skill 生成。主要风险是自动内容经过 Phase 1 和 Phase 2 两次模型变换，且短索引最终进入
`developer`；它虽然保留 rollout 指针和读取 citation，但没有 `bot` 当前这种“用户归因必须读取原始
`role=user` position”的协议门禁。

#### 5.4.4 与 Hermes Memory Bus 的差异

Hermes 的内置记忆与外部 Provider 必须分开看：

- 内置 `MEMORY.md`/`USER.md` 是字符有界的条目表。启动时做 threat-pattern 扫描并冻结为 System
  Prompt snapshot；中途 Tool 写入只更新磁盘/live state，下一次 prompt 重建才进入 snapshot。
- 默认每 10 个用户轮次可在回复交付后启动后台 Review Agent，仅开放 memory/skill 管理 Tool。它
  直接判断是否把用户偏好写入内置文件，不产生 `bot` 风格的候选 key、confidence 和消息位置。
- 外部 Provider 每轮可异步同步 user/assistant/Tool 消息；下一轮对非 trivial prompt 执行带超时的
  prefetch。召回结果带 `<memory-context>` fence，但仍拼入当前 `role=user` 的 API 副本并保存同字节
  sidecar 以复用 Prompt Cache。

Hermes 的优势是后端可插拔、可使用语义/图谱检索，并用超时、单写线程、shutdown drain 和 Provider
失败隔离避免记忆服务拖死主 Turn。内置 Tool 还具备字符预算、文件锁、原子写、外部漂移检测和注入
扫描。代价是不同 Provider 的提取、冲突和证据合同不统一；内置自动 Review 可以把推断直接写进随后
以 `system` 加载的 `USER.md`，外部 recall 又使用 wire `user`，二者的来源/权限隔离都弱于 `bot` 的
一次性 `role=tool` 与原始证据门禁。

#### 5.4.5 与 Nanobot Dream 的差异

Nanobot 先用 Consolidator 在 token 压力或 replay window 溢出时，把旧消息的 LLM 摘要追加到
`history.jsonl`；LLM 失败则写最多约 16K 字符的 `[RAW]` breadcrumb，并继续推进游标。Dream 再由
受保护定时任务或 `/dream` 启动，每批读取最多 20 条未处理 history、每条截到 500 字符，使用受限
文件 Tool 修改：

- `SOUL.md`：Agent 行为和 Tool 策略；
- `USER.md`：用户画像和偏好；
- `memory/MEMORY.md`：项目与战略上下文；
- `skills/<name>/SKILL.md`：可复用操作流程。

Dream Prompt 主动要求 MECE 分类、替换冲突、迁移流程到 Skill、按年龄删除过时事实。修改由 Git
记录，可通过 `/dream-log` 审计并用 `/dream-restore` 恢复。

这条路线的优势是学习范围最广，能主动整理、删旧、更新人格和生成 Skill；Git diff/restore 也比
`bot` 的当前 Markdown 投影更直观。代价是 Dream 多数读取已压缩的 history，而不是带精确角色和位置
的原始证据；冲突按 Prompt 直接覆盖，自动过程还能修改随后以 `system` 注入的 `SOUL.md` 和
`USER.md`。`MEMORY.md`、recent history 和 archived summary 又默认每轮进入同一个 `system`，即时
召回高，但 token、Prompt Cache 失效和错误记忆权限都更高。Git restore 能撤销一次错误，却没有
`FORGET.md` 这种防止下次 Dream 再次学回同一内容的抑制机制。

#### 5.4.6 可观察的 trade-off 与可借鉴部分

`bot` 已对 `eager` 与默认 `on_demand` 做过一次固定 fixture 的真实 Provider canary：4 个场景、
两种变体、3 次交错重复，共 24 case，全部通过。on-demand 相对 eager 的成对中位变化为：

| 场景 | 模型请求 | 输入 token | 费用 | 端到端延迟 |
|---|---:|---:|---:|---:|
| 无关请求 | 0 | -355 | -$0.000647 | -1.263 s |
| 隐式相关 | +1 | +1,834 | +$0.001693 | +2.323 s |
| 显式历史 | +1 | +1,374 | +$0.001094 | +0.894 s |
| 用户归因冲突 | +1 | +1,698 | +$0.001448 | +0.501 s |

它证明当前固定样本上的协议可以跑通，并量出了“无关轮次省输入，相关轮次付一次往返”的方向；每桶
只有一个语义 fixture，不能外推总体误归因率或漏召回率。完整边界、测试设计和脱敏结果见
[Memory Router 设计与验收](memory-routing.md)与
[真实 canary JSON](memory-routing-live-canary-2026-08-29.json)。

几种路线可概括为：

| 实现 | 最强项 | 最大代价 |
|---|---|---|
| `bot` | 精确证据、冲突隔离、低权限和低误归因 | 语义漏召回、相关轮次额外往返、缺少自动衰减和全局综合 |
| Codex | 两阶段全局知识手册、usage 排名、渐进披露 | 多次 LLM 变换；自动摘要常驻高权限 `developer` 上下文 |
| Hermes | 多后端语义召回、异步和故障隔离 | 合同随 Provider 变化；内置/外部记忆分别使用 `system`/`user` |
| Nanobot | 自主维护人格、知识与 Skill，Git 可恢复 | 自动内容权限最高，证据与角色边界最弱，常驻上下文最大 |

对当前 `bot`，值得吸收但不应破坏现有证据边界的部分是：

1. 从 Codex 引入 extraction job lease/并发、memory citation、usage count/last-used aging 和 Git
   baseline diff；
2. 从 Nanobot 引入用户可见的 `/memory log` 与 `/memory restore`，但不允许自动过程写入 Core、
   `AGENTS.md` 或 `USER.md` 信任域；
3. 从 Hermes 引入可超时、可隔离的语义检索 Provider 接口，但把返回值统一降为一次性 Tool Result；
4. 保留 `bot` 自身的自动记忆不进入高权限 role、同 key 冲突物理隔离、用户归因必须回读原始
   `role=user` 证据三项不变量。

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

自动压力压缩和用户输入 `/compact` 是两个接口：前者发生在 Agent Loop 内，可能需要压缩后立即
重试或继续；后者是空闲会话上的控制操作，应该明确命令是否进入 Transcript、能否接受参数、如何
与新提示词竞争，以及何时才向用户报告成功。以下先比较真实命令面，再比较共用的压缩后端。

### 7.1 `/compact` 命令面的真实语义

| 实现 | 用户入口 | 接纳与执行 | 一次调用实际做什么 | 失败与恢复 |
|---|---|---|---|---|
| `bot` | `/compact`；另有 `/compact rebuild`、`/compact rollback <id>` | Slash 文本不进 Transcript；活动 Run 存在时拒绝，CLI/Web 等待 `compact_session()` 返回 | 固定本次目标位置，按默认 20K 连续 Tool-safe tail 尽量填满；三条 user 停止条件只用于 `force=True` 的 Agent 恢复路径，不用于手动命令；超大单轮可从 Assistant 边界切分；循环压缩多个最老安全分块，默认最多 8 个 Provider 请求、600 秒、请求间费用阈值 $0.25，不自动续写任务 | 每个分块 fail-closed；命令可能“已压缩但未追到 target”，同时返回 stop reason；可从 raw rebuild 或回滚到已验证的 `ready/superseded` 版本 |
| Codex | `/compact`，不接受 inline args | 活动 task 中禁用；启动独立、不可 steer 的 `CompactTask`，命令本身不是 user turn；执行 pre/post compact hooks | 依 Feature/Provider 选择三条路：Token Budget 模式直接开启新 context window；支持时调用远端 Responses compaction v1/v2；否则本地 LLM 生成 handoff。本地视图保留最近最多约 20K 的真实 user 消息和 summary | 中断传播；其他错误由压缩任务发出但不会替换成功前的活动 history。没有面向用户的 rebuild 或 compaction-version rollback |
| OpenCode | `/compact`，TUI 别名 `/summarize` | 需要已选模型和非空会话；创建 `auto=false` 的 synthetic compaction user marker，再由 Session Prompt Loop 串行处理 | 选择旧 head，保留 usable input 约 25%、且限制为 2K–15K 的近期 tail；超大 turn 可从内部 assistant 边界切开。摘要保存为 `assistant(summary=true)`；手动模式不追加 auto-continue user 消息 | 摘要请求仍 overflow 时写 `ContextOverflowError` 并停止；旧 message/part 仍在存储，但没有 rebuild/rollback 命令 |
| Pi | `/compact [custom instructions]`；RPC 同样接受 `customInstructions` | 先 `abort()` 当前 Agent 操作，再触发 manual compaction；Interactive UI 会暂存压缩期间的新输入；extension 可取消或直接提供压缩结果 | 基于 active session-tree path 保留默认约 20K tail；可把用户指令加入摘要 prompt。若切点落在超大 turn 内，可分别摘要旧 history 与该 turn prefix，再合成一个 compaction entry；完成后不自动续写 | 过小会话报 `Nothing to compact`，连续执行报 `Already compacted`；取消/Provider 失败不追加 entry。原 JSONL tree 不删，但没有摘要版本选择命令 |
| Hermes | `/compress`，`/compact` 是全端一致别名；支持 `<focus>`、`here [N]`/`--keep N`、`up to here`、`--preview`/`--dry-run` | 少于 4 条消息拒绝；即使关闭自动压缩仍可手动执行，`force=True` 绕过自动 cooldown；有压缩锁和 host commit fence；`--aggressive` 明确不支持 | 全量模式使用现有 head/summary/tail 压缩器，focus 调整摘要预算；partial 模式只压 head，再把最近 N 个 user exchange 原样接回；preview 只估算不写入。Codex app-server 会委托原生 thread compact | 默认可原地 soft-archive，也支持旧式 child-session rotation；摘要失败按配置保持原文或发布显式低保真 fallback。没有按 compaction ID 选择历史摘要的命令 |
| DeepSeek Harness | `/compact`，严格无参数 | 只在 idle agent 接纳；命令本身不排队，也不进模型历史。先原子预留 maintenance admission；压缩期间已接纳的新 prompt 保留 FIFO 身份，等持久化 checkpoint 后启动 | 即使未到压力线，也选择“除最近一个合法平衡单元外”的最老 head；写 `compaction/start {turn:null}`，直接调用一次摘要 LLM，重验 selected span，提交 summary + replacement user checkpoint + end，并显式 flush | `busy/cancelled/changed/summary/commit/persistence` 有封闭错误分类；失败尝试仍在 event log，`changed/summary` 保证 surface 未替换，`commit` 明确提示可能部分变化。没有 rebuild/rollback 命令 |
| Nanobot | **没有 `/compact`**；`/dream` 不是替代品 | Chat command palette 不暴露会话压缩；SDK 提供 `compact_session()` 和 `compact_idle_session()`，后者供 idle AutoCompact 使用 | `compact_session()` 只在 replay/token 压力成立时推进；idle API 以最近 8 条合法 suffix 为起点，必要时向前扩到最近 user，再把更老消息归档到 `history.jsonl`。`/dream` 消费这些归档并更新长期记忆文件 | 归档 LLM 失败时写有界 `[RAW]` breadcrumb，仍推进 cursor/删除 live prefix；优先生存性，没有会话摘要 rebuild/rollback。Dream 的 Git restore 只恢复记忆文件，不恢复会话压缩视图 |

命令能力不能从“都有 summary”推出。可由用户控制的维度如下：

| 实现 | 摘要 focus | 用户指定保留边界 | Dry-run | 单次命令追赶多分块 | 原文重建 | 摘要版本回滚 |
|---|---:|---:|---:|---:|---:|---:|
| `bot` | 否 | 否 | 否 | 是 | 是 | 是 |
| Codex | 否 | 否 | 否 | 否 | 否 | 否 |
| OpenCode | 否 | 否 | 否 | 否 | 否 | 否 |
| Pi | 是，自定义摘要指令 | 否 | 否 | 否 | 否 | 否 |
| Hermes | 是 | 是 | 是 | 否 | 否 | 否 |
| DeepSeek Harness | 否 | 命令不支持；编程接口可指定 region | 否 | 否 | 否 | 否 |
| Nanobot | 无命令 | 无命令 | 无命令 | 无命令 | 否 | 否 |

### 7.2 自动触发与压缩后端

| 实现 | 触发与默认预算 | 近期原文 | 边界和单轮超大输入 | 非 LLM 剪枝 | 摘要失败/仍超限 |
|---|---|---|---|---|---|
| `bot` | hard input 由窗口减输出/协议/安全预留后再与 120K 取小；默认 80% 触发 | 20K 连续 tail；当前用户锚点可从压缩范围独立回放 | 不拆 Tool 原子组；单个原子组可越界，单轮整体过大时允许从 Assistant 边界切 | 大消息外置、Tool schema 卸载、Planner 可丢可选组 | compaction fail closed、游标不动；最终 Planner 仍可能非连续丢组 |
| Codex | 模型元数据和当前 context-window 状态触发 auto/native compaction | 本地 replacement 最多约 20K 原始 user message；远端由 checkpoint 结果决定 | Tool output 可截断；压缩请求自身溢出时逐个移除最旧输入 item | Tool output 定长截断 | 正常 sampling overflow 会返回错误并进入明确 compaction 路径，不使用无摘要滑窗 |
| OpenCode | usage 达到 usable input；默认最多预留约 20K output/buffer | usable 的 25%，限制在 2K–15K | 可以在一个超大 turn 内从 assistant 消息边界保留 suffix | 保护近期约 40K 后清空更旧 Tool output，累计收益不足 20K 不提交 | compaction 仍返回 overflow 时写 `ContextOverflowError` 并停止 |
| Pi | `contextTokens > contextWindow - 16,384` | 默认约 20K | 可对超大单 turn 的 prefix 单独摘要；不从 Tool Result 开始 tail | 摘要输入中的 Tool result 截至约 2K 字符 | overflow 最多 compact-and-retry 一次，失败后显式报错 |
| Hermes | 配置默认在有效窗口约 50% 触发；小于 512K 的窗口会把比例提高到至少 75%，也可按模型覆盖或设 token cap | tail 预算默认约阈值的 20%，同时保护有限 head/tail 和最近 user | Tool-safe 切分；活动 turn reasoning 计费与旧 turn 区分 | 主动/溢出 Tool result prune、输入 head+tail bound | 可生成明确的确定性 fallback handoff 并丢中段；也可配置 abort；有 cooldown/anti-thrash |
| DeepSeek Harness | 默认窗口 80% 触发，Provider overflow 也可强制恢复 | 默认窗口 16% | 选择最旧、完整、Tool 配对平衡的 surface 单元；未闭合尾部拒绝压缩 | 可选 Tool result pruner | 默认 1 次额外收敛重试和 1 次 overflow retry；失败保持最新 durable surface，不滑窗 |
| Nanobot | 每次请求都受 `context - output - 1024` replay budget；达到预算时 consolidate 到约 50% | 按消息数和 token 从尾部取连续后缀 | 对齐最近 user 与合法 Tool 起点；不能安全切分时保留可见尾部 | in-flight Tool result compact + `snip_history()` | 摘要失败写有界 `[RAW]` breadcrumb 并推进 cursor；下次不重复攻击同一前缀 |

这里最值得区分的是三种“退路”：

- **失败并停止**：不制造未知信息丢失，Codex/OpenCode/Pi/DeepSeek Harness 主要采用这一类。
- **显式低保真 checkpoint**：Hermes/Nanobot 会告诉后续模型摘要不完整或保存 raw breadcrumb，
  再推进边界，优先避免压缩重试循环。
- **隐式请求裁剪**：当前 `bot` 的 Planner 会记录 dropped item，但模型请求中没有同等醒目的
  gap marker。若被裁掉的是尚未进入摘要的中间 Tool 组，模型无法知道缺口存在。

### 7.3 压缩时如何保留真实用户轮次

“摘要记住了用户目标”和“原始 `role=user` 仍逐字留在压缩后请求里”不是同一件事。本节把
`user turn` 进一步拆成三个可观测量：

- **user-only anchor**：只保留原始用户消息，不要求与其 Assistant/Tool 后续一起保留；
- **完整 turn**：从真实 user 开始，连续保留其 Assistant、Tool Call/Result，直到下一真实 user；
- **摘要承载**：原始 user 已被替换，只能从 summary/checkpoint 恢复其语义。

本地源码中的实际保留策略如下；“至少 N 轮”只在代码存在显式计数门槛时使用，不从 token
预算或“通常能看到几轮”的样例反推：

| 实现 | 选择单位与默认预算 | 原始 user 的硬保证 | 超大单轮与主要代价 |
|---|---|---|---|
| `bot` | 从尾部按完整原子组回扫；20K token 是有界目标；强制路径收集到 3 条 user 即停，否则在下一组会超预算时停止 | 自动压缩回放活动 Run 中所有已覆盖 user（含 steering）；手动以最新 user 为候选，仍在 raw tail 时不重复；rebuild 继承已有锚点；raw tail 本身不保证 N 个 user | 仅最新单个原子组可为进展/原子性越界；完整单轮过大时 raw tail 可从 Assistant 开始，早期执行进入摘要，因此游标仍能推进 |
| Codex 本地 | 从所有真实 user message 中倒序选择，合计硬限 20K；然后追加 summary | 没有轮数下限，也不保留完整 turn；只保证预算内最近的 user-only anchor | 最老一条入选 user 可被截断；Assistant、Tool 和 reasoning 全部从 replacement history 消失，压缩率高但原始因果链只剩摘要 |
| Codex 远端 V2 | 先筛选可保留的真实 user/Agent message，按最新优先限制为 64K，再追加原生 compaction item | 没有轮数下限；developer/system 由当前 canonical context 重新注入，不靠旧消息保留 | 超预算旧文本被截断或丢弃；opaque item 承载语义，无法用正文检查某个 user turn 是否逐字幸存 |
| OpenCode | 把每个真实 user 到下一个真实 user 视为 turn；近期预算为 usable input 的 25%，限制在 2K–15K，可再用 `tail_turns` 限制候选轮数 | 优先保留若干个完整最近 turn，但没有最小轮数保证 | 下一个较老 turn 放不下时可从 turn 内的 Assistant 消息开始保留；若最新单轮本身超大，原始 user 可以只进入 summary，raw tail 从 Assistant 开始 |
| Pi | 从尾部累计到默认约 20K；合法切点可以是 user、Assistant、branch summary，但不能从 Tool Result 开始 | 没有最小轮数；切在 user 时保留完整连续 tail | 切点落在超大单轮内部时，先单独摘要该轮前缀（包含 Original Request），只原样保留后缀；因此任务语义有专门 handoff，但原始 user 不一定仍在 tail |
| Hermes | head + summary + token tail；tail 默认约为压缩阈值的 20%，并显式锚定最新 user、最新 Assistant；`min_tail_user_messages` 默认 1、可配置更大 | 通常至少 1 个真实、非合成、可执行 user；可配置最近 N 个 | user 锚点和 Tool-safe 对齐可令 tail 超预算；若 user 紧贴保护 head，强留会把可压中段耗尽，则把完整已完成 turn 一起摘要，避免把 user 留成悬空待办。首次还保护 3 条非 system 头部消息，后续该保护衰减为 0 |
| DeepSeek Harness | 对 surface node 从尾部累计，默认保留窗口的 16%，然后向前移动到 Tool Call/Result 平衡边界 | 没有 user-aware 保证，也没有轮数概念；raw tail 可以只含 Assistant/Tool | 自动压缩优先满足统一 token meter 和 Tool 配对；手动 `/compact` 以 `retainTokens=0` 只保留最后一个可平衡单元。最近 user 可能完全进入 replacement checkpoint |
| Nanobot | token consolidation 只在下一真实 user 处推进 cursor；普通 replay 先按消息数、再按 token 取连续后缀；idle compact 默认保留最近 8 条 | 有真实 user 时尽量让活动后缀从至少 1 个 user 开始；token 尾部若只剩 Assistant，会向前找最近 user，即使略超预算。没有 N 轮保证 | idle 的 8 条也是可扩展上限：最后 8 条没有 user 时会回退到更早 user；优先生存和合法连续后缀，超预算风险小于多轮下限，但更早任务只依赖 archive summary/`[RAW]` breadcrumb |
| KAT（演进参照） | 较早 Nanobot 路线；token consolidation 在 user 边界切分，单块最多 60 条；idle 保留最近约 8 条 | 尾部切点向前扩到最近 user，没有固定 N 轮 | 比当前 Nanobot 缺少 token-tail 的二次 user 恢复和更完整失败治理；它证明“8 条”是消息窗口，不是 8 个用户轮次 |

对应源码中的决定点分别是：Codex `compact.rs::build_compacted_history_with_limit()` 与
`compact_remote_v2.rs::truncate_retained_messages_for_remote_compaction()`；OpenCode
`compaction.ts::{turns,splitTurn,select}`；Pi `compaction.ts::{findCutPoint,prepareCompaction}`；
Hermes `context_compressor.py::_find_tail_cut_by_tokens()`；DeepSeek Harness
`compaction-basic/src/region.ts::selectCompactableRange()`；Nanobot
`memory.py::pick_consolidation_boundary()`、`Session.get_history()` 和
`Session.retain_recent_legal_suffix()`。

这组实现给当前 `bot` 的直接结论不是“把 3 改成 1”或“照搬 20K”，而是拆开两类保护：

1. **正确性硬保护**应是最新真实 user、尚未闭合的活动 Tool 链和合法 Tool 边界；Hermes 与
   Nanobot 都把“至少一个 user 锚点”放在 token 选择之后补强。
2. **交互质量软保护**才是更多历史 user turn；OpenCode/Pi 允许超大单轮切分，说明完整 N 轮
   不应无条件压倒硬 token 上限。
3. 应分别观测 `raw_user_turns_retained`、`complete_turns_retained`、`retained_tail_tokens` 和
   `compaction_progress_messages`。只断言“摘要包含用户目标”无法证明原始轮次保留策略有效。
4. `bot` 已把三轮下限改为预算内停止条件，并实现“活动 Run 的原始 user/steering 锚点 +
   Assistant-safe raw suffix”。当前实现仍使用一份累计摘要承载早期执行，还没有把 history
   checkpoint 与 active-turn-prefix 拆成两个独立持久化段；这是后续提高时间顺序精度的剩余工作。

### 7.4 当前 `bot` 手动压缩的 trade-off

与其他实现相比，当前命令不是“最灵活”或“并发控制最强”，而是把主要复杂度投入到恢复：

1. **恢复能力最强**：`rebuild` 会重新读取连续原始范围，`rollback` 激活前先重验来源 SHA-256
   和摘要结构。Codex/Pi/DeepSeek Harness 也保留原始事件，但没有把“重建/选择旧摘要”直接做成
   `/compact` 子命令；Hermes 和 Nanobot 的恢复目标则分别偏向 archived transcript 与记忆文件。
2. **长 backlog 追赶更可控**：其他手动命令通常只产生一次 checkpoint；`bot` 会按最老安全
   前缀循环多个有界分块，并同时受请求、时间、费用三类门禁。代价是 `compacted=true` 只表示至少
   发布了一个分块，不保证 `cursor_position == target_position`，调用方必须读取 `reason`。
3. **用户控制弱于 Hermes/Pi**：没有 focus、preview 或显式 keep-last 参数。固定策略减少命令
   分支和不可测组合，但用户无法在压缩前确认范围，也无法告诉摘要器“优先保留某个主题”。
4. **接纳仍有竞态窗口**：`compact_session()` 先检查 `_steering_queues`，随后才进入包含多个
   `await` 的压缩循环；它没有在同一临界区预留会话 maintenance 状态。因此 Web 上新 Run 可在
   检查后开始。固定 target 和事务 parent 校验能避免把新消息误纳入旧摘要，但不能提供 DeepSeek
   Harness 那种“checkpoint flush 前新 prompt 不启动”的强顺序保证。这是源码可见风险，不是本文
   已复现的生产故障。
5. **高保真发布换来更多状态**：`building/ready/superseded/failed`、parent、cursor、source hash
   和 budget stop reason 让失败可诊断；相较 Codex/OpenCode/Pi 的单次 append entry，状态机、恢复
   测试和运维观测成本更高。

`bot` 现在把压缩范围中的活动用户锚点按原始 `role=user` 独立回放，把派生摘要作为
`role=assistant, name=context_compaction` 放在锚点之后。它没有把包含 Tool/文件数据的摘要提升为
System/Developer，也不再用 synthetic user 承载摘要。DeepSeek Harness 和 Codex 本地路径使用
user checkpoint，Pi 将 compaction summary 转成 synthetic user；OpenCode 使用 compaction-user +
summary-assistant；Hermes 根据相邻 role 选择 user/assistant 或合并进 tail；Nanobot 把 archive
summary 放入 system。role 分离降低了“派生摘要被误认成用户亲述”的风险，但历史归因仍须通过
`load_compaction_source` 核验原始 Transcript。

### 7.5 为什么 Codex 通常看起来比 `bot` 压得更彻底

Codex 不是单一压缩路径：Feature 打开时可直接开启新的 Token Budget context window；Provider
支持时使用 Responses 原生 compaction v1/v2；否则走本地文本摘要。与 `bot` 最容易逐行比较的是
本地路径：

| 维度 | `bot` | Codex 本地 compaction | 对压缩率的影响 |
|---|---|---|---|
| 压缩后会话尾部 | 原始 user 锚点 + Assistant summary + cursor 后的连续近期消息；raw tail 可从 Assistant 开始，Tool Call/Result 整组保留 | 从整个旧 history 只重新收集真实 user message，倒序取最多硬上限 20K，再追加一份 summary；旧 assistant、Tool 和 reasoning 不进入 replacement history | `bot` 仍保留近期执行原文，Codex 直接去掉全部旧执行链，因此 Codex 降幅通常更大 |
| 20K 的性质 | `recent_conversation_tokens=20K` 是连续 tail 的有界目标；只允许最新单个不可拆原子组越界 | `COMPACT_USER_MESSAGE_MAX_TOKENS=20K` 是 user 文本硬预算，最后一条过长消息还会截断 | `bot` 为至少一个最新进展单元及 Tool 协议原子性允许有限越界，Codex 为文本硬上限牺牲原文完整度 |
| 摘要输入过大 | 只压连续、已验证、Tool-safe 的最老分块；分块放不下会降级消息视图，仍不安全则不推进 cursor | 本地摘要请求 overflow 时不断移除最旧的规范化 history item，直到请求能发出 | Codex 更容易一次“完成”，但被移除的最旧内容没有进入本次摘要；`bot` 会把它表现为 backlog/部分完成 |
| 摘要发布门槛 | 八段结构、长度、来源范围/引用模式、parent 和 SHA-256 都通过后才事务发布 | 取压缩模型最后一条 assistant 文本，加 summary prefix 后直接构造 replacement history | `bot` 拒绝不合格候选；Codex 接受更自由、更可能遗漏细节的 handoff |
| 原始目标 | 自动压缩回放活动 Run 中已覆盖的所有 user/steering；手动把最新 user 作为候选，仍在 raw tail 时不重复；超大正文可外置成预览 + `context_ref` | 把最近 user messages 作为独立原文重放，受总计 20K 硬限 | 两者都保锚点；`bot` 按当前 Run 身份和版本继承选锚点，Codex 按全历史新旧与硬预算选锚点 |
| 一次命令的范围 | backlog 可拆为多次增量摘要；最多 8 个请求并受时间/费用停止 | 通常一个 standalone compact task 替换当前活动 history | Codex 交互上更像一次清空；`bot` 更明确暴露尚未覆盖的范围 |
| 可恢复合同 | 原 Transcript、每版覆盖范围、hash、parent 和状态均可重建/回滚 | append-only rollout 仍保留事件，但命令面没有按摘要版本重建/回滚 | Codex 的激进 replacement 不等于原始日志被物理删除，但恢复不是 `/compact` 的一等用户操作 |

因此，“Codex 能压得更彻底”本质上是四个选择叠加，而不是同一保真约束下免费得到更高压缩率：

1. 它保留的是 **user-only anchor**，不是最近几个完整 turn；
2. Codex 的 20K 是 user 文本硬上限；`bot` 的 tail 只让最新单个原子组例外，活动 user 锚点则
   作为独立 pinned 投影，并对超大正文使用有界预览和可回读引用；
3. 本地摘要输入溢出时 Codex 可以从最旧 item 开始丢素材，`bot` 不会把整个未进入 payload 的
   消息组标成已覆盖；两者仍都可能截断超长单条消息的摘要视图；
4. 原生 Responses compaction 还可以把历史变成 Provider 理解的 opaque checkpoint，`bot` 当前基于
   OpenAI-compatible Chat Completions，只能用可见文本 summary + 原文 tail 保持跨 Provider 可移植性。

这个取舍也由 Codex 自己的运行时 warning 明示：长 thread 和多次 compaction 会降低准确性，建议
尽量开启新 thread。若只比较“压缩后 token 数”，Codex 更占优；若同时要求 Tool 因果链完整、来源
范围完整性可验证、失败不推进以及用户可选择历史摘要版本，`bot` 用更低压缩率换取了更强恢复合同。

## 8. 摘要机制

| 实现 | 摘要输入 | 摘要输出约束 | 发布与溯源 |
|---|---|---|---|
| `bot` | `previous_summary + new_messages`，周期性可从 raw rebuild；消息含 role/content/Tool 信息，不含 reasoning | Goal、Constraints、Progress、Key Decisions、Relevant Files、Failures、Next Steps、Critical Context 八段；默认目标 3K、正文硬限 4K，支持格式修复和候选凝练 | `building -> ready -> superseded/failed`；记录连续覆盖范围、SHA-256、活动 user 锚点和父版本，默认 `source_refs` 可为空；SHA 覆盖含 reasoning 的完整持久消息，但校验通过才推进 cursor |
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
3. **分段 checkpoint**：有界 tail、原始 user 锚点和 Assistant-safe 切分已经实现，但一份累计
   summary 仍同时承载更老历史和当前 turn prefix；后续可拆成两个有独立来源范围的派生段，进一步
   保持时间顺序并独立控制预算。
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
- [`src/bot/cli/app.py`](../src/bot/cli/app.py) 与
  [`src/bot/web/server.py`](../src/bot/web/server.py)：`/compact` 命令和 Web 控制入口；
- [`src/bot/sessions/store.py`](../src/bot/sessions/store.py)：原始消息、压缩版本和记忆提取 job
  持久化；
- [`src/bot/memory/service.py`](../src/bot/memory/service.py)：自动候选生成与确定性验证；
- [`src/bot/memory/store.py`](../src/bot/memory/store.py)：Markdown 记忆、证据、冲突和忘记规则；
- [`src/bot/memory/routing.py`](../src/bot/memory/routing.py)：四级确定性 Memory Router。

### 本地开源仓库

| 实现 | 关键路径 |
|---|---|
| Codex | `codex-rs/tui/src/{slash_command.rs,chatwidget/slash_dispatch.rs}`；`codex-rs/core/src/tasks/compact.rs`；`codex-rs/core/src/{client.rs,compact.rs,compact_remote.rs,compact_remote_v2.rs,compact_token_budget.rs}`；`codex-rs/core/src/context_manager/{history.rs,updates.rs}`；`codex-rs/core/src/context/{user_instructions.rs,world_state/}`；`codex-rs/memories/{README.md,write/src/phase1.rs,write/src/phase2.rs,write/templates/memories/}`；`codex-rs/ext/{memories,skills}/` |
| OpenCode | `packages/tui/src/routes/session/index.tsx`；`packages/app/src/pages/session/use-session-commands.tsx`；`packages/opencode/src/server/routes/instance/httpapi/handlers/session.ts`；`packages/opencode/src/session/{prompt.ts,message-v2.ts,compaction.ts,instruction.ts}`；`packages/opencode/src/session/llm/request.ts`；`packages/opencode/src/provider/transform.ts` |
| Pi | `packages/ai/src/{types.ts,api/transform-messages.ts,api/openai-completions.ts,api/openai-responses-shared.ts,api/openai-codex-responses.ts}`；`packages/coding-agent/src/modes/interactive/interactive-mode.ts`；`packages/coding-agent/src/core/{messages.ts,system-prompt.ts,session-manager.ts,agent-session.ts}`；`packages/coding-agent/src/core/compaction/{compaction,utils}.ts` |
| Hermes | `hermes_cli/partial_compress.py`；`cli.py`；`gateway/slash_commands.py`；`agent/{conversation_compression.py,system_prompt.py,turn_context.py,conversation_loop.py,context_compressor.py,chat_completion_helpers.py,message_sanitization.py,memory_manager.py,memory_provider.py,background_review.py}`；`tools/memory_tool.py`；`plugins/memory/` |
| DeepSeek Harness | `packages/core/system-prompt/src/index.ts`；`packages/core/agent-loop/src/{agent.ts,runtime-context.ts,tool-calls.ts}`；`packages/compaction/command-compact/{README.zh.md,src/index.ts}`；`packages/compaction/compaction-basic/{README.zh.md,src/index.ts,src/region.ts,src/summarizer.ts}`；`packages/llm/llm/src/message.ts`；`packages/llm/llm-deepseek/src/serialize.ts` |
| Nanobot | `nanobot/command/builtin.py`；`nanobot/sdk/clients.py`；`nanobot/agent/{autocompact.py,context.py,context_governance.py,memory.py,loop.py}`；`nanobot/session/manager.py`；`nanobot/templates/agent/dream.md`；`nanobot/skills/memory/SKILL.md` |

## 11. KAT 演进参照

KAT `a93d3c262cae` 中的 `nanobot` 已具备 JSONL session、`last_consolidated`、合法近期后缀、
token 压力 consolidation 和 LLM 失败 raw archive。`Session.get_history()` 的默认上限是 500
条，但 Agent loop 在 consolidation 后以 `max_messages=0` 读取全部未归档尾部，因此实际主边界
来自 consolidation，而不是无条件的 500 条滑窗。它会随保留消息回传 `reasoning_content`，但
摘要 formatter 仍只读取普通 `content`。与当前 Nanobot 相比，它还没有每次请求的显式 replay
token budget、`thinking_blocks` 的完整历史复制和更细的 in-flight context governance。因此它
证明了 Nanobot 路线的演进重点一直是“让游标前进并保持请求有界”，而不是构建 Codex/Pi 式的
高保真 checkpoint 投影。
