# 摘要时继续执行任务：开源处理方案与缓存取舍

核对日期：2026-09-11。结论是：**有直接同类案例，也有可借鉴的双路径实现；在本轮核对
范围内，没有可直接保证 DeepSeek 同时保留全部热前缀、又绝不生成工具调用的通用开关。**
本次只调研和补充文档，未修改运行时代码，未新增模型实验。

后续扩大检索见[可复用主会话缓存的开源实现](cache-reusing-compaction-implementations.md)：
补充 VS Code Copilot 后台摘要、oh-my-pi handoff 和 Harness 社区缓存参数修复，
区分已实现代码、真实用量报告及未合并 PR。

用户选定的“前缀尝试一次，失败后专用角色摘要”已形成
[具体设计](compaction-prefix-fallback-design.md)，当前仍为设计阶段。

## 1. 与本项目直接对应的故障

[DeepSeek Harness discussion #5521](https://github.com/deepseek-ai/deepseek-harness/discussions/5521)
报告了同一种行为：长工具调用轨迹之后，摘要请求仍携带业务工具，模型返回工具调用；
文本提取器找不到摘要，报 `summarization produced no text summary content`，失败路径又没有
保留完整响应。这是用户部署 Qwen/llama.cpp 的报告，不能当成官方 DeepSeek API 的实测。

截至本轮核对的 `c291e7961a51`，
[summarizer.ts](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/compaction/compaction-basic/src/summarizer.ts#L145)
仍透传 tools、末尾追加压缩指令，未指定 tool choice；空文本检查在返回 rawOutput 前抛错。
因此源码能支持这一失败路径，但不能替讨论者验证其生产成功率。

社区已有 [dsh-compaction-tools-gate](https://github.com/mattafaak/dsh-compaction-tools-gate)
临时插件，按 `purpose=compaction` 移除工具。作者同时报告重新 prefill 的代价，并提供工具
结果限长选项。其失败比例描述涉及 64/74 两种分母，修复后样本量也不完整，本报告不采用其
“100% 成功”作为可靠收益数据。讨论中“none 能保住缓存”的建议同样不是跨 Provider 保证。

本项目已有更直接的两个证据：

- [真实 cache probe](tool-choice-cache-probe.md)：三组原消息对照中 auto→none 均为 0% 命中，
  auto→auto 均为 99.73%；切回 auto 仍命中，旧缓存没有被清空。
- [首次压缩重放](compaction-tool-replay.md)：3/3 返回业务工具调用、0/3 摘要，输入均为
  73,262 tokens；两次重复 objdump，一次重复计划。重放命中率 99.94%。

这说明当前边界首先缺的是摘要成功率。高缓存命中本身不能把工具调用变成摘要。

## 2. 已实现机制和未合并方案

| 项目与状态 | 如何控制摘要行为 | 缓存取舍 |
|---|---|---|
| Pi，已实现 | 专用摘要 system；历史转为带角色标签的文本，放入 conversation 包装；不提供工具；遇到工具调用仍拒绝 | 与主会话前缀不同；减少角色混淆，但不能复用主历史的热前缀 |
| Moon IDE，已实现 | 先同前缀、同 tools、tool_choice 不变，仅追加摘要指令；工具调用、空输出或错误转独立摘要 | 成功时走热前缀；失败才支付独立摘要成本 |
| OpenCode 当前 core 路径 | 历史序列化后放入单个 user；tools 为空 | 独立摘要；同缓存 key 不会让不同前缀命中 |
| OpenCode PR #46369，未合并 | 同模型、合法边界才重放；工具执行函数移除，同时设置 none；超限转序列化路径 | 有缓存复用意图，但其 none 方案不能直接套用为 DeepSeek 的保缓存修复 |

**Pi 的价值在于把“待总结的数据”与“当前要执行的对话”分开。** 其注释明确说明，文本
序列化用于防止模型继续对话；还有独立 system 约束和工具调用拒绝检查。源码：
[序列化与提示](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/compaction/utils.ts#L101)、
[请求与响应校验](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/compaction/compaction.ts#L641)。
这些防线减少继续执行的倾向并阻止工具执行，不保证每次正文都合格。

**Moon IDE 最接近我们可以借鉴的折中。** 它明确保留 tool_choice，避免为了禁工具破坏
messages 缓存；原生历史和图像裁剪状态保持一致。只进行一次同前缀摘要尝试，输出含
tool_calls 就返回失败，调用方转独立摘要，不执行工具。独立路径用专用 system、空工具，
必要时分块并合并。源码与设计记录：
[热路径及工具调用检查](https://github.com/huggingface/moon-ide/blob/a1e9a71927105609ea357179e99b1dabd0d0bd44/crates/moon-coder/src/compaction.rs#L479)、
[双路径选择](https://github.com/huggingface/moon-ide/blob/a1e9a71927105609ea357179e99b1dabd0d0bd44/crates/moon-coder/src/compaction.rs#L266)、
[ADR 0067](https://github.com/huggingface/moon-ide/blob/a1e9a71927105609ea357179e99b1dabd0d0bd44/specs/decisions/0067-in-session-compaction-summary.md)。

只建议借鉴它的分流结构。它允许跳过失败的分块、截断单个超大消息，并可能用部分摘要
覆盖历史；这些行为不符合本项目完整覆盖、校验后发布的要求，不能一起照搬。

**OpenCode 的 PR 需要特别避免误读。** GitHub API 核实 #46369 为 open、merged=false，
head=`f22237ed7652`。代码将工具的 execute 设为 undefined，并传 toolChoice=none；它的
接口测试检查请求前缀和工具字段，不是 DeepSeek 真实缓存 usage 测试。它还限制首次压缩、
相同模型/Provider、无媒体、无自定义压缩 Agent/插件改写等情形。
见 [PR](https://github.com/anomalyco/opencode/pull/46369)、
[关键调用](https://github.com/anomalyco/opencode/blob/f22237ed76521269e9fb9d34ac3734e62f075939/packages/opencode/src/session/prompt.ts#L1198)、
[当前独立 core 路径](https://github.com/anomalyco/opencode/blob/193de13a88d62a6409c6d385831180f1def527dc/packages/core/src/session/compaction.ts#L157)。
此前 #43249 对旧版本 prompt 顺序的描述也不能直接套用当前代码，当前 buildPrompt 已变化。

## 3. 其他缓存/费用优化分别解决什么

1. **复用真实出站前缀，末尾追加摘要指令。** Claude Code 团队公开了这一结构，强调
   system、工具、历史和缓存敏感配置保持一致。可以减少“生成摘要这一请求”的冷输入；
   替换旧历史后的下一次主请求仍要重建变化部分的缓存。公开文章没有给出摘要误调工具的
   完整恢复算法，不能替它补造。见 [团队说明](https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything)。
2. **避免每次只削掉一点，就再次改写历史。** Hermes 的确定性工具结果清理要求达到最小
   回收量，并等待上下文重新增长后才再次改写；当前版本在 Provider 已报告超阈值时允许绕过
   等待门槛，但不绕过最小回收量门槛。这减少反复失去热前缀的机会，适合参考到本项目连续
   repack 问题中；它本身不是 LLM 摘要生成算法。见
   [prune_tool_results_only](https://github.com/NousResearch/hermes-agent/blob/45a6101f36576367359c171cd5820ee76a3d047b/agent/context_compressor.py#L2840)。
3. **独立的一次性摘要可以避免付不必要的缓存写入费。** Pi 在摘要调用中设置
   cacheRetention=none。它优化的是支持该选项且对写入收费的 Provider 的费用，不提高
   命中率，也不能套到 DeepSeek 自动硬盘缓存上。见
   [completeSummarization](https://github.com/earendil-works/pi/blob/d12cd92e45e308d4af000554292165ef1984253b/packages/coding-agent/src/core/compaction/compaction.ts#L578)。
4. **Provider 原生压缩是另一条路线。** OpenAI 官方接口接收窗口内的历史，返回可用于后续
   Responses 请求的 encrypted_content 状态。它将压缩交给专用接口，但依赖 Provider，
   也不等于替换历史后旧 KV 可全部复用；本项目 DeepSeek 路径不能直接接入这种状态。
   见 [OpenAI Docs](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide#compaction)、
   [缓存约束](https://developers.openai.com/api/docs/guides/prompt-caching#what-is-the-prompt-cache)。

缓存 key、并行摘要和后台生成本身都不会恢复已经改变的前缀。按块替换历史，也不代表每块
都有位置无关的 KV 缓存。DeepSeek 要求匹配已持久化的完整前缀单元；Anthropic 也明确列出
tool_choice 变化会使 messages 缓存失效。见
[DeepSeek 规则](https://api-docs.deepseek.com/guides/kv_cache/)、
[Anthropic 规则](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-use-with-prompt-caching#what-invalidates-your-cache)。

## 4. 针对本项目的建议与验证

优先比较以下两条路径，均保留现有摘要覆盖、结构及长度校验：

```text
同前缀路径：主请求副本 + 明确的尾部“摘要阶段”指令
  ├─ 合格摘要 → 发布
  └─ 工具调用 / 无正文 / 不合格 → 独立摘要路径

独立摘要路径：专用摘要 system + 作为数据的历史文本 + 无业务工具
  ├─ 合格摘要 → 发布
  └─ 不合格 → 明确失败，保留原文，不反复执行相同请求
```

同前缀路径先加强末尾指令：暂停任务执行；只记录已完成事实和待办；不要核验常量、重跑
命令或更新计划；无法确认的内容标记待核实。这是针对本次 objdump/plan 行为的**待测建议**，
不是已证实的修复。放在尾部的目的是保留前缀，不代表提升了消息权限或保证模型遵守。

先用同一恢复输入比较原提示、加强尾部提示、独立摘要三组，每组少量重复，保持模型、
thinking、输出额度和覆盖范围一致。除工具调用率/合法摘要率外，检查摘要是否保留：
0x485adc 为 1.0、已完成的 objdump 结果、天空颜色公式的纠错、四项计划与尚未完成的验证。
答案依据只取该边界之前的原文，不把后续运行才确认的结论反灌给摘要或评分标准。
记录每组真实 cached/uncached usage、总费用、耗时，失败重试和 fallback 全部计费。

如果加强提示后仍经常失败，同前缀优先只是在独立摘要前增加一次失败调用，应考虑直接
走独立路径。3 次重放来自同一个边界，不能据此估计所有任务的成功率。随后仍需短续跑
检验重复取证、计划推进和上下文压力，才决定是否扩大到完整任务对比。

本次原始源码快照在 `artifacts/compaction-research-20260911/`；
[来源版本与文件哈希](data/compaction-task-continuation-research-sources.json)固定了核对对象。
源码审计和社区报告不作为本项目新增收益数据。
