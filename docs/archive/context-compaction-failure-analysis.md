# 长会话上下文压缩问题：原因、案例、方案与取舍

> 状态：历史故障分析与实施记录。第 1–15 节为修复前分析与候选方案；推荐方案已于
> 2026-08-25 实施并通过推广门禁，当时的行为与测试结果见第 16 节
>
> 日期：2026-08-18；缓存命中率补充：2026-08-19；落地验证补充：2026-08-25
>
> 代码基线：`f78d911 fix(context): bound and recover compaction backlog`
> 关联设计：[recoverable-context-compaction.md](../recoverable-context-compaction.md)

> 2026-08-31 后续：第 16 节中的“至少三轮 user”也是当时的历史方案。当前实现已把它改为
> 强制路径的预算内停止条件（收集到 3 条 user 即停，否则在下一组会使 tail 超过 20K 时停止），
> 并支持原始 user 锚点回放与 Assistant-safe 超大单轮切分；当前行为以关联设计文档为准。

除第 16 节外，文中的“当前机制”“当前配置”和“待决策”都指 `f78d911` 历史基线，不是当前
仓库默认值。当前请求组装与硬上限动作见
[模型上下文分块与组装顺序](../context-assembly.md)，当前压缩状态机见
[可恢复的单摘要上下文压缩](../recoverable-context-compaction.md)。

## 1. 摘要

本次问题不是单一的“压缩失败”，而是三层问题连续叠加：

1. 中断的 Tool Call/Result 使安全压缩边界长期停在旧游标附近；
2. 边界恢复后，旧实现把数百条积压消息一次性送入摘要模型，输入达到 15 万至 17 万
   token；
3. 输入分块修复后，摘要输出目标仍为 8,000 token，而 API 硬上限只有 8,192 token，
   且格式修复继续使用相同上限，导致“生成写满 → 修复再次写满 → 缩小范围重来”。

持久化本身没有失效。`resume` 能加载最近的 `ready` 摘要；失败版本不会推进游标，也不会
删除原始 Transcript。用户看到的“卡住”主要来自 `/compact` 同步追赶多个分块、单个分块
可能包含两次模型调用、CLI 又没有逐块进度显示。

在真实会话 `f561a46eb09d480496a2743475883ca9` 中，一次显式 `/compact` 最终成功将游标
从 104 推进到 599，但代价是：

| 指标 | 实际值 |
|---|---:|
| 压缩计划记录 | 11 |
| 基础摘要调用 | 11 |
| 修复调用 | 8 |
| LLM 调用总数 | 19 |
| 累计输入 | 441,252 token |
| 累计输出 | 122,332 token |
| 模型调用总时长 | 1,765.1 秒（约 29 分 25 秒） |
| 最终活动摘要 | 1,912 token |
| 按当前本地价格配置估算 | 约 `$0.686`，不代表 Provider 实际账单 |

这说明当前主要矛盾已经从“输入是否有界”转为“怎样用可预测的调用次数，稳定地产出一份
短而可恢复的摘要”。

## 2. 当前机制

当前运行时使用单一活动摘要：

```text
Core / Project / Skills / Memory
              +
       一个 ready 摘要
              +
       cursor 之后的原始消息
```

每个压缩版本保存在 `context_compactions` 中，并记录：

- 覆盖范围 `covered_start_position` / `covered_end_position`；
- 增量起点 `delta_start_position`；
- 父版本 `parent_id`；
- 原始消息摘要 `source_sha256`；
- 摘要引用、锚点、Token 使用量和状态；
- `building → ready → superseded` 或 `building → failed` 生命周期。

正常增量请求是：

```text
上一份 ready 摘要 + cursor 之后的新原始消息 → 新的完整滚动摘要
```

当前相关默认配置为：

```toml
[context]
compaction_summary_tokens = 8000
compaction_max_output_tokens = 8192
compaction_max_input_tokens = 60000
compaction_input_target_ratio = 0.8
compaction_repair_attempts = 1
compaction_range_attempts = 2
compaction_failure_backoff_seconds = 300
compaction_max_message_chars = 12000
compaction_rebuild_every = 5
```

因此压缩输入的规划目标是约 48K token，硬限制是 60K；摘要可见正文要求不超过 8K，
模型 completion 上限为 8,192。

## 3. 事件时间线

### 3.1 初始成功版本

2026-08-13，版本 `7655c6e788f2` 成功覆盖消息 1–104：

| 输入 | 输出 | 落库摘要 | 耗时 |
|---:|---:|---:|---:|
| 46,588 | 5,525 | 2,288 | 60.8 秒 |

此版本一直是后续失败期间的恢复基线。退出进程或执行 `resume` 不会让它丢失，也不需要重新
压缩 1–104。

### 3.2 Tool 协议边界阻塞

旧游标之后存在中断运行遗留的 Tool Result/Tool Call 不完整关系。安全边界逻辑无法确认这些
消息是否属于仍在运行的 Tool 原子组，因此压缩边界长期不能稳定推进。

提交 `7e7b9da fix(context): recover compaction past interrupted tools` 增加了对已终止或遗留
运行的逻辑闭合：原始 Transcript 不被伪造或改写，但压缩视图可以用
`interrupted_result_unknown` 表达无法恢复的结果，从而越过旧边界。

### 3.3 边界恢复后暴露无界 backlog

边界恢复后，压缩器直接追赶几百条积压消息：

| 版本 | 覆盖范围 | 实际输入 | 实际输出 | 失败原因 |
|---|---:|---:|---:|---|
| `7bc66c2b475d` | 1–512 | 159,646 | 8,000 | 缺少 `Next Steps`、`Critical Context` |
| `4ea964259181` | 1–527 | 167,229 | 7,246 | 存在无来源列表条目 |
| `64a3f9a5f2e7` | 1–527 | 0 | 0 | 模型没有返回摘要 |
| `cee54d0f6b3d` | 1–531 | 0 | 0 | 重启后回收为 `recovered_stale_build` |

