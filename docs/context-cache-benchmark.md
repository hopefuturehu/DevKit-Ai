# 长任务上下文缓存评测

## 要回答的问题

这个评测不是只测一两个相邻请求的缓存命中，而是让同一个确定性任务完整经历：

```text
上下文增长 → 压缩 → 新 epoch 增长 → 再压缩 → …
```

所有请求都经过生产代码中的 `AgentRunner`、`ContextPlanner`、`ContextCompactor` 和 SQLite
Transcript。只有模型 Provider 与 Tool 被替换为确定性离线实现，因此不会访问网络或消耗真实
模型额度。

评测同时回答三类问题：

1. 压缩前后的请求前缀有多少可以复用，缓存需要几个请求恢复；
2. 缓存命中改善是否真的降低了整个长任务的成本，而不是仅提高百分比；
3. 优化缓存布局时，是否破坏事实保留、Tool 原子性、单活动摘要或 Transcript 不可变性。

## 两个套组

### Fast：CI 与本地回归

Fast 使用 24K effective input budget、40 个逻辑轮、5,500 字符的每轮 Tool 输出和 6,000
字符的确定性稳定显式记忆。默认至少跨过 3 次压缩；当前配置通常产生 5 次压缩。它会同时
运行：

- `current`：当前上下文压缩机制；
- `no-compaction`：同一工作负载、同一消息和 Tool schema，但给 Context Planner 一个足够大的
  虚拟窗口，作为不压缩的反事实基线。

```bash
.venv/bin/pytest -q tests/integration/test_long_context_cache_benchmark.py
```

### Soak：生产窗口规模

Soak 使用 131,072 context window、120K configured input limit、168 个逻辑轮、16,000 字符
的稳定显式记忆和默认生产压缩参数，要求完整经历至少 6 次压缩。它默认不会随普通 `pytest`
执行：

```bash
RUN_CONTEXT_CACHE_SOAK=1 \
  .venv/bin/pytest -q tests/soak/test_context_cache_soak.py
```

首次压缩必定是 `rebuild_from_raw`，其后通常是 `incremental_update`。周期性 raw rebuild 还有
“完整原文小于模型窗口 70%”的安全门；真正的超长任务在第五次压缩时往往已经不再满足这个
条件，所以 Soak 不把周期性 rebuild 当成必然结果，而是显式记录实际 rebuild 数量。

## 命令行运行

默认运行 Fast 的当前方案与不压缩对照组：

```bash
.venv/bin/python scripts/run_context_cache_benchmark.py \
  --suite fast \
  --output .bot/benchmarks/context-cache
```

只运行生产规模当前方案：

```bash
.venv/bin/python scripts/run_context_cache_benchmark.py \
  --suite soak \
  --variants current \
  --output .bot/benchmarks/context-cache
```

`--suite all` 可依次运行两个套组。探索时还可单独覆盖：

- `--turns`：逻辑轮数；
- `--tool-output-chars`：每轮确定性 Tool 输出量；
- `--stable-memory-chars`：每次请求都必须保留的确定性显式记忆量；
- `--minimum-cacheable-tokens`：缓存最小可复用前缀；
- `--seed`：工作负载 seed；
- `--cached-input-cost-ratio` 和 `--output-cost-ratio`：成本模型。

覆盖 `--turns` 后会关闭套组的最小压缩次数门禁，因为短探索运行不一定应当触发压缩。其他
正确性门禁仍然生效。

## 缓存模拟口径

离线 Provider 将实际 `ModelRequest` 拆成稳定 Tool schema 和完整 message 原子。只有从请求
开头连续、逐原子完全相同的内容才可命中；任何早期 message 的改写都会使它之后的内容全部
miss。每个已完成 prompt，以及 prompt 加本次 Assistant 输出的边界，都会成为后续请求可复用
的候选前缀。

Token 数使用项目自己的 `TokenEstimator`，而不是假装复现某家 Provider 的私有 tokenizer。
因此这个套组适合比较同一代码库不同方案的相对变化，不等同于线上账单。需要校准线上绝对值
时，可在少量真实请求中读取 Provider 返回的 `prompt_cache_hit_tokens` 和
`prompt_cache_miss_tokens`，再与离线 trace 对齐；不需要用真实模型跑完整长任务。

每次模拟 usage 也使用 DeepSeek 兼容字段：

```json
{
  "prompt_tokens": 12000,
  "prompt_cache_hit_tokens": 9800,
  "prompt_cache_miss_tokens": 2200
}
```

## 主要指标

