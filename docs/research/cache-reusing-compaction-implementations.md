# 可复用主会话缓存的开源压缩实现

核对日期：2026-09-11。范围是生成交接/压缩摘要时复用 Provider 的主会话输入缓存。
这是[前一轮调研](compaction-task-continuation-research.md)的补充；本轮未修改运行时，
未调用付费模型，也未测得这些项目在本项目 DeepSeek 配置下的成功率。

## 1. 结论与证据等级

除 Moon IDE 外，**VS Code Copilot 的后台摘要是本轮新增、最直接的参考**。
oh-my-pi 的 handoff 也有完整的缓存对齐实现，但默认改用 `toolChoice: none`，存在
Provider 兼容性边界。DeepSeek Harness 本身也按前缀复用设计，其社区分支提供了实际
cache usage 报告。不能把这三种证据都写成“已经证明稳定命中并成功压缩”。

| 实现 | 核对状态 | 复用方式 | 关键限制 |
|---|---|---|---|
| VS Code Copilot 后台摘要 | 主分支 `a2abfe9056619`，已实现 | 复用主循环渲染消息、工具及模型能力配置，尾部追加摘要 user 消息；该调用不主动设置 none | 提示词约束输出；没有合格文本仍会失败；没有本轮真实命中率测量 |
| oh-my-pi handoff | 主分支 `3b3a6dc9bbd8`，已实现，MIT | 共用消息转换、工具规范化、Provider 转换和缓存路由配置 | 默认 none；base system 与逐轮 hook 覆盖可能不同；不能保证全部历史命中 |
| DeepSeek Harness | 主分支 `c291e7961a51`，已实现前缀摘要 | 原 system/历史前缀与 tools，末尾追加压缩指令 | 主分支仍未完整继承请求参数；空正文/工具调用问题仍存在 |
| Harness 社区修复 | zoahdev 分支 `1cf3ee626dd6` | 继承 routed header 配置，避免摘要请求无故改变 reasoning/output 参数 | 社区分支和社区 usage，不能算官方主分支已修好 |
| OpenCode #42506 / #46369 | 本轮 API 核对均 open、merged=false | 主循环组装或受限原生历史重放 | 未合并；前者明确把禁工具留给后续，后者使用 none |

“命中缓存”由实际请求前缀、Provider 路由和缓存可用性决定；模型不负责决定是否命中。
“模型是否按要求输出摘要”则是另一个成功条件。我们已有重放是 99.94% 命中、0/3 摘要，
所以需要分别记录这两个指标。摘要替换历史后，主循环也要重新缓存从变化点开始的内容。

## 2. VS Code Copilot：保留前缀的后台摘要

原 `microsoft/vscode-copilot-chat` 仓库已归档并迁入 `microsoft/vscode`；本轮以新仓库的
固定 commit 为准，而非只看旧仓库的 Full/Simple 同步摘要实现。

