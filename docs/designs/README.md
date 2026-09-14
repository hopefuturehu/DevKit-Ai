# 设计演进

返回[文档索引](../README.md)。这里保存实施方案、变更记录、候选设计和交互原型。
目录归属不表示方案全部落地；状态以各文档注明的实现范围为准，结果查阅[评测索引](../evaluations/README.md)。

## Skill、执行控制与界面

| 文档 | 状态与用途 |
|---|---|
| [移除 Active Skill 独立层：首版方案](active-skill-layer-removal-plan.md) | A–C 已实现；D 有小规模行为对照，完整成本评测待补 |
| [Skill 上下文生命周期设计](skill-run-lifecycle-design.md) | 原始完整提案；Run 隔离、历史交付和恢复已落地，通用投影与微裁剪待实施 |
| [重复工具调用：四项调整](repeated-tool-execution-plan.md) | 首版已落地，默认观测；包含开源比较、误拦边界和验收方案 |
| [输出截断后的恢复方案](output-truncation-recovery.md) | 开源实现与恢复建议；保留方案分析时的基线与待验证项 |
| [任务可靠性修复设计](task-reliability-recovery-design.md) | 待实施；进程取消、流式重复恢复、交付版本检查的接口与验收 |
| [Web 实时任务工作台设计](web-execution-workbench-design.md) | 三阶段已接入 Web UI；保留界面设计与实现对应 |
| [Web 交互原型](bot-execution-workbench.html) | 使用虚构数据的 HTML 演示，不执行真实任务 |

## 压缩策略与发布规则

按候选比较、实施方案和后续变更的顺序阅读；各次实验的旧默认值保留在原记录中。

| 文档 | 状态与用途 |
|---|---|
| [上下文压缩重设计候选](context-compaction-redesign-options.md) | 四类架构候选；A+D 有评测原型，生产默认入口未接入 AD |
| [先复用前缀、失败后独立摘要](compaction-prefix-fallback-design.md) | `a_fallback` 的原始设计；已实现，适用边界见对应实测 |
| [压缩正确性与实现复杂度的取舍](compaction-correctness-complexity.md) | 精简提示已实现；保留原决策及本地、真实模型验证边界 |
| [摘要改进：保留关键证据与判断状态](compaction-evidence-improvement-design.md) | 后续候选设计；强制引用、固定证据预算与跨轮继承暂缓 |
| [默认启用双路径压缩](compaction-dual-path-default.md) | 默认 `a_fallback`、继承主模型 thinking 与空闲 `/compact` 的变更记录 |
| [取消摘要正文发布门限](compaction-summary-body-limit-removal.md) | 取消正文硬门限，保留摘要软目标与输出截断检查的变更记录 |
