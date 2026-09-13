# 超大上下文的摘要输入预算与恢复：本项目及开源实现

核对日期：2026-09-09。本文接续[历史 reasoning 输入调研](compaction-reasoning-input.md)，
解释单次摘要输入预算、超过预算后的动作，以及历史大于摘要模型窗口时的处理方式。
本轮仅补充研究文档，运行本地测试和合成探针；未改变运行器或进行新的模型质量实验。

Claude Code 的官方机制补充见[单独调研](claude-code-compaction.md)：它复用主会话前缀生成
摘要，仍需为指令及输出预留窗口，并区分摘要失败与压缩成功后立即重新填满的 thrashing。

允许重做架构的候选见[上下文压缩重设计](../designs/context-compaction-redesign-options.md)，包括大范围
摘要、完整分层覆盖、任务状态检索和阶段交接；这些候选尚未实现，参数也不是实测最优值。

## 1. 为什么本项目限制为 60K，实际按 48K 规划

这里有三个不同概念：上游模型实际窗口、本地配置的窗口、压缩器单次请求的策略预算。
`compaction_max_input_tokens=60000` 是第三项，不是上游模型的能力声明。

当前计算为：

```text
单次摘要输入预算 = min(
    compaction_max_input_tokens,
    model.context_window_tokens
      - compaction_max_output_tokens
      - protocol_reserve_tokens
      - safety_margin_tokens
)
规划目标 = floor(单次摘要输入预算 × compaction_input_target_ratio)

默认值：min(60,000, 131,072 - 8,192 - 2,048 - 2,048) = 60,000
规划目标：60,000 × 0.8 = 48,000
```

规划时计入完整摘要请求，包括 system、user 包装、previous_summary 和本块历史。
源码见 [预算函数](../../src/bot/compaction/service.py)及[默认配置](../../src/bot/config/models.py)。
主会话自动压缩触发目标默认是 96K，摘要正文目标通常为 3K、校验上限为 4K；这些也不是
摘要输入上限。`max_output_tokens=8192` 控制输出，不能用于说明模型只接受 8K 输入。

保留输出与协议空间，是为了使完整请求能放入模型窗口；额外余量用于应对计数误差。将单次
压缩限制在较小范围，还能控制调用时延、费用和失败后重做的范围。这些是工程动机；该配置
来自 `f78d911`（`fix(context): bound and recover compaction backlog`），现有证据没有证明
60K 是最优值，更不能据此断言超过 60K 的摘要质量必然下降。

估算也不是硬保证：已有 SymPy 摘要请求估算 36,722，API 实际为 47,876。20% 的规划余量
并不能覆盖所有输入分布的误差，仍需 Provider 计数或实测校准。另一个当前限制是预算读取
`model.context_window_tokens`：即使指定独立 `compaction_model`，这里也没有单独解析摘要
模型的实际窗口。调整上限时必须核对摘要目标模型，不能只看主模型容量。

## 2. 本项目超限时究竟做什么

| 情况 | 当前动作 | 是否推进摘要覆盖范围 |
|---|---|---|
| 待压缩历史整体超过规划目标 | 按工具调用/结果原子组划分，二分选择能放下的最长、最老连续前缀 | 成功后只推进到该前缀末尾 |
| 最早一个完整原子组也过大 | 普通正文先限 12,000 字符，再尝试每条 2,000、512 字符的降级视图；降级工具参数也有界 | 生成有效摘要后才推进 |
| 单个组降级后仍放不下 | 返回 `source_group_exceeds_budget`，不发摘要请求 | 不推进 |
| 估算能放下，但摘要 API 报 context overflow | 原子组数量减半后重试；默认总共两次范围尝试，单组无法再减半 | 只有成功的较小范围推进 |
| 摘要格式不合格 | 对候选摘要做有限格式修复，不缩短历史范围 | 校验通过后推进 |
| 摘要输出截断/过长 | 对已有候选做有限凝练；这是输出恢复，不是输入分块 | 校验通过后推进 |
| 摘要失败 | 保留原始消息和旧有效摘要；普通自动重试有退避 | 不推进 |

对应 `_plan_chunk()`、`_shrink_plan()`、`compact()` 和 `_compact_plan()`，见
[压缩器源码](../../src/bot/compaction/service.py)。生成失败与输出截断的质量边界另见
[可恢复压缩设计](../architecture/recoverable-context-compaction.md)：候选凝练不会让模型读到未输入的历史。

