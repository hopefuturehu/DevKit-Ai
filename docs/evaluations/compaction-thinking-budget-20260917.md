# 独立兜底摘要 thinking 预算对照实测

2026-09-17；生产代码基线 `627ef16`，实测脚本基线 `539aabc`。

**24 次真实 API 请求全部完成：现方案 0/8、关闭 thinking 0/8、额外预留 8K 后 8/8
通过摘要生成校验。第三组若同时要求正文 ≤8K，仅 3/8。** 不能把正常结束率当作
最终摘要长度达标率，更不能当作完整压缩发布或事实保真率。

执行时间：北京时间 2026-09-17 00:32:22–00:37:50。所有响应型号均为 `deepseek-flash`，
没有超时、网络或格式失败；共 16 次 `length`、8 次 `stop`。生产配置、摘要和游标未修改。
逐次统计、用量、输入与摘要哈希见[机器可读数据](../data/compaction-thinking-budget-20260917.json)。

## 结果

| 方案 | 正常结束且通过现有校验 | 同时正文 ≤8,192 | 平均 thinking | 平均正文 | 平均耗时 |
|---|---:|---:|---:|---:|---:|
| 现方案，共享 8K | 0/8（0%） | 0/8（0%） | 6,373 | 1,819 | 36.6 秒 |
| 关闭 thinking，共享 8K | 0/8（0%） | 0/8（0%） | 0 | 8,192 | 30.1 秒 |
| thinking 开启，8K＋8K 预留 | 8/8（100%） | 3/8（37.5%） | 4,767 | 8,598 | 53.4 秒 |

现方案八次全部 `length`，其中一次连正文都没有（现有校验先报 `empty`，其余七次报
`output_length`）。关闭 thinking 后八次全部输出 8,192 tokens 正文，仍然 `length`。
提示词的“约 3,000 tokens 软目标”没有形成可靠约束。两组的失败原因分别是思考挤占正文
与正文自身写不完；仅关 thinking 并不足以解决这批失败。

额外预算组八份均含八个必要章节、正常结束，但正文范围为 **5,912–12,859 tokens**，
其中五份超过 8,192。部分收益来自允许正文变长，而非单纯把 thinking 从同一个额度中移开。

| 输入覆盖终点 | 第一次：thinking / 正文 | 第二次：thinking / 正文 | 正文 ≤8K 次数 |
|---|---:|---:|---:|
| 238 | 5,530 / 7,920 | 7,231 / 6,612 | 2/2 |
| 248 | 4,227 / 8,304 | 7,636 / 8,436 | 0/2 |
| 253 | 1,390 / 10,232 | 3,452 / 12,859 | 0/2 |
| 259 | 1,695 / 5,912 | 6,977 / 8,510 | 1/2 |

### 费用与缓存

24 次总费用按实际用量与官方空闲时段价格估算为 **$0.21915**，不是账单查询值。

| 方案 | 第一轮缓存命中比例 | 第二轮缓存命中比例 | 八次合计费用 |
|---|---:|---:|---:|
| 现方案 | 86.73% | 99.78% | $0.04842 |
| 关闭 thinking | 0.22% | 99.81% | $0.09319 |
| 额外预算 | 78.45% | 99.78% | $0.07754 |

比例按输入 tokens 加权。关闭 thinking 的第一轮缓存命中只有 0–384 tokens/次，
第二轮同输入则达到约 99.8%。这次切换并未直接复用已热的 thinking 请求缓存，
因此不能笼统承诺“关 thinking 不影响缓存”；它也不会导致后续永远无法命中。
这是当前提供商、当前输入下的观察，不能由此推定其内部缓存键实现。
冷缓存与交叉预热也使总费用不适合直接充当长期成本排序。

非 thinking 响应没有 `reasoning_tokens` 字段。离线复核同时确认请求显式 `disabled`、
捕获的 reasoning 文本为空、无工具调用，才把 reasoning 记作 0、completion 记作正文。
原始 null 字段和原始执行脚本完整保留；修正使用独立 `--analyze-only`，没有追加模型请求。

### 正文质量抽查与决策

正常结束不等于内容可靠。例如 `p259-r1-thinking_reserve_8k` 把历史助手的
“空正文异常发生在 USAGE 事件前，因而用量记为零”写成了事实；但相同输入的工具证据
位置 211 显示，空正文检查位于 `async for` 流循环之后。其他样本有的保留了“推断、未实测”
标签，表现并不一致。此处仅作具体风险抽查，不计算事实准确率。

本轮不把关闭 thinking 改成生产默认：它对这批困难输入的生成通过率没有收益。
额外生成额度可以缓解截断，但落地应分开管理**请求生成额度**和**最终摘要正文预算**。
后续优先验证更强的内容取舍与章节预算，并对完整但超长的候选做有界精简。
不能直接把全局 `compaction_max_output_tokens` 翻倍当作同一实验：当前该配置还参与
摘要边界选择与低水位空间预留，整条流水线仍需独立验证。

