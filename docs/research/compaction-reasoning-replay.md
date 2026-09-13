# 压缩 reasoning 的保存与回放：开源实现对照

核对日期：2026-09-09。本文检查当前 `bot`、四个开源项目的固定源码，以及 DeepSeek 官方协议。
本轮没有修改运行器或新增模型调用；保存、组装和回传能力的结论来自源码，真实 API 接受性仅引用
既有 Flash 对照。前文“派生摘要采用空 reasoning”的建议应理解为一个兼容候选，不能推广成
“压缩 reasoning 不能回传”或“任何模型都必须使用空字符串”。

## 1. 三种信息需要分开

历史任务 reasoning 的输入过滤、正文内 think 块例外、两次真实区间的输入增量及质量验证方法，
另见[历史 reasoning 是否进入压缩输入](compaction-reasoning-input.md)。

| 信息 | 含义 | 与本问题的关系 |
|---|---|---|
| 历史任务 reasoning | 主 Agent 在解决任务时产生的推理 | 是否作为摘要输入、是否在保留尾部回放，是两项独立选择 |
| 压缩调用 reasoning | 摘要模型在筛选、组织历史时产生的推理 | 用户问的“把压缩过程的 reasoning 返回”指这一项 |
| 派生 checkpoint 的协议字段 | 运行器把摘要加工成新消息后，为目标 Provider 构造的字段 | 可以携带兼容的原始输出，也可以是有来源的占位；不能冒充遗失的历史推理 |