前两个调用已经接近或超过许多模型的有效上下文范围。即使 Provider 接受请求，模型也很难
同时完成信息筛选、固定章节和逐条引用。

### 3.4 输入有界与失败恢复修复

提交 `f78d911 fix(context): bound and recover compaction backlog` 引入：

- 按最旧完整 Tool 原子组对 backlog 分块；
- 48K 规划目标和 60K 输入硬限制；
- 候选摘要的小型修复请求；
- 修复失败后的范围缩小；
- 持久化失败退避；
- 失败调用 usage 的持久化；
- `/compact` 多分块追赶和费用累计。

离线规划显示，原来计划一次处理 105–527 的请求，被拆成首块 105–231，估算输入 47,619，
不再产生 16 万 token 的单次压缩输入。

### 3.5 新机制下的显式 `/compact`

重启新进程后，`resume` 正确加载游标 104。用户执行 `/compact`，目标为消息 599。

关键记录如下：

| 版本 | 增量范围 | 状态 | 累计输出 | 落库摘要 | 耗时 | 说明 |
|---|---:|---|---:|---:|---:|---|
| `7617807a7c47` | 105–231 | failed | 16,384 | 0 | 230.8s | 生成和修复均达到长度上限 |
| `54c6f89ffe31` | 105–171 | success | 14,459 | 2,587 | 199.9s | 缩小范围后修复成功 |
| `a6f8736fdb8c` | 172–319 | success | 7,990 | 2,882 | 128.6s | 无修复 |
| `95c13c3b431d` | 320–465 | success | 12,468 | 385 | 203.1s | 修复后极度收缩 |
| `a082bcaa807d` | 466–556 | failed | 9,371 | 0 | 156.7s | 没有消息来源引用 |
| `4787eeed504b` | 466–507 | success | 8,567 | 1,860 | 122.4s | 缩小范围后成功 |
| `a547291da1a8` | 508–579 | failed | 8,844 | 0 | 127.8s | 没有消息来源引用 |
| `47c841d22ac8` | 508–539 | success | 13,812 | 2,168 | 174.0s | 缩小范围后修复成功 |
| `9af941a99aa1` | 540–599 | failed | 16,385 | 0 | 222.3s | 再次达到长度上限 |
| `e62f4be790dd` | 540–570 | success | 6,829 | 3,038 | 93.5s | 缩小范围后成功 |
| `61ccbbc5d361` | 571–599 | ready | 7,223 | 1,912 | 105.9s | 最终活动版本 |

`superseded` 表示曾成功发布、后来被更新版本取代，不表示失败。

## 4. 根因分析

### 4.1 根因一：安全边界与中断状态耦合

Tool Call 和 Tool Result 必须作为原子组保存，否则主模型可能看到无法满足协议的历史。旧逻辑
把中断运行留下的缺失结果当作仍然活跃的阻塞，导致 cursor 后积压不断增长。

此问题已由 `7e7b9da` 解决。解决方式的代价是：压缩源中会出现
`interrupted_result_unknown` 这样的派生逻辑闭合，因此摘要必须明确区分“真实 Tool 结果”和
“中断后未知”。

### 4.2 根因二：旧压缩输入没有 backlog 上界

从 105 到 527 的整个增量被放进同一个请求，实际输入达到 167,229 token。大输入会同时造成：

- Provider 上下文溢出或响应时间变长；
- 模型难以遵守固定输出结构；
- 输出更容易写满；
- 一次失败损失整次大调用成本；
- 重试重复支付大部分输入费用。

此问题已由 `f78d911` 的分块规划解决。当前 48K 目标、60K 上限是合理的第一层防线，暂不
建议回退。

### 4.3 根因三：摘要软目标与硬上限几乎相同

提示告诉模型目标为 8,000 token，而 API `max_tokens` 是 8,192，只留下 192 token、约 2.4%
余量。该余量不足以吸收：

- 模型长度服从误差；
- Provider tokenizer 与本地估算器差异；
- Markdown 标题和引用开销；
- 推理模型可能计入 completion usage 的 reasoning token；
- 增量更新中旧摘要与新事实的合并波动。

目标值接近硬上限时，“Target ~8000”实际会鼓励模型充分使用整个窗口，而不是主动收敛到一份
短摘要。

### 4.4 根因四：长度错误走了普通格式修复

当前校验流程为：

```text
生成候选
  → 校验失败
  → 候选摘要 + 错误信息进入修复请求
  → 修复仍失败时缩小原文范围
```

这一流程适合“缺少标题”或“引用格式错误”，不适合 `finish_reason=length`。长度截断候选通常
已接近 8K，且可能在中间章节截断；修复请求仍使用 8,000 目标和 8,192 上限，容易完整重写后
再次达到同一个边界。

`7617807a7c47` 的 16,384 输出正是两次 8,192 累加。它不是无限循环，因为范围尝试和修复
次数都有上限，但会形成高成本、长延迟的有限循环。

### 4.5 根因五：严格引用合同增加长度和脆弱性

摘要要求八个固定章节，且每个事实列表项必须携带 `[m:N]` 或 `[m:N-M]`。好处是便于导航
原文；代价是：

- 每个主条目和子条目都有额外 token；
- 漏掉一个引用会让整个版本失败；
- 修复提示要求无法安全补引用时删除条目；
- 增量摘要越长，引用范围越多，格式错误概率越高。

更重要的是，当前校验只验证引用格式和位置范围，不验证某条消息是否真的语义支持该事实。
因此它提供的是“原文导航指针”，不是严格的事实证明。

`95c13c3b431d` 为这一取舍提供了反例：模型累计输出 12,468 token，修复后落库摘要只有
385 token。它形式上通过了标题和引用校验，但过度删除可能造成语义信息损失。

### 4.6 根因六：所有失败共享范围缩小策略

当前 `_compact_plan()` 内的异常最终都表现为 `compaction_failed`，外层通常尝试缩小范围。
但失败原因需要不同处理：

