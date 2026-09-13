# 压缩输入、输出上限、原文保留与思考模式对照

核对日期：2026-09-13。对照 bot 当前工作树和此前调研的固定公开提交；没有修改运行时代码，也没有发起新的付费模型测试。已有 Flash 数据来自三轮 `path-tracing-reverse`。

## 先区分五个不同的预算

1. **摘要请求输入上限**：压缩提示、旧摘要、历史、可能附带的系统提示和工具定义之和，不等于被替换原文的长度。
2. **单次生成额度**：发送给模型适配层/API 的 `max_tokens` 或 `maxTokens`。启用思考时，部分 API 的额度由 reasoning 与正文共享；SDK 也可能调整额度。
3. **摘要正文发布上限**：模型已经返回完整正文，程序再检查长度，决定是否采用。bot 的 4,000 属于这一层。
4. **保留原文预算**：选择最近消息的目标，常使用估算，并服从用户轮次、工具调用完整性等边界，不一定是精确 token 硬切。
5. **恢复后完整请求预算**：固定提示、工具定义、摘要、原文、重新加载的文件/Skills 和运行提示合计。

本文精确数字保留源代码单位；K 为约数。**字符数、启发式 tokens、供应商实际 tokens 不能直接互换。** “没有额外固定上限”仍受模型窗口与服务端限制，不能理解为无限输入或无限输出。

## bot：四种实现的上限与失败处理

下表使用最近 Flash 测试配置：窗口 131,072，主输出 32,768，协议与安全预留各 2,048。摘要正文目标为 3,000，发布门限为 4,000 启发式 tokens。

| 方案 | 摘要输入限制 | 初次摘要生成额度 | 摘要过长/截断时怎样处理 |
|---|---|---|---|
| CURRENT | 独立请求预算 60,000；选材通常以 48,000 为目标 | 8,192 | 非空的超长候选可再 condense 一次；condense 额度为 `min(8192,4000)=4000`，结果重新验证；仍失败则不发布 |
| A | 完整待摘要请求按窗口预留后检查，本配置 118,784；不走 CURRENT 的 60K 分块 | 8,192 | 默认同一请求最多尝试两次；没有专用 condense；仍失败则保留旧根 |
| B | 叶子请求预算 24,000；合并请求预算 118,784；默认四路合并 | 每个叶子/合并请求 8,192 | 每个请求同样最多尝试两次；叶子或合并过长可导致本次根无法发布；不是把所有叶子直接恢复给主模型 |
| 双路径 `a_fallback` | 前缀路径本配置 94,208；独立兜底 118,784；各自做整份请求预检 | 前缀继承主请求 32,768；独立兜底 8,192 | 前缀一次失败后，用原始证据重新组装独立摘要请求一次；仍失败则保留旧根；当前没有在两条路径之间增加 condense |

这里的 118,784 是 `131072−8192−2048−2048`；94,208 是把输出预留改成 32,768 后的值。输入检查还使用现有计数/校准机制，不能把这些值当成供应商精确的原文长度。

四组共用的 `_validate_candidate()` 明确区分检查步骤，但把以下两种情况都归到 `OUTPUT_LENGTH`：

- `finish_reason=length/max_tokens`：生成本身未完整结束。
- `finish_reason=stop`，但摘要正文估算超过 4,000：生成完整，发布预算不合格。

**CURRENT 的 condense 只读候选摘要，不重新读原始历史。** 对完整但偏长的摘要，它能缩短正文；对已截断摘要，它无法恢复根本没有生成出来的尾部事实。因此不能把“condense 后通过格式/长度检查”等同于“截断造成的信息缺失已修复”。A/B 的相同请求重试也不等于定向缩短。

CURRENT 的输入超限处理比较具体：选择最老的连续原子消息组；摘要材料单条正文默认最多 12,000 字符，首组仍放不下时降到 2,000、512 字符，并在降级表示中限制参数。若实际 API 返回上下文溢出，会缩小覆盖区间再试；只替换真正成功覆盖的区间。自动压缩通常完成一个区间后继续任务，手动压缩可在总请求/时间预算内继续。A/双路径没有这套 60K 区间缩小循环；B 用叶子分块，无法容纳的单个原子组不能随意拆开。

代码依据：[配置](../src/bot/config/models.py)、[生成与验证](../src/bot/compaction/service.py)、[A/B/双路径](../src/bot/compaction/strategies.py)。

## 外部框架：输入上限与输出超限

