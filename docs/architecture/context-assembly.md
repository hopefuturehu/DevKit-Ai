# 模型上下文分块与组装顺序

> 状态：当前实现说明
>
> 核对日期：2026-09-10
>
> Skill 实现基线：`875750c`；默认运行时使用 `ContextCompactor`，`HandoffEngine` 目前仅由独立评测入口调用。

本文描述主 Agent 每次调用模型时的实际请求视图。SQLite Transcript、压缩记录、计划事件、
子 Agent mailbox、Markdown 记忆和 Skill 文件是事实源；组装过程生成本次 `ModelRequest`，
不会为了排序或修复协议而改写原始 Transcript。新用户输入、Tool 结果和 mailbox 桥接消息仍会
正常追加到 Transcript；“不改写”不等于整个组装与执行过程没有持久化操作。

当前代码有三种不同请求，不能把实验路径的行为当作默认路径：

| 请求 | 当前入口 | 输入形态 |
|---|---|---|
| 主 Agent 执行 | `AgentRunner._run_loop()` | 本文的分层 `messages` + 独立 `tools` |
| 默认容量压缩 | `ContextCompactor._summary_request()` | 独立摘要 System + JSON User，携带旧摘要与有界历史片段，无业务 Tool |
| A/D handoff 实验 | `HandoffEngine.generate()` | 复制已有主请求并追加交接指令，保留原有前缀、历史和 Tool schema |

Codex、OpenCode、Pi、Hermes Agent、DeepSeek Harness 和 Nanobot 的端到端组装、reasoning
回传、长对话压缩与摘要差异见
[本地开源 Agent 框架上下文管理对比](../research/context-framework-comparison.md)。

## 从事实源到模型请求

一次 Run 开始时，Agent 先探测环境并读取基础上下文，再读取当前活动压缩的游标。会话 tail
只加载 SQLite 中 `position > cursor_position` 的消息；活动压缩覆盖的用户锚点另行回读。
没有非空正文且没有 Tool Call 的无效 Assistant 消息（包括只有 reasoning 的消息）会从请求视图排除，并记录
`context.invalid_message_dropped`，不删除原记录。大消息随后转换为有界引用视图。

若启用了子 Agent，未消费 mailbox 会先以闭合的 Assistant/Tool 消息对追加到 tail，然后才
持久化本轮输入。显式记忆在 Run 开始时加载；每个 step 再排空 steering 队列、更新后台进程
与 TODO 提示、选择 Tool schema，并检查本 Run 的 Skill 正文是否完整可见。正文来自带来源的
历史交付；压缩覆盖后从绑定的原文 blob 追加恢复，Catalog 在 Run 开始时冻结。

每个模型 step 按以下路径处理：

```text
基础上下文 + 记忆 + 活动压缩 + 历史（含 Skill 正文）/子任务结果 + 运行时状态
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
                         │
                         ▼
       最终 Provider 序列化后的 Skill 完整性、工具配对及输入预算检查
```

`_build_context_items()` 的列表追加顺序不是最终顺序。最终顺序由
`ContextPlanner._render_order()` 决定；预算选择发生在排序之前。

## `messages` 的最终顺序

| 顺序 | Layer | 典型角色 | 来源和用途 | 稳定性策略 |
|---:|---|---|---|---|
| 0 | `CORE_POLICY` | `system` | 内置安全、工具、TODO 和完成规则；当前版本 `7` | `PINNED`，只随代码版本变化 |
| 1 | `PROJECT_INSTRUCTION` | synthetic `user` | 从仓库根到当前目录的 `AGENTS.md` | 父目录先、具体目录后；`PINNED`；不能授权 |
| 2 | `ENVIRONMENT` | synthetic `user` | OS、架构、workspace、可执行文件探测 | Run 内稳定；`PINNED`；仅为事实 |
| 3 | `SKILL_CATALOG` | synthetic `user` | 可用 Skill 的精简目录 | 可从磁盘重建；不能授权 |
| 4 | `TOOL_CATALOG` | synthetic `user` | Tool schema 超预算时的未加载工具目录 | 仅超预算时出现；稳定后进入前缀 |
| 5 | `ACTIVE_SKILL` | synthetic `user` | 仅持久布局为 `legacy` 的会话保留 header 和正文 | 新会话默认 `history`，不产生此层 |
| 6 | `MEMORY` | synthetic `user` | 用户显式确认的 `USER.md`/兼容 SQLite 记忆 | 放在会话前，参与稳定前缀复用 |
| 7 | `AUTOMATIC_MEMORY` | synthetic `user` | 仅 `memory.context_mode="eager"` 兼容模式下的自动索引 | 默认不出现；兼容模式也放在 Transcript 前并带 reference-only 边界 |
| 8 | `COMPACTION` | 原始 `user` + 派生 `assistant` | 自动压缩保存活动 Run 中已被覆盖的真实用户输入（包括 steering），随后是唯一活动的 `context_compaction` 摘要 | 均为 `PINNED`；锚点按原位置排序，摘要位于它们和 cursor 后 raw tail 之间 |
| 8 | `SNAPSHOT` | synthetic `user` | 旧 checkpoint 兼容层 | 默认主路径不注入 |
| 9 | `RECENT_CONVERSATION` / `TOOL_RESULT` | 原始角色或 synthetic `user` | 压缩游标后的 SQLite 消息，包括 Skill 正文和 mailbox/必需子任务的 Assistant/Tool 桥接对 | 按 `position` 排序；本 Run 必需 Skill 正文及所在工具组整体 `PINNED` |
| 11 | `RUNTIME_NOTE` | synthetic `user` | Memory Router、持久 TODO、后台进程、停滞恢复、终止及临时约束 | 最易变化，放在动态尾部；硬约束由代码门禁执行 |

