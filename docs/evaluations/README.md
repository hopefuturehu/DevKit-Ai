# 评测指南与记录

返回[文档索引](../README.md)。先按运行指南选择入口，再查看对应实验。
所有结果限定于原文记录的版本、日期、模型、任务和计量口径；本次目录整理没有重跑评测。
机器可读结果与来源清单统一见 [data/](../data/README.md)，更早的已归档记录见[历史归档](../archive/README.md)。

## 运行指南与指标

| 文档 | 用途 |
|---|---|
| [通用任务评测](evaluation.md) | Fixture 隔离、文件/JSON/容器验收、真实工具结果、失败分类和复现清单 |
| [上下文整体测试矩阵](context-evaluation-matrix.md) | 各专项入口、覆盖范围和仍缺的验收 |
| [上下文效率评测指标口径](context-efficiency-benchmark-metrics.md) | Trace、聚合指标、门禁和差值公式 |
| [长任务上下文缓存评测](context-cache-benchmark.md) | Fast / Soak 运行方法、缓存模拟口径与历史布局实验 |
| [上下文压缩有效性评测](context-compaction-effectiveness.md) | 故障矩阵、Provider 回放、推广门禁与历史基线 |
| [Terminal-Bench 评测](terminalbench-evaluation.md) | 任务集运行、预算、诊断和结果归档 |
| [SWE-bench 评测](swebench-evaluation.md) | 单实例准备、容器执行和 prediction 产物 |
| [A+D 交接收益测试方案](context-handoff-evaluation-plan.md) | 分层实验、缓存计费、质量门禁和统计判据；L2/L3 待验证 |

## 公开基准批次

| 文档 | 用途 |
|---|---|
| [执行预检（2026-09-09）](benchmark-preflight-2026-09-09.md) | 固定 20＋20 范围、环境检查、官方判分控制实验及阻塞 |
| [40 题公开基准执行记录](public-benchmark-40-execution.md) | 冻结范围、运行约定与停止时间 |
| [40 题公开基准部分结果与失败分析](public-benchmark-40-results.md) | 已覆盖任务的官方结果、失败归因、复验和未完成项 |
| [已覆盖 18 题的上下文管理分析](public-benchmark-context-analysis.md) | 该停止批次的缓存、输出外置和上下文机制审计 |

## 压缩与缓存实验

策略比较可先读 [V4 Flash 三轮复测](context-strategy-flash-repeats.md)及[归因边界](context-strategy-flash-causal-analysis.md)，再追溯早期实验。

| 文档 | 用途 |
|---|---|
| [A+D 交接 L0/L1 量化结果](context-handoff-l0-l1-results.md) | 固定片段的容量、缓存、费用、质量与失败分析；未执行 L2/L3 |
| [CURRENT / A / B 真实任务评测](context-strategy-path-evaluation.md) | 三种路径的原型与首次完整任务比较 |
| [A 的主输出额度补测：8K → 32K](context-strategy-a-output-32k-evaluation.md) | 单任务补测；区分产物验收通过和运行正常结束 |
| [前缀摘要与独立兜底：32K 实测](compaction-prefix-fallback-evaluation.md) | 双路径断点重放和整题重跑，记录收益与限制 |
| [精简版压缩正确性：重放与短续跑](compaction-fidelity-evaluation.md) | 提示改动的真实对照、标题回退和质量边界 |
| [摘要标题兼容修复与 Flash 复测](compaction-heading-compatibility.md) | 历史失败候选校验及五组真实对照 |
| [V4 Flash 四策略首次对比](context-strategy-flash-evaluation.md) | CURRENT / A / B / 双路径的首轮完整任务结果 |
| [V4 Flash 四策略三轮复测](context-strategy-flash-repeats.md) | 12 场有效比较、排除记录和完整任务验收 |
| [三轮复测的结果复核与归因边界](context-strategy-flash-causal-analysis.md) | 对齐轨迹、摘要、源码和独立产物验证，限定因果结论 |
| [V4 Flash 压缩前后的上下文组成](context-strategy-flash-composition.md) | 释放率分母、组成与既有数据复核 |
| [真实任务缓存未命中拆解](context-cache-miss-analysis.md) | 历史逐请求归因及后续缓存假设修正 |
| [DeepSeek tool_choice 缓存实测](tool-choice-cache-probe.md) | `auto / none` 切换的真实 API 对照 |
| [A32 首次压缩工具调用重放](compaction-tool-replay.md) | 三次重放的工具申请与摘要失败证据 |
| [历史会话裁剪容量分析](history-pruning-analysis.md) | 离线容量回放；不代表生产裁剪的质量或费用收益 |

## 功能实施验证

| 文档 | 用途 |
|---|---|
| [Skill 历史交付：实施与验证](skill-context-validation.md) | Run 隔离、压缩恢复、会话迁移、回归与小规模真实模型对照 |
| [重复工具调用修复验证报告](repeat-guard-validation.md) | 四项调整的回归、真实进程、历史回放和模型续跑边界 |
