# 长上下文 Episode 压缩基准

> 执行日期：2026-07-24
> 目标：验证融合版上下文压缩在长任务中的压缩、保真、检索、恢复和故障安全性
> 模型：`deepseek-v4-pro`（真实模型组）与确定性 LLM double（回归组）

## 1. 场景

评测生成三个多阶段长任务。每个 run 包含 User、带 Tool Call 的 Assistant、Tool Result
和最终 Assistant 消息；Tool 输出混入约 2,400 字符的阶段诊断噪音，长期事实分散在不同
run 中。

| 场景 | Run | 消息 | 原文 Token | 关键事实 |
|---|---:|---:|---:|---:|
| 十阶段架构迁移与回归验证 | 10 | 40 | 14,858 | API v2 兼容、SQLite WAL、128 passed、切换窗口、迁移手册 |
| 线上性能事故定位、调优和回滚准备 | 10 | 40 | 15,061 | 连接池根因/目标、P99、回滚提交、错误率阈值 |
| 跨轮次发布治理与偏好演化 | 9 | 36 | 13,986 | pnpm 偏好更新、周五禁发、版本、安全扫描、灰度策略 |

发布治理场景使用同一个 `preference.package_manager` 稳定键先记录 pnpm 9，再更新到
pnpm 10，用于验证 Card 就地更新而不是产生两个有效冲突 Card。

## 2. 指标和通过条件

- `compression_ratio`：代表性续写查询实际注入的 Episode 摘要与有效 Card Token /
  原始消息 Token，要求 `< 20%`。
- `fact_recall`：摘要或 Card 中保留关键值的比例，要求 `>= 80%`。
- `retrieval_recall`：逐事实查询后，召回投影保留关键值的比例，要求 `>= 80%`。
- `cursor_coverage`：连续摘要游标 / 最新消息位置，全量整合要求 `100%`。
- `raw_preserved`：整合前后全部原始消息的顺序和 JSON SHA-256 完全一致。
- `source_traceable`：每个 Card 的限定 Episode/消息引用都能回溯到原文。
- `snapshot_free`：新流程没有创建 `ContextSnapshot`。
- Tool 原子性：压缩 Episode 中每个 Assistant Tool Call 都有同一 Episode 内的 Tool Result。

## 3. 确定性回归结果

确定性 LLM double 每批处理 3 个 Episode，用于稳定验证 Harness 状态机和不变量。

| 场景 | 原文→投影 Token | 压缩率 | 事实召回 | 检索召回 | 批次 |
|---|---:|---:|---:|---:|---:|
| 架构迁移 | 14,858 → 803 | 5.40% | 5/5 | 5/5 | 4 |
| 性能事故 | 15,061 → 695 | 4.61% | 5/5 | 5/5 | 4 |
| 发布治理 | 13,986 → 789 | 5.64% | 6/6 | 6/6 | 3 |
| **合计** | **43,905 → 2,287** | **5.21%** | **16/16** | **16/16** | **11** |

三组的游标覆盖、原文保留、来源回溯和 snapshot-free 均为 100%。

## 4. 真实模型结果

下表采用当前默认的每批最多 8 个 Episode，三组场景均一次通过。

| 场景 | 原文→投影 Token | 压缩率 | 事实召回 | 检索召回 | 批次 | 模型 Input/Output |
|---|---:|---:|---:|---:|---:|---:|
| 架构迁移 | 14,858 → 1,044 | 7.03% | 5/5 | 5/5 | 2 | 16,950 / 5,192 |
| 性能事故 | 15,061 → 1,033 | 6.86% | 5/5 | 5/5 | 2 | 17,862 / 7,554 |
| 发布治理 | 13,986 → 1,196 | 8.55% | 6/6 | 6/6 | 2 | 17,256 / 4,262 |
| **合计** | **43,905 → 3,273** | **7.45%** | **16/16** | **16/16** | **6** | **52,068 / 17,008** |

另以每批 4 个 Episode 运行三组场景，全部通过。10 Episode 单批在不同运行中既出现过
失败，也出现过成功；成功输出达到 3,815–4,011/4,096 Token，缺少稳定余量。因此默认
8 保持不变，但失败后会自动缩小批次。

## 5. 故障和压力测试

### LLM 输出失败

在第 2 个批次注入无效 JSON：

1. 失败批次从 `consolidating` 回到 `pending`；
2. Harness 把批大小从 3 降为 2；
3. 6 次调用后完成全部 10 个 Episode；
4. 游标到达 40，原始消息 SHA-256 不变。

若继续让 2-Episode 和 1-Episode 批次失败，整体结果明确返回未完成；前三个已发布
Episode 保持有效，游标停在 12，剩余 7 个 Episode 仍为 pending，下一次可以继续。

### 上游流中断

真实模型曾返回 `incomplete chunked read`。该次运行没有发布摘要或 Card，游标保持 0，
40 条原始消息完全保留。重试成功后游标才推进到 40。

### Context Pressure

用 32k 模型窗口、55% 自动压缩阈值和 5k 最近历史预算直接运行 `AgentRunner`：

- 仅整合规划选中的旧 Episode，没有把全部 pending 历史一并压缩；
- 最近未封存历史仍以原文进入模型请求；
- 已封存且跨越切分点的 Episode 作为整体原子单元处理；
- Assistant Tool Call 与 Tool Result 未被拆分；
- 请求同时包含 `consolidated_episodes` 和最近原文；
- 没有创建 snapshot。

## 6. 测试发现并修复的问题

1. `compact_ranges()` 原先会调用全量 pending 整合，导致计划保留的最近历史也被压缩。
   现改为只处理选中的 Episode ID。
2. 已封存 Episode 跨越切分点时不能再次切开。现将其作为原子单元整体整合，未封存消息
   仍按 Tool Call/Result 原子组切分。
3. 多批整合遇到 LLM/API 失败时原先直接停止。现自动将批大小减半并重试，直到单
   Episode；持续失败才停止。
4. 前几批成功、后一批失败时，聚合结果原先可能仍为 `consolidated=true`。现终止失败或
   达到批次上限会明确报告整体未完成，同时保留已安全发布的前缀。

## 7. 结论与边界

在这组三类、共 116 条消息和约 44k 原始 Token 的合成长任务中，当前机制满足压缩、
事实保留、检索、原文保全、来源回溯、游标和 Tool 原子性要求。真实模型投影约为原文的
7.45%，关键事实与检索召回均为 100%。

这不是对所有自然对话摘要质量的最终证明。场景中的关键事实带有明确结构和值，后续应增加
无标记的自然语言长任务、代码 diff、大型 Tool blob、跨 100+ Episode 的耐久测试，并使用
独立模型或人工样本评价因果关系和隐含约束的语义保真。

## 8. 复现

确定性回归：

```bash
.venv/bin/python scripts/run_context_memory_benchmark.py \
  --provider deterministic \
  --output /tmp/context-memory-deterministic.json
```

真实模型：

```bash
.venv/bin/python scripts/run_context_memory_benchmark.py \
  --provider live \
  --config .bot/config.toml \
  --max-episodes 8 \
  --output /tmp/context-memory-live.json
```

单场景或批大小边界：

```bash
.venv/bin/python scripts/run_context_memory_benchmark.py \
  --provider live \
  --scenario architecture_migration \
  --max-episodes 4
```