- 上下文溢出适合缩小输入；
- 网络超时适合同范围重试；
- HTTP 402/401/403 应立即停止；
- 输出截断适合凝练候选；
- 缺章节适合轻量格式修复；
- SQLite 持久化失败不应再次调用模型。

统一缩小范围会把与输入大小无关的错误转化为新的模型费用。

### 4.7 根因七：同步批量追赶缺少进度和命令级预算

`/compact` 会同步循环处理目标游标之前的所有分块，最多 100 块。CLI 只在整个命令返回时打印
一次结果，因此用户在 20–30 分钟内看不到：

- 正在处理哪一块；
- 是否进入修复；
- 是否缩小范围；
- 已经成功推进到哪个 cursor；
- 已消耗多少 Token、费用和时间。

本次进程一直存活，HTTPS 连接也保持 `ESTABLISHED`，但交互上与死锁没有区别。当前配置未启用
`agent.max_cost_usd`，显式压缩也没有独立的请求数和墙钟预算。

### 4.8 关联影响：Provider 前缀缓存命中率大幅降低

历史排查中，DeepSeek 控制台显示的 Prompt Cache 命中率一度只有约 10%。这不是
`TokenEstimator._cache` 的本地哈希缓存，而是 Provider 返回的前缀/KV Cache 指标。原始数据
其实已经随 `model.usage` 事件落库：

```json
{
  "prompt_tokens": 98500,
  "prompt_cache_hit_tokens": 6528,
  "prompt_cache_miss_tokens": 91972
}
```

框架当时没有把这些字段归一化为正式指标，也没有查询命令；调查使用
`turn_usage.prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens` 离线计算。代表性结果为：

| Run | 调用数 | Prompt token | Cache hit token | Token 加权命中率 |
|---|---:|---:|---:|---:|
| `8c2991b8faa0` | 21 | 2,044,299 | 124,800 | 6.1% |
| `53b6f551b33e` | 6 | 573,373 | 39,552 | 6.9% |
| `7376c04d24bf` | 5 | 191,420 | 158,336 | 82.7% |

前两个低命中 Run 中，单次请求通常约 95K–99K token，但预热后每次只命中约
6.5K–6.8K，说明 Provider 能复用的基本只有固定 System/项目上下文和少量稳定前缀；体积最大的
对话部分从很靠前的位置就已经变化。第三个 Run 说明相同 Provider 和模型在前缀稳定时可以达到
高命中率，因此问题主要来自请求形态，而不是 DeepSeek 缓存整体失效。

造成这一结果的代码链路有四层。

#### 4.8.1 压缩游标停滞使主循环长期进入“滑动装箱”

活动摘要只覆盖消息 1–104，而缓存调查已经发生在 570 之后。由于压缩边界被中断 Tool Call
阻塞，随后又反复生成无效摘要，`cursor` 后积累了数百条原始消息。每次调用都会超过
`target_input_limit`，但 Compaction 又不能稳定推进，于是只能依赖 `ContextPlanner.pack()`
临时裁剪后继续调用主模型。

这解释了为什么“压缩只成功过一次”和“缓存命中率突然降到约 10%”同时出现：前者使请求从
正常的 append-only 对话退化成每一步都重新选择历史子集的请求。

#### 4.8.2 Planner 优先保证语义优先级和 Tool 原子性，不保证缓存前缀单调

`ContextPlanner.pack()` 先按 `priority`、再按消息新旧选择可选原子组，最后恢复时间顺序。用户
消息优先级高于 Assistant/Tool 消息；当预算被占满时，新加入一个组会挤掉另一个旧组。因此相邻
两步并不一定是：

```text
request N + 新消息 → request N+1
```

而更可能是：

```text
固定头 + 历史子集 A + 当前轮
固定头 + 历史子集 B + 下一轮
```

即使两次请求都接近 96K，Provider 也只能命中 `A`、`B` 首次分歧之前的约 6.5K 固定头。这个
策略对“不超过上下文上限”和“不拆 Tool Call/Result”是正确的，但在 Compaction 长期不可用时
会牺牲前缀缓存局部性。

#### 4.8.3 对话之前仍有会变化的上下文块和 Tool Schema

当前渲染顺序中，`ACTIVE_SKILL`、`RUNTIME_NOTE` 和 `COMPACTION` 都位于原始对话之前；Tool
schemas 还会因预算卸载、`activate_tools` 或 Skill 激活而改变。以下变化都会让其后的大段对话
失去缓存：

- 激活或卸载 Skill 正文；
- Progress/受管进程等 Runtime Note 出现、消失或换内容；
- 发布新 Compaction 后替换活动摘要；
- Tool schema 集合或顺序变化。

仓库此前已经做过两项针对性优化：`4b0741f` 将易变 Memory 移到对话之后，`377285e` 将受管
进程详情改成内容恒定的提醒。但这些修复没有覆盖 Planner 的滑动历史子集，也不能消除
Runtime Note 的有无变化、摘要替换和 Tool schema 变化。

成功 Compaction 替换一次摘要而使缓存重建是正常成本；异常之处在于压缩长期失败后，主请求
每一步都发生历史子集变化。

#### 4.8.4 控制台聚合混入不同 Prompt 家族，放大了低命中观感

同一个 API Key/模型还承载主 Agent、Compaction 生成、格式修复、范围缩小重试和 Memory
Extraction。它们的 System Prompt、User payload 和输入范围不同，不能相互复用完整前缀。本次
显式 `/compact` 就产生了 19 次 LLM 调用和 441,252 个输入 token，其中生成、修复和缩范围请求
大多是不同 Prompt 家族。

因此 Provider 控制台的全局命中率同时包含两部分：

1. 主 Agent 因滑动装箱产生的真实低命中；
2. 辅助模型调用和失败重试混入分母造成的聚合口径下降。

