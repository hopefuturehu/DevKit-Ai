# Skill 卸载与缓存复用：本地开源框架调研

> 日期：2026-09-08；性质：固定 checkout 的源码调查与设计建议，未运行新的线上缓存实验。
>
> 调研时本项目基线：`1c9e6e9`。本文记录当时对[按 Run 释放 Skill 的设计](../designs/skill-run-lifecycle-design.md)的修订建议；外部框架结论仍限定于文中固定 checkout。
>
> 2026-09-10 更新：`875750c` 已实现首版 Run 隔离、历史交付与恢复；通用裁剪与投影发布仍未落地。
> 当前能力及新增模型 smoke 见[实施与验证记录](../evaluations/skill-context-validation.md)。下文推荐策略不等于全部已实现。

## 1. 结论

最值得借鉴的不是一个 `unload_skill()` API，而是三种组合策略：

1. **正文在使用时进入会话历史，不反复插入历史前面的活动层。** OpenCode、Pi、Hermes、DeepSeek Harness 的工具加载路径，以及 Codex 的显式 Skill 注入路径都体现了这一点。
2. **正常执行期间尽量保持已经发送过的历史不变。** Skill 不再适用，与历史正文立即消失，是两个不同动作。本次检查的主路径没有展示“每轮结束都清除历史 Skill 正文”的统一机制。
3. **真的需要减载时，集中改写并提供恢复提示。** Hermes 对这种取舍最明确：保护近期 Skill、正文丢失后保留重读标记，并用最小回收量与再次触发门槛减少连续缓存失效。

对 `bot` 的推荐是：**Run 结束立即释放绑定；正文只在加载位置交付一份；在自然压缩或收益足够大的批次中卸载；Skill 控制工具的 schema 不随 active 数量变化。** 这减少可避免的前缀扰动，但不能保证最高缓存命中率，也不能保证未卸载的历史正文完全不影响模型。

## 2. 范围与版本

| 本地仓库 | 核对的 HEAD | 本次重点 |
|---|---|---|
| `codex` | `41ece455b7fa` | Catalog 与正文角色、显式注入、历史记录 |
| `opencode` | `da4730e4a41d` | Skill Tool、V1 工具裁剪、LLM 缓存策略 |
| `pi-mono` | `a4453b79bb8d` | read / 显式命令、通用压缩、Anthropic adapter |
| `hermes-agent` | `eac1e25127a7` | Skill 保护、卸载标记、批量裁剪、缓存断点 |
| `deepseek-harness` | `47f943859bef` | Skill 消息、目录更新、压力裁剪 |
| `nanobot` | `c78421cf1651` | always Skill 与按需 Skill、历史窗口 |

这些是 `/Users/huyang/codespace` 中的本地版本，不代表上游最新实现。以下相邻仓库链接用于本机复核；离开该目录布局后应按表中 commit 查阅。正文中的“未见”限于所检查的加载、组装和压缩主路径，不等于穷尽全部插件。

## 3. 缓存的约束：删掉中间内容，后面不能直接接着命中

将实际模型输入简化为：

```text
已发送：P（稳定前缀） + S（Skill 正文） + H（后续历史）
卸载后：P（稳定前缀） +                   H（后续历史）
```

`H` 的字符虽然没变，它在旧请求中的计算依赖前面的 `S`。因此卸载后最多保留 `P` 对应的可用缓存，不能把旧 `H` 的缓存无条件接上来。实际复用还取决于模型、已写入的缓存边界、有效期等条件；共同前缀长度只是结构性机会，不是服务端命中量。[OpenAI Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)