Claude Code 核心不是开源实现。该行仅使用官方公开行为，未公开的内部数值不作推测。Codex 本地文本压缩与服务端原生压缩也不能合并为一种机制。

| 实现与固定版本 | 摘要输入有没有额外上限 | 输出限制及超限处理 |
|---|---|---|
| Codex `02a8f038b87a`：本地文本路径 | 没有 bot 式固定 60K 原文限额；受模型窗口限制。实际上下文溢出时，从最老的历史项开始删减重试 | 没有找到统一的 4K 摘要正文发布门限。必须收到完成事件；适配层将 `response.incomplete` 转成流错误，不能把部分输出当成功摘要。现有错误重试有界，不是正文超长后专用 condense |
| Codex：原生压缩路径 | 请求仍受模型窗口约束，客户端有溢出裁剪；独立 `/responses/compact` 也要求输入在窗口内 | 输出为 opaque/encrypted compaction item；客户端验证协议完成与 checkpoint 结构。内部摘要正文长度、服务端重试算法没有公开，不能套用文本摘要的“几 K”上限 |
| pi `71dca871bc80` | 没有独立固定 60K；保留模型窗口余量；序列化时每个工具结果只取前 2,000 字符并加截断标记 | 摘要层传 `min(floor(reserveTokens×0.8),model.maxTokens)`，默认 reserve=16,384，基础额度 13,107；最终 API 额度还受适配层思考预算和窗口钳制影响。`stopReason=length` 明确拒绝，不存 checkpoint；瞬态错误可重试，未见专用超长 condense。长轮次可能生成两份摘要再拼接，因此单次额度不等于最终组合摘要上限 |
| OpenCode core `95daf90670b7` | 对序列化后的完整摘要 prompt 预检：估算超过 `context−summaryOutput` 就返回失败；不是固定 60K | `summaryOutput=min(主请求输出额度或默认值,4096)`，默认 4,096。没有额外正文长度复验。压缩调用层只检查流异常、provider error、空正文，未检查 `length`；OpenAI Chat 适配路径把 `length` 正常转成 finish 事件，因此静态链路显示非空截断文本可能被采用，未做实测 |
| OpenCode 在线 V2 文档 | 描述复用正常系统提示、工具及历史加后缀；按模型输入/输出预留和 buffer 触发 | 描述至少命中一个预期标题；不合格再纠正一次。未公开统一正文硬上限或 `length` 完整处理链。不能把固定提交的 4,096 和文档描述的另一条摘要路径拼成一个已验证实现 |
| DeepSeek Harness `c291e7961a51` | 无额外固定 60K；按模型压力与完整消息组选择区间。正常压缩保留尾部；主请求实际溢出时走特殊恢复，保留目标降到 0，尝试压缩全部可压区间 | 默认 `maxTokens=8192`；`max-tokens` 明确抛错，拒绝残缺 checkpoint。没有额外 4K 正文门限，但带封装摘要必须比被替换区间更小。正常压力路径成功后仍超阈值，可以再压一次，默认总共最多两次；这是成功后的继续压缩，不是截断摘要的补救 |
| Hermes `45a6101f3657` | 有 160,000 字符的摘要材料截取上限；历史块与旧摘要分别处理，不是整份 API 请求统一 160K，更不是 160K tokens。lean 模式分八段均匀采样，legacy 保留头 45%/尾 55%，均标记省略 | 压缩调用刻意不传 `max_tokens`，避免思考挤占一个过小的额外输出限额；下层/服务端仍有限制。约 2K–10K 的摘要预算写在 prompt 中，不是统一正文硬门限。`finish_reason=length` 拒绝；独立摘要模型失败可回退主模型，最终截断失败保留原会话并进入冷却 |

OpenCode 的版本差异是本次明确发现的限制：2026-09-13 查询公开 `dev` 得到 `95daf90670b7`，对应 core 文件的 SHA-256 为 `35bc2da1578b6bb80a3c39c1d6c51f234f34ce6586dd4f9d230e2a31b257138d`。这份源码默认 keep=8,000、独立无工具摘要请求；在线 V2 文档写 keep=15,000、复用正常请求加后缀。未找到将两者对齐到同一发行版本的证据，故并列说明，不宣称全部安装版本采用其中一种。