超过 60K 因此**不等于这批历史无法压缩**，而是不能保持相同覆盖范围直接用一次当前预算
完成摘要。上轮“全量 reasoning 输入约 76K”应按这个含义理解；若新增字段参与规划，压缩器
应选更小的块。reasoning 自身的降级规则也要另行实现，现有正文截断不会自动约束新字段。

还必须区分调用入口：

- 自动压力压缩：一次 `_consolidate_conversation()` 调用取得一个成功块，随后重新组装主请求；
  没有在此处循环清完全部积压。如果 planner 仍无法装下，可能以 `context_limit` 停止。
- 显式 `/compact`：循环调用压缩器，持续推进 cursor，直到目标范围完成或预算耗尽。默认共享
  最多 8 次摘要相关请求、600 秒总墙钟，以及配置单价后计算的费用阈值；修复和重试也占请求数。
- 主请求已经遭 Provider context overflow：另有一次强制压缩/外置后重试。这是主请求恢复，
  与摘要 API 自身的“范围减半重试”是两层机制。

[入口与主循环](../../src/bot/core/agent.py)。未覆盖的历史仍留在原始记录中；默认增量路径下一次
使用“当前摘要 + 下一块新历史”，不是把同一批超长原文反复全部发送。

## 3. 开源项目如何生成超大历史的摘要

以下比较固定提交中的指定路径，不声称覆盖插件扩展或最新主分支。它们普遍有上下文预算，
但不全有本项目这种独立的 60K 摘要输入参数，也不全实现任意长度的递归分块。

### Codex：接近整个活动窗口的原生压缩，没有独立的 60K 输入预算

补充核对本地 `openai/codex` 提交 `41ece455b7fa7166f4fc38522952afdaa2604e18`，以及当前
OpenAI 官方 Compaction 和配置说明。源码快照不能直接等同于用户当前桌面客户端或后端版本。