`weighted_cache_hit_ratio` 是所有请求的 `sum(cache_read) / sum(prompt)`，不能对每次请求的
百分比做简单平均。`agent_cache_hit_ratio` 只统计主 Agent，便于把压缩模型的请求成本分开看。

主评分是 `cost_per_logical_turn`：

```text
(miss_input
 + cache_read × cached_input_cost_ratio
 + output × output_cost_ratio)
÷ logical_turns
```

同时输出两个反事实：

- `no_cache_cost_units`：所有输入都 miss；
- `perfect_prefix_cost_units`：所有历史中已经存在的 exact prefix 都按缓存价计费。

所以“命中率提高但 prompt 膨胀更多”的方案不会被误判为优化。Fast 基线就有意覆盖了这个
例子：不压缩方案通常具有更高 raw hit rate，却可能有更高的每轮缓存折算成本。

`epochs.csv` 按 compaction epoch 汇总首个请求的 miss spike、加权命中率和恢复到 80% 命中
所需的请求数。`requests.jsonl` 则包含每次请求的 phase、epoch、压缩 mode、首个变化 segment、
prompt/read/miss 和请求指纹，不保存原始 prompt 或任何密钥。

## 正确性门禁

缓存指标只有在以下门禁全部通过时才有效：

- 每个逻辑轮都正常完成；
- 所有已引入的 `CACHE_FACT` 在后续最终模型请求中仍可见；
- 稳定显式记忆在每轮最终模型请求中完整可见；
- 任一主模型请求最多只有一个 `context_compaction`；
- 每次压缩前的既有 SQLite Transcript SHA-256 完全不变；
- 没有 Tool protocol repair、context limit 或 compaction failure；
- 当前方案达到套组要求的最小压缩周期与 raw rebuild 次数。

## 产物与控制变量流程

每个 `<suite>/<variant>/artifacts` 目录包含：

- `summary.json`：配置、工作负载 SHA-256、正确性门禁和总指标；
- `requests.jsonl`：逐请求 trace；
- `epochs.csv`：逐压缩 epoch 聚合。

输出根目录还有 `comparisons.csv` 与 `comparisons.json`。推荐的优化流程是：

1. 固定 suite、seed、turns、Tool 输出量和成本比例；
2. 记录修改前的 `current` 与 `no-compaction`；
3. 一次只改变一个上下文变量或组装规则；
4. 确认两次运行的 `workload_sha256` 相同且所有 quality gates 通过；
5. 比较 `cost_per_logical_turn`、压缩后首请求 miss 和恢复请求数，再把 raw hit rate 作为辅助
   指标。

这样可以把“工作负载变化”“正确性退化”和“缓存布局改善”三者分离。

重复使用同一个 benchmark 输出目录时，评测会复用内容相同的专用记忆，而不会叠加多份。
如果目录中的 SQLite 含有非 benchmark 长期记忆，评测会拒绝运行，防止隐式污染控制变量。

## 组装顺序优化实验（2026-08-23）

本节保留 `cb911b1` / `d16b203` 的历史请求布局与测量值，包含当时的前置 Active Skill 层。
`875750c` 已将新会话 Skill 正文改为历史交付；下列命中率及折算成本变化不用于证明这次改造的
收益。当前形状见[上下文组装](context-assembly.md)，新布局的单样本 usage 见[Skill 验证](skill-context-validation.md)。

### 可还原代码点和控制变量

- 旧顺序：本地分支 `context-cache-order-baseline`，commit `cb911b1`；
- 新顺序：主分支 commit `d16b203`；
- Fast workload SHA-256：
  `7c63935ef76595f81c50601756554b29becede6965dd64101d7f1410943a867e`；
- Soak workload SHA-256：
  `7b978d0752ced358c269df1be61924dbb77b3f1e22731a7a340be98a59733f58`；
- 两边使用相同 seed、轮数、Tool 输出、稳定记忆、预算、压缩配置和成本比例；所有 quality
  gate 均通过，Fast 都压缩 5 次，Soak 都压缩 7 次。

旧顺序是：

```text
稳定 system → runtime note → compaction → history → explicit/automatic memory
```

它导致稳定显式记忆每轮都被插到增长中的会话后面，既不能进入可复用前缀，也会挡住同一
Run 后续 Tool step 的 append-only 边界。新顺序拆成：

```text
稳定 system/tool catalog/active skill/explicit memory
→ compaction/history
→ automatic memory/runtime note
```

同时 Tool schema 改为按名称确定性排序。完整分块和取舍见
[模型上下文分块与组装顺序](context-assembly.md)。

### 结果