表中的数字是 `_render_order()` 的排序权重，不是连续消息编号。`TOOL_SCHEMA` 只用于预算与
统计，不会作为一条消息插入表中。同层按 `position`、稳定 `id` 排序；未设置位置时落到 `-1`。
Skill 正文在新模式下随历史排序，不进入独立前置槽位。旧布局仅由 `_legacy_skill_items()` 生成。

`skills.context_mode` 默认为 `history`。数据库升级把已有会话标为 `legacy`，新会话首次 Run
按配置确定并持久化布局；重启、修改配置和 fork 不会隐式切换已有会话。默认配置下使用 `/new`
开始 history 布局。
自动激活把完整正文写入真正的 Tool Result；显式加载在真实用户消息后追加 `skill_body`。
同版正文已在当前历史中时直接复用。正文与 `skill_deliveries` 来源记录在一个事务中提交，
不经过通用头尾预览；原文版本、blob 引用及消息哈希用于恢复和完整性校验。

绑定只属于当前 Run，完成、异常、取消及收尾失败均释放；旧加载消息不代表后续 Run 已激活。
失效正文继续随普通历史预算和压缩回收。实现边界与验证见
[移除 Active Skill 独立全文层](../designs/active-skill-layer-removal-plan.md)。

## 四角色安全模型

主 Agent 只使用 Chat Completions 已有的 `system/user/assistant/tool`，不依赖 `developer`。
整个主请求只允许一条由代码内置的 Core Policy 使用 `system`；其余动态来源即使由程序探测
或生成，也不获得 System 权限。这个主 Agent 不变量不用于限制独立压缩请求自己的摘要 System。

| 来源 | 当前角色 | 边界 |
|---|---|---|
| Core Policy | 唯一 `system` | bot 自身发布的稳定策略，定义其余来源的解释规则 |
| `AGENTS.md` | `user(name=project_instruction)` | 仓库文件不与内置策略同权 |
| Environment | `user(name=environment_context)` | 探测结果是事实，不是高权限指令 |
| Skill Catalog/正文 | Catalog、显式/恢复正文为 synthetic `user`；自动加载正文为 `tool` | 指导方法，不能扩大权限；来源侧表区分合成消息与真实用户 |
| Tool Catalog | `user(name=tool_catalog)` | 只负责发现能力 |
| 显式/自动记忆 | `user(name=explicit_memory/automatic_memory)` | 历史参考，不是本轮输入且不能授权 |
| Runtime Note | `user(name=runtime_context)` | 搜索、终止和审批门禁由 Agent/Policy 执行；TODO 是执行状态参考 |
| Compaction | 原始 `user` 锚点 + `assistant(name=context_compaction)` | 用户原文和模型派生摘要分开回放 |
| Tool Result / 子 Agent 结果 | `tool` | 保留协议配对，始终按不可信数据处理 |

上表中合成上下文层的 `user` 正文都使用同一个紧凑 JSON 信封：

```json
{
  "schema": "bot.context.v1",
  "kind": "project_instruction",
  "is_current_user_message": false,
  "can_authorize": false,
  "scope": "project",
  "source": "/workspace/repo/AGENTS.md",
  "content": "..."
}
```

`name` 和信封是模型可见的来源标记，不是安全沙箱。普通用户输入及 steering 的 `user` 消息保持
`name` 为空，上表中合成层的 `user` 消息必须同时带保留 `name` 和有效信封。组装器还会检查主
Agent 上下文中只要出现不是内置 `core-policy` 的 `system`，或合成层缺少不可授权信封，就在
调用 Provider 前失败。真正的权限仍由 Policy Engine、实时 Approval、Tool schema 和 Agent
loop 决定；项目文件、Skill、memory、摘要和历史审批都不能通过文本授予能力。