Hermes 另提供 `salvage_grown_transcript()` 防止候选越压越大：削减旧 reasoning、旧工具结果，在该救援函数中可把摘要正文截到前 8,000 字符再加省略标记。这是条件性、有损的候选瘦身机制，不是每份摘要都必须小于 8K 字符的发布规则，也不代表被 API 截断的摘要会通过其 `length` 检查。

主要源码与官方依据：[Codex 文本压缩](https://github.com/openai/codex/blob/02a8f038b87ad34d4a1dc5058eda26972ed7aa6c/codex-rs/core/src/compact.rs)、[Codex 原生路径](https://github.com/openai/codex/blob/02a8f038b87ad34d4a1dc5058eda26972ed7aa6c/codex-rs/core/src/compact_remote_v2.rs)、[OpenAI compaction API](https://developers.openai.com/api/docs/guides/compaction)、[pi](https://github.com/earendil-works/pi/blob/71dca871bc80b6bc97be37f0ca3189399d651fff/packages/coding-agent/src/core/compaction/compaction.ts)、[OpenCode core](https://github.com/anomalyco/opencode/blob/95daf90670b7c039c436c85537da5fbfe2205b41/packages/core/src/session/compaction.ts)、[OpenCode V2 文档](https://opencode.ai/v2/docs/compaction)、[DeepSeek 摘要器](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/compaction/compaction-basic/src/summarizer.ts)、[Hermes](https://github.com/NousResearch/hermes-agent/blob/45a6101f36576367359c171cd5820ee76a3d047b/agent/context_compressor.py)。

Claude Code 官方没有给出足以验证的固定“摘要原文 60K”或“摘要正文 4K”规则。官方错误文档说明，摘要本身也可能因窗口没有剩余空间而失败；建议回退若干消息再 `/compact`，必要时 `/clear`。这不足以证明内部一定会分块、自动缩短摘要或无限重试。[官方错误说明](https://code.claude.com/docs/en/errors#error-during-compaction-conversation-too-long)

## 到底保留哪些原文，还会怎样处理

“保留原文”至少有三种不同含义：继续放入下一次模型请求；保留在会话数据库/日志里供读取；从文件系统重新读入。以下主要比较第一种，另行注明后两种。

| 实现 | 压缩后继续进入请求的内容 | 对这些内容的额外处理 |
|---|---|---|
| bot 四组 | 固定上下文、用户原文锚点、一份活动摘要、尚未覆盖的近期消息、必要运行状态；近期历史目标 20,000 启发式 tokens | 按原子工具组封闭边界，可能超过目标；A/B/双路径还检查完整恢复请求不超过 40,000 预算。长内容可外置 blob，正文留头尾和引用；读取引用的正文只临时投递，持久历史留短回执；未消费正文在压缩时受保护。Skills 可由独立恢复层补回，本轮测试关闭 Skills |
| Codex 本地文本 | 新摘要 + 预算内的用户原文；重新建立初始上下文 | 用户原文总预算约 20,000，优先保留最新，边界处旧用户内容可截短；不保留整段最近 Assistant/工具原文链。历史日志仍可保存完整记录，不能等同于模型还能直接看到 |
| Codex 原生 V2 客户端 | checkpoint + 特定用户/Hook 消息、符合条件的 Agent 消息；配置允许时保留客户端 developer 消息 | 所检查源码的 retained-message 总预算 64,000，单个 Agent 消息筛选阈值 10,000；过滤特定子 Agent 进度/完成通知；截短旧文本并按图像预算处理图像。64K 是保留消息预算，不是摘要输入或 encrypted checkpoint 长度上限 |
| pi | 旧历史摘要 + 最近约 20,000 的原始消息后缀；长用户轮次切开时额外加入该轮前半段摘要 | 避免从孤立 tool result 开始续跑；切分受完整消息边界约束。**摘要输入的工具结果 2,000 字符截取，不会自动套用到保留的原始 tail**。另外程序提取读/改文件列表并附到摘要；列表来自工具参数，不是修改成功的证明 |
| OpenCode core 固定提交 | 一份摘要 + 预算内的 recent 文本；默认 keep=8,000 | recent 已被序列化，不是完整原生 tool-call/tool-result 链。工具结果及 shell output 最多前 2,000 字符加标记，附件变成描述；助手正文、可见 reasoning、调用参数也参与序列化。最新整条超过预算时可能一条都留不下 |
| OpenCode 在线 V2 文档 | 摘要 + 最近序列化文本；文档默认 keep=15,000 | 工具结果 2,000 字符截取，附件用描述表示。更老的 V1 `193de13a88d6` 则按真实消息/轮次保留约 2K–15K，并有独立旧工具清理路径；不能把 V2 序列化 tail 的行为反推到 V1 |
| DeepSeek Harness | 原系统提示 + checkpoint + 最近完整消息组；正常尾部默认窗口的 16% | 保持工具调用配对；可选独立 tool-result-pruner 对超过 8,192 字符的工具结果保留前 4,096/后 1,024 字符及省略标记，富内容块保持顺序；先清理、重新计量，足够时不用摘要。实际 overflow 恢复不坚持保留 16% |
| Hermes lean 默认 | 固定 head + 摘要 + 小段近期原文 + 程序生成的索引/用户锚点/恢复引用 | tail 目标 `clamp(window×2.5%,10000,25000)`；保护最近六轮工具结果，更旧且至少 1,500 字符的 tail 工具正文降为恢复短桩；至少保留最近用户消息的要求仍可能突破目标。附加用户原话也有单条/总字符预算，不能称为全部逐字保留 |
| Claude Code | 会话摘要 + 恢复的启动规则/记忆/计划 + 有界文件和 Skill 内容；普通历史 tail 的统一硬预算未公开 | 最多重新读取五个最近相关文件，优先最近修改；超过 5,000 tokens 的文件只保留路径。Skill 正文每份 5,000、总计 25,000，保留开头、超总额先丢最旧。路径规则随相关文件读取恢复；`SessionStart(compact)` hook 输出重新加入 |

bot 的内容外置也不是无条件把每个 tool response 卡到 4K：当前外置触发取工具 inline 预算和 recent/4 的较大值，普通正文与调用参数走的处理并不完全相同；完整工具参数和一次性引用投递都可能贡献大块输入。不能把“工具输出已外置”解释成尾部一定很小。

Claude Code 的恢复规则依据[官方上下文文档](https://code.claude.com/docs/en/context-window#what-survives-compaction)。恢复文件是重新读取当前文件内容，和保留上次读文件的旧 tool result 是两种语义。

## 摘要模型是否开启 thinking/reasoning

| 实现 | 摘要请求的思考设置 | 对判断输出长度的影响 |
|---|---|---|
| bot CURRENT / B | 专用 `compaction_thinking`；默认 auto 在官方 DeepSeek 域名上显式 disabled，其他服务商保留默认；也可显式 enabled/disabled/provider_default | 不必与主任务相同。adapter 最终 payload 才是实际请求开关；没有开关字段不等于关闭 |
| bot A | 从主请求复制，继承主请求 thinking；只调整摘要输出额度等字段 | 主任务开思考时 A 也可能开思考；不能用 CURRENT 的 auto 规则推断 A |
| bot 双路径 | 前缀路径继承主请求 thinking；独立兜底使用专用 compactor 的 thinking 设置 | 一次压缩的两条路径可能不同；最近三轮 Flash 实验四组均为 disabled |
| Codex | 本地文本与检查到的原生 V2 请求都调用 `reasoning_effort_for_request(...Compaction)`；通常跟随会话请求 effort，启用 effort pin 功能时复用当前窗口固定的 baseline | 没有“摘要一律关闭思考”规则；原生 checkpoint 不公开内部思考正文，不能据此判定没有思考 |
| Claude Code | 官方明确：自 v2.1.198 起，摘要请求继承会话 extended thinking，开则开、关则关 | thinking 影响摘要生成；生成结束后会话设置不因压缩而改变 |
| pi | `AgentSession` 传 `this.thinkingLevel`；模型支持 reasoning 且级别不为 off 时传入摘要调用 | 不统一关闭。传统 Anthropic budget 模式可追加 thinking 预算；adaptive 路径处理不同，不能把所有模型都按同一加法计算 |
| OpenCode core 固定提交 | 新摘要请求只显式设置 maxTokens，没有复制主请求全部 providerOptions；仍合并同一 model/route 的默认选项 | 是否思考取决于模型/路由默认值；不能宣称一律关闭，也不能宣称严格继承当前请求临时 effort。在线 V2 文档没有给出足够字段细节去覆盖这一源码结论 |
| DeepSeek Harness | 摘要调用构造 GenerateOptions 时没有显式 thinking/reasoning 开关；可选摘要 provider/model | 需要继续看实际适配器与路由配置才能确定；“未传 thinking”不等于“已关闭”。本表不把未核验的最终 wire payload 写成确定开关 |
| Hermes | 独立 auxiliary compression 路由，可配置 `reasoning_effort`/`extra_body.reasoning`；没有在压缩器中一律关闭 | 有明确关闭 reasoning 的专用 fast-lane 配置，并限制其只作用于匹配路由，避免回退模型误用。默认行为仍取决于所选模型/路由；它不设置压缩专用 max_tokens 的理由之一就是防止思考挤占正文空间 |

pi 的具体例子：摘要层基础 maxTokens=13,107，传统 Anthropic thinking=medium 的默认预算为 8,192，适配层可把总额度调成 21,299，然后再受模型输出上限和剩余窗口钳制。high 对应 16,384 的思考预算。适配层还尽量保留至少 1,024 的回答空间。这说明比较应该记录最终 wire 的 max_tokens，而不是只抄压缩函数的参数。

**“把历史 reasoning 喂给摘要器”和“摘要器本轮重新开启 thinking”是两件事。** 例如 pi/OpenCode 的历史序列化可包含可见的旧 thinking/reasoning，但本轮是否思考仍由新的请求设置决定；仅看到输入中存在 reasoning 字样，不能证明本轮 thinking 已开启。

依据：[Codex effort 选择](https://github.com/openai/codex/blob/02a8f038b87ad34d4a1dc5058eda26972ed7aa6c/codex-rs/core/src/session/reasoning_effort.rs)、[pi 摘要适配预算](https://github.com/earendil-works/pi/blob/71dca871bc80b6bc97be37f0ca3189399d651fff/packages/ai/src/api/simple-options.ts)、[pi Anthropic 分支](https://github.com/earendil-works/pi/blob/71dca871bc80b6bc97be37f0ca3189399d651fff/packages/ai/src/api/anthropic-messages.ts)、[Hermes auxiliary 路由](https://github.com/NousResearch/hermes-agent/blob/45a6101f36576367359c171cd5820ee76a3d047b/agent/auxiliary_client.py)、[Claude Code 官方行为](https://code.claude.com/docs/en/context-window#what-survives-compaction)。Claude API 的 thinking tokens 与正文共享 max_tokens，见[官方 thinking 说明](https://platform.claude.com/docs/en/about-claude/models/extended-thinking-models)。

## 对 bot 当前结果的解释与可借鉴项

- **最近九次双路径摘要失败不是思考耗尽额度。** 三轮总计 22 次摘要模型调用，采用 13 份，拒绝 9 份；九份均正常 `stop` 后被 4,000 启发式正文门限拒绝。前缀 16 次中成功 10 次，六次进入兜底、挽救三次，另三次两路均失败。四组当时都关闭 thinking。
- **4K 正文门限是 bot 的额外设计，不是普遍协议要求。** 它控制恢复体积，但当前把完整长摘要与生成截断都归到同一错误类。后续可拆成不同事件和恢复政策；本次仅提出比较结论，没有实现修改。
- **更值得优化的是尾部原文。** 四组首次压缩现场，近期 Assistant/工具内容仍占恢复请求约 76%–87%，摘要及封装只占约 4%–10%。CURRENT 的 recent 启发式计数 19,119，对应 tokenizer 36,656；20K 目标不是精确 20K 上限。
- **可借鉴 pi/Hermes 的两类做法，但不要混为一谈。** 生成截断应拒绝发布，保留可恢复状态；完整但略长的摘要可在总恢复预算允许时处理，或进行一次有界缩短。尾部可采用短桩/引用和对最新轮次的保护，减少重复大型工具材料；这会改变恢复成本与信息可见性，需要单独验证。
- **思考开关应成为测试变量，而不是直接归因。** 开启 thinking 可能改善整理和冲突识别，也增加输出预算及耗时；目前没有对照数据证明其能提高本任务摘要事实准确率。若以后测，至少同时记录请求思考设置、最终输出额度、reasoning 用量、正文长度、finish reason、发布结果与恢复后的输入；不把普通 Assistant 长正文误计为 reasoning。

量化依据：[三轮报告](context-strategy-flash-repeats.md)、[上下文组成](context-strategy-flash-composition.md)。它们是 bot 实测；外部项目数值是实现/配置对照，未在同一任务上跑出外部框架的压缩率，不能据此宣称外部框架更准确或更省钱。

来源提交、文件哈希与本次核验范围归档于 [source manifest](data/compaction-limits-retention-thinking-sources.json)。
