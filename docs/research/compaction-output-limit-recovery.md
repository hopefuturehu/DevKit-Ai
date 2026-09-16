# Coding agent 摘要输出超限与恢复机制

核对日期：2026-09-16。检查公开仓库的固定提交及官方文档，重点是摘要生成额度、正文过长、截断失败与后续恢复。本文是静态源码调研，没有实测外部产品成功率，没有修改 bot 运行时或发起付费摘要请求。

结论：**所检查实现没有一套统一的“摘要超长就自动反复缩写”流程。较明确的共同做法是给生成预留空间、拒绝不完整摘要、成功后再替换历史；但不同项目甚至同一仓库的不同路径仍存在检查差异。** 完整摘要超过软目标，与服务端截断，是不同问题。

## 先分清四种超限

| 情况 | 如何识别 | 本次看到的处理方式 |
|---|---|---|
| 正文完整，但超过提示词里的目标 | 正常结束，正文偏长 | 多数被检查路径没有额外固定正文门限；仍可能采用。是否有实际压缩收益另行检查 |
| 生成额度耗尽，摘要尚未完成 | `length`、`max-tokens`、`response.incomplete` | 拒绝候选、报错或有限重试；部分实现可换模型。不能仅凭非空正文判断成功 |
| 摘要请求输入已放不下 | provider context overflow / 输入预检失败 | 缩减输入、清理工具结果、减少保留内容或回退消息；不等于修复输出截断 |
| 摘要完整，但替换后会话仍太大 | 对完整恢复请求重新计量 | 继续压缩、要求摘要比原区间更小，或对候选作有损瘦身；不同实现取舍明显不同 |

服务端共享生成额度的模型仍然会把 thinking 和正文一起计入额度。应用可以只按正文计算摘要长度，但这不会改变服务端何时停止生成。

## 固定版本与主要发现

源码提交及文件哈希见[来源清单](../data/compaction-output-limit-recovery-sources.json)。分支名仅说明取样位置，下面的结论绑定提交，不代表全部发行版本。

| 项目 | 固定提交 | 输出额度与恢复 |
|---|---|---|
| Codex 本地文本压缩 | `4701aa4b4239` | 未见统一 4K 正文发布门限；`response.incomplete` 在 SSE 层变成错误，压缩循环有限重试，成功后才替换历史。不是专用的超长改写流程 |
| pi coding-agent 路径 | `9e05370b298d` | 基础生成额度为 `min(floor(reserveTokens × 0.8), model.maxTokens)`，默认 reserve=16,384，即基础 13,107。明确拒绝 `length`；瞬态错误重试不负责缩写或扩大额度 |
| DeepSeek Harness basic | `0d1f50007f9b` | 默认生成额度 8,192；`max-tokens` 抛 `MAX_TOKENS`，不生成成功 checkpoint。完整且带封装的摘要还必须比替换区间更小 |
| Hermes 常规压缩路径 | `03b0c7947262` | 刻意不传压缩专用 `max_tokens`；正文目标约 2K–10K 写在提示词中。拒绝 `length`；独立摘要模型失败可回退主模型一次，最终截断失败保留原会话并冷却 |
| OpenCode core 路径 | `350c726aa8b6` | `summaryOutput=min(主输出额度或默认值,4096)`。此路径检查错误与空正文，没有检查结束原因；OpenAI Chat 适配器将 `length` 当普通 finish，静态链路存在采用非空残缺摘要的风险 |
| Claude Code | 官方在线文档 | 核心压缩算法不公开，无法验证“输出过长自动缩写”的内部算法。官方对压缩窗口不足建议回退若干消息后重试，仍不行则开启新会话 |

### Codex：完成事件是发布前提，原生压缩另算

