# OpenClaw 与 Hermes：摘要输出上限的处理

核对日期：2026-09-16。检查固定提交：OpenClaw `bfa10898b2055f29fc1447e8f8dbc9f7cef0396a`，Hermes `dd566d52aa009facd38f5847da60f3877e359cce`。文件来源与哈希见[清单](../data/openclaw-hermes-summary-output-recovery-sources.json)。

范围是内置文本摘要及相关恢复、质量检查，不把普通回复续写当成摘要续写，也不推广到所有原生服务端压缩、插件和 provider。此次只做静态源码核对并阅读现有测试，没有运行外部项目测试或付费模型请求。

**两者都有应对，但侧重点不同：OpenClaw 有最终摘要预算、结构保留与有界纠正；Hermes 明确拒绝服务端截断摘要，独立摘要模型失败可回退主模型，最终失败保留历史并冷却。没有在这两条摘要路径中发现 tact 那种直接由 length 触发的正文续写循环。**

## OpenClaw

### 生成额度和分块

常规摘要基础额度为 `min(floor(0.8 × reserveTokens), model.maxTokens)`；split-turn-prefix 使用 0.5 系数。safeguard 还会先把 reserve 限制到已知模型输出能力。这里是摘要层计算值，底层 provider 对 reasoning 与额度的处理另算，不能将它当成服务端独立的正文上限。来源：[摘要生成额度](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/packages/agent-core/src/harness/compaction/compaction.ts#L763)、[reserve 的能力约束](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/agent-hooks/compaction-safeguard.ts#L555)。

大历史可分阶段生成多个摘要，再合并。阶段内部也可以分块滚动更新摘要，每块失败时最多 3 次尝试，常规重试复用同样的生成参数；失败后可排除过大的消息再试。没有过大消息时不重复同样的整个输入。来源：[分块与每块重试](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/compaction.ts#L118)、[超大消息回退](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/compaction.ts#L200)、[分阶段摘要与合并](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/compaction.ts#L296)。

若部分块已成功、后续块失败，帮助函数可能返回已完成块的摘要并附未覆盖块说明。这种“部分摘要”指**只有部分输入块被总结**，不能与服务端截断的半段输出混同；它也不保证全历史覆盖。最终是否接受还取决于外层质量检查。

### 最终摘要过长：裁剪，再检查，再定向重生成

持久化摘要有独立于模型的 **16,000 UTF-16 code units** 上限。这是 JavaScript 字符串长度口径，**不是 16K tokens**；若完整请求剩余 token 预算更少，最终文本还会进一步缩小。基础路径提供机械截断和截断标记。来源：[保存上限与截断](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/packages/agent-core/src/harness/compaction/compaction.ts#L120)、[完整请求剩余额度约束](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/packages/agent-core/src/harness/compaction/compaction.ts#L162)。

safeguard 启用质量 guard 时，流程更完整：

1. 先按最终保存预算处理正文、附加信息及应保留的事实。
2. 对实际将保存的文本检查必需标题、待办请求、精确标识符；部分结构检查还对照原始生成正文，不能用后来补上的标题冒充生成合格。
3. 检查失败，把缺失项和可用正文长度写进新指令，重新对选定历史生成。
4. 默认可纠正 1 次，配置上限为 3 次；始终不通过则取消，不写成功的压缩结果。必需事实本身放不进预算时也会取消。

来源：[纠正次数默认值](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/agent-hooks/compaction-safeguard.ts#L88)、[生成与最终校验循环](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/agent-hooks/compaction-safeguard.ts#L1283)、[拒绝条件](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/agent-hooks/compaction-safeguard.ts#L1375)、[带实际正文预算的纠正指令](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/agent-hooks/compaction-safeguard.ts#L1418)。已有测试检查第二次请求带有具体的 UTF-16 长度要求，但本文未运行该测试。[纠正重试测试](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/agent-hooks/compaction-safeguard.test.ts#L2906)

因此它确实提供了“最终摘要太长导致必需内容丢失，再按预算重写”的机制。但检查是结构与部分关键事实启发式，不是完整语义保真证明；裁剪后能通过检查的候选仍可采用。

### 服务端 length：存在检查缺口

检查的 `runSummarizationCompletion()` 只将 `aborted` 和 `error` 显式转成失败，再检查是否有正文；没有显式拒绝 `length`。`extractSummaryText()` 也只抽取非空文本。这意味着**不能宣称所有服务端截断摘要都会被拒绝或触发恢复**；若适配器保留 length 而没有转错误，非空残缺内容可能进入后续处理。safeguard 的标题和关键事实检查可能挡住部分情况，却不能等同于结束原因校验。来源：[结束状态检查](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/packages/agent-core/src/harness/compaction/compaction.ts#L686)、[正文提取](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/packages/agent-core/src/harness/compaction/utils.ts#L144)。

OpenAI Chat provider 的这层包装仅对 aborted/error 抛错，其余结束状态可产生 done 事件；本文未对所有协议做运行实验。[provider 完成事件](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/packages/ai/src/providers/openai-completions.ts#L173)

官方文档另说明：符合模型回退条件的 provider 错误可沿会话已有 fallback 链重试；显式指定 compaction.model 时不继承该链。不能据此认为 length 必然触发换模型。[固定版本官方文档](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/docs/concepts/compaction.md#L1)

## Hermes

### 预防：避免把 thinking 和正文挤进很小的摘要专用额度

`_call_summary_llm()` 刻意不传摘要专用 `max_tokens`，注释直接说明 thinking 模型会先花掉额度。正文期望长度放在提示词中。**不传不等于无限输出**：仍受适配器和服务端默认值约束，要求 max_tokens 的协议会由适配器补值。来源：[摘要调用](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/agent/context_compressor.py#L3221)。

辅助调用层还支持明确的非 reasoning 摘要路由：配置明确指定 provider/model 并关闭 reasoning，且实际路由匹配时，才应用相应设置；换到其他路由时防止这些专用控制错误继承。这是可配置预防措施，不是所有摘要默认关闭 thinking。[摘要非 reasoning 路由检查](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/agent/auxiliary_client.py#L5839)

### length 后：拒绝候选，有条件换模型一次

摘要响应若为 `finish_reason=length`，立即抛出截断错误，不把该候选更新为滚动摘要。

- 有一个与主模型不同的 `summary_model`，且尚未回退时：清除该覆盖，进入主模型回退流程，对相同待摘要区间重新生成一次。
- 没有可回退的独立摘要模型，或回退也失败：记录截断失败，并设置 30 秒冷却；已有更长冷却不会被缩短。
- 最终截断失败时，压缩返回原历史，回滚这次摘要状态；即使 `abort_on_summary_failure=false` 也不能用静态占位覆盖原消息。
- 挂接会话 DB 和 session ID 时，冷却写入持久化状态并可重新读取，不只依赖当前 compressor 实例的内存。

来源：[拒绝 length](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/agent/context_compressor.py#L3278)、[一次模型回退和冷却](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/agent/context_compressor.py#L3484)、[最终失败保留会话](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/agent/context_compressor.py#L4430)、[持久化冷却](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/agent/context_compressor.py#L2136)。

示意：

```text
独立摘要模型 → length → 丢弃这份候选
主模型重做   → stop   → 采用完整候选
             → length → 本次压缩失败，保留原历史，进入冷却
```

仓库专门测试了“非空 length 正文不得成为 checkpoint”和“独立摘要模型截断后只回退一次”。这些是阅读到的测试断言，不是本次运行结果。[截断回归测试](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/tests/agent/test_compressor_truncated_summary_guard.py#L61)。

### 成功但越压越大：另外的机械瘦身

Hermes 的 `salvage_grown_transcript()` 可以清理旧 reasoning、旧工具结果，并将过长摘要截为前 8,000 字符再加标记；只有候选实际小于原预算才返回。这是另一个有损退化路径，**不表示 length 候选能绕过前面的拒绝检查**。来源：[膨胀候选瘦身](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/agent/context_compressor.py#L415)。

普通主对话确实另有截断续写相关代码和测试，但不能把它套到独立的摘要调用上。摘要路径在 length 时抛错并进入上述模型回退／冷却，未见把部分摘要追加成 assistant 消息继续生成的循环。

## 对 bot 的具体启示

1. 把“服务端生成截断”和“候选全文正常结束但最终太长”分开处理。前者先恢复生成，后者才定向缩写。
2. 可借鉴 OpenClaw 的**用最终保存文本做检查，再把实际预算和缺失项反馈给重写请求**；不要只在截断前检查一遍，也不要把字符数当 token 数。
3. 可借鉴 Hermes 的**明确拒绝 length、一次独立模型回退、跨 Run 冷却、失败不推进覆盖范围**。
4. OpenClaw 的机械裁剪／部分块兜底和 Hermes 的 8,000 字符瘦身都有信息损失代价，不应为了提高表面成功率而直接照搬。

对照前两轮调查：[摘要输出超限与恢复](compaction-output-limit-recovery.md)、[更多开源项目](compaction-output-truncation-recovery-expanded.md)。