调用入口是
[`AgentIntentInvocation._startBackgroundSummarization`](https://github.com/microsoft/vscode/blob/a2abfe9056619001322c95a9761a6671d233de68/extensions/copilot/src/extension/intents/node/agentIntent.ts#L1105)：

1. 直接接收主循环渲染好的消息，沿用主循环的内部工具 ID 清理，追加摘要 user 消息。
2. 按主循环方式规范化工具，保持相同 endpoint、conversationId 和 thinking、reasoning
   effort、tool search、context editing 能力配置。后台调用保留 tools，未设置 tool_choice。
3. 这是一条独立摘要请求，`ignoreStatefulMarker: true`，不会进入业务工具执行循环。
4. 提取响应正文中的 summary 标签；找不到标签则失败。后台失败、后续渲染预算不足时，
   回到同步摘要路径。它还记录真实 `prompt_tokens_details.cached_tokens`。

这条路径同时关注触发时机：有已完成工具轮次时，在预算占用约 78%–82% 的随机阈值
启动；无此“缓存已热”信号时，90% 才紧急启动。轮次数只是热缓存的启发式信号，
不是缓存实测。后台结果按轮次锚点发布，计算期间主循环可以继续。
见[触发条件](https://github.com/microsoft/vscode/blob/a2abfe9056619001322c95a9761a6671d233de68/extensions/copilot/src/extension/prompts/node/agent/backgroundSummarizer.ts#L45)、
[触发调用和能力对齐](https://github.com/microsoft/vscode/blob/a2abfe9056619001322c95a9761a6671d233de68/extensions/copilot/src/extension/intents/node/agentIntent.ts#L1001)。

对本项目的价值：从实际渲染结果派生摘要请求，同时保留缓存敏感能力配置，避免重新
拼装造成差异；在窗口满前完成摘要。这不会天然解决模型继续任务的问题。
它的解析器允许缺少闭合 summary 标签，且后台调用未单独拒绝“正文夹带工具调用”的
所有情况；本项目应保留现有长度、覆盖、结构和工具调用校验。
见[提示与解析](https://github.com/microsoft/vscode/blob/a2abfe9056619001322c95a9761a6671d233de68/extensions/copilot/src/extension/prompts/node/agent/summarizedConversationHistory.tsx#L1158)。

不要混淆另一条路径：同文件中的 `ConversationHistorySummarizer.getSummary` 是同步摘要，
Full 模式携带 tools 并设置 none，还会处理图片/工具搜索等历史内容；Simple 模式重新
组织摘要输入。不能用它们的调用结构解释后台路径的缓存行为。

## 3. oh-my-pi：共用请求管线，但 none 有边界

[`SessionHandoff.generateDocument`](https://github.com/can1357/oh-my-pi/blob/3b3a6dc9bbd85102ce19d0b1c11bf6870915f6ec/packages/coding-agent/src/session/session-handoff.ts#L126)
把 handoff 指令追加到当前消息副本，使用 `convertMessagesToLlm`、
`buildSideRequestContext` 和 `prepareSimpleStreamOptions`，连同文本混淆、工具
规范化、请求 hooks 和缓存路由参数一起对齐。缓存 key 继承主 Agent 实际使用的 key，
辅助请求有独立 side sessionId，避免与有状态主会话混用。

但有两个不能略过的细节：它固定使用 base system，而非当前轮的 hook 覆盖；
[`generateHandoffFromContext`](https://github.com/can1357/oh-my-pi/blob/3b3a6dc9bbd85102ce19d0b1c11bf6870915f6ec/packages/agent/src/compaction/compaction.ts#L1079)
默认强制 `toolChoice: none`，并按模型支持范围转换 thinking 配置。只有 Provider 明确
拒绝 none、要求 auto 时才重试一次 auto；不是“缓存没命中就重试 auto”。

返回内容仅取 text，不执行返回的工具。无文本时，手动 handoff 报失败；自动 handoff
可以转下一种维护方法。当前实现会在原会话写入 compaction entry，保留切点后的近期
历史；不能沿用旧版本“始终新开 session”的描述。
见[项目管线说明](https://github.com/can1357/oh-my-pi/blob/3b3a6dc9bbd85102ce19d0b1c11bf6870915f6ec/docs/handoff-generation-pipeline.md)。

可借鉴的是共用出站管线和辅助请求隔离。其 none 不能直接用于本项目保缓存：
[现有 probe](../evaluations/tool-choice-cache-probe.md)中，DeepSeek 三组 auto→none 均为零命中。
因此它是已实现的缓存对齐方案，不是跨 Provider 的命中保证。

## 4. DeepSeek Harness：有社区 usage，需区分分支

主分支 [`summarizeWithLlm`](https://github.com/deepseek-ai/deepseek-harness/blob/c291e7961a515f6d7af9304e7fd1d257929aef26/packages/compaction/compaction-basic/src/summarizer.ts#L121)
保留原前缀与 tools，但摘要请求仍只取 provider/model，并额外指定 maxTokens。
[Discussion #1944](https://github.com/deepseek-ai/deepseek-harness/discussions/1944)
报告 opencode-go + deepseek-v4-flash + high reasoning 配置下的缓存失配。
其[社区分支](https://github.com/zoahdev/deepseek-harness/blob/1cf3ee626dd6cb5d21f89e0fbd240a6dc8ec2bb3/packages/compaction/compaction-basic/src/summarizer.ts#L29)
增加 header 配置继承，按主循环语义移除 adapter 物化的默认值，不再无条件添加摘要输出
额度覆盖。核对的官方主分支尚无这项完整继承。

社区公布的一次修复后 usage 为 220,928 cached、402 uncached 输入 tokens，按二者合计
计算命中率约 **99.82%**。原帖写约 99.7%，这里明确采用列出的数值计算。
修复前报告是 122,558 输入、无 cacheReadTokens；两次输入长度不同，不是相同快照的
严格 A/B，不能直接计算节省金额或证明某一个参数单独导致失配。报告来自网关配置，
不能据此声称官方 DeepSeek API 总把 max_tokens 纳入缓存 key。

这个分支证明有可检查的修复代码与社区用量线索，不解决
[#5521 的工具调用/无摘要问题](https://github.com/deepseek-ai/deepseek-harness/discussions/5521)。
继承 header 还需确保其来自当前 Provider/model 配置，避免重启后继承过期路由。

## 5. 本项目优先参考什么

优先参考 Copilot 后台路径的“已渲染请求派生 + 能力配置对齐 + 提前触发”，并结合 Moon
的独立摘要兜底。oh-my-pi 可用于检查主调用和辅助调用是否共享了完整出站转换过程。
这些结构仍需用本项目的同一压缩快照验证摘要成功率、真实 cache usage 和兜底总成本。

[OpenCode #42506](https://github.com/anomalyco/opencode/pull/42506)和
[#46369](https://github.com/anomalyco/opencode/pull/46369)可以继续跟踪，但本轮仍未合并。
Pi、Cline 当前 agentic compaction 和本轮核对的 Kimi CLI SimpleCompaction 使用独立
摘要输入，不纳入已复用主会话热前缀的正例；Claude Code 官方文章可作设计参考，
其运行时源码不作为本轮开源实现的证据。

源码快照存于 `artifacts/compaction-cache-implementations-20260911/`；
[来源清单](../data/cache-reusing-compaction-sources.json)记录固定版本、哈希和证据等级。