官方 standalone `/responses/compact` 接收完整活动窗口并返回替换窗口，要求输入仍在所选
模型上下文窗口以内；结果可包含保留消息，以及承载先前状态和 reasoning 的加密 compaction
item。它不是必须可读的 Markdown 摘要。后端内部如何生成该状态、是否内部拆分，不在公开
客户端代码中，不能凭 UI 的“272K → 几 K”推断服务端一定只进行了一次普通摘要生成。
[官方 Compaction 说明](https://developers.openai.com/api/docs/guides/compaction#standalone-compact-endpoint)。

客户端的几条路径必须分开：

| 路径 | 压缩输入 | 过大时的客户端处理 |
|---|---|---|
| remote V1 | 当前 `history.for_prompt()`、基础指令与 tools，调用专用 compact endpoint | 发送前按有效模型窗口估算；只尝试改写历史末尾连续、可改写的工具输出；不是按 60K 选旧块 |
| remote V2 | 同样取活动历史，追加 `CompactionTrigger`，经 Responses 流取得 compaction item | 共用 V1 的发送前工具输出改写；未见本项目式按小输入预算递归切块 |
| local fallback | 当前历史加摘要指令，使用普通模型流生成文本 | 实际 context overflow 后逐个移除最老 history item，再发请求 |

远端改写只在估算超出窗口时发生；从末尾向前扫描，遇到非支持类型就停止，不能保证修复所有
超限输入。常规工具输出在进入活动历史时也可能已被截断，因此“活动窗口”不能等同于完整
原始工具输出档案。local 路径被移出的旧 item 不会进入那次成功摘要请求。

这些行为对应 `codex-rs/core/src/compact_remote_request.rs`、
`compact_remote_v2_attempt.rs`、`compact_remote.rs::trim_function_call_history_to_fit_context_window`
和 `compact.rs`。路由在 `tasks/compact.rs` 按 Provider 能力和 feature 选择；另有
`TokenBudget` 特殊模式会直接开启新窗口、跳过模型/服务端摘要，不能把所有同名 UI 事件都
推断成完全相同的调用。

272K 也不是通用的压缩触发常数。这份本地模型目录的多个条目配置 `context_window=272000`；
在没有覆盖、采用默认总量计数与默认比例时：

| 数值 | 该快照示例 | 含义 |
|---|---:|---|
| 模型目录窗口 | 272,000 | 客户端模型元数据中的窗口值 |
| 有效窗口 | 258,400 | `context_window × 95%`，留出客户端余量 |
| 自动压缩阈值 | 244,800 | 默认 `context_window × 90%`，可被较低配置阈值覆盖 |

分别见 `models-manager/models.json`、`core/src/session/turn_context.rs`、
`protocol/src/openai_models.rs::auto_compact_token_limit()`。当前官方配置还支持
`model_context_window`、`model_auto_compact_token_limit` 和计数范围选择；服务器下发模型信息
及配置覆盖都可能改变实际值。未读取该用户具体压缩事件的请求 trace，不能确认其界面上的
272K 究竟是容量显示、触发前总量，还是实际压缩输入 usage。
[官方配置说明](https://learn.chatgpt.com/docs/config-file/config-reference#configtoml)。

“压到几 K”也不是客户端固定保证：local 压缩后保留的真实 user 文本预算为 20K，再追加摘要；
remote V2 的保留消息总预算为 64K，再追加 compaction item。这些是**压缩后的保留预算**，
不是压缩输入上限；V1 的返回窗口由 endpoint 提供，客户端还有类型过滤与上下文重建。
旧执行链被替换、保留原文较少时，活动占用可以显著下降，但不能由此证明全部细节都被保存。

对本项目的直接启示是：6 万不是摘要必须遵守的通用界限。若摘要目标模型容量和实测计数
允许，可以提高单次输入预算，用一次调用处理更大范围；需要独立比较调用成本、延迟及继续
任务质量。原生 compaction 与普通文本摘要不是相同接口，不能直接假定两者同等保真或压缩率。

### Hermes：批量路径裁剪输入；另有可选逐次合并

普通批量路径对消息正文和工具参数先做有界序列化，再把历史文本块限制为 **160,000 字符**。
超出后保留约 45% 开头、55% 尾部，中间替换为明确的省略标记。previous_summary 也分别经过
同样限制，再与新历史、模板等组成一次摘要请求，因此 160K 是单个材料块的字符上限，不能
说成整个 HTTP 请求的严格 token 上限；中文、代码等也不能统一按 4 字符/token 换算。
[输入裁剪](https://github.com/NousResearch/hermes-agent/blob/eac1e25127a75c61116a0bbb8ce7681a2f8b61b4/agent/context_compressor.py#L3823)、
[调用组装](https://github.com/NousResearch/hermes-agent/blob/eac1e25127a75c61116a0bbb8ce7681a2f8b61b4/agent/context_compressor.py#L3886)。

这条路径不会把省略的中间材料分块交给模型。辅助模型失败时可尝试回退主模型；后续根据错误
和配置，保留会话并中止压缩，或用确定性降级交接记录替换中间窗口。不能把后者统计为模型
完整读取历史后的摘要。其批量调用刻意不设置 `max_tokens`，但仍受 adapter/模型输出限制。

另有默认关闭的 micro-compaction 入口：每次把最老的一个 exchange 合并进 rolling summary；
summary 变大时单独重新凝练它，每轮一次辅助调用。这更接近持续、小步的增量摘要，不能把
它与批量路径的首尾裁剪混为一谈。
[逐次合并](https://github.com/NousResearch/hermes-agent/blob/eac1e25127a75c61116a0bbb8ce7681a2f8b61b4/agent/context_compressor.py#L5982)。

### Pi：提前触发、保留尾部；超长单轮分两份摘要

默认在上下文接近 `contextWindow - 16384` 时触发，近期原文预算为 20,000 tokens。
正常生成使用“旧摘要 + 待压缩历史”的专用 prompt，工具结果在序列化时限制为 2,000 字符。
若切点落在一个很长的用户轮次内部，则分别总结：①更早历史；②该轮被移出的前缀；最后将
两份摘要拼接，再接上保留的近期后缀。
[摘要生成与双摘要组合](https://github.com/badlogic/pi-mono/blob/a4453b79bb8d66b5f385b28ae0e33843c947504a/packages/coding-agent/src/core/compaction/compaction.ts#L623)。

这是“旧历史 / 当前轮前缀”的拆分，不是按摘要模型窗口递归切分所有输入。该路径没有
独立的摘要输入 token 上限规划；单次生成仍放不下时会报摘要错误，通用重试针对瞬时故障，
不会把确定的超限错误自动改成多层摘要。`0.8 × reserveTokens` 在这里约束的是摘要输出。

### OpenCode：清理旧工具输出，单次摘要仍超限则停止

`packages/opencode` 路径根据模型容量和输出/保留空间判断压力，可清理较老工具输出。
选近期尾部时默认预算为可用窗口的 25%，夹在 2K–15K tokens；过长单轮可以拆出一个后缀。
被压缩的头部连同 previousSummary 组成一次文本摘要，工具输出在摘要文本中最多 2,000 字符。
没有按摘要输入大小继续递归拆 head。

主请求发生 overflow 时，会尝试暂时移出最近用户轮次，先压缩之前的历史，成功后重放该轮。
若**摘要调用本身**仍返回需要压缩，代码设置 `ContextOverflowError` 并返回 `stop`。
因此不能假定“压缩器会再压缩自己，直到一定成功”。
[预算](https://github.com/anomalyco/opencode/blob/da4730e4a41dcbb2cb2d907dd2b06ac481b8f962/packages/opencode/src/session/overflow.ts#L10)、
[选择与超限停止](https://github.com/anomalyco/opencode/blob/da4730e4a41dcbb2cb2d907dd2b06ac481b8f962/packages/opencode/src/session/compaction.ts#L315)。

### DeepSeek Harness：按模型窗口提前压缩，重用请求前缀

普通压力默认阈值为路由模型窗口的 80%，保留尾部为 16%。可先做工具结果清理，再选择平衡
前缀，沿用原 system、tools、messages，追加压缩指令生成 checkpoint，意图复用缓存前缀。
生成后要求带包装的摘要确实比原范围小；若总压力仍高，默认允许再做一次压缩。
[策略与循环](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/compaction/compaction-basic/src/index.ts#L258)、
[请求前缀](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/compaction/compaction-basic/src/region.ts#L489)。

这里的再次压缩建立在上一份摘要已成功生成的基础上，不能解决首次摘要调用已经装不下的
所有情形。指定路径没有按独立摘要模型的输入窗口切块；摘要错误会向上传播。主请求 overflow
恢复又是另一层，默认一次，不能当作摘要请求的无限缩块重试。配置 `maxTokens=8192` 是输出
预算。[生成调用](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/compaction/compaction-basic/src/summarizer.ts#L121)。

**“成功但压力仍高”的精确定义：摘要输出限长与主请求总压力是两个条件。**
默认 `maxTokens=8192` 会传到 DeepSeek wire 的 `max_tokens`；输出因 token cap 结束时，
`finishError()` 返回 `MAX_TOKENS`，不会把截断候选当作成功摘要进入这个循环。
成功替换还要求带包装 checkpoint 的估算大小小于被替换范围，但这不等于整个主请求已低于
`contextWindow × thresholdRatio`。前者是局部缩小，后者检查摘要、保留历史、system 和
工具定义等组成的请求。`tokenMeter` 可以使用 Provider usage 锚点加历史变化量来估算，
并非只量摘要正文。
[截断判定](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/compaction/compaction-basic/src/summarizer.ts#L198)、
[总压力计量](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/llm/token-meter/src/index.ts#L116)。

16% 是选择尾部的保留目标，不是严格的最大值：代码从末尾累计到目标后，还要向前对齐工具
调用/结果边界；单条大消息或原子组可能使实际尾部远超目标。举一个假设规模：窗口 100K、
阈值 80K，第一次有效压缩把 20K 旧历史变成带包装的 5K 摘要，但仍有 66K 不可拆尾部和
10K system/tools，则总量仍是 81K。摘要符合输出预算，总压力检查仍不通过。这个算例不是
真实运行数据，也没有计量误差；实际判断以 meter 为准。

循环会在**已被 checkpoint 替换的当前历史**上重新选范围，而不是重新发送被移出的原始
历史。它可能只重新总结刚生成的 checkpoint；若下一次缩到 3K，上例总量可降至 79K。
代码不会自动把第二次的 `maxTokens` 调小，仍使用相同的摘要配置。默认
`compactionRetries=1` 表示最多两轮；若无法继续有效缩小或仍高于阈值，就失败。若压力主要
来自无法压缩的固定头部或保留原子组，重复摘要不保证解决问题。

因此这是局部压缩成功后的整体预算检查及有限恢复分支，不是“没有摘要长度限制”，也不是
“每次通常需要反复压缩”的实测结论。在摘要、正常尾部和固定头部合计已经低于阈值时，
第一次成功后就立即返回。

### Aider：有递归重压缩，但不保证每条历史都被模型读到

`ChatSummary` 把历史分为旧 head 和近期 tail，先总结 head；如果摘要加 tail 仍超过历史目标
预算，再递归总结组合结果。递归深度超过 3 或消息很少时转为整体摘要，并可尝试备用摘要模型。
选 head 时会根据模型 `max_input_tokens - 512` 限制送入量；容量未知时采用 4096 再减余量。

关键边界是：这段实现只把能放下的 head 前缀送去摘要，没有循环处理 head 中其余超限消息；
它们也不属于保留 tail。因此不能把这项“递归”描述成全历史分块覆盖，少量超大消息的整体
摘要分支也没有同样的输入裁剪保证。
[固定源码](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/history.py#L33)。

## 4. 历史真的大于模型窗口时，适合本项目的方案

应先区分：历史虽然很长但摘要模型一次能装下，可以做一次摘要；如果连摘要模型都装不下，
则需要显式分块、裁剪、检索选择或更大窗口。更换模型仍须核对实际输入格式和费用，不能把
模型名称当作容量保证。要覆盖全部历史，单纯删除中间材料不够。

本项目已有的连续分块和增量摘要适合作为默认基础：

```text
S1 = summarize(旧摘要 + 最老的一块历史)
S2 = summarize(S1 + 下一块历史)
S3 = summarize(S2 + 下一块历史)
主请求 = 最新有效摘要 + 尚未处理的旧历史 + 保留尾部 + 其他上下文
```

优先改进方向（本轮未实现）：

1. 根据摘要目标模型单独计算输入、输出与协议余量；完整请求计数，并用实际 usage 校准估算。
2. 自动压力入口也进行有限循环：每块成功后重算主请求，达到目标即停止；共享请求、费用和
   墙钟预算，防止无限循环。预算耗尽时明确报告实际覆盖范围。
3. 极大积压可另做分层摘要：完整原子块各自生成带来源的部分摘要，再合并；合并输入仍过大就
   再分组，直到得到最终摘要。并行有助于延迟，但事实依赖和更正关系需要合并阶段按时间处理。
4. 每个块记录覆盖范围与省略量，维护关键事实、待办和验证状态；最终校验所有应处理范围都已
   覆盖，不能将裁掉的消息声称为已总结。重复摘要仍会丢细节，保留原文查询与版本回退能力。

分层方案是建议，并非上述项目都已实现的共同能力。滚动合并和分层合并都要通过固定任务
验证事实保留、错误假设纠正、后续产物通过率，以及全部辅助调用的成本；不能只看摘要生成成功。

## 5. 本轮验证与证据边界

- 本项目 3 个既有单元测试通过：按输入预算选最老前缀、仅在 context overflow 后减半范围、
  格式失败不缩范围或重发原文。它们使用测试 Provider，不能代替超大真实任务质量实验。
- Hermes 纯函数合成探针：400,017 字符输入被限制为 160,000 字符，开头和尾部标记保留，
  中间独有标记消失。这是裁剪行为验证，不是模型遗忘率。
- Aider 用替代摘要函数记录实际输入：10 条合成消息中，0–3 被总结、8–9 原样保留，4–7
  既未送入摘要也未返回。模型输入容量设为 1600，历史目标为 100；0–7 每条计 250 tokens，
  8–9 每条计 10，替代摘要计 20。它验证上述超限 head 分支，不代表所有普通会话都存在同等丢失。
- Hermes、Pi、OpenCode、Harness 各一份本轮关键文件与固定 Git 提交及 GitHub raw 下载
  逐字节一致；Aider 从上述固定提交下载并核对函数。版本同[上轮调研](compaction-reasoning-replay.md)，
  Aider 为 `5dc9490bb35f9729ef2c95d00a19ccd30c26339c`。

没有新跑付费摘要模型或整题测试，未给出这些方案的通过率、成本增益或“最佳上限”结论。