后者不是主请求本身的缓存退化，但会让 API Key 级别的总命中率更低。当前 `raw_usage` 虽然
保留了 DeepSeek 字段，框架仍缺少按 `phase`、`session_id`、`run_id`、模型和请求类型拆分的
命中率报表；仅看控制台总百分比，无法判断是主循环前缀不稳定，还是 Compaction/Memory 调用
占比上升。

综上，低命中率的首要修复不是增加一个本地缓存，而是先保证 Compaction 能推进，使主请求
恢复为“稳定摘要 + append-only 原始尾部”；其次才是让 Planner 在降级时保留稳定缓存锚点、
减少对话前的动态块，并将缓存指标按请求阶段分别统计。

## 5. 为什么不是持久化失效

压缩结果通过 SQLite 持久化。Resume 时：

1. 读取最新 `ready` 版本；
2. 重新计算覆盖原始消息的 SHA-256；
3. 校验摘要结构和引用；
4. 有效则直接恢复 cursor，不调用 LLM；
5. 无效则废弃该版本并沿父版本回退。

失败记录不会覆盖旧 `ready`。进程在摘要请求期间退出时，`building` 不会成为活动版本；超过
恢复阈值的遗留 `building` 会标记为 `recovered_stale_build`。

因此本次 `resume` 后从 104 继续，而不是重新压缩 1–104。成功分块也会逐块发布：如果用户在
游标 507 时中断，下一次仍可从 508 继续。

## 6. 当前摘要的典型形态

最终版本 `61ccbbc5d361` 覆盖 1–599，约 1,912 token。典型片段为：

```markdown
## Progress
- 已通过 grep 全仓库搜索缓存命中相关关键词 [m:571-575][m:577-585]：
  - `tests` 下 `cache` 仅命中
    `test_managed_process_runtime_reminder_is_cache_stable` [m:575][m:578]。
  - `命中率` / `hit_ratio` / `hit_rate` / `cache_hit`
    / `cache hit` 命中 0 处 [m:573][m:582][m:586]。
  - `prompt_cache` / `cached_tokens` / `cache_read`
    / `cache_creation` 无命中，grep 退出码 1 [m:579][m:586]。
```

这份最终摘要并不接近 8K。高消耗发生在被丢弃的初始候选、修复结果和 Provider 报告的其他
completion 开销中。因此不能通过“把最终摘要再裁短一点”单独解决问题；需要调整生成合同和
失败路径。

## 7. 本地开源实现对比（原始摘要合同调研）

> 本节保留 2026-08-24 为压缩失败分析所做的摘要合同调研。包含 OpenCode、reasoning 回传、
> 请求组装、长对话切分和最新源码快照的完整端到端对比，见
> [本地开源 Agent 框架上下文管理对比](../context-framework-comparison.md)。

本次只读分析了 `codespace` 中的以下 checkout：

- `openai/codex`；
- `badlogic/pi-mono`；
- `NousResearch/hermes-agent`；
- `deepseek-ai/deepseek-harness`；
- `HKUDS/nanobot`；
- `KAT` 中较早的 Nanobot 派生实现。

### 7.1 对比表

| 实现 | 固定结构 | 正文逐条引用 | 长度策略 | 原始尾部 | 溯源与失败策略 |
|---|---|---|---|---|---|
| Codex | 只规定内容，不强制精确标题 | 否 | 提示要求 concise，未见压缩专用正文目标 | 最多约 20K 用户消息 | Append-only rollout、replacement history、窗口 ID；上下文错误时裁旧输入 |
| Pi | 是 | 否 | 默认最大约 13.1K，未严格拒绝缺章节 | 最近约 20K | Append-only session tree、`parentId`、`firstKeptEntryId` |
| Hermes | 是 | 否 | 动态软目标 2K–10K；明确不发送 wire `max_tokens` | 保护近期尾部 | 旧消息软归档、可搜索；辅助模型失败可回退主模型或确定性摘要 |
| DeepSeek Harness | 是 | 否 | 默认硬上限 8,192；截断 fail closed | 窗口的 16% | `compactionId`、`shadowedRange`、精确 `shadowedSeqs`、append-only 事件 |
| Nanobot | 事实标记列表 | 否 | 持久历史摘要最多 8,000 字符 | 按预算保留 | LLM 失败 raw archive；普通游标归档保留 session 原消息，idle compact 较弱 |
| KAT | 与旧 Nanobot 类似 | 否 | 主要按最多 60 条消息分块 | 按用户轮次 | 较简单的 history/raw dump |

### 7.2 Codex：最简摘要合同，依靠原始用户消息兜底

Codex 的压缩提示只要求进度、决定、约束、剩余事项和关键数据，并要求简洁。它不校验固定
标题或来源引用；同时会在替换历史中保留最多约 20K token 的原始用户消息。

优点：

- 合同简单，格式失败率低；
- 用户原始意图不完全依赖摘要复述；
- append-only rollout 支持恢复和回滚。

代价：

- 摘要质量更依赖模型；
- 无法自动定位某条摘要事实的来源；
- 缺少严格格式门禁。

### 7.3 Pi：固定结构，但宽松接受

Pi 要求 Goal、Constraints、Progress、Key Decisions、Next Steps、Critical Context 等固定
结构，默认保留最近 20K token。其摘要最大输出约为
`min(0.8 × reserveTokens, model.maxTokens)`，默认约 13.1K。

优点：

- Handoff 结构稳定；
- 近期工作有原文兜底；
- session JSONL 树保留旧条目。

代价：

- 没有严格验证章节和语义覆盖；
- 达到长度边界时可能接受不够完整的内容；
- 摘要正文没有逐条证据。

### 7.4 Hermes：动态软目标与强生存性

Hermes 使用：

```text
summary_target = clamp(compressed_content × 20%, 2K, min(context × 5%, 10K))
```

并明确不向摘要调用发送 wire `max_tokens`。其注释记录了实践原因：Thinking 模型可能先消耗
输出额度，造成截断或只返回 reasoning，继而触发压缩循环。摘要输入本身限制为约 160K 字符
（约 40K token）。