DeepSeek 当前文档要求：带 `tools` 的 thinking 请求应回传所有历史助手轮次的 reasoning，
包括没有工具调用的轮次；不带 tools 时，回传的 reasoning 会被忽略。
文档没有规定“摘要模型产生的 reasoning 一律不得回传”，也没有为运行器合成 checkpoint
规定通用的空值表示。是否把辅助调用迁移成主会话中的助手消息，仍需验证实际组装与端点兼容。
[官方协议](https://api-docs.deepseek.com/guides/thinking_mode/#tool-calls)。

## 2. 开源框架的实际区别

| 实现与路径 | 压缩请求怎样组织 | 压缩调用产生的 reasoning | 主会话中的摘要表示 |
|---|---|---|---|
| 当前 bot | 专用 system + user JSON；旧摘要与选中历史；历史 reasoning 不进入 JSON；不传 tools | DeepSeek 默认关闭 thinking；即便开启，目前也只计字符数、不保留文本 | 带来源前缀的 assistant；目前未设置 reasoning，序列化也会过滤非工具助手的该字段 |
| OpenCode `packages/opencode` | 将选中历史序列化为文本，包含 `[Assistant reasoning]`；用 compaction agent 生成 | 使用会话 processor 保存 reasoning part 及 Provider metadata | 摘要作为 assistant 保存；同模型转换路径保留 reasoning part，跨模型转为可读 text 并移除相关 metadata |
| Pi `packages/coding-agent` | 专用摘要 system + user 文本；历史 thinking 可作为摘要材料 | 调用允许 thinking；摘要结果只提取 text 和 usage | 内部为 compactionSummary，转为带说明的 user 消息，无摘要助手 reasoning 字段需求 |
| Hermes 普通批量压缩 | 旧摘要 + 有界新历史的专用文本 prompt；只序列化可见历史，剥离 think 文本 | 提取响应 content 作为摘要，不把辅助 reasoning 直接附到摘要消息 | 根据相邻角色选择 user/assistant，必要时合并进尾部；发送边界按目标 Provider 保留或补 reasoning |
| DeepSeek Harness | 保留被压缩前缀的原 system、tools、messages，追加一条压缩指令，意图复用缓存前缀 | 保存完整 rawOutput 供审计，活动摘要只选 text block | 用明确来源的 user checkpoint 替换旧区域；不回放摘要调用的 reasoning block |

**OpenCode 是可以保留摘要调用 reasoning 的直接实现参照。** 它创建 `summary: true` 的助手
消息，并交给普通 session processor；processor 记录 reasoning 流事件，消息转换时并不因
`summary: true` 而跳过 reasoning。针对 DeepSeek 的发送转换还为缺少 reasoning 的助手消息
补空 reasoning part，并在适配 interleaved 字段时保留空字符串。
这证明相关消息可以进入 Provider 适配路径，不能代替当前 Flash 的真实 API 回放结论。
[压缩入口](https://github.com/anomalyco/opencode/blob/da4730e4a41dcbb2cb2d907dd2b06ac481b8f962/packages/opencode/src/session/compaction.ts#L394)、
[reasoning 保存](https://github.com/anomalyco/opencode/blob/da4730e4a41dcbb2cb2d907dd2b06ac481b8f962/packages/opencode/src/session/processor.ts#L280)、
[消息回放](https://github.com/anomalyco/opencode/blob/da4730e4a41dcbb2cb2d907dd2b06ac481b8f962/packages/opencode/src/session/message-v2.ts#L362)、
[发送适配](https://github.com/anomalyco/opencode/blob/da4730e4a41dcbb2cb2d907dd2b06ac481b8f962/packages/opencode/src/provider/transform.ts#L305)。

同一个 OpenCode checkout 的 `packages/core` 还有另一条压缩实现，只累计 text delta；
因此本结论明确限定路径，不能描述成所有 OpenCode 压缩入口都保留辅助 reasoning。
[另一条路径](https://github.com/anomalyco/opencode/blob/da4730e4a41dcbb2cb2d907dd2b06ac481b8f962/packages/core/src/session/compaction.ts#L198)。

Pi 将历史 thinking 纳入摘要输入，但生成结果仅保留 text；这恰好说明“读旧 reasoning”不等于
“回放压缩 reasoning”。DeepSeek Harness 则进一步区分完整输出存档和活动 checkpoint。
[Pi 生成与提取](https://github.com/badlogic/pi-mono/blob/a4453b79bb8d66b5f385b28ae0e33843c947504a/packages/coding-agent/src/core/compaction/compaction.ts#L623)、
[Pi 历史序列化](https://github.com/badlogic/pi-mono/blob/a4453b79bb8d66b5f385b28ae0e33843c947504a/packages/coding-agent/src/core/compaction/utils.ts#L109)、
[Pi user 回放](https://github.com/badlogic/pi-mono/blob/a4453b79bb8d66b5f385b28ae0e33843c947504a/packages/coding-agent/src/core/messages.ts#L176)、
[Harness 摘要调用](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/compaction/compaction-basic/src/summarizer.ts#L121)、
[Harness 保存与替换](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/compaction/compaction-basic/src/region.ts#L426)。

Hermes 缺字段时使用的是 **单个空格 `" "`**，且会把旧空字符串转换为空格；其代码注释和回归
记录将此归因于 V4 Pro 的非空校验。已有非空 reasoning 优先保留，切到不接受该字段的端点则移除。
这是该版本的工程兼容策略，不是官方保证，也不是本轮对 Pro 的实测结果。
本项目此前 Flash 对照中 `""` 两次被接受，不能据此推断所有模型/端点都接受 `""`。
[Hermes 摘要提取](https://github.com/NousResearch/hermes-agent/blob/eac1e25127a75c61116a0bbb8ce7681a2f8b61b4/agent/context_compressor.py#L4181)、
[摘要角色选择](https://github.com/NousResearch/hermes-agent/blob/eac1e25127a75c61116a0bbb8ce7681a2f8b61b4/agent/context_compressor.py#L7056)、
[reasoning 策略](https://github.com/NousResearch/hermes-agent/blob/eac1e25127a75c61116a0bbb8ce7681a2f8b61b4/agent/message_sanitization.py#L712)。

Harness 自带 DeepSeek serializer 仍只在带工具调用且 reasoning 非空时回传该字段；不能因为它
是官方开源仓库，就把其整个 adapter 当作当前协议的无缺陷标准。它用 user checkpoint，避开了
本项目摘要 assistant 的这一个触发条件，但不证明其他助手历史路径一定兼容。
[该版本 serializer](https://github.com/deepseek-ai/deepseek-harness/blob/47f943859bef60e4160492346772ded9b24f765a/packages/llm/llm-deepseek/src/serialize.ts#L70)。

## 3. 当前 bot 为什么没有压缩 reasoning 可回传

这是当前实现及运行事实的结果，而非协议禁止：

1. 官方 DeepSeek 端点的压缩默认 `thinking=disabled`；本批 Chess、SymPy 两次摘要调用的
   `reasoning_chars` 都为 0，正常输出分别为 1,974、2,131 tokens。过去两次没有可恢复的非空
   压缩 reasoning，不能为了修复而事后制造它。
2. `_consume_text_request()` 即便遇到 reasoning delta，也只累计字符数；`_TextResponse`
   不携带 reasoning 文本，摘要记录没有对应输出文本供 renderer 读取。
3. 最终摘要可能经过 normalize、格式 repair、condense，再被加上运行器来源前缀。若未来要
   回放真实 reasoning，应记录最终被采用响应的来源；不能给修复后的摘要错误挂上第一次生成
   候选的推理，也不能声称加工后的消息就是原始响应的逐字节回放。
4. 现有 serializer 会过滤非工具助手 reasoning。即使保存并填入非空压缩 reasoning，仍需要
   修复发送边界，才能真正传出去。

[请求消费](../../src/bot/compaction/service.py)、[消息序列化](../../src/bot/core/models.py)、
[本批量化记录](../evaluations/context-compaction-effectiveness.md)。

## 4. 如何选择方案

**可以支持真实压缩 reasoning 的保存与回放，但不必为了填一个协议字段而开启摘要思考。**
API 接受性、摘要质量和成本需要分别验证；独立辅助请求不构成通用禁止回传的理由，也不保证
把该响应迁入另一段主历史后一定被目标服务接受。

| 方案 | 适用条件 | 要验证的代价或边界 |
|---|---|---|
| 无思考摘要 + assistant 协议占位 | 当前默认摘要路径；占位明确来自派生 checkpoint | 按端点验证缺失、空字符串、单空格；不是对未知模型历史统一补位 |
| 真实摘要响应 + reasoning | 摘要调用确有完整 reasoning，来源与目标协议兼容 | 保存最终采用响应及 Provider/model/mode；保留正文与 reasoning 的关联；新增生成与回放用量 |
| text-only user checkpoint | Pi/Harness 风格的派生历史表示 | 明确来源、不能当作用户新指令；与本项目用户锚点及角色校验的语义需独立验证 |

历史固定负载探索中，开启摘要思考的一次调用 completion 为 4,488 tokens、可见摘要估算 815；
关闭后的一次为 583 / 538，两者均保留 12/12 标记事实。这说明存在控制摘要思考成本的动机，
但只有单次探索，输入也略有变化，不能宣称确定的普遍节省率或证明 reasoning 永远无用。
[历史探索记录](../archive/context-compaction-failure-analysis.md#164-真实-provider-探索过程)。

若补充支持真实摘要 reasoning，应将完整辅助输出与活动 checkpoint 分开存储，类似 Harness：
存档保留 raw content/reasoning、模型、模式、finish reason 和阶段；checkpoint 记录采用的
响应与正文变换。Provider 层再选择回放形式，并让 TokenEstimator 计入实际回放 reasoning。
摘要推理不能替代保留尾部中其他助手消息本来需要的历史推理。

## 5. 验证矩阵与结论边界

先使用同一份摘要正文和固定尾部，比较 assistant 摘要字段缺失、`""`、`" "` 三组；记录最终
payload 结构、接受状态、原生工具调用和费用。已有 Flash 记录只覆盖其中缺失与空字符串两组。

要比较真实 reasoning，先通过一次确实产生 reasoning 的摘要调用取得配对的正文与推理，
固定这份正文、尾部及所有其他参数，再比较回传真实 reasoning 与两种占位。保持正文变换一致，
避免将不同摘要内容误算成 reasoning 的效果。不同模型和代理端点分别统计，不能混合分母。

user checkpoint 是另一种角色方案，独立比较后续任务理解、指令归因和产物验收；不能只用
“不会触发 assistant reasoning 校验”作为采用依据。所有 API 探针先只检查接受性，最终仍需
独立任务运行，报告压缩后首请求、后续工具执行、事实保留、产物通过及额外成本。

## 6. 固定版本及核对范围

| 项目 | commit | 本轮核对的关键文件数 |
|---|---|---:|
| Hermes | `eac1e25127a75c61116a0bbb8ce7681a2f8b61b4` | 2 |
| OpenCode | `da4730e4a41dcbb2cb2d907dd2b06ac481b8f962` | 5 |
| Pi | `a4453b79bb8d66b5f385b28ae0e33843c947504a` | 3 |
| DeepSeek Harness | `47f943859bef60e4160492346772ded9b24f765a` | 3 |

13 个关键文件均与对应本地 Git 提交逐字节一致；每个项目的一份关键文件另从固定 GitHub raw
地址下载并核对 SHA-256 一致。本轮是固定源码研究，未声称运行四个框架的完整测试套件、验证
最新主分支，或证明其兼容策略在所有 Provider 上有效。
