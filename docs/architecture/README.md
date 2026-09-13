# 架构与实现

返回[文档索引](../README.md)。建议先读实现状态和总体设计，再按模块查阅。
各文档保留自己的核对日期；近期行为调整及其验证见[设计演进](../designs/README.md)。

## 项目与运行

| 文档 | 用途 |
|---|---|
| [实现状态](implementation-status.md) | 已完成能力、环境验收和延期项 |
| [总体设计](design.md) | 产品边界、模块职责和设计基线；落地进度参照实现状态 |
| [自动终止与长任务控制](termination.md) | 进展检测、重复调用限制、恢复、硬预算和统一收尾 |
| [项目思维导图](project-mindmap.xmind) | 模块与上下文链路的阅读辅助；具体行为和指标以对应说明为准 |

## 上下文与记忆

| 文档 | 用途 |
|---|---|
| [模型上下文分块与组装顺序](context-assembly.md) | 请求顺序、角色边界、预算和超限处理的主要入口 |
| [输入 token 计数与安全余量](input-token-calibration.md) | Tokenizer、输入估算、预算接入与历史请求核对 |
| [可恢复的单摘要上下文压缩](recoverable-context-compaction.md) | 压缩不变量、事务发布、原文恢复、配置和测试 |
| [Markdown 长期记忆](markdown-memory.md) | 文件布局、提取生命周期、读取路径和配置 |
| [Memory Router 设计与验收](memory-routing.md) | 按需检索、归因证据门禁、行为评测与历史 canary 边界 |

Skill 历史交付的实现范围见[首版方案](../designs/active-skill-layer-removal-plan.md)与[验证记录](../evaluations/skill-context-validation.md)。
压缩默认值与摘要长度规则的后续调整见[双路径默认策略](../designs/compaction-dual-path-default.md)和[取消正文门限](../designs/compaction-summary-body-limit-removal.md)。