优点：

- 避免软目标与硬上限贴合；
- 对 Thinking 模型友好；
- 辅助模型、主模型和确定性 fallback 提供多层生存性；
- 原始消息以 `active=0, compacted=1` 软归档，仍可搜索。

代价：

- 不设 wire 上限时最坏成本不够确定；
- 确定性 fallback 的语义质量较低；
- 实现复杂，状态与反抖逻辑较多。

### 7.5 DeepSeek Harness：结构化范围溯源与 fail closed

DeepSeek Harness 与当前实现最接近：默认摘要输出上限 8,192，达到 `max-tokens` 即拒绝
发布。但它不要求逐条引用，并保留窗口 16% 的原始尾部。

其 `compaction/summary` 事件记录：

- `compactionId`；
- `shadowedRange`；
- 每一条被替换事件的 `shadowedSeqs`；
- Token 数、模型和 usage；
- replacement message 的 `sourceEventSeqs`。

优点：

- 摘要正文与溯源元数据解耦；
- 原始范围可精确重放；
- 截断摘要不会进入活动上下文；
- 事务边界清晰。

代价：

- 只能快速定位摘要对应的原始范围，不能直接定位每个 bullet；
- 8,192 硬上限仍可能在 Thinking 模型上失败；
- 没有当前实现的逐条导航指针。

### 7.6 Nanobot/KAT：优先保证游标前进

Nanobot 把压缩更接近地定义为历史归档：LLM 摘要失败时保存有界原始文本并推进游标，避免
反复攻击同一批消息。它的生存性较强，但不是高保真的 Agent handoff。

优点：简单、成本可控、不易形成重试循环。

代价：摘要成功后的细粒度来源关系较弱，极端失败时可能只剩 raw breadcrumb。

## 8. 候选解决方案

以下方案均为候选设计，本文不决定立即实现。

### 8.1 方案 A：低风险止血

只修改输出与失败路径，不改变摘要存储模型：

- 摘要软目标降至 4K–6K；
- 可见正文硬限制保持 8K；
- wire 输出上限提高到 12K–16K，给 reasoning 和 tokenizer 差异留空间；
- `finish_reason=length` 不走普通格式修复，改为只输入候选的强制凝练；
- HTTP 402/401/403 立即失败，不缩小范围；
- 网络超时同范围重试一次；
- 只有上下文溢出才缩小原文范围；
- `/compact` 输出逐块进度，并设置请求数、费用和墙钟预算。

优点：改动面小，直接解决本次最高成本路径。

缺点：仍然没有近期原文尾部，逐条引用仍会造成格式脆弱；滚动摘要仍可能逐渐膨胀。

### 8.2 方案 B：推荐的混合方案

在方案 A 上增加：

1. 默认保留最近约 20K token 或至少 3 个真实用户轮次；
2. 保留固定章节，但取消每个列表项强制 `[m:N]`；
3. 使用 `covered_range`、精确 source positions、`source_sha256` 和版本链做结构化溯源；
4. 可选保存不注入模型上下文的章节级 evidence sidecar；
5. 正常失败继续 fail closed；只有上下文已无法继续时才发布确定性最小 checkpoint；
6. 摘要提示明确允许删除过时信息，而不是只做单调累加。

运行时形态变为：

```text
系统/项目上下文
      +
长期滚动摘要（目标 4K–6K）
      +
近期原始消息（约 20K，Tool 原子组完整）
```

优点：

- 当前任务由原始尾部兜底；
- 摘要可以更短；
- 格式失败和引用膨胀显著下降；
- 原始 Transcript、哈希和版本链仍提供恢复与审计。

代价：

- 压缩后上下文不会降到最小；
- 更早再次触发压缩；
- 失去正文逐条消息定位，只保留范围级或章节级溯源；
- 需要调整压缩边界、上下文装配和测试基线。

### 8.3 方案 C：分块摘要 + 长期 Rollup

为每个原始分块生成独立 delta summary，再合并到长期 rollup：

```text
消息 105–231 → delta A（约 1.5K）
消息 232–380 → delta B（约 1.5K）
旧 rollup + A + B → 新 rollup（约 4K）
```

每个 delta 都保留自己的来源范围。长期摘要丢失细节时，可以先加载 delta，再读取原始
Transcript。

优点：来源层级清晰、长期扩展性最好、按需恢复粒度更细。

缺点：通常每块需要两次模型调用；Schema、恢复、回滚和迁移复杂；Rollup 本身仍可能漂移。

不建议作为第一阶段。

## 9. 推荐参数草案

若后续选择方案 B，可从以下配置开始评测，而不是直接作为最终默认：

```toml
[context]
# 输入策略：保留 f78d911 已实现的有界分块
compaction_max_input_tokens = 60000
compaction_input_target_ratio = 0.8

# 输出策略：软目标、可见正文限制、wire 限制三者分离
compaction_summary_target_min_tokens = 2500
compaction_summary_target_max_tokens = 6000
compaction_summary_hard_tokens = 8000
compaction_max_output_tokens = 16384

# 原始尾部
compaction_recent_tail_tokens = 20000
compaction_min_recent_user_turns = 3

# 调用级恢复
compaction_format_repair_attempts = 1
compaction_transport_retries = 1
compaction_range_attempts = 2
compaction_failure_backoff_seconds = 300

# /compact 命令级预算
compaction_command_max_requests = 8
compaction_command_max_seconds = 600
# compaction_command_max_cost_usd = 0.25
```

动态软目标可使用：

```python
target = clamp(
    int((previous_summary_tokens + delta_tokens) * 0.12),
    min_value=2500,
    max_value=6000,
)
```

对独立非推理摘要模型，可以降低 wire 上限；对 Thinking 模型，也可评测 Hermes 的“不发送
wire `max_tokens`”策略，但必须配合命令级费用和墙钟预算。

