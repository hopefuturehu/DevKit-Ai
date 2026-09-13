# 历史文档归档

归档整理日期：2026-09-08；分类导航更新：2026-09-13。返回[文档索引](../README.md)。

这里保存已被替代的方案、重复材料和固定版本的实验记录，供设计溯源和结果核对。
正文中的“当前”、配置、源码行号和测试数字应结合原记录的日期与提交理解，不能直接作为现版本说明。归档不表示历史数据无效，也不表示相关问题仍未修复。

## 已被替代的分析与问答

| 文档 | 归档原因 | 当前阅读入口 |
|---|---|---|
| [上下文管理优化方案](context-management-solutions.md) | 基于旧 `ContextSnapshot` / `compact_messages()` 的分析；生产路径已改为可恢复单摘要 | [单摘要压缩](../architecture/recoverable-context-compaction.md) |
| [压缩高失败率快照](context-compaction-current-state.md) | 明确以 `f930a19` 为历史基线，推荐修复已落地；与故障复盘部分重叠，但保留独立数据口径 | [请求组装](../architecture/context-assembly.md)、[压缩有效性评测](../evaluations/context-compaction-effectiveness.md) |
| [长会话压缩故障分析](context-compaction-failure-analysis.md) | 保留修复前原因、候选方案及 2026-08-25 的实施记录；其中三轮 user 硬下限已被后续方案替代 | [单摘要压缩](../architecture/recoverable-context-compaction.md)、[压缩有效性评测](../evaluations/context-compaction-effectiveness.md) |
| [旧面试问答](interview-qa.md) | 仅一个 Prompt 组装问题，新手册已覆盖；旧列表仍把自动记忆列在历史尾部，并未列出原始 user 锚点 | [面试手册](../career/context-management-interview-guide.md)、[请求组装](../architecture/context-assembly.md) |

## 历史评测与数据

| 记录 | 归档原因与适用边界 | 当前运行与验收入口 |
|---|---|---|
| [Terminal-Bench 六任务结果](terminalbench-context-medium-six-results.md) | 固定任务集首次运行与定向复测记录；有效六任务样本未触发自动压缩，不能据此评价压缩收益 | [Terminal-Bench 评测](../evaluations/terminalbench-evaluation.md) |
| [Memory Router canary 聚合数据](memory-routing-live-canary-2026-08-29.json) | 2026-08-29 的一次固定 fixture 实验，保留原始 JSON；不是持续更新的总体效果报告 | [Memory Router 设计与验收](../architecture/memory-routing.md) |

## 相关材料的分类入口

- [架构与实现](../architecture/README.md)：总体设计与实现状态分别解释设计边界和落地进度。
- [评测指南与记录](../evaluations/README.md)：缓存评测与压缩有效性评测包含运行入口、指标和验收方法；历史结果保留为明确标注的基线。Memory Router 的机制与验收见[架构说明](../architecture/memory-routing.md)。
- Skill 生命周期提案见[设计演进](../designs/README.md)，卸载调研见[调研与对比](../research/README.md)：Run 隔离、历史交付及恢复已按[首版方案](../designs/active-skill-layer-removal-plan.md)落地；通用投影、微裁剪与完整成本评测仍待实施。相关容量分析继续作为历史证据保留。
- [框架对比](../research/context-framework-comparison.md)是限定版本的研究资料，[思维导图](../architecture/project-mindmap.xmind)是架构阅读辅助；分别保存在对应分类。
