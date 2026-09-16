# oh-my-pi 压缩：分块、缓存、替换与输出上限

核对日期：2026-09-17。沿用上一轮固定提交 `042028fd018b1282fbe660ab7255ebad18dd4db5`，不声称覆盖此后的最新版。方法为静态源码审阅及读取上游已有测试，未运行产品、未调用付费模型。文件及 SHA-256 见[来源清单](../data/oh-my-pi-compaction-deep-dive-sources.json)。

结论：oh-my-pi 有多种上下文维护方法。本地文本摘要对**输入过大**提供滚动分块和超窗减半恢复，但对**摘要输出被截断**没有同等严格的拒绝及恢复链。分块处理全部选定消息，也不意味着完整内容都被模型读取：工具结果会先裁剪，超大单条消息也可能被截尾。

## 1. 多条路径，不能混作一个算法

默认方法顺序为 `remote → snapcompact → handoff → shake → soft`，实际按配置、能力、触发原因与失败情况选择，不保证每轮全部执行。

| 方法 | 实现思路 | 主要边界 |
|---|---|---|
| remote | 使用可用的提供商原生压缩或配置的端点 | 原生替换历史可能是 opaque payload；不能用本地摘要的输出限制解释服务端内部行为 |
| snapcompact | 本地把历史排版为图片，让视觉模型在后续请求读图 | 不调用摘要模型；仍有图片 token、载荷和视觉读取质量成本，不是无损语义保证 |
| handoff | 在主会话请求结构后追加交接指令，生成交接文档 | 注重主前缀缓存机会；输入已经 overflow 时跳过，因为它仍重放该输入 |
| shake | 把可恢复的大工具结果、代码/XML 块替换为 artifact 引用 | 无摘要生成开销，但后续读取细节需要额外检索 |
| soft | 独立请求对序列化历史生成文本摘要 | 下文的滚动分块与额度主要属于这条路径 |

另有默认关闭的实验性 notes/context rollover，依靠笔记与历史检索切换窗口，不纳入本地摘要算法的完整性结论。

