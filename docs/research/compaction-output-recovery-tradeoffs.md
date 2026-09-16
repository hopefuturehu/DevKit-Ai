# 开源摘要压缩方案：取舍总览

整理日期：2026-09-16。汇总本轮对话已核对的 **17 个开源项目**；沿用各自固定提交，不重新解释为“所有最新发行版都如此”。Hermes 采用后一次核对的提交。Claude Code 和智谱 ZCode 的核心压缩未能作公开源码审计，另列为未知项。

结论：这些实现各自保护的目标不同。**拒绝残缺摘要保护历史，但可能让压缩暂时无法推进；续写消耗额外请求和窗口；删输入／裁剪输出能提高可用性，但减少信息覆盖；保留主请求前缀有利于缓存，却也保留其协议和模型行为约束。** 表中的收益、代价是根据实现推导的工程判断，不是实测成功率排名。

来源与完整边界：[首轮调查](compaction-output-limit-recovery.md)、[11 项补充调查](compaction-output-truncation-recovery-expanded.md)、[OpenClaw 与 Hermes](openclaw-hermes-summary-output-recovery.md)。各文档附固定源码与文件哈希。

## 先用同一套标准比较

1. **输入覆盖**：选定要替换的历史是否都进入了摘要请求；分块全部处理也不代表模型保留了每项事实。
2. **生成完成**：是否正常结束；`length` 表示额度耗尽，非空正文不能代替这项判断。
3. **最终可用**：摘要、system、tools、近期原文和待处理输入合起来是否放得下，是否实际回收空间。
4. **失败可恢复**：失败是否保留旧摘要、未覆盖原文和覆盖游标；磁盘上有原文也不代表下一次请求会自动带上它。

正文软目标、服务端生成额度和最终请求预算应分开。thinking 可以不进入摘要正文，却仍可能计入服务端生成额度。

## 最有区分度的八种实现