信封优先使用 `workspace/project/session/run`、`skill:<name>` 等稳定逻辑标识；精确绝对路径仍
保存在内部 `ContextItem.source` 用于审计。只有模型确实需要定位的项目指令才显示文件路径，
避免相同任务因临时 workspace 名称不同而产生无意义的 token 和缓存差异。

采用这个设计的原因是：

1. **消除高权限文件注入**：`ContextTrust.UNTRUSTED` 不再可能同时以 `system` 发给模型。
2. **减少角色误认**：统一信封明确区分真人 Transcript 与合成 `user` 上下文。
3. **兼容 Provider**：不要求 OpenAI-compatible 服务正确实现 `developer` 的优先级语义。
4. **稳定缓存前缀**：唯一 System 内容只随 Core Policy 版本变化；仓库、环境和运行状态变化
   不再改写 System 层。
5. **把保证放回程序**：Memory Router 的 required Tool、终止阶段无 Tool、审批和 workspace
   边界均由代码 fail-closed，prompt 只帮助模型选择正确动作。

同一层内先按持久化 `position`，再按稳定 `id` 排序。Assistant 的 Tool Call 与其全部 Tool
Result 使用同一个 `atomic_group`，预算不足时整组保留或整组丢弃，不能拆开。

这里的“典型角色”就是送入 `ChatMessage.to_openai()` 的 wire role，不是 `ContextTrust` 的别名。
`ContextTrust.TRUSTED/USER/UNTRUSTED` 仍是 `ContextItem` 的内部来源/审计元数据，Planner 不按
它改变排序；但是发送前的角色不变量会阻止任何非 Core 内容取得 `system`。`name` 和
`bot.context.v1` 信封帮助兼容 Provider 区分消息，但不会改变 wire role 的权限含义。因此默认
路径不再 eager 注入自动记忆：模型先收到真实 Transcript；Router 只注入不含记忆正文的
synthetic `user` runtime note，需要时强制或建议模型
调用 Tool。检索正文作为紧邻 Assistant Tool Call 的 `role=tool` 一次性交付：

```text
... -> 最新真人 user -> memory-router(user, bot.context.v1)
assistant(tool_call=search_memory) -> search_memory(tool, disposable)
```