| Suite / Variant | 指标 | 旧顺序 `cb911b1` | 新顺序 `d16b203` | 变化 |
|---|---|---:|---:|---:|
| Fast / current | 全请求加权命中率 | 70.4433% | 84.8110% | +14.37 pp |
| Fast / current | 主 Agent 命中率 | 75.5406% | 90.9818% | +15.44 pp |
| Fast / current | 每逻辑轮折算成本 | 7,857.92 | 5,106.16 | -35.02% |
| Fast / current | cache miss token | 251,463 | 129,189 | -48.63% |
| Fast / no-compaction | 全请求加权命中率 | 93.0252% | 97.6648% | +4.64 pp |
| Fast / no-compaction | 每逻辑轮折算成本 | 10,783.60 | 8,028.35 | -25.55% |
| Soak / current | 全请求加权命中率 | 89.7017% | 95.6215% | +5.92 pp |
| Soak / current | 主 Agent 命中率 | 90.9821% | 96.9864% | +6.00 pp |
| Soak / current | 每逻辑轮折算成本 | 26,482.36 | 19,174.94 | -27.59% |
| Soak / current | cache miss token | 2,372,697 | 1,008,757 | -57.48% |

Fast 在压缩后首个 Agent 请求的命中率由约 15.8% 提升到约 41.7%–42.2%，恢复到 80% 命中
从 6 个请求缩短到 2 个。Soak 因 Provider 的 1,024-token 最小缓存单元，压缩后首请求从完全
不计命中提升到约 9.36%；后续恢复仍为 2 个请求，但第 0 epoch 的首次稳定恢复从 17 个请求
缩短到 3 个。

### Trade-off

> 这一节记录 2026-08-23 的历史排序实验；当前默认 `on_demand` 已不再注入 automatic memory。
> 这里的 “automatic memory/runtime note 尾部” 只描述当时版本，不是当前请求形状。

- 显式记忆若由用户修改，会在修改后的首个请求重建前缀；这是让它在其余绝大多数请求中
  被缓存的代价。
- 自动记忆和 runtime note 留在尾部，因此其自身复用率较低；换来的是后台提取或运行状态
  更新不会击穿整段会话缓存。
- compaction summary 每次替换都会形成新 epoch，不能通过纯排序消除；新顺序只扩大摘要前
  仍可复用的稳定区。
- Tool 按名称排序改变了模型看到的展示次序，但不改变名称、schema、权限或执行语义；收益是
  注册/扫描顺序不再制造无意义 cache miss。

## 真实任务观测（2026-09-09，18 题停止快照）

40 题计划中已覆盖的 18 个不同题目，按每题一次模型运行统计：16 个原始 attempt 1、Chess
attempt 3 环境恢复和大文本编辑 `native-1` 环境变体。源码基线为 `578f660`，模型为
`deepseek-v4-flash`。以下取 Provider 逐响应 usage，包含异常收尾和官方停止后的迟到响应；
不与上面的确定性 Fast/Soak 指标合并，也不构成缓存布局优化前后的 A/B。

| 阶段 | 有 usage 的响应数 | 输入 tokens | 缓存命中 tokens | 未命中 tokens | token 加权命中率 |
|---|---:|---:|---:|---:|---:|
| 常规推理 | 597 | 14,313,350 | 13,292,544 | 1,020,806 | 92.87% |
| 异常收尾 | 7 | 214,776 | 3,712 | 211,064 | 1.73% |
| 主样本合计 | 604 | 14,528,126 | 13,296,256 | 1,231,870 | **91.52%** |

计算为 `Σ cache_hit / Σ prompt`，604 次均满足 `hit + miss == prompt`。
598/604 次有至少部分命中（99.01%）；单请求命中率中位数为 94.64%，按题目等权平均为
86.55%。异常收尾占输入量的 **1.48%**，却贡献未命中量的 **17.13%**，值得单独优化请求构造。
排除 `largest-eigenval` 在官方停止后返回的两次响应后，共 602 次响应，加权命中率为 91.90%。

主样本没有发生摘要压缩。另列的 SymPy attempt 2 候选在压缩后，首个主请求输入从 101,373
降至 28,738 tokens，首请求命中率为 14.70%，第 4–31 次请求合计为 95.06%；两次候选压缩
请求自身的缓存命中均为 0%。这是一次真实缓存重建的观测，不能外推普遍恢复轮数或回本时间。

数据、样本边界和重算入口见[18 题上下文分析](public-benchmark-context-analysis.md)与
[结构化指标](data/public-benchmark-context-metrics.json)。这些值衡量已发生的缓存复用，
没有冷缓存或相同任务的布局对照，不能直接折算为某项设计带来的费用节省。
