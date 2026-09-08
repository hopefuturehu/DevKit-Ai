# 历史文档归档

整理日期：2026-09-08。返回[文档索引](../README.md)。

这里保存已被替代的方案、重复材料和固定版本的实验记录，供设计溯源和结果核对。
正文中的“当前”、配置、源码行号和测试数字应结合原记录的日期与提交理解，不能直接作为现版本说明。归档不表示历史数据无效，也不表示相关问题仍未修复。

## 已被替代的分析与问答

| 文档 | 归档原因 | 当前阅读入口 |
|---|---|---|
| [上下文管理优化方案](context-management-solutions.md) | 基于旧 `ContextSnapshot` / `compact_messages()` 的分析；生产路径已改为可恢复单摘要 | [单摘要压缩](../recoverable-context-compaction.md) |
| [压缩高失败率快照](context-compaction-current-state.md) | 明确以 `f930a19` 为历史基线，推荐修复已落地；与故障复盘部分重叠，但保留独立数据口径 | [请求组装](../context-assembly.md)、[压缩有效性评测](../context-compaction-effectiveness.md) |
| [长会话压缩故障分析](context-compaction-failure-analysis.md) | 保留修复前原因、候选方案及 2026-08-25 的实施记录；其中三轮 user 硬下限已被后续方案替代 | [单摘要压缩](../recoverable-context-compaction.md)、[压缩有效性评测](../context-compaction-effectiveness.md) |
| [旧面试问答](interview-qa.md) | 仅一个 Prompt 组装问题，新手册已覆盖；旧列表仍把自动记忆列在历史尾部，并未列出原始 user 锚点 | [面试手册](../context-management-interview-guide.md)、[请求组装](../context-assembly.md) |

## 历史评测与数据

| 记录 | 归档原因与适用边界 | 当前运行与验收入口 |
|---|---|---|
| [Terminal-Bench 六任务结果](terminalbench-context-medium-six-results.md) | 固定任务集首次运行与定向复测记录；有效六任务样本未触发自动压缩，不能据此评价压缩收益 | [Terminal-Bench 评测](../terminalbench-evaluation.md) |
| [Memory Router canary 聚合数据](memory-routing-live-canary-2026-08-29.json) | 2026-08-29 的一次固定 fixture 实验，保留原始 JSON；不是持续更新的总体效果报告 | [Memory Router 设计与验收](../memory-routing.md) |

## 本次保留在主目录的相关材料

- 总体设计与实现状态分别解释设计边界和落地进度，不能仅因主题相近就合并。
- 缓存评测、压缩有效性评测和 Memory Router 文档仍含运行入口、指标和验收方法；历史结果保留为明确标注的基线。
- Skill 生命周期提案、卸载调研及最新裁剪容量分析仍支撑待实施工作；“尚未实现”不等于“已过时”。
- 框架对比是限定版本的研究资料，思维导图是阅读辅助；它们与模块实现说明用途不同，继续保留并由主索引区分。