显式记忆仍是带强边界的 synthetic `user`，所以不能把所有 `role=user` 都解释为当前真人输入；
压缩投影则把真实用户锚点按原始 `user` 回放，并把派生摘要改为 `assistant`，不再用同一个 user
消息混装两类来源。“最新真人 user 后追加整份自动记忆”的默认路径已经移除。可直接运行的
`client.chat.completions.create` 请求样例见
[`artifacts/memory-diagnostics/client-chat-completions-create-example.py`](../../artifacts/memory-diagnostics/client-chat-completions-create-example.py)；
其他本地框架逐项把哪些内容放入哪些 role，见
[Role 分配与最终消息位置](../research/context-framework-comparison.md#53-role-分配与最终消息位置)。

几个容易混淆的点：

- 显式记忆由用户控制，仍在 Run 开始时加载。自动记忆默认只检索；`eager` 只用于回滚和 A/B，
  并把自动索引放在会话前，避免形成“最新真人 user 后又出现 synthetic user”的归因歧义。
- 压缩摘要不能移到近期会话之后。当前顺序是“被覆盖的原始 user 锚点 → Assistant 派生摘要 →
  游标后的原始消息”；摘要若落到 raw tail 后面会反转继续执行的因果顺序。
- 自动压力压缩把当前活动 Run 的所有真实 user 输入（包括运行中的 steering）作为候选，并保存
  本次实际覆盖的交集；空闲 `/compact` 把最新 user 作为候选，若它仍在 raw tail 就无需重复保存；
  `/compact rebuild` 则继承当前活动压缩版本已有的锚点，不重新选择另一条历史指令。
- Router note 是 synthetic `user` 动态尾部；检索正文只在下一次请求可见，随后替换成不含
  正文的收据。required Tool 由 named choice 或 Agent fail-closed 门禁保证，不依赖该提示的角色。
- 历史中由旧版本写入的 `system` 消息会降级成名为 `historical_context` 的 `user` 消息，
  不会重新获得当前系统策略权限。

## TODO、steering 与子 Agent 结果如何进入上下文

`update_plan` 返回经过校验的完整计划快照，Agent 发出 `plan.updated`，SQLite 保存计划事件。
每个 step 和终止收尾组装时，`_refresh_plan_note()` 从 `store.load_plan(session_id)` 读取最新
计划：只要还有未完成项，就把完整列表和最近调整原因包装为 `session-plan` runtime note。
它使用 `DISPOSABLE`、priority 850，不是 pinned；全部完成或清空后不再注入。压缩游标不覆盖
计划事件，因此即使原来的 `update_plan` Tool 交互已被压缩，后续仍能重建当前 TODO。

steering 按真实 `user` 消息追加。每个 step 开始先排空队列；模型返回纯文本时还会再检查一次，
有新输入就继续循环。若模型返回 Tool Call，则先完成该批 Tool Result，再处理 steering，避免
用户输入拆开协议消息组。多个 steering 都会持久化，Memory Router 使用本批最新一条重新路由。

子 Agent 的结果有两条回注路径，均进入会话层，而不是 System 或 Memory 层：

| 时机 | 请求与持久化形态 |
|---|---|
| 新 Run 开始时收到未消费 mailbox | 构造 `assistant(tool_call=task, delivery=background_inbox)` + `tool(name=task)`；追加消息与 mailbox 确认在同一事务内完成，位于本轮输入之前 |
| 主模型准备结束，但还有必需子任务结果 | 收集结果并构造 `assistant(tool_call=await_agents)` + 对应 `tool`；追加消息并标记已报告，然后继续调用主模型 |

两种结果正文都先保存为 blob，再用有界引用视图回注；不能把成功启动或仍为 `running` 的结果
当成完成证据。普通 `task`/`await_agents` Tool 调用也沿用 Tool Result 的外置与原子组规则。

一个需要按代码实际区分的例外是 `agents.auto_resume_background`：它默认关闭；开启后，运行时
会在父会话空闲时用程序生成的“后台子 Agent 已有新结果或问题……”作为 `RunRequest.prompt`
启动新 Run，随后也写成 `name` 为空的 `user`。因此该可选路径下，单靠 `role=user` 和空 `name`
不能证明消息由真人输入；mailbox 正文本身仍通过上表中的 `tool` 回注。

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
| `memory_tokens` | 8K | 显式记忆预算；`eager` 兼容模式下与自动索引共享 |
| `active_skill_tokens` | 16K | 活动 Skill 正文累计预算 |
| `tool_schema_tokens` | 16K | 业务 Tool schema 的选择预算；内部恢复 Tool 始终先保留 |
| `tool_result_inline_tokens` | 4K | 外置内容在请求中的摘录预算 |
| `recent_conversation_tokens` | 20K | 连续近期原文的有界目标；为保证至少保留一个进展单元，只有最新单个不可拆原子组可突破 |
| `compaction_min_recent_user_turns` | 3 | 强制压缩的预算内停止条件：收集到 3 条真实用户消息即停，合成 Skill 不计入；否则在下一个更旧组会使 tail 超过 20K 时停止 |
| `compaction_summary_target_tokens` | 3K | 摘要软目标；未显式配置时运行时取 `min(3000, compaction_summary_tokens)` |
| `compaction_summary_tokens` | 4K | 摘要正文的估算 token 校验上限；不含回放时附加的溯源头和用户锚点 |
| `compaction_max_output_tokens` | 8,192 | 独立压缩请求的输出预算，与摘要正文限制分开 |
| `compaction_max_input_tokens` | 60K | 独立压缩请求的配置输入上限，仍受模型窗口扣除预留后的可用空间约束 |
| `compaction_input_target_ratio` | 0.8 | 压缩分块规划比例；默认有效输入上限 60K 时按 48K 规划 |

排序靠 layer；是否能进入请求则靠 retention、priority 和预算：

1. 先确定包含 `PINNED` 项的完整原子组，再整组选入，包括核心策略、项目指令、环境、
   最新真实用户消息、history 模式下本 Run 必需 Skill 交付，以及活动压缩中的
   原始 user 锚点和 Assistant 摘要；
2. 其余项按 atomic group 聚合，优先级高者先选；同优先级保留更新的会话组；
3. Tool schema 的 token 先从 target message budget 中扣除；
4. Provider 计数或模型输入预算仍超过 hard limit 时，从低优先级、较旧的非 pinned 组开始二次卸载；
5. 最后才按上表恢复模型应看到的语义顺序。

因此，表中的“靠前”不代表更高保留优先级。例如 `SKILL_CATALOG` 排在会话前是为了形成稳定
前缀，但它的 priority 低于用户消息，压力下仍可能先被卸载。可选组采用按优先级和新旧程度
择优装箱，不保证一定是连续的最近后缀；被跳过的组写入 `dropped_items` 和
`context.packed` 事件，但当前不会在模型消息中插入 gap 标记。

官方 DeepSeek V4 Flash 已通过 `estimate_input_tokens()` 接入对应 tokenizer 和消息编码，
返回本地预测与加余量的 `budget_tokens`，不占用 `exact_tokens` 字段。完整候选请求低于
target 时直接保留，避免字符估算过高造成无谓卸载；触发压缩、装箱和最终发送均检查模型预算。
词表缺失时使用明确标记的保守降级估算；其他模型保留已有计数路径。
见[输入 token 计数与安全余量](input-token-calibration.md)。

`ModelProvider.count_tokens()` 仍是可选的精确计数接口，默认返回 `None`。hard 是本地门禁，
不是服务端精确 tokenizer 保证；若仍漏判，可通过首次上下文长度错误进入一次恢复路径。
模型计数回调先修复 Tool 协议，发送前还会核对最终请求；旧的字符估算仅作为初步规划与诊断。
`recent_conversation_tokens` 约束压缩时保留的 tail，不是 Planner 给整个会话层再设的独立
20K 上限。

## 不调用 LLM 时，当前 `bot` 如何减载

自动摘要不是当前 `bot` 唯一的上下文控制手段。应区分两件事：LLM 压缩负责把旧历史做
**语义整合**；下列确定性机制负责让每次请求的**实际输入视图**变小。它们都不会删除 SQLite
中的原始 Transcript。

| 机制 | 触发或预算 | 对请求视图的处理 | 原文恢复 |
|---|---|---|---|
| 大消息外置 | 普通消息默认超过约 5K token；Tool Result 摘录预算默认 4K | 完整正文写入内容寻址 blob，只内联 head/tail 和 `context_ref` | `load_context_reference` 先按 `query` 检索，或按 `offset/limit` 分块读取 |
| 引用结果一次性交付 | 成功检索或读取外置内容 | 原始命中片段只进入紧随其后的单次模型请求，之后换回短回执；审计仍可保存结果 blob | 原始 `context_ref` 保持可再次检索/读取，回执不改成指向读取结果的新引用 |
| Tool schema 渐进披露 | 全部 schema 超过 `tool_schema_tokens=16K` | 只保留内部恢复 Tool 和已激活业务 Tool，其余降为短目录 | `activate_tools` 按名称重新加载 |
| Skill 正文限额 | 活动正文累计超过 `active_skill_tokens=16K` | 新模式不提交不完整绑定；显式加载终止，自动加载返回工具错误 | 已成功绑定的正文完整保留；恢复或整组装箱后仍超限则停止执行 |
| Memory 限额和按需检索 | 显式记忆使用 `memory_tokens=8K`；`eager` 兼容索引最多 2K | 默认不注入自动索引；Router 按需检索 | `search_memory` / `load_memory_evidence` 一次性交付 |
| 分层预算装箱 | 任意主请求组装时都执行 | `PINNED` 必留；其余按 priority、recency 和 atomic group 选择，放不下的组不进入本次请求 | Transcript 保留；`search_session_history` 可检索 |
| Reasoning 作用域收窄 | Assistant reasoning 不属于仍需回放的 Tool Call 消息 | 普通请求不重放该 reasoning；默认独立压缩输入也不携带 reasoning 正文 | SQLite 仍持久化，来源哈希仍覆盖；handoff 输入见后文 |
| 协议清理 | 组装后的请求副本存在孤儿 Tool 或缺失结果时 | 修复 Call/Result 配对，丢弃孤儿结果；不把历史伪 `system` 恢复为特权消息 | 不改写持久 Transcript |
| Provider 溢出应急外置 | 本 step 首次报 context-length error、未用修复额度，且强制压缩没有推进 | 将超过 2,000 字符的普通正文缩成约 1,500 字符前缀和引用；跳过已登记的 Skill 交付，再重试一次 | blob 中保留完整正文；必要 Skill 仍须通过最终检查 |

其中“预算装箱”是有损的请求投影，但不是持久删除。它目前可能跳过中间原子组后保留更早的
小组，形成没有 gap 标记的非连续视图；这与“连续最近窗口”不是同一种策略。大消息外置、按需
Tool/Skill/Memory 加载则属于可恢复的渐进披露，优先级应高于不可恢复裁剪。

## 大消息和 reasoning 如何计入

普通 Tool Result 都先把完整 `model_content()` 写入内容寻址 blob，再把带 `context_ref` 的请求
视图写入 SQLite；短结果保留全文，长结果只保留受 `tool_result_inline_tokens` 限制的 head/tail。
history 模式的首次 `activate_skill` 正文是专门分支：消息与来源原子保存全文，不走这段预览。
`load_skill_resource` 沿用有界交付，但已交付视图受保护直到下一次有效模型响应；之后保留为普通
历史，不自动改成一次性回执。
成功且带 `context_delivery` 的 `load_context_reference`、`search_memory` 和
`load_memory_evidence` 是一次性交付分支：SQLite Transcript 只保存包含原始引用、操作和范围等
元数据的短回执；内存请求视图临时保存正文，并以
`DISPOSABLE`、priority 800 参与装箱。只在该原子 Tool Call/Result 组实际进入模型请求且 Provider
完成响应后，内存视图才降回短回执；如果 Planner 本次没有选中该组，则不会提前过期。

当前 Agent 还对普通、内部和委派 Tool 统一发出 `tool.result` 事件，并把完整
`result.model_content()` 保存为审计 blob。这个步骤也覆盖上述一次性交付分支。因此“一次性”
指正文不在后续模型窗口重复回放，并不表示 SQLite/blob 中绝不保存读取结果。回执仍指向原始
内容，模型不需要沿着审计 blob 逐层回读；相同内容的重复 blob 写入由内容寻址复用。

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
5K token，摘录上限为 4K token。已登记的 Skill 交付跳过这些通用外置路径。Provider 在本 step
首次报告上下文超限、修复额度未使用且强制压缩没有推进时，对普通正文执行一次更激进的
1,500 字符前缀外置；完整 Skill 依赖不因此变成预览。

Assistant 的 `reasoning_content` 独立持久化，但只有同一条 Assistant 消息还带 Tool Call 时才
序列化回普通 Agent 请求，`TokenEstimator` 也只在这一条件下计入它。默认独立压缩模型的
`new_messages/raw_messages` 不包含 reasoning；不过 `source_sha256` 对完整持久消息计算，因此
reasoning 的任何变化仍会使来源校验失败。压缩请求收到的 reasoning delta 只计入诊断和 usage，
不会成为摘要正文。

## Tool schema 是独立请求字段

Tool 定义不转换成普通 `messages`，而是放入 `ModelRequest.tools`，由 OpenAI-compatible
Provider 序列化成顶层 `tools`。有 Tool 时默认 `tool_choice=auto`；Memory Router 要求检索且
Provider 支持 named choice 时会指定工具，否则由 Agent 的响应门禁执行必需调用约束。
终止模型收尾使用空 Tool 列表。所有可见 Tool 按名称排序，使注册扫描
顺序变化不会无意义地改变缓存前缀。

Skill 控制工具使用本 Run 的 Catalog 快照：非空目录且允许自动激活时提供 `activate_skill`，
非空目录提供 `load_skill_resource`。两者作为内部控制定义优先保留，不因 active 数量变化增删；
资源是否可读由执行端检查当前 Run 绑定。

默认 schema budget 是 16K token。未超预算时发送完整且已排序的集合；超预算时先保留内部
恢复/激活工具、按运行时能力加入的历史/记忆检索工具，以及子 Agent controller 提供的工具，
再加入模型显式激活且仍能放入预算的业务工具，其余工具生成
`TOOL_CATALOG`，模型可调用 `activate_tools` 加载。内部 Tool 的总 schema 若自身已经超过 16K，
不会在这里被删除；最终仍由主请求 `hard` 门禁兜底。

未加载 Tool 的目录按名称排序，每项最多取 160 个描述字符，条目按
`min(4000, tool_schema_tokens)` 的估算预算截取；省略标记和合成信封还会增加少量输入。
`request_handoff` 不在默认内部 Tool 集合中，只有 handoff
实验会额外提供该定义。

## 压缩和 Tool 协议对请求视图的影响

压缩成功后，会话历史层只包含：

```text
被覆盖范围中选定的原始 user 锚点
  + 一个 Assistant 活动摘要
  + covered_end_position 之后的原始消息
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
| 未规划候选输入超过 `target` | 同步尝试压缩一个最旧、Tool-safe 的连续前缀；普通压力保留约 20K 连续 tail，超大单轮可从 Assistant 边界切分并独立回放真实用户锚点 | 压缩成功则推进 cursor；失败则旧摘要/cursor 不变，继续交给 Planner |
| 压缩失败或没有安全前缀 | 不删除原文；普通同增量失败默认退避 300 秒 | Planner 仍可丢弃非 pinned 组并发送，因此压缩失败不等于 Run 立即失败 |
| Planner 估算超过 target | 丢弃放不下的非 pinned 原子组；Provider 有精确计数且超过 hard 时再从低优先级、较旧组开始卸载 | 能降到 hard 内则继续请求，可能形成非连续历史视图 |
| pinned messages + 已选 Tool schema 仍超过 hard | Skill 依赖存在时最多强制压缩修复一次，再验证原文；仍超限不发出执行请求 | `limit_reached/context_limit` 或 `skill_context_budget_exceeded`，禁用模型收尾并使用确定性文本 |
| Provider 返回上下文长度错误且本 step 尚未修复 | 从尾部按真实用户消息和完整原子组选取保留范围；强制压缩未推进则外置普通正文；重建并检查必要 Skill 后重试 | 成功响应后重置修复额度；该额度与 Skill 本地装箱修复共用 |
| 本 step 修复后 Provider 仍返回上下文长度错误 | 不再循环压缩或采样 | Run 为 `failed/provider_error`，禁用模型收尾，使用确定性文本 |

普通预防性压力压缩每轮尝试一个分块；本地 Skill 装箱或 Provider 超限还可在共享修复额度内
强制压缩。修复重试不递增逻辑 step，也不重复普通预防性压缩。backlog 仍很大时，下一 step
再继续推进。显式 `/compact` 则在空闲会话中循环处理分块，直到目标位置、无进展或请求数/时间/费用预算之一
到达上限。

## 默认压缩请求与 handoff 实验的区别

默认 `ContextCompactor` 不直接重发完整主请求，而是重新构造一条摘要 System 和一条
`user(name=context_compaction_input)`。后者是压缩专用 JSON payload，不使用主 Agent 合成层
的 `bot.context.v1` 信封。增量模式传 `previous_summary + new_messages`；从原文重建时传
`raw_messages`，不叠加旧摘要。每条消息默认最多提供 12,000 字符正文，不包含
`reasoning_content`，历史 Tool Call 也作为 JSON 数据而非可执行调用传入。

Skill 历史正文同样可能进入这份有界摘要输入；首版没有把它提前转换成收据。执行请求需要的
全文由 Runtime 在摘要发布后恢复，因此不能把摘要里保留 Skill 名称当作已恢复，也不能忽略
压缩长手册本身的输入开销。

这一路径有自己独立的输入预算：

```text
compaction_hard = min(
  context.compaction_max_input_tokens,
  model.context_window_tokens
    - context.compaction_max_output_tokens
    - context.protocol_reserve_tokens
    - context.safety_margin_tokens
)
compaction_target = floor(compaction_hard * context.compaction_input_target_ratio)
```

默认值为 60K / 48K，计数覆盖摘要 System、JSON 结构、旧摘要和本次历史片段。源码在计算
预算时还以 1 为下限。分块优先选择能放下的最旧连续原子组前缀；连第一个组都放不下时，
会依次尝试把正文压到 2,000 / 512 字符并限制序列化 Tool 参数，再不行才放弃本次压缩。
摘要生成、空正文重试、格式修复、超长再压缩和传输重试都受独立配置约束，不能把“一次推进
一个分块”理解为“最多调用一次 LLM”。范围重试默认最多 2 次，可以缩小待覆盖前缀。

摘要正文默认软目标 3K、估算校验上限 4K，输出预算另为 8,192；八个必需标题是 `Goal`、
`Constraints`、`Progress`、`Key Decisions`、`Relevant Files`、`Failures`、`Next Steps`、
`Critical Context`。默认 `compaction_source_refs="range"` 由运行器保存范围和来源哈希；
配置为 `item` 才额外要求条目来源标注。`compaction_thinking="auto"` 在配置的 DeepSeek
官方域名上设为 disabled，其他端点使用 Provider 默认值；可显式覆盖。原文重建、校验、修复与
恢复的细节见[可恢复上下文压缩](recoverable-context-compaction.md)和
[超大上下文压缩](../research/oversized-context-compaction.md)。

`src/bot/compaction/handoff.py` 中的 `HandoffEngine` 是另一个显式调用的实验入口：

| 项目 | 当前 handoff 实现 |
|---|---|
| A：容量触发交接 | 沿用快照请求的模型、前缀、历史、schema 与 thinking 设置，末尾追加交接指令，要求只输出摘要正文；若返回 Tool Call 则拒绝 |
| D：Tool 提交交接 | 实验请求提供 `request_handoff(reason, summary)`；生成候选时明确提示只调用它，但不强制 named choice；校验后保存闭合的 Assistant/Tool 控制对 |
| AD 策略 | 优先验证已有 D 候选；Tool 协议闭合、未发布且达到高水位时尝试 A 兜底；相同失败快照不无限重试 |
| 引擎构造默认值 | 高水位 90K、低水位 40K、输入上限 114,688、最小释放量 1,024、最多 2 次尝试、单次超时 90 秒；这些是引擎参数，不是生产 `ContextConfig` 默认值 |
| 发布检查 | 原始消息哈希、调用方提供的文件版本、新用户更正、Tool 协议闭合、完整切换后请求预算、父版本与单活动记录约束 |
| 成功后的视图 | 指定的稳定前缀 + 覆盖范围内全部 user 锚点 + 唯一 Assistant 摘要 + 原始 tail；D 另带闭合控制对 |

handoff 复制主请求，因此其中带 Tool Call 的 Assistant reasoning 仍可按 `to_openai()` 规则
回传，不能套用默认压缩“历史 reasoning 不入摘要请求”的结论。保留前缀的长度由
`prefix_count` 指定；未指定时只识别开头连续的 System 消息，L1 驱动显式传入基础上下文长度。
Tool schema 在生成与发布预算中继续计数，summary 合格本身不足以证明完整请求已经降压。

成功发布复用 `context_compactions` 和 `ContextCompactor.context_messages()`，默认 Runner
可以读取这些记录继续运行；但 `cli/runtime.py` 尚未实例化 `HandoffEngine`，主 Agent 也没有
自动 A/D 触发和 `request_handoff` 注册。故生产默认仍是前文的容量分块压缩。
截至核对日期，L0/L1 已完成，L2/L3 尚未运行；L1 是受控断点、强制切换实验，不能证明 D 已能
自主选择交接时机或 AD 已有整题收益。范围与数值见
[A+D 交接测试结果](../evaluations/context-handoff-l0-l1-results.md)和
[交接收益评测方案](../evaluations/context-handoff-evaluation-plan.md)。

## 本地开源实现中的非 LLM 减载策略

本节保留既有调研快照，不代表 2026-09-10 重新核验过各上游最新版。以下结论来自
`/Users/huyang/codespace` 的本地源码快照，只统计不依赖生成式模型产出摘要的
路径。对比基线为 Codex `41ece455b7fa`、OpenCode `da4730e4a41d`、Pi
`a4453b79bb8d`、Hermes Agent `eac1e25127a7`、DeepSeek Harness `47f943859bef`、
Nanobot `c78421cf1651` 和 LightRAG `441a3e4872c2`。更完整的端到端压缩与 reasoning 对比见
[本地开源 Agent 框架上下文管理对比](../research/context-framework-comparison.md)。

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

## 观测字段与源码定位

`/status` 的 `context_manifest` 只列基础 Core/AGENTS/Skill Catalog 的来源和字符数；
`context` 字段另外给出 hard/target、活动摘要、cursor 后消息数和估算 token、活动 Tool，以及
最近一次 pack 的逐层 token 与 `dropped_items`，以及本会话的 `active_skills`、`skill_context_mode`。
在尚未发生 pack 时，`last_pack` 为空，因此它
不是任意时刻都完整的逐层实时 token 清单。

`active_tools` 是会话中显式激活的名称集合，不等于最近请求实际发送的全部 schema；启用子
Agent 时另有 `subagents` 状态。`context.packed` 只在发生卸载时发出，协议修复另发
`context.tool_protocol_repaired`，无效历史消息排除另发 `context.invalid_message_dropped`。
评估最终输入时应同时检查请求记录、Tool schema、pack 报告和协议修复事件。

| 职责 | 当前源码 |
|---|---|
| Core Policy、角色校验、基础来源、预算与排序 | [`core/context.py`](../../src/bot/core/context.py)：`ContextAssembler`、`ContextPlanner`、`validate_main_agent_context_roles` |
| 主循环、TODO/mailbox 回注、外置、Tool 选择与恢复 | [`core/agent.py`](../../src/bot/core/agent.py)：`_run_loop`、`_build_context_items`、`_select_tool_definitions`、`_refresh_plan_note` |
| 序列化角色和 reasoning 规则 | [`core/models.py`](../../src/bot/core/models.py)：`ChatMessage.to_openai`；[`providers/openai_compatible.py`](../../src/bot/providers/openai_compatible.py) |
| 默认压缩请求、分块、锚点与活动投影 | [`compaction/service.py`](../../src/bot/compaction/service.py)：`ContextCompactor` |
| handoff 引擎与受控续跑 | [`compaction/handoff.py`](../../src/bot/compaction/handoff.py)、[`evals/context_handoff.py`](../../src/bot/evals/context_handoff.py) |
| 生产装配与全部默认配置 | [`cli/runtime.py`](../../src/bot/cli/runtime.py)、[`config/models.py`](../../src/bot/config/models.py) |

## 为什么采用这个顺序

本节只解释组装顺序的直接来源；完整源码对比另见
[本地开源 Agent 框架上下文管理对比](../research/context-framework-comparison.md)。本地调研的直接参照版本为
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
[长任务上下文缓存评测](../evaluations/context-cache-benchmark.md#组装顺序优化实验2026-08-23)。

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
同时记录每个变体的费用中位数、模型请求总延迟中位数、请求 p95 延迟中位数，以及 `current`
相对 `raw` 的 token、费用和延迟差值。延迟是 Provider stream 的端到端观测值，不混入本地 Tool
执行时间；它受网络和服务端排队影响，因此只报告、不设固定回归门禁。固定的 50% 比例只作为离线
可控工作负载门禁，不用于约束存在采样波动的真实模型。

规模、模型自主检索和长任务生命周期不应由上述 12 轮 case 代替。对应的独立 case、指标来源、
运行命令和当前仍未覆盖的边界见
[上下文整体测试矩阵](../evaluations/context-evaluation-matrix.md)。
