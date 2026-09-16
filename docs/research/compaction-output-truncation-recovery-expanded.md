# 摘要生成截断：更多开源实现的恢复方案

核对日期：2026-09-16。补充检查 11 个公开仓库、34 个固定版本源文件，版本和 SHA-256 见[来源清单](../data/compaction-output-truncation-recovery-expanded-sources.json)。本文延续[第一轮调查](compaction-output-limit-recovery.md)，只讨论生成摘要时耗尽输出额度，以及相关预防、恢复和信息损失。

**扩大样本后发现了明确反例：tact 针对 `MaxTokens` 续写并调整 reasoning 额度，Kimi Code 针对截断缩减待摘要历史后重新生成。不能把第一轮的观察扩大为“开源都没有解决方案”。** 但这两种机制都没有同时保证任意规模历史的完整覆盖、最终摘要固定长度和有限成本。

这是静态源码核对：阅读了相关测试，未运行外部项目测试，也未进行真实模型成功率、成本或事实保真度实验。下文“未见”只针对指定路径，不代表所有适配器和发行版。

## 1. tact：续写部分摘要，并调整 thinking 预算

这是本轮最直接针对输出截断的实现。其本地摘要路径：

1. 规划正文额度，按窗口的 20% 计算且最多 2,000 tokens，再额外规划 reasoning reserve，二者相加形成请求的 `max_tokens`。
2. 初次生成继承会话 reasoning effort。初始 reserve 按档位取 0 / 2K / 4K / 8K / 16K；未指定 effort 时，该实现为 DeepSeek、Kimi 按 high 预留 8K。这是客户端的假设和行为，不是本文独立验证的所有服务端默认值。
3. 收到 `MaxTokens` 后，把已经返回的内容追加为 assistant 消息，再追加继续生成的 user 指令；保留原摘要输入，发起下一次请求。
4. 第一次续写降低 effort，暂时把 reserve 设为 0。后续仍截断时，参考实际 `usage.reasoning_tokens`，给观测值增加 25% 余量，结合初始档位、上一轮 reserve 和正文额度下限，逐步调整生成额度，并受窗口余量约束。
5. 最多继续 5 次，即初次请求加最多 5 次续写；瞬态请求错误另有退避重试。最终提取所有轮次的 text 并拼接，thinking 不写进摘要正文。

