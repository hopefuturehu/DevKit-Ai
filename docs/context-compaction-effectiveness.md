# 上下文压缩有效性评测

## 目的

该评测把压缩正确性和 Provider 表现分开验证。普通 pytest 使用确定性 Provider 覆盖错误分流、
版本安全、事实保留和近期尾部；真实模型评测必须显式开启，只回放压缩请求，不执行 Agent Tool。

历史基线为提交 `3dfae7b`：26 次计划中成功 11 次（42.3%），失败 15 次（57.7%）；最近一次
长度失败和缩范围重试共等待 491.6 秒。候选方案的目标是至少 95% 成功率，p50 不超过 30 秒、
p95 不超过 60 秒，且单请求硬上限为 90 秒。

## 离线故障矩阵

```bash
.venv/bin/pytest -q tests/unit/test_context_compaction.py
.venv/bin/pytest -q tests/integration/test_compaction_effectiveness.py
```

也可单独生成可审计产物：

```bash
.venv/bin/python scripts/run_compaction_effectiveness.py \
  --provider scripted \
  --variant hybrid-20k \
  --scenario length \
  --output .bot/benchmarks/compaction-effectiveness
```

支持的 scripted 场景为 `success`、`length`、`format`、`rate-limit`、
`context-overflow` 和 `authentication`。每次运行写出：

- `summary.json`：配置结果、cursor、事实 recall 和正确性门禁；
- `requests.jsonl`：请求阶段、范围、usage、reasoning 字符数、耗时、费用和错误分类；
- 聚合摘要：成功率、p50/p95、completion/可见摘要比例和总费用。

产物不保存原始 prompt。需要回放真实 Transcript 时，先在受控位置生成脱敏 JSONL，每行格式为：

```json
{"run_id":"run-1","message":{"role":"user","content":"[EVAL_FACT:F01] 已脱敏关键事实"}}
```

需要自动评分的关键事实使用 `F` 加数字的稳定 ID，并逐条标注；未标注内容仍参与摘要，但不计入
自动 recall。再通过 `--transcript /path/to/redacted.jsonl` 传入。不要把原始会话或密钥提交到
仓库。

## A/B 变体

| 变体 | 改变量 |
|---|---|
| `a0` | 主模型继承、8K 目标/硬限制、逐条引用 |
| `a1` | 仅隔离到 `deepseek-v4-flash` |
| `a2a` | 3K 软目标、4K 硬限制、8K wire |
| `a2b` | 4K 软目标、6K 硬限制、8K wire |
| `hybrid-20k/24k/48k` | 范围级溯源、关闭摘要思考、至少三轮用户消息及对应原始尾部 |

A2a、A2b 都满足质量门禁时选择 A2a；否则选择通过门禁的 A2b。尾部选择通过全部门禁的最小值。

## 真实 Provider

真实调用默认禁止，必须显式设置开关并配置费用上限：

```bash
RUN_CONTEXT_COMPACTION_LIVE=1 \
  .venv/bin/python scripts/run_compaction_effectiveness.py \
  --provider live \
  --variant hybrid-20k \
  --scenario success \
  --transcript /path/to/redacted.jsonl \
  --repeat 3 \
  --max-cost-usd 0.50 \
  --output .bot/benchmarks/compaction-effectiveness
```

真实评测还要求配置 `model.input_cost_per_million` 与 `model.output_cost_per_million`；脚本会把
剩余费用额度传入单次压缩，在请求之间停止恢复调用，并在每次尝试后停止后续回放。

Screening 对 A0、A1、A2a、A2b 各运行 3 次，前四组保持 Provider 默认思考行为；Hybrid
候选显式关闭摘要思考。胜出配置再运行 20 次；20 次最多失败 1 次，
completion/可见摘要比例不超过 2，并且所有正确性门禁必须通过。真实确认总费用上限建议为
1 美元。达到费用、请求或时间上限时停止后续调用，已发布 cursor 保持有效。

## 完整回归门禁

```bash
.venv/bin/pytest -q tests/unit/test_context_compaction.py
.venv/bin/pytest -q tests/integration/test_context_compaction_benchmark.py
.venv/bin/pytest -q tests/integration/test_long_context_cache_benchmark.py
RUN_CONTEXT_CACHE_SOAK=1 .venv/bin/pytest -q tests/soak/test_context_cache_soak.py
```

Fast 和 Soak 必须保持事实可见、单活动摘要、Transcript 不可变、Tool 协议平衡、零压缩失败；
候选方案的 `cost_per_logical_turn` 相对基线不得回退超过 5%。

## 推广记录（2026-08-25）

逐次冒烟、失败方案、评分器修正、关闭思考前后对比以及全量缓存基准见
[长会话上下文压缩问题第 16 节](context-compaction-failure-analysis.md#16-实施与验证记录2026-08-25)。

`hybrid-20k` 使用 12 轮合成 Transcript（每轮含 14,000 字符 Tool 输出）完成 20 次 DeepSeek
V4 Flash 真实回放，全部门禁通过：

| 指标 | 结果 |
|---|---:|
| 成功与质量通过 | 20/20（100%） |
| 每次请求数 | 1 |
| p50 / p95 / 最大耗时 | 5.57s / 9.96s / 9.96s |
| completion / 可见摘要 p95 | 1.216 |
| 最大 reasoning 字符数 | 0 |
| 总费用 | $0.272528 |

因此默认采用 20K 近期尾部、至少三轮用户消息、3K 软目标、4K 可见硬限制、范围级溯源，
并在 DeepSeek 官方端点关闭压缩请求的思考模式。该记录验证固定合成负载；真实业务 Transcript
仍应按上文格式脱敏后持续回放。
