# 文档索引

本目录保留当前实现说明、可复用的评测指南，以及仍在讨论的设计和配套分析。
已被替代的方案、重复问答和单次历史评测产物移至 [archive/](archive/README.md)；归档原因和替代入口见归档索引。

了解项目可按“实现状态 → 总体设计 → 对应模块说明”阅读。设计提案不代表已实现能力，历史实验数字只适用于文中标明的版本、样本和测量口径。

## 项目与运行

| 文档 | 用途 |
|---|---|
| [项目 README](../README.md) | 安装、配置、常用命令和验证入口 |
| [实现状态](implementation-status.md) | 已完成能力、环境验收和延期项 |
| [总体设计](design.md) | 产品边界、模块职责和设计基线；落地进度参照实现状态 |
| [自动终止与长任务控制](termination.md) | 进展检测、恢复、硬预算和统一收尾 |
| [Markdown 自定义 Agent](custom-agents.md) | 定义格式、信任、父子交互和 worktree 交付 |
| [Web 任务工作台](web-workbench.md) | 启动与使用、状态语义、历史恢复、文件产物、协议和验证 |
| [Web 实时任务工作台设计](web-execution-workbench-design.md) | 已落地的界面设计与原始交互原型 |
| [项目思维导图](project-mindmap.xmind) | 项目模块与上下文链路的可视化阅读辅助；具体行为和指标口径以对应说明为准 |

## 上下文与记忆：当前实现

| 文档 | 用途 |
|---|---|
| [模型上下文分块与组装顺序](context-assembly.md) | 请求顺序、角色边界、预算和超限处理的主要入口 |
| [Skill 历史交付实施与验证](skill-context-validation.md) | 移除独立 Active Skill 层、Run 隔离、压缩恢复、会话迁移及测试证据 |
| [移除 Active Skill 独立层：首版方案](active-skill-layer-removal-plan.md) | 已实现 A–C 的数据结构与恢复路径；D 已有行为 smoke，完整成本评测待补 |
| [输入 token 计数与安全余量](input-token-calibration.md) | DeepSeek V4 Flash tokenizer、usage 误差记录、预算接入和 517 条历史请求核对 |
| [可恢复的单摘要上下文压缩](recoverable-context-compaction.md) | 压缩不变量、版本发布、原文恢复、配置与测试 |
| [Markdown 长期记忆](markdown-memory.md) | 文件布局、提取生命周期、读取路径和配置 |
| [Memory Router 设计与验收](memory-routing.md) | 按需检索、归因证据门禁、行为评测与历史 canary 的适用边界 |

## 评测指南与指标

| 文档 | 用途 |
|---|---|
| [通用任务评测](evaluation.md) | 隔离 fixture、文件/JSON/容器验收、真实工具结果、失败分类和复现清单 |
| [上下文整体测试矩阵](context-evaluation-matrix.md) | 各评测入口、覆盖范围和仍缺的验收 |
| [上下文效率评测指标口径](context-efficiency-benchmark-metrics.md) | Trace、聚合指标、门禁和差值公式 |
| [长任务上下文缓存评测](context-cache-benchmark.md) | Fast / Soak 运行方法、缓存模拟口径与历史布局实验 |
| [上下文压缩有效性评测](context-compaction-effectiveness.md) | 故障矩阵、Provider 回放、推广门禁与历史确认结果 |
| [Terminal-Bench 评测](terminalbench-evaluation.md) | 任务集运行、预算、诊断和结果归档 |
| [SWE-bench 评测](swebench-evaluation.md) | 单实例准备、容器执行和 prediction 产物 |
| [公开基准执行预检（2026-09-09）](benchmark-preflight-2026-09-09.md) | 找回 20＋20 任务范围、环境实测、官方判分控制实验及剩余阻塞 |

这些文档仍对应仓库中的评测脚本和测试，因此保留在当前目录；其中带日期的结果是历史基线，不表示本次整理重新运行了评测。

## 设计演进与配套分析

| 文档 | 状态与用途 |
|---|---|
| [重复工具调用：四项调整与开源方案比较](repeated-tool-execution-plan.md) | 待实施：稳定证据、新证据进展、独立检测和执行前限制；含误拦边界与验收方案 |
| [Skill 上下文生命周期设计](skill-run-lifecycle-design.md) | 原始完整提案；Run 隔离、历史交付和恢复已落地，通用投影与微裁剪待实施 |
| [Skill 卸载与缓存复用调研](skill-unloading-cache-comparison.md) | 首版与后续裁剪设计的源码依据；固定 checkout 调研，区分历史建议与当前实现 |
| [历史会话裁剪容量分析](history-pruning-analysis.md) | 2026-09-08 的离线容量回放，支撑待实施设计；不代表生产裁剪收益 |
| [裁剪容量统计数据](data/history-pruning-summary.json) | 上述分析的参数、统计和逐压缩结果 |
| [开源 Agent 框架上下文管理对比](context-framework-comparison.md) | 固定本地源码快照的跨框架研究；本项目当前行为参照模块说明 |
| [Claude Code 压缩机制调研](claude-code-compaction.md) | 官方资料核对；摘要请求、缓存、thinking、上下文恢复及失败边界，区分本机版本与在线文档 |
| [上下文压缩重设计候选](context-compaction-redesign-options.md) | 四类候选及 A+D 优先组合；有评测原型，生产默认入口尚未接入 |
| [A+D 交接收益测试方案](context-handoff-evaluation-plan.md) | L0/L1 已执行，L2/L3 待验证；固定状态与整题实验、缓存计费、质量门禁和统计判据 |
| [A+D 交接 L0/L1 量化结果](context-handoff-l0-l1-results.md) | L0 37/37；L1 36/36，完整样本的容量、缓存、费用、质量及失败归因 |

## 面试与历史资料

- [中国大陆 Agent 招聘调研](agent-hiring-mainland-2026-09.md)：2026-09-10 的岗位样本、来源与时效、招聘关注点，以及 bot 简历素材的匹配顺序和能力边界。
- [历史开发记录与简历素材库](resume-materials.md)：覆盖全项目的 42 条候选描述、开发时间线、指标边界与提交证据，按个人实际贡献筛选；完整提交清单见 [development-history.json](data/development-history.json)。
- [上下文管理架构与面试手册](context-management-interview-guide.md)：统一的面试准备入口，包含请求组装、项目深挖、指标边界和源码索引。
- [历史归档](archive/README.md)：旧压缩方案、故障复盘、被替代的简短问答和单次评测记录。

## 维护约定

- 当前实现说明随代码更新；提案明确标注“尚未实现”，研究与评测结果记录版本、日期和样本边界。
- 同一主题优先更新已有主要文档；摘要、面试材料和配套分析通过链接引用，避免复制整套实现说明。
- 方案被替代或文档重复时移入 `archive/`，保留历史正文，在归档索引记录原因和替代入口，并同步修改引用。
- 当前分析的机器可读附件放在 `data/`；已归档实验的附件随历史记录保存在 `archive/`。