来源：[effort 与自适应 reserve](https://github.com/rust-infra/tact/blob/1ae0e933dcce743bbd3516b24644102742175189/crates/tact/src/agent/mod.rs#L80)、[正文和总生成额度](https://github.com/rust-infra/tact/blob/1ae0e933dcce743bbd3516b24644102742175189/crates/tact/src/agent/mod.rs#L1507)、[截断续写循环](https://github.com/rust-infra/tact/blob/1ae0e933dcce743bbd3516b24644102742175189/crates/tact/src/agent/mod.rs#L1660)。

示意，不是实测：

```text
原摘要输入 H
  → 部分正文 S1，结束原因为 MaxTokens
H + assistant(S1) + user(继续)
  → 正文 S2，正常结束
最终候选 = S1 + S2
```

源码实际回放的是返回的内容块，可能也包含 thinking；最终候选才只提取 text。不能将“最终剔除 thinking”理解为它不占服务端额度。

有四个重要边界：

- **2K 不是服务端单独执行的正文硬上限。** 服务端收到总额度，正文和 reasoning 如何分配由模型决定；各次正文拼接后也可能超过 2K。它不是“保证最终得到 2K 摘要”的算法。
- **5 次续写耗尽后，非空残缺摘要仍会被采用。** 代码明确允许最终 stop reason 为 `MaxTokens`；为空则报错。因此它提高可用性，但没有严格坚持“完整结束才替换”。仓库还有专门测试这一行为。
- **摘要模型并不始终读取全部原历史。** 它先保存 transcript，摘要输入选取预算内的近期消息；该历史预算最多 20K tokens。不能把它解释为对任意 1M/1B 历史的完整分块覆盖。
- **自适应 reserve 不应说成全程最多 16K。** 16K 是最高初始档位；实现中的后续上限是当前 floor 的两倍，再受窗口限制，不是固定 16K。

来源：[摘要输入选择](https://github.com/rust-infra/tact/blob/1ae0e933dcce743bbd3516b24644102742175189/crates/tact/src/agent/mod.rs#L1584)、[耗尽后采用部分摘要](https://github.com/rust-infra/tact/blob/1ae0e933dcce743bbd3516b24644102742175189/crates/tact/src/agent/mod.rs#L1742)、[结束状态校验和正文提取](https://github.com/rust-infra/tact/blob/1ae0e933dcce743bbd3516b24644102742175189/crates/tact/src/agent/mod.rs#L1783)、[部分摘要兜底测试](https://github.com/rust-infra/tact/blob/1ae0e933dcce743bbd3516b24644102742175189/crates/tact/src/agent/mod.rs#L2803)。其文档对预算有简化描述，以上以固定源码为准。

## 2. Kimi Code：识别截断，删最老的待摘要消息后重试

这里指 MoonshotAI/kimi-code 的 TypeScript `agent-core-v2`，不是下面表格中的 Python Kimi CLI。

`collectSummary()` 遇到归一化的 `providerFinishReason === 'truncated'` 就抛出 `CompactionTruncatedError`，不接受该次候选。外层捕获后：

1. 待摘要消息多于一条且尝试次数未耗尽时，删除最老的一条。
2. 继续删除因此暴露在头部、没有对应调用的 tool 消息。
3. 使用缩小后的历史重新生成；不续写旧候选，也不专门增大输出额度。
4. 默认整个恢复循环最多 5 次尝试，可配置；网络错误和截断等恢复路径共用这个尝试计数。耗尽就报错。

来源：[额度与尝试配置](https://github.com/MoonshotAI/kimi-code/blob/d9d1f5980470d1b1ac3932dc487656539ee639e6/packages/agent-core-v2/src/agent/fullCompaction/fullCompactionService.ts#L640)、[截断后的专用重试](https://github.com/MoonshotAI/kimi-code/blob/d9d1f5980470d1b1ac3932dc487656539ee639e6/packages/agent-core-v2/src/agent/fullCompaction/fullCompactionService.ts#L708)、[截断检测](https://github.com/MoonshotAI/kimi-code/blob/d9d1f5980470d1b1ac3932dc487656539ee639e6/packages/agent-core-v2/src/agent/fullCompaction/fullCompactionService.ts#L856)、[删除最老消息](https://github.com/MoonshotAI/kimi-code/blob/d9d1f5980470d1b1ac3932dc487656539ee639e6/packages/agent-core-v2/src/agent/fullCompaction/fullCompactionService.ts#L916)。此次数指外层摘要请求循环；凭据恢复等内层请求行为另算。

示意：

```text
[m1, m2, ..., m100] → 截断候选，拒绝
[    m2, ..., m100] → 重新生成
```

如果 m2 等是头部孤立的 tool 消息，会一起移除。这是**减少需要总结的信息**，不是让模型把同一份完整证据写得更短。成功后替换范围仍是原始历史，`droppedCount` 记录丢弃数，且在能获取日志范围时附带恢复入口；被删掉的原文不能算已经被摘要覆盖。[替换范围、丢弃数和恢复引用](https://github.com/MoonshotAI/kimi-code/blob/d9d1f5980470d1b1ac3932dc487656539ee639e6/packages/agent-core-v2/src/agent/fullCompaction/fullCompactionService.ts#L749)

它的默认输出配置也较宽松：优先使用 `resolvedModel.maxOutputSize`，否则根据上下文能力计算、最多取 128 × 1024。**这是此层的配置值，不代表每个 provider 都实际支持或收到 128K 输出额度。** 截断重试复用该值。[输出额度选择](https://github.com/MoonshotAI/kimi-code/blob/d9d1f5980470d1b1ac3932dc487656539ee639e6/packages/agent-core-v2/src/agent/fullCompaction/fullCompactionService.ts#L633)

相关测试覆盖尝试次数为 1 时立即失败、不同恢复路径共同消耗尝试额度；本文只阅读测试，未执行。[尝试上限测试](https://github.com/MoonshotAI/kimi-code/blob/d9d1f5980470d1b1ac3932dc487656539ee639e6/packages/agent-core-v2/test/agent/fullCompaction/fullCompaction.test.ts#L1454)

## 3. Qwen Code：20K 额度、两条生成路径与连续失败停止自动尝试

Qwen 的重点是预留输出空间、避免反复撞限，而不是截断后拼接正文。

- 摘要生成上限常量为 20,000；独立摘要请求会结合剩余窗口降低实际请求额度。
- 缓存共享路径保留原会话配置，要求返回完整 `state_snapshot` 且没有工具调用；检查失败可以转独立摘要路径。
- 独立摘要路径按输出用量是否达到请求额度判断疑似截断，并在部分估算场景补查 snapshot 闭合；失败返回 `newHistory: null` 和 `COMPRESSION_FAILED_OUTPUT_TRUNCATED`。
- 连续失败计数达到 3 时，非强制自动压缩入口停止继续尝试。

它还明确留有 TODO：独立查询暂未暴露 `finish_reason`，用 token 数判断只是启发式，恰好完整写到额度的摘要也可能被误判。该检查不能推广为缓存共享和独立路径都严格验证了结束原因。配置中的 `includeThoughts: false` 也不能单独证明所有 provider 都不再进行内部思考。

来源：[输出额度及窗口约束](https://github.com/QwenLM/qwen-code/blob/3d7673c1507f35a1d7241ecb154b86d794c71d0e/packages/core/src/services/chatCompressionService.ts#L58)、[连续失败入口检查](https://github.com/QwenLM/qwen-code/blob/3d7673c1507f35a1d7241ecb154b86d794c71d0e/packages/core/src/services/chatCompressionService.ts#L446)、[共享路径与回退](https://github.com/QwenLM/qwen-code/blob/3d7673c1507f35a1d7241ecb154b86d794c71d0e/packages/core/src/services/chatCompressionService.ts#L879)、[疑似截断判断和已知缺口](https://github.com/QwenLM/qwen-code/blob/3d7673c1507f35a1d7241ecb154b86d794c71d0e/packages/core/src/services/chatCompressionService.ts#L983)。

## 4. 其余项目：处理的是哪一种失败

以下项目也有压缩和重试，但不能直接当成“摘要输出截断后的专用恢复”。

| 项目／范围 | 实际观察 | 与输出撞限的关系 |
|---|---|---|
| Cline SDK agentic compaction | 摘要配置设 `thinking: false`；默认生成额度 4,096，可配置，受模型能力限制。记录 reasoning 字符数和 incomplete reason，空正文时跳过。异常时可回退 basic 策略 | 有预算预防和退化路径；摘要层没有因非空结果携带 incomplete reason 而主动续写或拒绝。实际能否得到此类结果还取决于 provider adapter |
| Gemini CLI chatCompressionService | 第一份摘要后，携带同一份历史和候选，再生成一次校验后的完整摘要 | 第二次是常规事实复核，不是仅在 MAX_TOKENS 时启动的续写；该层未见结束原因专项检查 |
| Goose summarize | 输入 context overflow 时逐步移除更多工具结果再请求 | 明确是输入超限恢复；该摘要循环未见输出 finish reason 的专用分支 |
| OpenHands SDK LLM summarizing condenser | hard reset 捕获异常后按 0.8 缩短每个事件的表示，最多 5 次尝试；常规摘要取返回正文 | 有缩短输入的通用异常恢复，未见截断摘要的专项加额度或续写 |
| Aider history + models | 保留尾部、递归缩写，摘要模型失败可换备选；底层 helper 直接取 message.content | 未消费 finish_reason；“完整结果仍过大再压缩”和“识别输出截断后恢复”不是同一机制 |
| Roo Code condense | 消费流式 text、usage，检查异常和空正文，再构造替换内容 | 该层未见输出截断专用恢复 |
| oh-my-pi agent compaction | 本地文本摘要对过大输入做滚动分块，输入超窗后减半；限制单次生成额度，检查 error | 工具结果先裁到 2,000 字符；单次额度不是最终合并上限；未拒绝 length，详见[2026-09-17 补充核对](oh-my-pi-compaction-deep-dive.md) |
| Kimi CLI Python SimpleCompaction | 生成后移除 ThinkPart，提取摘要内容 | 该路径未见 Kimi Code TypeScript 那样的截断删消息重试；不能混为同一实现 |

逐项源码：

- Cline：[额度与 thinking 配置](https://github.com/cline/cline/blob/15f001ad0b1992965112fc3158f45ec6d2cb090d/sdk/packages/core/src/extensions/context/compaction-shared.ts#L694)、[生成结果](https://github.com/cline/cline/blob/15f001ad0b1992965112fc3158f45ec6d2cb090d/sdk/packages/core/src/extensions/context/agentic-compaction.ts#L70)、[空正文诊断](https://github.com/cline/cline/blob/15f001ad0b1992965112fc3158f45ec6d2cb090d/sdk/packages/core/src/extensions/context/agentic-compaction.ts#L253)、[异常回退](https://github.com/cline/cline/blob/15f001ad0b1992965112fc3158f45ec6d2cb090d/sdk/packages/core/src/extensions/context/compaction.ts#L501)。
- Gemini CLI：[初次摘要与第二次复核](https://github.com/google-gemini/gemini-cli/blob/6a466a7e2fe2b1255752c1e74f69b31f0216084d/packages/core/src/context/chatCompressionService.ts#L361)。
- Goose：[摘要重试条件](https://github.com/block/goose/blob/5a21df18c521136db3723735a973b1c96df7b4db/crates/goose-context-management/src/summarize.rs#L121)。
- OpenHands：[摘要提取](https://github.com/OpenHands/software-agent-sdk/blob/bd88f050259276978dc31541d8099d98e4994428/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L229)、[hard reset 重试](https://github.com/OpenHands/software-agent-sdk/blob/bd88f050259276978dc31541d8099d98e4994428/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L318)。
- Aider：[历史摘要](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/history.py#L1)、[调用结果与重试](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/models.py#L1039)。
- Roo Code：[消费摘要流及失败检查](https://github.com/RooCodeInc/Roo-Code/blob/b867ec9145750d0ae1ff7f02d35406e9bf2a0b16/src/core/condense/index.ts#L323)。
- oh-my-pi：[生成额度与分块](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L863)、[返回状态检查](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L1012)。
- Kimi CLI：[Python 摘要路径](https://github.com/MoonshotAI/kimi-cli/blob/86f136422a0aae6b217ea49e7ea1d2e8a1defcd2/src/kimi_cli/soul/compaction.py#L1)。

## 5. 对 bot 更有价值的组合

以下是基于调查和[今天的失败记录](../evaluations/compaction-incident-20260916.md)提出的方案，尚未修改运行时。

1. **先修正 thinking 和正文共用额度的问题。** 学习 tact 的预算拆分、Cline 的摘要模式配置；总生成额度不要直接等于期望正文长度。保留缓存的首轮与独立恢复阶段分别配置，不能为了兜底而先改坏首轮请求前缀。
2. **撞限后改变请求，而不是原样反复尝试。** 若 thinking 占满额度，降低可调的思考强度或增加总额度；若正文确实未写完，可保留原证据及部分正文，做有界续写。续写消耗新的上下文，必须计入窗口预算。
3. **完整但过长，再处理最终长度。** 续写后重新计量完整请求；只有候选正常结束才考虑定向缩写。缩写最好保留原证据或至少复核关键约束，不能把残缺候选包装成完整覆盖。
4. **不照搬两种有损兜底。** tact 的“最后接受残缺”、Kimi Code 的“丢弃未总结的老消息”都不能直接满足 bot 的历史覆盖要求。若采用任何有损退化，应显式记录遗漏范围，保留原文访问入口，不能推进成“已完整摘要”的覆盖游标。
5. **失败预算跨 Run 生效。** 借鉴 Qwen 的连续失败停止机制，保存失败类型、次数和恢复阶段；最终失败保留旧摘要与尚未覆盖的原文。
6. **成功率拆成两项。** 分开记录“恢复后还能继续对话”与“完整摘要通过校验并替换成功”。否则接受残缺文本或丢历史也会让成功率看起来提高。

目前最直接可借鉴的新增机制是 **tact 的有界续写与观测用量调额度**；Kimi Code 提供了另一种明确的截断恢复，但代价是减少原文覆盖。要同时保证覆盖和摘要长度，还需要完整性、窗口和替换范围三项检查。