本地文本路径收到流错误后按重试额度退避，耗尽后返回错误；没有观察到按已生成字数自动调整摘要目标或拼接续写。输入窗口错误则移除最老历史项后重试，这是输入恢复，不能算输出截断恢复。来源：[压缩循环与替换](https://github.com/openai/codex/blob/4701aa4b4239c70063ab6f2fcb835324f9c109f4/codex-rs/core/src/compact.rs#L255-L395)、[incomplete 转错误](https://github.com/openai/codex/blob/4701aa4b4239c70063ab6f2fcb835324f9c109f4/codex-rs/codex-api/src/sse/responses.rs#L472-L485)。

OpenAI 原生 `/responses/compact` 返回包含加密 checkpoint 的新窗口。官方要求原样传递返回窗口，不自行裁剪；输入仍必须能放入模型窗口。内部摘要长度和服务端恢复算法没有公开，不能套上文本摘要的 4K/8K 数字。[官方 compaction 文档](https://developers.openai.com/api/docs/guides/compaction#standalone-compact-endpoint)

### pi：正文额度与 thinking 预算可以分开规划

`coding-agent` 的 `getSummarizationFailure()` 明确把 `length` 当失败，理由是部分文本不能成为 checkpoint。重试封装只重试符合条件的 `error`；`length` 会先原样返回，再被摘要校验拒绝。因此它没有“截断后自动加额度”或专用 condense。来源：[摘要额度、失败检查](https://github.com/earendil-works/pi/blob/9e05370b298d0a6b8d9bc2c02e4bfae189ef1616/packages/coding-agent/src/core/compaction/compaction.ts#L556-L740)、[重试条件](https://github.com/earendil-works/pi/blob/9e05370b298d0a6b8d9bc2c02e4bfae189ef1616/packages/ai/src/utils/retry.ts#L174-L238)。

传统 Anthropic budget-based thinking 路径会把基础输出额度加上 thinking 预算，再受模型最大输出与剩余上下文限制，并尽量给正文保留至少 1,024 tokens。例如基础 13,107 加 medium thinking 8,192，可形成 21,299 的总生成额度，前提是模型和窗口都允许。**这是给 thinking 另留空间，不是让服务端免计 thinking。** adaptive thinking 和其他 provider 不能直接套用该公式。来源：[预算计算](https://github.com/earendil-works/pi/blob/9e05370b298d0a6b8d9bc2c02e4bfae189ef1616/packages/ai/src/api/simple-options.ts#L54-L95)、[Anthropic 实际调用](https://github.com/earendil-works/pi/blob/9e05370b298d0a6b8d9bc2c02e4bfae189ef1616/packages/ai/src/api/anthropic-messages.ts#L876-L902)。

版本内也有差异：同一提交的 `packages/agent/src/harness/compaction/compaction.ts` 在生成结果检查处只拒绝 `aborted/error`，未见同样的 `length` 检查。因此本文的明确拒绝结论只针对上面的 `coding-agent` 路径，不能推广为 pi 所有入口。来源：[另一条 harness 路径](https://github.com/earendil-works/pi/blob/9e05370b298d0a6b8d9bc2c02e4bfae189ef1616/packages/agent/src/harness/compaction/compaction.ts#L586-L610)。

### DeepSeek Harness：要求实际变小，继续压缩只发生在成功之后

摘要请求保留 system、工具定义和历史前缀，在末尾追加压缩指令；最终只提取 text 作为摘要。输出命中上限时抛错，不能把 thinking 或部分正文当成完整摘要。[摘要器](https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/compaction/compaction-basic/src/summarizer.ts#L164-L219)

完整摘要必须比原区间小。压力路径默认最多做两轮压缩，但下一轮的前提是上一轮已经成功、整体仍高于阈值；摘要抛错会跳出，不会把残缺摘要交给下一轮。`compaction/summary-error` 有扩展恢复钩子，默认返回 false，不能宣称插件组合永远没有额外恢复。来源：[默认配置](https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/compaction/compaction-basic/src/config.ts#L82-L98)、[成功后重测](https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/compaction/compaction-basic/src/index.ts#L303-L329)、[收益门限](https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/compaction/compaction-basic/src/region.ts#L397-L427)。

自动压缩失败时可以继续主任务；这说明“对话继续”本身并不是压缩成功证据。[失败后继续](https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/compaction/compaction-basic/src/index.ts#L145-L163)

### Hermes：不给过小的额外额度，换模型一次，再保留现场

摘要器明确不传 `max_tokens`，注释说明小额度会被 reasoning 消耗。但不传参数仍受适配层和服务端默认值约束，不能理解为无限输出，更不能直接照搬到默认输出只有 8K 的服务。

正文为空或 `finish_reason=length` 都拒绝。若使用了不同的摘要模型，可立即回退主模型一次；最终截断失败设置失败标记和约 30 秒冷却，压缩调用保留原消息。不是每种失败统一等待 600 秒。来源：[生成与校验](https://github.com/NousResearch/hermes-agent/blob/03b0c7947262b220f5148b75a30bb7a3faddcbb2/agent/context_compressor.py#L3220-L3294)、[模型回退和冷却](https://github.com/NousResearch/hermes-agent/blob/03b0c7947262b220f5148b75a30bb7a3faddcbb2/agent/context_compressor.py#L3485-L3554)、[失败保留消息](https://github.com/NousResearch/hermes-agent/blob/03b0c7947262b220f5148b75a30bb7a3faddcbb2/agent/context_compressor.py#L4430-L4460)。

同一生成函数还有正文为空时从 reasoning 字段提取文本的兼容兜底，最多 8,000 字符。因此也不能把“所有 agent 都只保留正文、不取 reasoning”当作事实；本项目不必照搬这个兼容行为。

它确实还有一个接近“摘要太长怎么办”的具体机制：`salvage_grown_transcript()` 对越压越大的候选作机械瘦身，清理旧 reasoning、旧工具结果，并可把摘要截到前 8,000 **字符**，加截断标记，仍不变小则拒绝。它有明确的信息损失代价，不是语义重写，也不代表服务端 `length` 摘要可通过正常校验。[候选瘦身](https://github.com/NousResearch/hermes-agent/blob/03b0c7947262b220f5148b75a30bb7a3faddcbb2/agent/context_compressor.py#L415-L454)

### OpenCode：固定源码与 V2 文档必须分开

所取 `core` 路径上限 4,096，摘要流只收正文，并检查异常、provider error 和空文本。适配器保留 `length` 结束原因，但该摘要调用没有消费它；因此非空截断文本可能被接受。这是静态链路发现，未做运行实测，也不能推广到所有 OpenCode 路径。来源：[core 压缩](https://github.com/anomalyco/opencode/blob/350c726aa8b6b11eb9242040bc5eb7ae837fbf8a/packages/core/src/session/compaction.ts#L176-L231)、[Chat finish 映射](https://github.com/anomalyco/opencode/blob/350c726aa8b6b11eb9242040bc5eb7ae837fbf8a/packages/llm/src/protocols/openai-chat.ts#L378-L468)。

在线 V2 文档描述的是保留正常指令、工具及前缀的摘要请求；缺少预期标题时纠正一次，再失败则拒绝。**格式纠正一次不等于正文超长重写一次**，该说明也未给出 `length` 的完整恢复链。文档所述路径与上面的 core 快照不能合并成一个实现。[OpenCode V2 文档](https://opencode.ai/v2/docs/compaction#checkpoints)

### Claude Code：只能确认官方公开行为

官方错误文档说明 `/compact` 也可能因窗口没有剩余空间容纳摘要而失败，建议回退若干消息再试，必要时 `/clear`；原会话可用 `/resume` 找回。这个说明不能证明内部是否对输出截断自动续写、分块或二次缩写。[官方错误说明](https://code.claude.com/docs/en/errors#error-during-compaction-conversation-too-long)

## 对 bot 的建议

以下是结合本项目故障提出的设计建议，不是声称其他项目已经整体实现了这套流程。

1. **保留三套独立预算。** 正文软目标、服务端总生成额度、替换后的完整请求预算分别计算。thinking 不进入摘要正文，不意味着它不占生成额度，也不应把全部生成额度算成未来摘要的占位。
2. **完整但偏长：先检查可用性和收益。** 已取消的 4K 正文硬门限不应恢复。如果全文正常结束、关键结构齐全、替换后请求能放下且实际变小，可以采用；若放不下，可用完整候选作一次定向缩写，再检查事实与完整请求预算，不直接截掉尾部。
3. **服务端截断：从原始证据恢复。** 明确归类 `OUTPUT_TRUNCATED`。前缀失败后，独立路径应在窗口允许范围内调总额度或使用合适的摘要模式/模型，不能只拿残缺候选缩写后宣称覆盖原区间。固定重试次数和总成本，避免同样的 8K 请求反复碰顶。
4. **缓存路径保持完整请求语义。** 前缀阶段保留工具定义及实际生效的 thinking 设置；是否能调整输出额度要按 provider 缓存规则验证。独立兜底可独立优化，不把 pi 的 Anthropic 预算公式直接当成 DeepSeek 的参数能力。
5. **失败不要推进覆盖范围。** 保留旧摘要和未覆盖历史，跨 Run 保存冷却；只要完整请求仍在硬上限内，避免失败后仅因软目标而静默丢掉未摘要原文。
6. **发布检查包含正文之外的内容。** 统计 system、tools、用户原文锚点、摘要和近期消息之和；同时记录 finish reason、thinking tokens、正文 tokens、缓存命中和恢复结果，才能区分“压缩失败但仍能回复”和“安全完成压缩”。

今天 bot 的失败请求都属于生成被截断，不是完整正文超过旧 4K 门限；本地已经只用实际正文构造摘要。最直接可借鉴的是 pi 的预算分离、Hermes 的有界回退与冷却、DeepSeek 的收益检查和成功后替换，而不是采用残缺摘要来提高表面成功率。[本地故障证据与预算核算](../evaluations/compaction-incident-20260916.md)

## 核验边界

本次核对了固定源码的默认值、provider 适配、结束原因检查、重试和发布路径，并保存每个下载文件的 SHA-256；官方在线文档以访问日期标记。没有通过产品实测比较成功率、延迟或摘要事实保真度。源码中存在机制，不代表它在所有 provider、插件、客户端发行版上都生效。
