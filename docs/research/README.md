# 调研与对比

返回[文档索引](../README.md)。这里保存固定版本的源码调查、官方资料核对和设计依据。
外部项目结论以原文的日期、提交及来源为准；本项目行为查阅[架构说明](../architecture/README.md)和[后续变更](../designs/README.md)。

## 框架与 Skill

| 文档 | 研究范围 |
|---|---|
| [开源 Agent 框架上下文管理对比](context-framework-comparison.md) | 固定本地源码快照的跨框架比较 |
| [Claude Code 压缩机制](claude-code-compaction.md) | 摘要请求、缓存、thinking、上下文恢复与失败边界 |
| [Skill 卸载与缓存复用](skill-unloading-cache-comparison.md) | Run 生命周期、历史交付、后续裁剪设计的源码依据 |

## 压缩输入、缓存与正确性

| 文档 | 研究范围 |
|---|---|
| [历史 reasoning 是否进入压缩输入](compaction-reasoning-input.md) | 历史推理的输入路径、对照与离线审计 |
| [压缩 reasoning 的保存与回放](compaction-reasoning-replay.md) | 摘要模型推理的持久化、回传及协议边界 |
| [超大上下文的摘要预算与恢复](oversized-context-compaction.md) | 单次输入预算、分块、重试和超过摘要窗口的处理 |
| [摘要时继续执行任务](compaction-task-continuation-research.md) | 压缩阶段意外工具调用、开源处理方法与缓存取舍 |
| [可复用主会话缓存的压缩实现](cache-reusing-compaction-implementations.md) | Copilot、oh-my-pi 与 DeepSeek Harness 等补充案例 |
| [摘要事实错误](compaction-factuality-research.md) | 事实可靠性调研、改进依据和后续实施顺序 |
| [压缩上限、原文保留与思考模式](compaction-limits-retention-thinking.md) | 固定版本对照；bot 旧门限与默认值的后续变更另有说明 |
| [摘要输出超限与恢复机制](compaction-output-limit-recovery.md) | 2026-09-16 源码核对：正文过长、生成截断、thinking 预算和有界恢复 |
| [摘要生成截断：更多开源恢复方案](compaction-output-truncation-recovery-expanded.md) | 补充 11 个仓库：tact 续写与预算调整、Kimi Code 缩减历史重试、Qwen 连续失败停止 |
| [OpenClaw 与 Hermes 的摘要输出恢复](openclaw-hermes-summary-output-recovery.md) | 最终摘要预算与纠正、length 检查缺口、模型回退和持久化冷却 |
| [开源摘要压缩方案取舍总览](compaction-output-recovery-tradeoffs.md) | 汇总 17 个项目：输入覆盖、生成完整性、最终预算、缓存成本与失败恢复 |
| [oh-my-pi 压缩详解](oh-my-pi-compaction-deep-dive.md) | 多路径维护、滚动分块与输入减半、工具裁剪、单次额度与全局摘要长度的区别 |
