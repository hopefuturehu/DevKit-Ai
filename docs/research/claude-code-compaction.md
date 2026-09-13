# Claude Code 压缩机制：请求复用、上下文重建与失败恢复

核对日期：2026-09-09。本机 `claude --version` 为 **2.1.197**，安装物是 macOS arm64
原生可执行文件。本轮依据官方行为文档、团队工程说明和公开 CHANGELOG 研究，没有取得
可逐函数核对的当前 CLI 压缩源码，也没有运行新的付费压缩实验。

版本记录固定在官方仓库
[`347b38e4a733d95b2f00690a4ca58ac1544f8a1c`](https://github.com/anthropics/claude-code/blob/347b38e4a733d95b2f00690a4ca58ac1544f8a1c/CHANGELOG.md)。
在线文档描述的版本可能高于本机版本；以下明确区分文档行为、版本变更与待验证细节。
本文是[开源框架对比](context-framework-comparison.md)的官方资料补充，不列入源码审计结果。

## 1. 整体流程与摘要请求

官方说明的流程是：先清理旧工具输出，仍有需要时生成会话摘要，再用摘要继续任务。
用户要求和重要代码信息是摘要保留重点，但早期细节仍可能丢失。
可用 `CLAUDE.md` 中的 `Compact Instructions` 或 `/compact` 后的自定义指令指定重点。
见[上下文管理说明](https://code.claude.com/docs/en/how-claude-code-works#when-context-fills-up)。

团队工程文章进一步公开了摘要调用的结构。下面是逻辑示意，不是抓取的生产请求：

```text
summary_request:
  system = 主会话的 system / session context
  tools = 主会话相同的工具定义
  messages = 当前会话的历史消息
           + 一条放在末尾的 user 摘要指令

后续主请求:
  system / tools + 重建后的项目上下文
  + 摘要及必要恢复内容 + 新消息
```

保持主会话的前缀，使摘要调用可以读取已存在的 prompt cache。文章也明确要求保留
compaction buffer，容纳追加指令和摘要输出。它没有公开这段 buffer 的统一固定数值。
见[Claude Code 团队工程说明](https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything)。

这里的“历史”是当前活动上下文，不能理解为从磁盘把所有历代原始消息重新读入。
清理旧工具输出会改变可用素材；已有摘要边界也影响活动范围。具体清理规则、完整内置
摘要 prompt、每种压缩入口的保留尾部规则，不能仅凭上述结构说明还原。

压缩生成与压缩后的下一轮缓存效果不同：生成摘要时可复用热缓存；用摘要替换历史后，
conversation 层需要重新建缓存。system 可复用，重新加载的项目内容需保持相同才命中。
缓存过期后再压缩，则需要重新处理原有输入。
见[压缩与缓存](https://code.claude.com/docs/en/prompt-caching#compacting-the-conversation)。

## 2. 原文上限、触发阈值与摘要长度

**没有查到可证实的、类似本项目额外 60K 的统一摘要输入上限；这不等于压缩输入无限。**
它仍受目标模型窗口、完整请求大小及输出空间约束，缓存命中只减少重复计算成本。

必须分清以下量：

| 量 | 官方可核对的行为 |
|---|---|
| 模型窗口 | 随模型、部署方式及配置而变 |
| auto-compact window | 当前文档允许通过 `/autocompact`、启动参数或配置调整；环境变量 `CLAUDE_CODE_AUTO_COMPACT_WINDOW` 优先级更高 |
| 百分比覆盖 | `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` 只能让适用的提前压缩路径更早触发，不能提高默认百分比；并非所有路径都适用 |
| 输出预算 | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 随模型有默认值和上限；调大后会减少压缩前可用空间 |

窗口设置范围当前为 100K–1M，并受模型实际窗口约束；环境变量只接受纯整数。
状态栏 `used_percentage` 以完整模型窗口为分母，可能与设定的压缩窗口不同。
见[模型与压缩配置](https://code.claude.com/docs/en/model-config#context-window-and-auto-compaction)、
[环境变量](https://code.claude.com/docs/en/env-vars)。

不应写成“所有 Claude Code 固定到 95% 就压缩”。默认行为按模型和版本变化：例如
CHANGELOG 的 2.1.247 将 Sonnet 5 的 1M 场景从约 934K 调到约 967K；2.1.260 又调整
Opus/Fable 的近 1M 触发行为。在线模型文档的概括不能替代具体版本和请求 trace。

CHANGELOG 2.1.69 已修复压缩忽略 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 的问题，因此也不能
假定摘要输出永远硬编码为 8K、16K 或固定几 K。当前资料没有给出所有路径统一的摘要正文
目标长度、标题校验器或 `max_tokens` 截断后的完整重试算法。

## 3. 压缩之后为何能继续工作

摘要不是压缩后唯一的内容。官方列出的恢复机制包括：

| 内容 | 压缩后的处理 |
|---|---|
| System / output style | 不属于被替换的消息历史 |
| 根目录 CLAUDE.md、无路径条件规则、auto memory、计划文件 | 从磁盘重新注入 |
| 读过或修改过的文件 | 最多重读 5 个，优先最近修改；超过 5K tokens 的文件改为路径引用 |
| 路径规则、子目录 CLAUDE.md | 随相关文件重新加载；不能保证所有曾读规则都常驻 |
| 已调用 skill 正文 | 单个最多 5K、合计最多 25K tokens，超预算优先丢较旧项 |
| Hook 上下文 | 旧输出参与摘要；`SessionStart` 的 `compact` hook 可追加新上下文 |

来源：[压缩后的内容保留规则](https://code.claude.com/docs/en/context-window#what-survives-compaction)。
这些限制是**压缩后的恢复预算**，不是摘要原文输入上限。

因此，“200K/1M 历史变成短摘要”不能直接推出“下一轮完整输入只有几 K”。下一轮还包括
system、工具、持久指令、恢复文件和新输出。较大的固定内容或重复读取足以让窗口重新升高。

另外，`/rewind` 的两种定向摘要可以分别压缩选点之后或之前的会话；后者保留后续消息。
原始消息仍留在会话 transcript，活动上下文替换不等于物理删除。
见[定向摘要与回退](https://code.claude.com/docs/en/checkpointing#rewind-and-summarize)。

## 4. Thinking：输入、生成、回放要分开判断

1. **摘要生成是否开启 thinking**：官方明确从 **2.1.198** 开始继承会话 extended thinking
   配置。本机 2.1.197 不能直接套用该结论。开启 thinking 本身也不保证不会输出截断。
   来源：[压缩与 thinking](https://code.claude.com/docs/en/context-window#what-survives-compaction)。
2. **旧 thinking 是否成为摘要输入**：官方说明复用历史，但是否包含每个原生 block，仍需检查
   出站请求及 Provider 处理。API 当前对 Opus 4.5+、Sonnet 4.6+ 默认保留更早 thinking；一些
   旧模型及 Haiku 则只保留最后一轮，较旧部分即使回传也会被服务端移除。
3. **生成的新 thinking 是否回放到摘要后的主会话**：公开的请求结构和“继承 thinking 设置”
   都不能证明这一点。必须核对摘要响应与后续主请求，不能把“生成过”写成“发布过”。

还有一个容易误判的地方：Anthropic API 的 `thinking: ""` 可以与有效的 opaque `signature`
一起出现；在 `display: "omitted"` 模式下，服务端可用该签名恢复原思考内容。它不能等同于
本项目给 DeepSeek 人工补一个空 `reasoning_content`，也不能从可读文本为空断言没有推理输入。
来源：[API thinking 保留与签名](https://platform.claude.com/docs/en/build-with-claude/thinking)。

## 5. 超限与失败恢复：已有机制及边界

| 场景 | 可验证的处理 |
|---|---|
| 摘要请求自身放不下 | 官方仍列有 `Error during compaction: Conversation too long`；建议回退几轮后重试，必要时新建会话 |
| 主请求先遭 context overflow | 有 reactive compaction；2.1.142 起首次摘要尝试会参考原请求超限量，减少一次接近满窗口的无效重试 |
| 自动压缩连续失败 | 2.1.76 的版本记录加入 3 次尝试后的熔断 |
| 摘要成功，但大输出立即反复填满窗口 | 2.1.89 的版本记录加入连续 3 次的 thrashing 检测，终止无进展循环 |
| 只有一个 exchange，主要是大输入/固定头部 | 官方说明没有更早轮次可摘要，会跳过压缩并提示缩小输入 |

来源：[固定版本记录](https://github.com/anthropics/claude-code/blob/347b38e4a733d95b2f00690a4ca58ac1544f8a1c/CHANGELOG.md)、
[压缩及请求错误](https://code.claude.com/docs/en/errors#error-during-compaction-conversation-too-long)。
这些发布记录证明相应恢复能力曾加入，不足以还原当前每一条分支的重试次数与切分算法。
尤其不能据此声称它会对任意超长原文递归分块、完整覆盖后再合并摘要。

thrashing 与“摘要失败”应分开计量：前者可能每次都生成了摘要，却没有稳定腾出任务空间。
官方恢复建议是限制文件读取范围、让 `/compact` 只保留计划和差异、隔离大文件工作，或清空
不再相关的上下文。见[无进展压缩循环](https://code.claude.com/docs/en/troubleshooting#auto-compaction-stops-with-a-thrashing-error)。

## 6. 不要把 Claude Code 与 API compaction 混为一谈

Anthropic Messages API 另有服务端 `compact_20260112`，当前文档默认输入触发值 150K、
最低 50K，返回 `compaction` block，后续以该块替代之前的内容，并支持压缩后暂停和自定义
摘要指令。它是另一层公开接口；这些默认值不能当成 Claude Code CLI 的实现常数。
同样，SDK 自带的客户端摘要示例也不能替代 Claude Code 的实现证据。
见[服务端 Compaction API](https://platform.claude.com/docs/en/build-with-claude/compaction)。

## 7. 对本项目的改进启示与验证方法

与[本项目及开源实现的输入预算](oversized-context-compaction.md)相比，Claude Code 的
摘要调用结构更接近 DeepSeek Harness：复用主请求前缀，再追加摘要指令。本项目则使用
独立 system 和序列化历史 JSON，默认不带 tools，另设 60K 输入预算、48K 规划目标。

值得验证的改动有三项，以下均为建议，尚未修改运行代码：

1. **增加可选的同前缀摘要路径**。同模型、协议合法且完整请求能放下时复用 system/tools/历史；
   不适用时继续增量摘要。预期收益是减少摘要输入的缓存未命中，不能靠它解决输入超限。
2. **将恢复内容纳入压缩后的压力判定**。对活动目标、计划和必要文件采用有界恢复；重组完整
   主请求后再判断成功，避免只看摘要正文长度。当前已有的锚点机制应纳入同一计量。
3. **区分生成失败和无进展成功**。前者按错误类型有限重试；后者先定位固定头部、恢复内容或
   巨型工具输出。若来源没有前进且压力没有下降，应停止重复压缩并改变输入形态。

Claude Code 的验证可利用官方 `PreCompact` / `PostCompact` hooks：前者提供触发类型和
手动指令，后者提供 `compact_summary`；它们不等于最终出站请求，也不能用 PostCompact
修改已发布结果。重新注入内容使用 `SessionStart` 的 `compact` 入口。
见[Hooks 参考](https://code.claude.com/docs/en/hooks#postcompact)。

建议按一次压缩关联以下记录，再做低成本、固定版本的对照实验：

- **覆盖**：压缩前活动消息 ID、摘要实际输入 ID、替换范围，以及 reasoning block 类型与签名
  是否保留。日志有 block 不等于模型看到了它；需结合 Provider 的保留规则。
- **容量**：前后完整主请求 tokens、摘要正文 tokens、恢复内容 tokens。`preTokens` 是压缩前
  用量指标，不是摘要请求实际 input usage，也不是原文覆盖率。
- **成本**：独立记录摘要调用的 input、cache read、cache creation、output、耗时和失败重试。
  主会话命中率不能代替摘要调用命中率。
- **质量和续跑**：在历史首/中/尾放置需保留事实、已否定方案和最新约束，检查压缩后问答及
  实际任务结果；同时记录首次后续请求成功率、再次触发压缩的间隔和 thrashing 次数。

`compact_boundary` / `compactMetadata.preTokens` 的官方日志示例见
[子 Agent 压缩记录](https://code.claude.com/docs/en/sub-agents#auto-compaction)。
本轮未采集 Claude Code 实际压缩请求，不能给出它在这批 18 个任务上的命中率、压缩率或
成功率；此前[真实测试数字](../evaluations/public-benchmark-context-analysis.md)仍只属于已测的 `bot` 运行。