| 项目／路径 | 关键机制 | 主要收益 | 主要代价或缺口 |
|---|---|---|---|
| [tact](https://github.com/rust-infra/tact/blob/1ae0e933dcce743bbd3516b24644102742175189/crates/tact/src/agent/mod.rs#L1660) | `MaxTokens` 后回放部分结果续写；降低 effort、按观测 reasoning 用量调额度；最多 5 次续写 | 直接处理单次生成不足，不必丢掉已生成正文 | 累计正文可能更长；次数耗尽仍可接受残缺；原摘要输入只选预算内近期历史，最多约 20K tokens，不能当成完整大历史覆盖 |
| [Hermes](https://github.com/NousResearch/hermes-agent/blob/dd566d52aa009facd38f5847da60f3877e359cce/agent/context_compressor.py#L3221) | 不加摘要专用小额度；拒绝 length；独立摘要模型可回退主模型一次；最终失败保留原文并冷却 | 截断失败边界清楚，冷却可持久化，不反复写坏滚动摘要 | 不传额度仍受服务端默认值限制；本来就用主模型时没有第二个模型可回退；另外的候选瘦身路径可能机械截摘要 |
| [Kimi Code TS](https://github.com/MoonshotAI/kimi-code/blob/d9d1f5980470d1b1ac3932dc487656539ee639e6/packages/agent-core-v2/src/agent/fullCompaction/fullCompactionService.ts#L633) | 截断后拒绝候选，删除最老待摘要消息及开头孤立工具结果再生成；默认最多 5 次外层尝试 | 减轻摘要任务，给重试改变条件 | 最早的信息不再进入新摘要；成功替换范围仍可覆盖原历史，只是另记 droppedCount 和日志恢复入口 |
| [OpenClaw safeguard](https://github.com/openclaw/openclaw/blob/bfa10898b2055f29fc1447e8f8dbc9f7cef0396a/src/agents/agent-hooks/compaction-safeguard.ts#L1283) | 分块／合并；最终保存预算；启用质量检查时，对最终文本做审计，失败按具体长度和缺失项重生成 | 对“完整但太长、裁剪后缺关键内容”有明确纠正流程 | 保存上限是 16,000 UTF-16 单位，不是 tokens；仍会裁剪；核心摘要函数未显式拒绝 length，结构检查不等于语义完整 |
| [DeepSeek Harness basic](https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/compaction/compaction-basic/src/summarizer.ts#L164) | 复用 system、工具和历史前缀；默认生成额度 8,192；拒绝 max-tokens；成功且实际变小才替换 | 有利于复用主请求缓存，替换条件清楚 | 缺少内置的截断续写／自适应扩额度；第二轮只在首轮成功后触发，无法挽救第一轮 length |
| [pi coding-agent](https://github.com/earendil-works/pi/blob/9e05370b298d0a6b8d9bc2c02e4bfae189ef1616/packages/coding-agent/src/core/compaction/compaction.ts#L556) | 明确拒绝 length；部分 Anthropic 路径将正文基础额度与 thinking 预算分开规划 | 能防止残缺正文成为 checkpoint，预算职责较清楚 | 检测到问题不等于恢复；没有专门续写／扩大额度；同仓库另一条 harness 路径检查不同 |
| [Qwen Code](https://github.com/QwenLM/qwen-code/blob/3d7673c1507f35a1d7241ecb154b86d794c71d0e/packages/core/src/services/chatCompressionService.ts#L879) | 共享缓存路径不合格可转独立摘要；20K 生成上限受窗口约束；疑似截断拒绝；连续失败 3 次停止普通自动尝试 | 兼顾首轮缓存与独立兜底，减少持续重复失败 | 独立路径未透传 finish_reason，部分检查是用量／闭合标签启发式；停止重试不等于已经释放空间 |
| [Codex 本地文本路径](https://github.com/openai/codex/blob/4701aa4b4239c70063ab6f2fcb835324f9c109f4/codex-rs/core/src/compact.rs#L255) | incomplete 转错误，压缩有限重试；完成后才替换 | 失败不会直接成为成功摘要，恢复流程有界 | 没有看到针对输出截断改变额度或续写的专用流程；原生服务端压缩内部另属未知 |

补充两个容易混淆的细节：

- DeepSeek 的第二轮可能压的是第一轮摘要加新选定区间，并非保证“把原历史另一半也压完”；第一轮报错不会进入正常的第二轮。详见首轮报告。
- OpenClaw 的质量纠正默认一次、可配置最多三次。裁剪后通过检查就可能采用，并非一律先语义缩写；Hermes 的 8,000 字符机械瘦身属于“候选越压越大”处理，并非接受 length 候选。

## 另外九个项目

下列“未见专门 length 恢复”只针对本轮已读路径，不能推成整个产品不存在额外 adapter、插件或原生端点能力。

| 项目／路径 | 做了什么 | 取舍 |
|---|---|---|
| [Gemini CLI](https://github.com/google-gemini/gemini-cli/blob/6a466a7e2fe2b1255752c1e74f69b31f0216084d/packages/core/src/context/chatCompressionService.ts#L361) | 初稿后携带同一份原历史，再做一次事实复核 | 更重视遗漏检查；通常多一次生成与延迟；第二次仍可能截断，不是 length 专用续写 |
| [Cline SDK](https://github.com/cline/cline/blob/15f001ad0b1992965112fc3158f45ec6d2cb090d/sdk/packages/core/src/extensions/context/agentic-compaction.ts#L70) | 摘要配置关闭 thinking、可配额度；记录 incomplete reason；异常可退回 basic 裁剪 | 降低生成压力、有退路；非空 incomplete 正文在摘要层未被专门拒绝，basic 回退不等于语义覆盖 |
| [Aider](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/history.py#L1) | 保留近期尾部、递归缩写，摘要模型失败可用备选 | 结构简单，针对压完仍过大继续处理；底层摘要 helper 未消费 finish_reason，可能无法区分完整长摘要和截断片段 |
| [oh-my-pi 本地文本路径](https://github.com/can1357/oh-my-pi/blob/042028fd018b1282fbe660ab7255ebad18dd4db5/packages/agent/src/compaction/compaction.ts#L863) | 对大输入滚动摘要，输入超窗后减半重分块；另有保前缀 handoff 等维护方法 | 工具结果先裁到 2,000 字符；单次额度不等于合并后长度上限；本地摘要未拒绝 length，详见[补充核对](oh-my-pi-compaction-deep-dive.md) |
| [Goose](https://github.com/block/goose/blob/5a21df18c521136db3723735a973b1c96df7b4db/crates/goose-context-management/src/summarize.rs#L121) | 输入 context overflow 时，分级移除工具结果再试 | 有助于让摘要请求进得去；丢失输入证据，不是在修复写到一半的摘要 |
| [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk/blob/bd88f050259276978dc31541d8099d98e4994428/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L318) | hard reset 异常时按 0.8 缩短事件表示后重试 | 有界减小输入；事件细节变少，未见摘要输出截断专用补救 |
| [OpenCode core](https://github.com/anomalyco/opencode/blob/350c726aa8b6b11eb9242040bc5eb7ae837fbf8a/packages/core/src/session/compaction.ts#L176) | 摘要生成上限 4,096，检查错误与空正文 | 调用简单、单次额度明确；未消费 length，存在采用非空残缺摘要的静态风险 |
| [Roo Code](https://github.com/RooCodeInc/Roo-Code/blob/b867ec9145750d0ae1ff7f02d35406e9bf2a0b16/src/core/condense/index.ts#L323) | 汇集 text/usage，异常或空正文时报错 | 实现简单；未见输出截断专用校验和恢复 |
| [Kimi CLI Python](https://github.com/MoonshotAI/kimi-cli/blob/86f136422a0aae6b217ea49e7ea1d2e8a1defcd2/src/kimi_cli/soul/compaction.py#L1) | 生成后去除 ThinkPart，提取摘要 | 路径直接；没有看到 Kimi Code TS 的截断删消息重试，两个实现不能混算 |

OpenCode 在线 V2 文档描述了前缀复用和标题纠正，但不能与表中 core 固定源码拼成一套已经验证的实现。

## 机制层面的成本与缓存取舍

以下是工程推断，不是跨项目性能实测；实际费用取决于模型价格、输入长度、缓存命中和是否成功。

| 机制 | 通常获得什么 | 需要支付什么 |
|---|---|---|
| 保留主会话请求前缀 | 有机会复用已有 prompt cache，减少长历史重读成本 | 保留 tools、协议和模型模式约束；指令可能仍诱发任务执行，需检测。不能仅凭保留工具断言必然命中 |
| 独立摘要请求／摘要模型 | 可以单独选择模式、输出额度和模型 | 主会话前缀通常不能直接复用；输入可能重新计费，模型切换也不保证质量提升 |
| 增加总生成额度 | thinking 与正文有更多空间 | 潜在生成成本、延迟和输出更长；部分 provider 要求 input + max_tokens 放进同一窗口 |
| 有界续写 | 能保留已写出的正文，继续完成 | 增加请求与累计输入；要处理重复、回放协议和最终过长，正常结束仍不证明无遗漏 |
| 重做／定向缩写 | 有机会在明确预算内给出完整新稿 | 额外请求；仅拿旧摘要重写可能固化已经发生的遗漏 |
| 分块再合并 | 单次请求压力更小，可以逐段处理大历史 | 更多调用；跨块依赖和合并过程仍可能遗漏、截断 |
| 删除输入／裁剪输出 | 能确定减少长度 | 明确信息损失；有日志引用也要额外检索才能恢复 |
| 拒绝候选并冷却 | 不覆盖成残缺摘要，限制重复花费 | 这次没有回收空间；若已达硬窗口上限，对话仍可能需要外部恢复动作 |

## 对 bot 的建议组合

这不是声称某个项目已经整体实现这套方案，而是按本项目“缓存有效、覆盖可解释、失败不丢原文”的需求组合已有机制：

1. **首次请求优先复用前缀。** 借鉴 DeepSeek / Qwen；保留实际生效的工具及 thinking 设置，单独规划服务端生成额度，不把正文软目标当成总生成上限。
2. **根据失败类型改变第二次请求。** thinking 耗尽额度时，按 provider 能力调思考模式或生成额度；正文确实未写完时，可在独立恢复阶段对原证据和部分候选做有限续写／重做。先控制总次数、总 tokens 和时间，再评估更复杂的分块。
3. **完整候选通过三项检查再替换。** 正常结束、关键约束可用、完整请求能放下且有收益。完整但过长才定向缩写；借鉴 OpenClaw 对最终文本的检查，不能只检查裁剪前的版本。
4. **最终失败采用 Hermes 式保留和持久化冷却。** 不推进覆盖游标；未达硬上限时保留未摘要原文，避免只为软目标而静默省略它。已达硬上限时明确报告无法继续，而不是伪装压缩成功。
5. **分别统计可用性和信息覆盖。** “还能回复”“完成摘要并替换”“原文有损省略”必须是不同结果；否则无法公平比较成功率。

不建议直接照搬固定的 4K/8K/16K 数字、tact 耗尽后接受残缺、Kimi 删未摘要历史，或以机械截尾作为常规成功路径。这些是不同产品的取舍，不是压缩正确性的充分条件。

## 不能据此评价的实现

Claude Code 的核心压缩内部算法未公开；本轮也未找到智谱 ZCode 核心 agent 的公开源码。ZCode 公开的自动压缩触发余量不能推导摘要 max_tokens 或 length 恢复策略；不将这两者列入源码方案优劣排名。[ZCode 官方 FAQ](https://zcode.z.ai/en/docs/qa#auto-compact)

本次仅汇总既有研究并建立取舍框架；没有修改 bot 运行时，没有新增外部产品成功率或摘要保真度实测。