Anthropic 的 `cache_control` 标记截至该位置的完整前缀，顺序包含 tools、system、messages。给多个块放断点，不会把它们变成与前文无关的缓存岛。[Anthropic Prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

由此可得几个工程边界：

- 将正文换成短收据、同长度空白或 hash，仍然改变内容；保存数据库原文也不会消除请求投影的差异。
- 给 Skill 一个稳定 ID，能帮助去重与回读，不能绕过前缀匹配。
- 删除位置越靠前，可能失去的后续复用越多。Tool schema 改变可能比 messages 内卸载正文更早产生差异。
- 缓存命中本身并不减少窗口占用。保留大量失效正文会继续消耗输入预算，并可能影响回答质量。
- 需要立即移出正文时，应接受缓存重建；不能同时承诺“彻底删除中间正文”和“后缀缓存完全保留”。

## 4. 各框架如何做

### 4.1 对照表

| 框架 | 正文如何进入请求 | 何时减少正文 | 对本项目的启发与限制 |
|---|---|---|---|
| Codex | Catalog 为 developer fragment；显式 Skill 正文为 user fragment，写入会话记录；按需读取也可通过工具进入历史 | 历史由通用上下文管理处理；“不跨 turn 沿用”是适用规则，不能解释为历史正文已删 | 本轮适用性与历史存在性可以分离；不能据此宣称实现了自动正文卸载 |
| OpenCode | `skill` 工具返回完整手册、基础目录和文件引用 | V1 旧工具输出裁剪跳过 `skill`；完整 compaction 仍可覆盖它 | 对近期方法保持连续性；保护正文也意味着继续占用窗口 |
| Pi | Catalog 给名称、说明、路径；自动用 read 工具读取；`/skill:name` 将正文展开进 user 消息 | 通用 compaction 以摘要与保留尾部构造请求 | 正文随历史演进，未见独立 Run 结束卸载机制 |
| Hermes | Catalog 发现；`skill_view` 返回正文；显式入口也可展开为 user scaffold | 近期保护、压力裁剪、摘要；大正文移出后留下重读标记 | 同时考虑行为连续性与缓存重建频率，是最直接的参考 |
| DeepSeek Harness | `skill` Tool Result 或显式命令生成的 user instruction 消息；目录变化按 digest 发布更新 | 可选工具结果 pruner 在压力 / overflow 路径运行，再决定是否摘要 | 少改旧目录；但 surface replacement 仍会改变模型前缀 |
| Nanobot | 普通 Skill 目录 + `read_file`；`always` Skill 正文放 system | 通用历史窗口 / 压缩；always 部分随 system 构建 | always 是常驻例外；没有提供任务结束卸载兼顾缓存的专门方案 |

### 4.2 Hermes：减少“为了节省少量 token 而频繁重建缓存”

在 [context_compressor.py](../../../hermes-agent/agent/context_compressor.py) 中可以直接核对：

1. `_collect_protected_skill_names()` 保护最近 10 条消息加载的 Skill、保留尾部中的 Skill，以及尾部 user 消息提及的 Skill。普通裁剪遵守保护；更强的压力降级可以越过它，因此不是永久保留。
2. `skill_view` 大正文进入对应压缩分支时，使用 `SKILL_PRUNED` 标记明确正文已经丢失、需要重新调用 `skill_view`。摘要后还会确定性补回遗漏标记，避免摘要写着“已加载”而手册已经不存在。
3. `prune_tool_results_only()` 只有通过触发量和最小回收量门槛才提交改写。提交后设置下一次触发门槛：

   ```text
   reclaimed = before - after
   runway = max(reclaimed, proactive_prune_tokens, min_reclaim_tokens)
   next_rearm_tokens = after + runway
   ```

   因而不会刚裁剪一点、下一步又裁剪一点。低于门槛直接返回原输入；配置为关闭或最低回收量为零时，不能声称所有保护都默认启用。
4. `micro_compact` 默认关闭；源码注释明确说明，每轮改写已经发送的历史会每轮破坏缓存前缀。主动裁剪也可配置关闭，不能把所有可选机制描述成默认常开。

这里的价值是**把重建缓存看成需要摊销的成本**。最小回收量与增长门槛适合参考，具体数值需要按本项目工作负载校准。

此外，[prompt_cache_boundary.py](../../../hermes-agent/agent/prompt_cache_boundary.py) 与 [prompt_caching.py](../../../hermes-agent/agent/prompt_caching.py) 支持由构建器声明：

```text
user block 1：固定 Skill scaffold / 正文    ← cache_control
user block 2：本次工单、时间或运行参数
```

它避免变化参数污染正文之前的缓存边界，适用于重复 webhook / cron 模板，前提仍是更早的前缀相同。边界由构建器注册，不靠搜索正文里的分隔符猜测。此实现也有边界：注册信息进程内保存；消息离开被标记的近期窗口后可能恢复为一个块，产生一次请求形状变化。它解决的是重复调用时静态部分的复用，不是删除正文后复用它后面的历史。

### 4.3 OpenCode：先保护 Skill，不急着卸载

V1 的 [session/compaction.ts](../../../opencode/packages/opencode/src/session/compaction.ts) 明确有：

```typescript
PRUNE_MINIMUM = 20_000
PRUNE_PROTECT = 40_000
PRUNE_PROTECTED_TOOLS = ["skill"]
```

其工具裁剪从旧历史选择候选，跳过受保护的 `skill` 结果；在累计可裁剪量超过门槛后统一标记。这里的 20K / 40K 是该源码路径的估算 token 参数，不是本次测出的收益，也不是每份 Skill 的预算。

需要分清版本：同一 checkout 还有 `packages/core/src` 的新实现。[新 Skill Tool](../../../opencode/packages/core/src/tool/skill.ts) 同样返回正文，但不能把 V1 的 `PRUNE_PROTECTED_TOOLS` 直接归给新 compaction 路径，更不能说 Skill 永不进入摘要。

[LLM cache-policy.ts](../../../opencode/packages/llm/src/cache-policy.ts) 的默认策略在最后一个工具定义、最后一个 system part、最新 user 消息处放断点。最新 user 在一个长工具循环中较稳定，适合保护本轮共同前缀。这一适配层仅对其支持的 Anthropic / Bedrock inline hint 协议执行；不代表全部 Provider 都接受同样字段。

### 4.4 Codex、Pi：把“加载”记录下来，让正文跟随历史

Codex 的 [fragments.rs](../../../codex/codex-rs/ext/skills/src/fragments.rs) 明确区分 `AvailableSkillsInstructions` 的 developer 角色和 `SkillInstructions` 的 user 角色。[turn.rs](../../../codex/codex-rs/core/src/session/turn.rs) 在 `build_skills_and_plugins()` 后将注入项逐个 `record_conversation_items()`。这不是每一步都从 active 列表重建一个位于旧历史前面的全文层。

[catalog_prompt.rs](../../../codex/codex-rs/ext/skills/src/catalog_prompt.rs) 中不跨 turn 沿用 Skill 的规则，描述何时继续应用 Skill；仅凭它不能推导出“上轮正文从请求删掉了”。这是逻辑释放与物理卸载需要拆开的直接例子。

Pi 的 [skills.ts](../../../pi-mono/packages/coding-agent/src/core/skills.ts) 只将可发现信息格式化进目录；[agent-session.ts](../../../pi-mono/packages/coding-agent/src/core/agent-session.ts) 的 `_expandSkillCommand()` 在显式命令位置展开正文。其 [compaction.ts](../../../pi-mono/packages/coding-agent/src/core/compaction/compaction.ts) 用通用摘要与 `firstKeptEntryId` 管理历史，没有在上述路径为 Skill 单独定义任务完成后清除正文。Anthropic 的 system、工具和符合条件的末尾 user 消息标记位于 [anthropic-messages.ts](../../../pi-mono/packages/ai/src/api/anthropic-messages.ts)。

### 4.5 DeepSeek Harness、Nanobot：可借鉴的局部细节

DeepSeek Harness 的 [tool-skill/index.ts](../../../deepseek-harness/packages/skill/tool-skill/src/index.ts) 在目录 digest 不变时避免重复发布；变化时发布新的目录说明，显式调用的正文也作为当时的消息注入。其 [compaction-basic](../../../deepseek-harness/packages/compaction/compaction-basic/src/index.ts) 在压力达到阈值后先调用可选 [tool-result-pruner](../../../deepseek-harness/packages/compaction/compaction-tool-result-pruner/src/index.ts)，重新计数后才决定是否摘要。后者是通用大工具结果裁剪，不是 Skill 任务生命周期机制。

它保留原始事件，并用 surface operation 替换模型视图。**存储 append-only 不等于请求 append-only**，裁剪仍需承担缓存差异。

Nanobot 的 [context.py](../../../nanobot/nanobot/agent/context.py) 把 always Skill 放 system，普通目录与 [skills.py](../../../nanobot/nanobot/agent/skills.py) 引导模型按需读取。它还把运行时信息放在当前 user 的正文之后，有利于保留更早前缀；但这没有解决 always 正文增删后的后缀重用问题。

### 4.6 运行中遇到压缩，与执行结束后的实际路径

这里的“结束”统一指一次用户输入对应的 Agent 执行返回，不把它等同于业务任务验收通过，也不等同于整个 session 关闭。这些框架通常没有可独立观测的“Skill 正在执行 / 已执行完成”状态机；它们处理的是包含 Skill 手册的消息。

#### Hermes

**运行中：** `skill_view` 正文是历史 Tool Result。普通工具裁剪按近期加载、保留尾部和用户近期提及保护 Skill；极端压力下可以取消保护。完整摘要还会覆盖原来受保护、后来进入旧历史的正文，因此在摘要输入中收集将丢失的 Skill，并在摘要输出后确定性补回 `SKILL_PRUNED` 标记。这个实现提供明确的重读提示，不能等同于已经自动执行 `skill_view` 或建立了执行前恢复门禁。

例如：读手册 → 执行多步 → 手册进入压缩中段 → 摘要保留任务进度，并补上“正文已丢失，请重读”。模型后续需要手册细节时仍需发起读取。大正文标记和近期保护是不同措施；不能因为有标记就声称全文始终保留。

**执行结束后：** [turn_finalizer.py](../../../hermes-agent/agent/turn_finalizer.py) 保存轨迹与会话，并调用任务资源清理；[cleanup_task_resources](../../../hermes-agent/agent/chat_completion_helpers.py) 清理的是 VM / 浏览器等资源，不是删除 Skill 消息。可选 `micro_compact` 在正常结束后处理历史，但默认关闭；可选 Skill 后台复盘用于维护经验与手册，也不是卸载正文。未被压缩或裁剪的正文继续随历史存在，没有在此处按 Skill 完成信号逐份删除。

#### OpenCode

**运行中：** V1 的 `prune()` 对 `skill` 工具结果有明确豁免，普通 `read` 读取的参考文件不能仅因属于 Skill 目录就自动获得同样保护。完整 `processCompaction()` 则选择历史 head 生成摘要、保留近期尾部；Skill 消息落入 head 时仍可能只剩摘要。插件可以定制压缩上下文，但这是扩展能力，不是默认恢复保证。

**执行结束后：** [V1 prompt.ts](../../../opencode/packages/opencode/src/session/prompt.ts) 在 `runLoop` 结束、返回最后 Assistant 消息前，异步启动 `compaction.prune()`。所以这里确实存在结束后的历史清理，但它仍跳过 `skill`，不是“任务结束自动卸载 Skill”。之后继续会话，正文可能一直保留到更大的 compaction 边界。

同一 checkout 的 [新 compaction.ts](../../../opencode/packages/core/src/session/compaction.ts) 使用单独的摘要与 recent 表示。V1 的豁免列表只证明 V1 行为，不外推到新路径；两者都不能从加载工具本身推导出按任务关闭正文的机制。

#### Codex

**运行中：** 正文通过 Skill 注入片段或读取工具进入历史。普通本地 [compact.rs](../../../codex/codex-rs/core/src/compact.rs) 生成摘要后，用有限的真实用户消息与摘要构建 replacement history；中途压缩会重新注入规范的初始上下文。这里有一个重要边界：[contextual_user_message.rs](../../../codex/codex-rs/core/src/context/contextual_user_message.rs) 将 Skill 注入识别为合成上下文，[event_mapping.rs](../../../codex/codex-rs/core/src/event_mapping.rs) 不把它当成真实用户消息，因此不能靠 user 角色推断整份手册被用户锚点机制保住。

重新注入初始上下文会恢复相关目录和使用说明；所检查的 Skill `ContextContributor` 负责目录，而完整正文由 `TurnInputContributor` 在输入时加载，二者不同。没有在这些路径中发现“遍历此前已加载 Skill 并在每次 compaction 后自动恢复全部正文”的统一操作。

所查 [compact_remote.rs](../../../codex/codex-rs/core/src/compact_remote.rs) 也会过滤旧的合成上下文并重新注入当前初始上下文。远端原生 compaction item 的内部语义保留程度由服务端实现决定，不能仅凭本地代码宣称 Skill 原文被完整保留。以上区分普通本地路径、所查远端过滤路径，不把它们概括成所有模型都保留相同原文。

**执行结束后：** 下一 turn 重新处理当前输入涉及的 Skill；Catalog 的“不跨 turn 沿用”是适用规则。已经记录的正文继续属于会话历史，所查路径未按 turn 完成事件将它逐条删除；同样没有一个可类比 `bot.SkillManager.active.clear()` 的统一正文关闭动作。完整重读仍取决于当前使用规则、模型调用或扩展机制。

#### Pi

**运行中：** 自动读取产生 Tool Result，`/skill:name` 展开成含手册的 user 消息。压缩按 `keepRecentTokens` 选择切点；在近期尾部的正文可保留，位于切点之前的则进入摘要。同一长 turn 也可能被拆分，较早的 turn prefix 单独摘要。`firstKeptEntryId` 是通用历史保留边界，不检查 Skill 是否仍适用。

触发时机也要说准确：[agent-session.ts](../../../pi-mono/packages/coding-agent/src/core/agent-session.ts) 的 `_checkCompaction()` 在 Agent 执行返回后与新 prompt 提交前检查。可恢复超限会压缩并允许一次继续重试；正常回答后达到阈值则压缩历史，后续等待用户或其他续接输入。不能将其描述成每个工具步骤都主动检测并保护活动 Skill。

**执行结束后：** user / assistant / toolResult 已在 `message_end` 持久化；`_handlePostAgentRun()` 处理重试、压缩和排队输入。压缩成功后追加 compaction entry，重建 `agent.state.messages`。未见默认的 Skill 逐份卸载、专门的正文丢失标记或强制重读门禁。扩展可以通过 `session_before_compact` 自定义，但不能把扩展可能实现的行为归为默认能力。

#### DeepSeek Harness

**运行中：** [compaction-basic](../../../deepseek-harness/packages/compaction/compaction-basic/src/index.ts) 在 `agent/pre-step` 检查压力，并处理 `agent/request-error` 的上下文超限。可选 pruner 遍历当前 surface 中的工具结果；默认对超过 8,192 Unicode code point 的文本保留头部 4,096、尾部 1,024，并插入通用中段裁剪标记。它不按 `skill` 工具名豁免，也没有近期活动 Skill 集合；因此大型 Skill Tool Result 也可能被裁短。

显式命令注入的 user Skill 正文不属于 Tool Result pruner 的对象，但仍可进入通用摘要范围。裁剪后重新计量，仍超阈值才继续生成摘要；原事件留存，通过 surface replacement 改变模型视图。保存原事件有利于溯源，不等于正文已自动恢复。

压缩后若原目录消息退出可见 surface，`catalogHistory()` 会使目录重新发布；这保障能力发现，不是重放所有已加载手册。

**执行结束后：** 所查 `agent/status` 的 idle 回调清除 overflow 重试计数，不清除 Skill 正文。Skill 插件注册工具和输入注入器，未见按任务完成删除正文的回调；正文随 session surface 延续，直到后续裁剪、摘要或会话操作改变它。

#### Nanobot

**运行中：** 必须区分 always 和普通 Skill。always 正文每次构建 system 时从定义加载，不属于旧会话裁剪范围；普通 Skill 通过 `read_file` 进入历史，服从通用消息数 / token 尾部窗口与 consolidation，未见专属 Skill 豁免或丢失标记。always 可以继续被注入，但没有消除基础 system 本身过大的风险，也不代表运行中所有 Skill 都有这种待遇。

**执行结束后：** [loop.py](../../../nanobot/nanobot/agent/loop.py) 的 `_state_save()` 保存消息，执行文件容量检查，并为普通持久会话调度 `maybe_consolidate_by_tokens()`。因此结束后可能发生通用历史整理，但不识别某个 Skill 已完成。

另有 [autocompact.py](../../../nanobot/nanobot/agent/autocompact.py)：配置有效空闲 TTL 后，跳过正在执行的会话，对到期会话调度归档。[compact_idle_session()](../../../nanobot/nanobot/agent/memory.py) 保留以 8 条消息为目标、可扩展到用户边界的合法尾部，将其他内容归档并保存摘要。它是会话空闲策略，不是 Skill 结束策略；普通 Skill 正文可能随历史移出，always 下次仍会进入 system。

### 4.7 从实际行为能得出多强的保证

| 问题 | 调研能支持的结论 |
|---|---|
| 是否知道某个 Skill 已完成？ | 所查主路径没有统一的 Skill 任务完成协议，不能把 turn / idle / session TTL 混为一谈 |
| 是否保护正在使用的正文？ | Hermes 有近期与提及启发式保护；OpenCode V1 有工具名豁免；它们都不是业务使用依赖的完整跟踪 |
| 正文被移出后是否明确告知？ | Hermes 的 `SKILL_PRUNED` 最明确；通用摘要或裁剪标记不等价于专属恢复契约 |
| 是否自动重读并检查成功后再执行？ | 所查默认路径未发现跨框架通用的强制恢复闭环，不能将“提示重读”说成“已保障恢复” |
| 结束后是否立即省掉 Skill token？ | 没有统一保证；多数仍随历史保留，有些在通用压缩、裁剪或空闲归档时才移出 |

对 `bot` 的直接启发是：移除独立 Active Skill 全文层，需要补上完整性检查、必要内容恢复与有界失败。
`875750c` 的 history 路径已经实现这个闭环，保留完整正文依赖并服从聚合及请求预算；通用历史
裁剪仍待实现。不能据此认为其他框架也提供相同保证，或把全文保护理解成无限预算。

### 4.8 摘要前有没有工具结果裁剪优先级

需要区分“先缩短哪些已加载结果”“每种结果缩短后留下什么”和“哪些历史进入摘要”。所查实现里，Hermes 的规则最完整，OpenCode V1 有明确的历史裁剪保护；这不等于它们有一张通用、可配置的数字 priority 表。

| 框架 | 摘要前相关处理 | 优先级的实际含义 |
|---|---|---|
| Hermes | 完整压缩前先运行工具结果 prune；也可配置独立主动 prune | 多阶段回收顺序、近期保护、Skill 特例、工具类型定制简短记录 |
| OpenCode V1 | 历史 prune 保护近期结果并跳过 `skill`；摘要输入另行截断 | 历史保护规则明确，但不能把豁免延伸到摘要输入的全文保留 |
| DeepSeek Harness | 可选 pruner 在压力路径先处理大工具结果，再重新计量决定是否摘要 | 主要看文本大小，没有按工具语义价值排序或 Skill 豁免 |
| Codex | 历史记录时按 TruncationPolicy 限制工具输出；所查本地摘要请求仍超限时从最早历史项开始移除 | 长度、历史位置及协议规则，未见该路径的工具价值排序表 |
| Pi | 工具自身可限制输出；通用 compaction 按近期 token 与合法切点选择摘要范围 | 时间位置与 Tool 配对保护，未见默认的历史工具结果多阶段语义 prune |
| Nanobot | 通用近期合法尾部；归档模型输入格式化后按 token 预算截断 | 位置与大小规则，未见按工具重要性分级的 pruner |

**Hermes 的四个 pass：** 先将较大的重复文本结果替换成指向近期副本的说明，再处理受保护尾部之外的旧工具正文，再缩短旧 Assistant Tool Call 的大参数；当受保护区域本身超过软预算，才进一步处理区域内的大结果。压力降级先保留短近期区域，再尝试保留最近一个工具结果，最终必要时连该结果也可缩短。去重不受近期尾部保护限制，因为较新的完整副本仍在；因此不能把整个算法简单概括为“旧的先删、新的绝不删”。

它的 `_summarize_tool_result_unguarded()` 是确定性的工具类型格式化，不是每条结果再调用一次 LLM：terminal 尽量留下命令、退出码和输出行数；read_file 留下路径、起始位置和长度；search_files 留下查询和匹配数；大型 skill_view 被缩短后给出重读提示。这比纯首尾截断更了解工具结构，但也不保证测试失败详情、文件正文等任务关键内容仍然存在。源码见 [context_compressor.py](../../../hermes-agent/agent/context_compressor.py) 的 `_prune_old_tool_results()`、`_summarize_tool_result_unguarded()` 和 `compress()`。独立主动 prune 是可选路径，另有最小回收量与再次触发门槛，不能说所有可选策略默认开启。

**OpenCode V1 的两个层次：** `prune()` 从历史尾部向前扫描，跳过最靠近当前输入的区域、保护累计约 40K token 的候选工具输出、豁免 `skill`，且可回收量超过 20K token 才提交。它不是对各工具逐个计算重要性分数。真正生成摘要输入时，`serialize()` 对已完成工具输出另用 `TOOL_OUTPUT_MAX_CHARS = 2_000` 截取开头；这里没有 `skill` 豁免。工具错误走单独的错误分支，也不能据此声称完整错误信息经过了语义优先级评分。V1 `prune()` 还在主循环结束后被调度，不能描述成每次 LLM 摘要前必定运行它。上述两个实现都在 [V1 compaction.ts](../../../opencode/packages/opencode/src/session/compaction.ts)。

Codex 的长度策略见 [history.rs](../../../codex/codex-rs/core/src/context_manager/history.rs) 的 `process_item()`，本地摘要超限处理见 [compact.rs](../../../codex/codex-rs/core/src/compact.rs)。Pi 的合法切点见 [compaction.ts](../../../pi-mono/packages/coding-agent/src/core/compaction/compaction.ts) 的 `findCutPoint()`。Nanobot 的摘要输入截断见 [memory.py](../../../nanobot/nanobot/agent/memory.py) 的 `archive()` 与 `_truncate_to_token_budget()`。这些均是对应主路径的结论，不外推为项目所有插件、模型和工具都只有同一种策略。

对 bot 的改进方向可先借鉴 Hermes 的分阶段回收与工具专用预览，以及 OpenCode 的有限近期保护；为当前 Skill、未解决错误和后续验证所需证据增加明确的保留 / 回读契约。原子组完整、正文可回读、回收量足够和语义不退化需要分别验收，不能用最终 Planner 的 priority 代替摘要前的信息保护。

## 5. 调研时对 `bot` 的改动建议

### 5.1 旧基线中的三种扰动

`1c9e6e9` 的 `src/bot/core/agent.py::_active_skill_items()` 和工具选择逻辑当时存在以下行为；
当前 history 路径已移除这些路径，见[首版方案](../designs/active-skill-layer-removal-plan.md)：

- Active Skill header/body 在 Memory、Compaction 和近期会话之前；增删会改变较早的消息前缀。
- 自动激活结果返回全文，Active Skill 层又注入全文。即使工具输出另有截断，两条正文交付路径仍同时存在。
- `load_skill_resource` 仅在 `self.skills.active` 非空时进入工具定义；Skill 状态变化可能同时改变 tools 和 messages。

原方案把激活结果改成短收据、仍保留前置 Active Skill 正文，可以减少重复，但无法解决正文撤下后较长后缀的缓存失效。每到新 Run 又把旧资源全文投影成收据，还会增加一次历史改写。因此本次调整不止是推迟清理，还需要改变正文交付位置。

### 5.2 推荐请求布局

```text
稳定的 Skill 控制工具 schema（Provider 按自身协议序列化）
Core / Project / Environment / Catalog / Memory / Compaction
已发布的历史
本轮真实 user
本次显式 Skill 正文，或 assistant 调用 → Skill Tool Result 正文
后续 assistant / tools
短运行时状态
```

这里的“加载一次”是指向历史追加一份，后续请求仍回放这份历史；不是正文只让模型看一次。避免把整个历史 Skill 全文再复制到前面的 `ACTIVE_SKILL` 层。显式加载沿用本项目不可授权的 synthetic user 信封；自动加载保持 Tool Call / Result 配对。

正文保留当前子预算约束、来源 hash、可授权访问的 blob 引用。正文未完整交付时必须标记范围；不能为了缓存绕过 token 硬限额。活动 Skill 正文需要在正常多步任务中稳定可见，不能直接照搬现有一次性 `load_context_reference` 的下一步即收据规则。

### 5.3 双生命周期

| 状态 | 运行绑定 | 模型历史中的正文 |
|---|---|---|
| 本 Run 激活 | active | 在首次加载位置交付；按正常预算处理 |
| Run 结束 | 立即 closed | 标记为待回收，暂时保留原位置与表示 |
| 新 Run | 从空 active 开始 | 旧正文只属于历史参考，不视为本轮激活 |
| 到达物理回收边界 | 仍为 closed | 替换为来源收据 / 摘要与重读提示 |
| 新 Run 再次需要 | 按本次选择重新 active | 同版正文仍完整可见时复用；已卸载时重新读取 |

“待回收”是内部状态，不要逐条改写旧消息加上新标签。当前 active 集合用执行状态与短尾部说明表达，Core 固定说明历史加载不代表当前激活。硬保证来自 Run 隔离与工具执行校验，不能把提示词规则描述为模型无法受旧正文影响。

同版本复用必须核验最终打包结果仍包含完整正文；仅存在于数据库或投影候选中不够。新 Run 重新绑定旧加载记录后，应保护这份仍在使用的正文，不能只看生产 Run 已结束就回收。

建议把一次压缩或批量历史改写之间的稳定区间称为 `cache_epoch`，它是本项目内部观测概念，不是 Provider 的缓存对象。epoch 内尽量保持请求投影、排序、正文块形状稳定；随机 epoch / Run ID 不加入稳定前缀。恢复和 fork 需要恢复既有投影决策，同时始终从空 active 开始。

### 5.4 回收触发与工具定义

首版优先在已有 compaction 时一并移出失效 Skill，减少单独触发历史改写。若独立裁剪，至少需要最小预计回收量和裁剪后增长门槛；不要以“又结束一个 Run”为充分条件。可参考 Hermes 的迟滞公式，但先将门槛做成配置与离线可验证策略，不预填所谓最优值。

临近硬上下文上限、明确要求移出正文、定义或授权变化不允许继续保留时，立即执行相应卸载 / 重建。常规缓存优化不能覆盖这些语义。

在能力启用、权限和配置允许的范围内，让 `activate_skill`、`load_skill_resource` 及既有引用读取工具保持稳定名称、描述、参数和顺序；`load_skill_resource` 是否能成功由当前 Run binding 在执行端校验。自动激活关闭时不额外开放自动激活能力，权限撤销或能力变更仍应实时生效。业务工具的渐进加载另行记账，不能将其变化算成 Skill 优化失败或成功。

### 5.5 防止“已加载但实际上没有正文”

物理卸载后，保留原调用配对和有界收据，说明正文已移出当前请求、引用在哪里、如何重新激活或回读。摘要保留工作成果、来源和必要的重读标记；不要只保留“使用了某 Skill”而让模型误以为手册还在。

原始 Transcript 和 blob 不删。主请求、finalizer、压缩输入与恢复必须共享已发布的投影版本；不能某次请求裁掉正文，下一次又从原文自动放回来。发布失败维持旧视图，不能部分更新引用、摘要和裁剪游标。

## 6. 一个成本算例：请求更短不一定立即更便宜

以下为理想化计算，不是任何框架实测或当前厂商报价。设普通输入单价 `c=1`，缓存读取单价 `r=0.1`；旧请求已缓存，卸载后恰好可命中全部 `P`，之后请求也都命中。忽略缓存写入溢价、窗口增长、输出、TTL、额外激活与压缩费用。

| 部分 | 假设 token |
|---|---:|
| `P` 稳定前缀 | 10,000 |
| `S` 待卸载正文 | 8,000 |
| `H` 正文之后的历史 | 42,000 |

继续保留的下一次输入成本是 `60,000 × 0.1 = 6,000` 单位；立即删除后的下一次是 `10,000 × 0.1 + 42,000 × 1 = 43,000` 单位。虽然少发了 8K token，第一次却多花 37,000 单位。

若之后完全相同地重复，共计 `N` 次请求，两者成本差为：

```text
删除方案 - 保留方案 = (c-r) × H - N × r × S
                     = 37,800 - 800N
```

该假设下直到第 48 次请求，累计成本才反超。若 Skill 靠近历史尾部，后缀 `H` 只有 6K，同样计算的门槛降为 7 次。实际若恰逢一次必需 compaction，后缀本就要重建，删除正文的额外重建代价也会不同。

因此优化应同时关注**正文位置、剩余会话长度、回收时机、实际计费和任务质量**，不能仅看删掉多少 token 或缓存百分比。

## 7. 如何验证这次建议

### 7.1 对照组与场景

| 变体 | 用途 |
|---|---|
| A：旧版 sticky active + 前置正文 + 激活历史正文 | 调研时基线，非当前 legacy 兼容实现 |
| B：Run 释放 + 前置正文 + 跨 Run 立即收据 | 检验上一版设计的收益与缓存代价 |
| C：Run 释放 + 正文仅在加载位置 + 压缩 / 批量卸载 + 稳定控制 schema | 调研推荐；首版未包含批量卸载 |

固定模型、Provider、目录、正文、预算与任务集合，覆盖：Skill 后接无关任务；连续相关任务；长工具循环；多 Skill 交替；重复激活；压缩后续接；同 Runtime 多 session；reload；正文超预算；异常 finalizer。缓存实验隔离各变体的预热与历史，控制执行间隔，防止交叉预热和 TTL 差异混淆结果。

### 7.2 必须报告的指标

- Run / session 状态泄漏、错误释放、Tool 配对破坏、无效引用：确定性验收为零。
- 同一 Skill 同版正文的自动重复交付量，以及 active / inactive-resident / evicted 各自 token，分开报告。
- 实际序列化请求的首次差异位置、共同前缀 token、Tool schema hash、投影版本和改写原因。它们是诊断数据，不能替代服务端命中量。
- Provider 实际返回的 cached / uncached / cache-write token 及费用；缺失字段记为不可观测，不记为零命中。
- 每任务总输入与费用、请求数、重新加载次数、首 token 延迟与端到端延迟，包含压缩和回读成本。
- 无关任务被旧 Skill 干扰的比例、需要细节时的恢复成功率、任务完成率和证据正确性。

用相同样本成对比较，区分第一次加载、稳定期、卸载边界、卸载后的恢复期。初期以正确性不退化、请求结构符合设计为门禁；收益结论须等真实计费与行为数据。仅用字符串前缀比较、单次成功样例或重复同模板，不能宣称线上缓存命中率提升了某个百分比。

## 8. 面试中的表达

> 这次调研把 Skill 的运行绑定和历史正文驻留分开考虑。首版已经实现 Run 结束关闭绑定、正文在
> 加载位置交付、控制工具定义稳定，以及压缩后的原文恢复。独立裁剪、批量回收和跨投影去重仍属
> 后续设计。小样本验证了新布局的规则执行，也暴露了历史正文与压缩输入增加的代价；缓存与费用
> 是否改善仍需隔离预热、按完整任务比较，不能用减少副本直接代替成本结论。
