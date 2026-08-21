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

Fast 使用 24K effective input budget、40 个逻辑轮和 5,500 字符的每轮 Tool 输出。默认至少
跨过 3 次压缩；当前基线通常会产生 4 次压缩。它会同时运行：

- `current`：当前上下文压缩机制；
- `no-compaction`：同一工作负载、同一消息和 Tool schema，但给 Context Planner 一个足够大的
  虚拟窗口，作为不压缩的反事实基线。

```bash
.venv/bin/pytest -q tests/integration/test_long_context_cache_benchmark.py
```

### Soak：生产窗口规模

Soak 使用 131,072 context window、120K configured input limit、168 个逻辑轮和默认生产压缩
参数，要求完整经历至少 6 次压缩。它默认不会随普通 `pytest` 执行：

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