来源：[方法顺序](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/coding-agent/src/session/compaction-methods.ts#L43)、[调度条件](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/coding-agent/src/session/session-maintenance.ts#L164)、[项目说明](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/docs/compaction.md#L141)。

## 2. 先决定保留哪部分原文

本地准备阶段找最近一个当前模型可读的压缩边界，把前次仍保留的原文与新增消息一起划分。默认 `keepRecentTokens = 20000`，可以按提供商 usage 与本地估算比值调整；这是保留目标，不是所有情况下的硬上限。

切点不能落在工具结果上，避免把 assistant 工具调用与对应结果拆开。最新一组消息即使独自超过目标，也会保留。因此一个巨大工具结果可能让保留尾部本身就过大。

如果切点位于同一次用户任务中间，会形成三段：

```text
以前的完整轮次        当前轮已过去的部分       最近原文
messagesToSummarize   turnPrefixMessages       recentMessages
        ↓                     ↓                    ↓
    历史摘要 S          本轮前缀摘要 T             原样保留 R

模型的新上下文 ≈ system + [S + T + 文件操作记录] + R + 后续消息
```

S 与 T 可以并行生成，再直接拼接。T 专门概括原始请求、前期进展、理解保留尾部所需的信息；它的生成函数没有 S 那套分块循环。

来源：[切点](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L513)、[分区](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L1391)、[两份摘要合并](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L1949)。

## 3. 输入过大时，顺序滚动更新摘要

对历史区域 S，先转换、序列化成文本。如果一个请求装得下，只调用一次；否则按消息边界分块：

```text
S1 = summarize(旧摘要（若有）, 第 1 块)
S2 = summarize(S1, 第 2 块)
S3 = summarize(S2, 第 3 块)
最终返回 S3
```

它不是让每块独立生成摘要、最后拼接全部结果。后续请求只带上一份累计摘要与新块，提示词要求保留旧信息、更新进展与待办。由此推断：单次输入规模可控，但前面遗漏的事实不会因为后续调用自动恢复，越早的信息经历的改写次数越多。

以摘要模型窗口 `W`、传入 reserve `R` 为例，生成层计算：

```text
O = min(floor(0.8 × R), 16384)                     单次历史摘要生成额度
F = min(16384, max(1024, floor(W / 8)))            输入预算下限
B = max(F, floor(0.8 × W) - O - 16384)            单块新历史预算
```

未知模型窗口时回退为 200,000。预算先对窗口打八折，再扣本次输出和累计摘要预留；这是本地规划，不是提供商必然接受的证明。

示例：明确取 `W = 200000`、`R = 16384`，则 `O = 13107`、`B = 130509`。假设**经过序列化预处理后**还有 300K 历史，可按消息边界组成约 120K、120K、60K 三块，执行上面的三次调用。具体块数取决于消息大小；这不是实测 token 或摘要质量。

若提供商仍返回输入 `ContextOverflow`，对当前失败块按 `floor(min(规划预算, 实际估算大小) / 2)` 重新分块，再继续滚动；低于 F 则停止此项恢复并抛错。已经成功的块不在这次内部减半中重做。其他错误不会触发这个减半分支。

还有两处有损处理：每条工具结果在摘要序列化时只保留开头 2,000 个 JS 字符单位及截断说明；单条消息仍超过一个输入块预算时，会按估算比例截尾并留标记。因此“所有消息都经过循环”不能等同“所有原始证据都进入模型”。

来源：[额度、窗口规划及减半](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L768)、[滚动循环](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L853)、[工具输出裁剪](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/utils.ts#L199)、[上游分块测试](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/test/compaction-oversized-input.test.ts#L68)。

## 4. 16K 不是最终摘要的全局长度保证

生成层传入 `completeSimple` 的额度分别为：

| 产物 | maxTokens | R = 16384 时 |
|---|---|---|
| 历史摘要，每一个滚动块 | `min(floor(0.8R), 16384)` | 13,107 |
| 同轮前缀摘要 | `min(floor(0.5R), 16384)` | 8,192 |
| UI 用 shortSummary | `min(512, floor(0.2R))` | 512 |

因此 split-turn 的 S + T 可以超过 16K，随后还追加文件操作记录。`shortSummary` 是额外生成的展示文本，并不把 S + T 再压缩成 512 tokens。这里描述的是摘要层传入额度；不同模型的 adapter、thinking 与服务端约束仍可能改变实际可用正文空间。

thinking 按用户选择传递；未指定或 Inherit 时请求 High，再按模型能力调整；Off 在此层省略 reasoning 参数。输出只取 text，thinking 不写进摘要。但这不证明 thinking 不占提供商生成额度，也没有发现依据本次 reasoning 消耗动态增加预算的恢复逻辑。

来源：[thinking 映射](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L623)、[短摘要](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L1164)、[拼接及文件记录](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L1949)、[前缀摘要](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L2041)、[额度测试](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/test/compaction-summary-cap.test.ts#L54)。

## 5. 摘要本身返回 length 时的缺口

本地 `summarizeConversationWindow` 仅在 `stopReason === "error"` 时抛错，随后拼接 text 返回。底层 `retryTransientCompletion` 对所有非 error 状态直接返回，包含 length。因此非空的 length 摘要可成为下一块的 `previousSummary`，末块也可成为最终候选；未见这里对它续写、提高额度或要求完整重做。

手动摘要默认 oneshot 瞬时错误最多尝试 3 次；自动压缩由外层管理重试并关闭内层重试，避免次数相乘。外层可换摘要模型候选，输入超窗不按瞬时故障原样重试。但这些都以错误浮出为前提，不能修复被当成普通文本返回的 length。

文档另有“主会话正常任务回答 length 后，压缩再重试该任务”的恢复。这是主回答的恢复，不能据此声称摘要子请求有同样保护。

来源：[摘要状态检查](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L1012)、[非 error 直接返回](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/ai/src/oneshot-retry.ts#L158)、[自动重试](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/coding-agent/src/session/session-maintenance.ts#L4616)、[主回答 length 恢复](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/coding-agent/src/session/session-maintenance.ts#L2924)。

## 6. 缓存与替换方式

soft 用摘要专用 system + 一个包含序列化历史的 user 消息，不能直接视为复用了主请求的相同前缀。handoff 则沿用消息转换、工具规范化、缓存 key 等出站管线，在真实历史尾部加交接指令；side sessionId 单独分配，隔离有状态请求。它用 base system 而非临时 hook 覆盖，默认 `toolChoice: none`，只有提供商明确不支持 none 时才重试 auto。因此这是缓存对齐的实现意图，不是所有提供商必然命中的保证。

本地摘要成功后追加 `CompactionEntry(summary, firstKeptEntryId, …)`，不是把原文从日志文件直接剪掉。重建模型输入时，使用最新摘要 + 边界后的保留原文 + 后续消息。此前发生的工具裁剪等维护行为另论；不能据此保证日志永远未经修改。

自动压缩写入并重建后，还检查是否装得下、是否腾出足够空间；不足时尝试额外清理，仍不足就告警、阻止自动续跑，避免无限压缩循环。**这个检查发生在提交之后，不是写入前对摘要正常结束和事实完整性的验收。**

来源：[handoff 请求构造](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/coding-agent/src/session/session-handoff.ts#L137)、[追加压缩记录](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/coding-agent/src/session/session-manager.ts#L2821)、[上下文重建](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/coding-agent/src/session/session-context.ts#L562)、[提交后进展检查](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/coding-agent/src/session/session-maintenance.ts#L4910)。

## 对 bot 的取舍判断

值得借鉴的是超大输入的滚动分块、输入超窗后改变块大小、保留工具配对与本轮任务上下文、隔离辅助请求并复用实际出站管线，以及防止无进展自动循环。它们主要改善输入可处理性、缓存机会和可用性。

不宜直接照搬的部分是工具结果固定截到 2,000 字符、非空 length 当作摘要、以及把每次调用上限误当成最终替换内容的全局预算。对本项目还应补上 length 拒绝／有界恢复、最终完整请求预算、关键事实检查与失败不推进覆盖边界。