## 10. 分类恢复流程草案

```text
开始压缩一个安全分块
  │
  ├─ HTTP 402/401/403 / 配置错误
  │    └─ 立即停止，长退避，不修复、不缩范围
  │
  ├─ 429/5xx/timeout/SSE 中断
  │    └─ 同范围短退避重试一次；仍失败则停止
  │
  ├─ context-window exceeded
  │    └─ Tool Result 预裁剪 → 缩小原子组范围 → 重试
  │
  ├─ finish_reason=length 或可见正文 > hard limit
  │    └─ 仅输入候选摘要，强制凝练到 3K–4K
  │
  ├─ 缺标题/多余围栏/格式问题
  │    └─ 本地规范化 → 必要时仅输入候选做一次格式修复
  │
  ├─ 摘要为空或继续原对话
  │    └─ 同模型一次重试或切换摘要模型；不发布坏结果
  │
  └─ SQLite/发布失败
       └─ 不再次调用模型，保留旧 ready，报告持久化错误
```

## 11. 溯源方案取舍

### 11.1 当前逐条引用

优点：模型和人可以直接从 bullet 跳到消息位置。

缺点：输出膨胀、格式脆弱、修复可能删除事实，且只验证引用存在，不验证语义支持。

### 11.2 范围级结构化溯源

压缩版本记录完整 source positions、范围和 SHA-256，摘要正文不带引用。

优点：摘要短、事务与恢复简单、类似 DeepSeek Harness。

缺点：核验单条事实时需要在原始范围内搜索。

### 11.3 章节级 Sidecar

摘要正文不带引用，数据库额外保存：

```json
{
  "Goal": [[105, 120]],
  "Progress": [[200, 260]],
  "Next Steps": [[480, 500]]
}
```

优点：在长度和导航之间折中，Sidecar 不注入后续上下文。

缺点：映射仍由模型生成，不能自动保证语义蕴含；需要额外 Schema 和校验。

推荐先采用范围级溯源，若真实使用证明逐条核验需求很高，再增加章节级 Sidecar。

## 12. 可观测性改进

当前 `output_tokens` 是一次计划内所有请求的累计值，无法直接区分初始生成、格式修复和凝练。
建议每次模型请求都记录独立事件：

```text
context.compaction.request.started
context.compaction.request.completed
context.compaction.request.failed
```

Payload 至少包括：

- `phase = generate | format_repair | condense | transport_retry`；
- `range_attempt`、`request_attempt`；
- `source_range`、planned/exact input；
- Provider `finish_reason`；
- Provider usage 与可见正文估算；
- duration、cost、error_class；
- 当前 cursor 和总目标。

CLI 可展示：

```text
[compact 1] 105–231 generating, input≈47K
[compact 1] output truncated; condensing candidate
[compact 1] completed, cursor=231, summary≈3.8K
[compact 2] 232–380 generating
```

这既解决“看起来卡住”的体验，也能让费用问题在请求继续前暴露。

## 13. 验证方案

### 13.1 单元测试

至少覆盖：

1. `finish_reason=length` 进入凝练，不进入普通格式修复；
2. 凝练只携带候选摘要，不重复发送完整原文；
3. HTTP 402/401/403 不重试；
4. 429/5xx/timeout 同范围重试，不缩小范围；
5. context-window exceeded 才缩小 Tool 原子组范围；
6. 缺标题优先本地规范化；
7. 最近 20K 尾部和最近 3 个用户轮次得到保护；
8. Tool Call/Result 不被尾部边界拆开；
9. 无正文引用时，范围、positions 和 SHA-256 仍可恢复原文；
10. 任意失败均不替换旧 `ready`；
11. Resume 直接加载有效 checkpoint，不调用 LLM；
12. 命令请求数、时间和费用预算能返回部分成功 cursor。

### 13.2 真实 Transcript 回放

使用本次 1–599 原始 Transcript，配合可复现的 Mock Provider 响应，至少比较：

- 当前 `f78d911` 基线；
- 方案 A；
- 方案 B。

核心指标：

| 指标 | 目标 |
|---|---|
| 单次摘要输入 | 不超过 60K |
| 正常摘要可见正文 | 2.5K–6K |
| 长度错误后的重复完整原文调用 | 0 |
| 最终恢复 cursor | 单调推进 |
| 旧 ready 在失败后 | 保持有效 |
| Resume 额外摘要调用 | 0 |
| 总调用数/输出 token | 显著低于本次 19 次/122K，具体阈值由回放基线确定 |
| 当前任务原始尾部 | 完整保留 |
| Tool 协议 | 始终平衡 |

### 13.3 摘要质量评测

不能只测格式通过。应从原始 Transcript 构造检查清单：

- 当前用户目标；
- 尚未完成事项；
- 明确约束和偏好；
- 关键文件和未提交改动；
- 已验证结果；
- 已知失败与阻塞；
- 下一步动作。

对每次压缩后的运行时上下文检查 recall，同时记录摘要中的无来源推断。范围级溯源保证可恢复，
质量评测负责判断摘要是否真正保留了继续任务所需的信息。

## 14. 分阶段实施建议

以下保留实施前的阶段计划；实际执行过程和结果见第 16 节。原建议顺序为：

### 阶段 1：可观测性与错误分类

- 增加每请求阶段事件和 CLI 进度；
- 分类 Provider/格式/长度/持久化错误；
- 增加 `/compact` 请求数、时间和费用预算；
- 不改变摘要格式和 cursor 语义。

风险最低，也最容易验证本次诊断。

### 阶段 2：输出收敛

- 分离软目标、可见硬限制和 wire 上限；
- 增加 length 专用凝练；
- 格式修复只处理候选；
- 允许摘要删除过时信息。

### 阶段 3：近期尾部与溯源调整

- 保留 20K 原始尾部和最少用户轮次；
- 评测取消逐条引用；
- 增加精确 source positions 或章节级 Sidecar；
- 更新恢复、回滚和上下文装配测试。

