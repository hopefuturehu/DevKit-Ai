# 上下文压缩有效性评测

> 2026-08-31 更新：生产选择器已把“三轮用户消息”从可无限突破 20K 的硬下限改为强制路径的
> 预算内停止条件（收集到 3 条 user 即停，否则在下一组会使 tail 超过 20K 时停止），并新增
> 真实 user 锚点回放与 Assistant-safe 单轮切分。下文 2026-08-25 的 `hybrid-*` 真实运行数字
> 保留为历史基线；当前离线门禁使用 `latest_user_anchor_visible`，不再要求 raw tail 自身含三轮 user。

## 目的

A+D 重设计采用单独的[交接收益测试方案](context-handoff-evaluation-plan.md)，覆盖
模型自主触发和整题交付；本文现有 `a0/a1/a2a/a2b/hybrid-*` 脚本变体不等同于新方案的 A/D/AD。
2026-09-09 的[新方案量化结果](context-handoff-l0-l1-results.md)已包含 L0 和部分 L1，
与下文历史基线分开统计；自主交接及整题收益尚未验证。

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
[长会话上下文压缩问题第 16 节](archive/context-compaction-failure-analysis.md#16-实施与验证记录2026-08-25)。

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

当时据此采用 20K 近期尾部、至少三轮用户消息、3K 软目标、4K 可见硬限制、范围级溯源，并在
DeepSeek 官方端点关闭压缩请求的思考模式。当前实现已用“20K 有界 tail + 活动 user 锚点 +
Assistant-safe 切分”替代三轮硬下限；该历史记录只验证固定合成负载，真实业务 Transcript 仍应
按上文格式脱敏后持续回放。

## 真实任务观测（2026-09-09，主样本与补充运行分列）

本节来自 40 题计划停止时已覆盖的 18 个不同题目及另外 4 次复跑/协议候选，基线为
`578f660`，模型为 `deepseek-v4-flash`。主样本包含 16 个原始 attempt 1、Chess attempt 3
环境恢复和大文本编辑 `native-1` 环境变体。该批任务轨迹与上面的 20 次合成压缩回放分开统计。

主样本压缩触发覆盖为 **0/18**，started/completed/failed 均为 0，摘要成功率为
**N/A（没有尝试）**。单请求实际输入 p50/p95/最大值为 19,707.5 / 60,339 / 68,913 tokens。
补充运行中，只有以下两次触发摘要压缩：

| 指标 | SymPy attempt 2 协议候选 | Chess attempt 4 协议候选 |
|---|---:|---:|
| 压缩位置 / 归并消息数 | 第 88 步前 / 143 | 第 52 步前 / 93 |
| 选中源字符数 → 摘要字符数 | 140,997 → 4,720 | 146,643 → 3,131 |
| 该段字符削减率 | 96.65% | 97.86% |
| 压缩 API 输入 / 输出 tokens | 47,876 / 2,131 | 37,674 / 1,974 |
| 压缩流程耗时 | 13.65 秒 | 17.33 秒 |
| 生成 / repair / condense / 传输重试次数 | 1 / 0 / 0 / 0 | 1 / 0 / 0 / 0 |
| 压缩后正常返回的主响应数 | 31 | 0 |

两次摘要均生成并安装成功，机械完成率为 **2/2（100%）**；合计消耗 85,550 输入和
4,105 输出 tokens，均未命中缓存。SymPy 相邻主请求输入从 101,373 降至 28,738，观测下降
71.65%。字符削减仅针对选中段落，相邻请求也有正常状态变化，均不代表整题累计净节省。

进一步要求“安装摘要后下一次主请求成功”，结果是 **1/2（50%）**；要求“安装后正常完成
整个运行”，结果是 **0/2（0%）**。后者不是产物通过率：SymPy 候选补丁通过了官方测试，
但运行在第 119 步遭遇协议错误。关键事实召回和语义保真没有独立评分，不能写成 100%。

Chess 在压缩后第一请求遇到 `reasoning_content` HTTP 400。既有保存状态重建对照中，
摘要缺失 reasoning 字段为 **2/2 次 HTTP 400**，仅补显式空字段为 **2/2 次 HTTP 200**，
后者均返回原生工具调用。这四次只验证 API 接受性，未执行工具；重建请求也不是原始出站
payload 的逐字节副本，不能计作整题恢复成功。SymPy 先成功返回 31 次，其最终错误尚未确认
具有相同的直接原因。

完整口径和原始文件哈希见[18 题上下文分析](public-benchmark-context-analysis.md)与
[结构化指标](data/public-benchmark-context-metrics.json)；协议对照见
[Chess 请求重建结果](../artifacts/public-benchmark/20260909-flash/diagnostics/chess-compaction-replay/result.json)。

该 Flash 对照只证明这组重建请求接受显式空字符串，不意味着压缩 reasoning 不能回传或所有
端点都应使用空字符串。OpenCode 存在保留摘要响应 reasoning 的路径；Hermes 使用单空格补位，
Pi/Harness 则以 user checkpoint 回放纯文本。具体条件与验证方案见
[压缩 reasoning 的保存与回放](compaction-reasoning-replay.md)。