## 固定实验条件

使用 [9 月 16 日事故记录](compaction-incident-20260916.md) 中全部四种不同的
`a_isolated` 请求。五次历史失败中有两次输入完全相同，按 request blob SHA-256 去重，
避免重复输入获得额外权重。历史的 **0/5** 不作为本次对照组结果。

| 组别 | 请求 thinking | `max_tokens` | 含义 |
|---|---|---:|---|
| `current` | 不传，沿用提供商默认值 | 8,192 | 原样重放现方案 |
| `thinking_off` | `disabled` | 8,192 | 关闭兜底思考，额度全部可用于正文 |
| `thinking_reserve_8k` | `enabled` | 16,384 | 8K 正文预算之外，额外预留 8K 生成额度 |

第三组是**预算规划上的分离**。DeepSeek Chat Completions 只有共享的生成上限，
没有独立的 reasoning/text 硬额度；不能保证 thinking 最多 8K，也不能保证正文一定获得 8K。
对应参数和使用量字段见 [DeepSeek 请求接口](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)。

每个输入、每组各两次：`4 × 3 × 2 = 24` 次。固定随机种子安排请求顺序，并发 3，
每次仅尝试一次，整体请求超时沿用 bot 的 90 秒。不修改提示词、历史、温度、工具列表、
输入边界或正文软目标。此实验只重放独立兜底，不调用前缀阶段。

| 摘要覆盖终点 | 历史服务端输入 tokens | 本次预算估计 tokens | 16K 生成时的输入上限 |
|---|---:|---:|---:|
| 238 | 77,750 | 81,602 | 110,592 |
| 248 | 83,751 | 87,903 | 110,592 |
| 253 | 88,189 | 92,563 | 110,592 |
| 259 | 102,607 | 107,702 | 110,592 |

原模型名保持 `deepseek-v4-flash`。按[官方模型与价格页](https://api-docs.deepseek.com/quick_start/pricing/)
在本次查阅时的说明，旧 Flash 名称已路由到 V4.1 Flash；记录服务端返回型号，
不能把重放当作固定旧模型版本的实验。全部输入未命中缓存、全部输出达到额度时，
24 次的高峰价格估算上界为 **$0.980159**。价格可能变化，该值不是账单保证。
计价依据：每百万 token 缓存命中 $0.006、未命中 $0.30、输出 $1.20；空闲时段减半。

## 计分与范围

- 主指标 `candidate_pass`：实际调用 `StrategyCompactor.summarize(attempts=1)`，通过现有
  正文非空、非 length、八章节、来源范围、无工具调用、正常结束等校验。保留真实错误分类。
- 额外指标 `body_budget_pass`：主指标通过，且服务端 completion 减 reasoning tokens
  不超过 8,192。防止把第三组的“正文也扩大了”误认为单纯解决思考挤占。
- 报告 thinking、正文、总输出、缓存命中、耗时、失败类型与估算费用；缺失 usage 标记为未知。
- 保存正文便于检查关键事实，但格式通过不代表事实无遗漏。本次不做任务续跑质量评估，
  不执行生产发布及替换检查，不能声称测出了完整压缩流水线或所有会话的成功率。
- 四个输入来自同一个、预先筛选的失败会话，且互相重叠；每组八次只能说明这批困难输入。
  跨试验缓存预热也会影响耗时及费用。

## 复现与记录

脚本：[test_compaction_thinking_budget.py](../../scripts/test_compaction_thinking_budget.py)。
离线预检读取生产 SQLite 时使用 `mode=ro`，校验 request blob 哈希，确认三组 HTTP payload
仅有 `thinking`/`max_tokens` 差异，使用提供商 tokenizer 预检窗口。响应、原文和临时数据库
只写入 Git 忽略的 `artifacts/`；报告不复制历史正文。每次必须使用新的输出目录。

```bash
.venv/bin/python scripts/test_compaction_thinking_budget.py \
  --output artifacts/compaction-thinking-20260917-dry-new

# 真实请求（必须使用新目录，原始实验已完成）：
.venv/bin/python scripts/test_compaction_thinking_budget.py \
  --output artifacts/compaction-thinking-new-live --live

# 对现有响应重新统计，不发模型请求：
.venv/bin/python scripts/test_compaction_thinking_budget.py \
  --output artifacts/compaction-thinking-20260917-live --analyze-only
```

执行后生成 `manifest.json`、固定脚本副本、每次 `result.json` 和总 `results.json`。
线上执行脚本的 SHA 与离线统计脚本的 SHA 分开记录；统计修正没有改变实验条件。

验证：Ruff 通过；`test_compaction_thinking_replay.py`、`test_compaction_strategies.py`、
`test_context_compaction.py` 共 **90 passed**。包含 length 被拒绝、空正文、章节缺失、
流未正常结束、正文独立超预算与缺失 reasoning 使用量等计分边界；这些是离线测试，
不能代替真实 API 成功率。