### 阶段 4：可选的分层摘要

只有真实长会话仍证明单一 rollup 不够稳定时，再实现 delta summary + rollup。

## 15. 当前结论与待决策项

已经确认：

- 压缩是持久化的，Resume 不会丢失有效 `ready`；
- Tool 中断边界和无界 backlog 已分别由 `7e7b9da`、`f78d911` 修复；
- 当前剩余核心问题是输出目标、修复路径、引用合同和 `/compact` 批处理体验；
- 最终摘要很短，但中间候选与修复造成了主要成本；
- 开源实现普遍用近期原文、append-only/软归档和范围元数据承担溯源，而不是在摘要正文逐条引用。

后续需要明确的产品取舍：

1. 是否接受从“逐条引用”降为“范围级/章节级溯源”；
2. 默认 `/compact` 是否保留约 20K 原始尾部；
3. 是否为摘要配置独立非推理模型；
4. 普通失败保持 fail closed，极端压力是否允许确定性 fallback；
5. 单次 `/compact` 默认允许消耗多少请求、时间和费用。

以上是实施前的待决策项。最终取舍、测试过程和推广结果记录在第 16 节；本文前 15 节继续作为
问题诊断与方案选择的历史基线。

## 16. 实施与验证记录（2026-08-25）

推荐的混合方案已在提交 `cff6338` 中落地。可复用的评测入口、产物格式和后续运行方法见
[上下文压缩有效性评测](../context-compaction-effectiveness.md)。本节补充实际执行过程，包括没有
通过的中间方案和为此做出的修正，避免只保留最终成功数字。

### 16.1 被验证的最终配置

| 项目 | 最终值或行为 |
|---|---|
| 压缩模型 | `compaction_model` 显式值；为空时冻结进程启动时的 `model.name`，不跟随 `/model` |
| 近期原文 | 20K token，并至少保留最近 3 个用户轮次及完整 Tool 原子组 |
| 摘要预算 | 3K 软目标、4K 可见正文硬限制、8,192 wire 输出上限 |
| 溯源 | 默认连续覆盖范围、SHA-256 和初始目标 anchor position；不再要求正文逐条 `[m:N]`，`source_refs_json` 默认可为空 |
| 输出恢复 | `length` 只凝练候选；`format` 只修复候选，不重发完整原文 |
| Provider 恢复 | 429/5xx/timeout/transport 同范围有限重试；仅 Context overflow 缩小范围 |
| Fail closed | authentication/payment/configuration/persistence 立即停止，cursor 不推进 |
| 预算 | 单请求 90 秒；单命令最多 8 请求、600 秒、默认 `$0.25`（需配置单价） |
| DeepSeek 思考 | 官方端点仅对压缩请求发送 `thinking.type=disabled`；普通 Agent 请求不变 |

### 16.2 测试负载、评分方法与产物

离线和真实 Provider 使用同一个确定性合成 Transcript：12 个用户轮次，每轮包含用户消息、
Assistant Tool Call、14,000 字符 Tool Result 和 Assistant 完成消息，共 48 条消息。每个用户轮次
携带一个从 `F01` 到 `F12` 的 `[EVAL_FACT:...]` 稳定事实标记。

`hybrid-20k` 在该负载上压缩位置 1–27，保留 21 条近期原文和 5 个用户轮次。保留数量大于最低
3 轮，是 20K token 预算与 Tool 原子边界共同计算的结果。每次回放同时检查：

- 12 个关键事实全部可见，摘要不得产生输入中不存在的数字事实 ID；
- Transcript SHA-256 前后不变，cursor 只单调推进；
- 最多一份活动摘要，失败不替换旧 `ready`；
- 输入不超过规划上限，近期 Tool Call/Result 始终平衡；
- 最近用户轮次满足下限，长度或格式恢复不得再次发送完整原文。

请求事件写入 `requests.jsonl`，包含阶段、范围、模型、思考模式、usage、可见摘要 token、
reasoning 字符数、耗时、费用和错误分类；`summary.json` 保存质量门禁与 cursor 结果。产物不保存
原始 prompt。本次真实产物位于临时目录 `/private/tmp/bot-compaction-live-confirmation`，未提交到
仓库。

### 16.3 离线故障矩阵

先使用确定性 Provider 运行单元与集成回放：

```bash
.venv/bin/pytest -q tests/unit/test_context_compaction.py
.venv/bin/pytest -q tests/integration/test_compaction_effectiveness.py

.venv/bin/python scripts/run_compaction_effectiveness.py \
  --provider scripted \
  --variant hybrid-20k \
  --scenario length \
  --output /private/tmp/bot-compaction-scripted-final
```

六类故障均得到预期分流：

| 场景 | 请求序列 | 结果 |
|---|---|---|
| `success` | generate | 1 次请求发布摘要 |
| `length` | generate → condense | 2 次请求成功；condense 只携带候选 |
| `format` | generate → format repair | 2 次请求成功；repair 只携带候选 |
| `rate-limit` | generate → 同范围 transport retry | 2 次请求成功；没有缩小范围 |
| `context-overflow` | generate → 缩小 Tool 原子范围后 generate | 2 次请求成功；这是唯一缩范围场景 |
| `authentication` | generate | 1 次请求失败；不重试，cursor 保持 0 |

额外单元测试覆盖了 repair 响应再次遇到 `length` 时切换到 condense、请求超时、费用耗尽、
SQLite 发布失败、模型冻结、范围溯源和损坏恢复。

### 16.4 真实 Provider 探索过程

真实请求使用 DeepSeek V4 Flash。第一次从受限 sandbox 发起的调用在到达 Provider 前即发生
transport 失败，因此不计入模型成功率、延迟或费用；获得网络授权后才开始以下有效样本。先逐次
冒烟定位剩余变量，没有直接跳到 20 次确认：

| 步骤 | 配置与结果 | 得到的结论或修正 |
|---|---|---|
| A2a 严格逐条引用 | 2 请求，35.46s，`$0.031665`；生成与格式修复后仍因缺少消息引用失败 | 逐条引用仍是独立的高失败因素，改用范围级结构化溯源 |
| Hybrid，思考开启 | 1 请求，19.19s，`$0.016717`；发布成功但事实 recall 为 50% | 模型把 `F01`–`F07` 泛化成模式；提示词增加稳定事实标记必须逐条原样保留的规则 |
| 修正提示词后 | 1 请求，32.16s，`$0.021077`；摘要实际保留事实，但评分器把 `...`、`FXX` 示例当成事实 ID | 评分器改为只识别 `F` 加数字的 golden ID，排除格式占位符 |
| 修正评分器后 | 1 请求，32.98s，`$0.021387`；12/12 事实通过 | Provider 输入 12,411、completion 4,488、可见摘要 815 token，比值 5.51；剩余瓶颈是默认思考 |
| 关闭压缩思考 | 1 请求，5.48s，`$0.013498`；12/12 事实通过 | Provider 输入 12,332、completion 583、可见摘要 538 token，比值 1.084，`reasoning_chars=0` |

最后一步依据 [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 的
思考模式开关实现：官方端点默认思考开启，可通过 `thinking: {"type": "disabled"}` 关闭。
实现只在压缩请求上设置该字段，并为其他 OpenAI-compatible 端点保留 Provider 默认行为。

原计划还包括 A0、A1、A2a、A2b 各 3 次的完整因子筛选；实际执行在历史基线已经证明主模型
继承代价高、且 A2a 单次即暴露严格引用硬失败后，停止继续为已淘汰组合付费，转而验证包含全部
修正的 Hybrid。所以下述 20 次结果证明 Hybrid 满足绝对推广门禁，但不用于声称每个参数的独立
边际收益或 A0/A1/A2b 之间的统计排序。

### 16.5 20 次真实确认

单次关闭思考冒烟通过后，按费用上限执行最终确认：

```bash
RUN_CONTEXT_COMPACTION_LIVE=1 \
  .venv/bin/python scripts/run_compaction_effectiveness.py \
  --provider live \
  --variant hybrid-20k \
  --scenario success \
  --repeat 20 \
  --max-cost-usd 0.50 \
  --output /private/tmp/bot-compaction-live-confirmation
```

聚合结果如下：

| 指标 | 门禁 | 实际结果 |
|---|---:|---:|
| 成功率 | ≥95% | 20/20，100% |
| 质量门禁 | 全部通过 | 20/20；最低事实 recall 100% |
| 每次请求数 | 越少越好 | 固定 1；总计 20，无 repair/retry |
| p50 延迟 | ≤30s | 5.57s |
| p95 延迟 | ≤60s | 9.96s |
| 单次最大延迟 | ≤90s | 9.96s |
| completion / 可见摘要 p95 | ≤2 | 1.216 |
| Provider 输入 token | ≤60K | 每次 12,332 |
| completion token | 观察值 | 473–796，p50 681 |
| 可见摘要 token | 3K 软目标、4K 硬限制 | 443–675，p50 570 |
| reasoning 字符数 | 0 | 最大 0 |
| 总费用 | ≤`$0.50` | `$0.272528` |

因此 `hybrid-20k` 通过推广门禁，并成为默认策略。这里的延迟改进不能与历史 491.6 秒事件做
严格同分布比较：历史数据来自真实长会话和旧恢复链路，本次确认来自固定合成回放；它能直接
证明的是新策略在固定负载下有界、可重复且不依赖隐藏 reasoning 输出。

### 16.6 全量回归与缓存成本

实现完成后执行：

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
RUN_CONTEXT_CACHE_SOAK=1 .venv/bin/pytest -q tests/soak/test_context_cache_soak.py
.venv/bin/python scripts/run_context_cache_benchmark.py \
  --suite all \
  --variants current no-compaction \
  --output /private/tmp/bot-context-cache-documentation-final
git diff --check
```

最终结果为 214 个 pytest 用例中 213 通过、1 个默认跳过的 Soak 用例；显式开启后 Soak 通过。
Ruff 和 whitespace 检查通过。测试仅出现已有的 Starlette/httpx 弃用警告。

固定 seed 缓存基准结果：

| Suite | 当前压缩 cost/turn | 无压缩反事实 | 降幅 | 压缩结果 |
|---|---:|---:|---:|---|
| Fast | 5,109.7425 | 8,028.7950 | 36.36% | 5 次成功、0 失败；1 次 rebuild + 4 次 incremental |
| Soak | 19,178.3226 | 39,236.6089 | 51.12% | 7 次成功、0 失败；1 次 rebuild + 6 次 incremental |

两组都通过事实可见、稳定记忆可见、Transcript 不变、单活动摘要、零 Context limit 和零 Tool
协议修复门禁。原始 cache hit ratio 在无压缩反事实中更高，但它发送了远大得多的 prompt，因而
主判据使用 cache-adjusted `cost_per_logical_turn`。

全量回归还发现一次配置兼容问题：若把 3K 软目标直接设成字段值，旧测试中仅覆盖 2K 硬限制的
配置会变成“软目标大于硬限制”。最终改为未显式配置时动态使用
`min(3000, compaction_summary_tokens)`；用户显式配置冲突值时仍拒绝启动。修正后重新运行上述
全部门禁并通过。

### 16.7 结论与尚未覆盖的范围

本轮测试确认了错误分流、调用上界、事实保留、可恢复性、默认参数和 DeepSeek 延迟成本目标，
也确认默认 20K 尾部在固定负载上实际保留了 5 个完整用户轮次。尚未声称覆盖的是不同 Provider
对 `thinking` 扩展字段的兼容性、真实业务 Transcript 的语义多样性，以及跨版本模型漂移；这些
场景应继续使用脱敏回放，并以同一组请求级产物和质量门禁监控。
