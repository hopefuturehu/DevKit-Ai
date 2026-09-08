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
| [项目思维导图](project-mindmap.xmind) | 项目模块与上下文链路的可视化阅读辅助；具体行为和指标口径以对应说明为准 |

## 上下文与记忆：当前实现

| 文档 | 用途 |
|---|---|
| [模型上下文分块与组装顺序](context-assembly.md) | 请求顺序、角色边界、预算和超限处理的主要入口 |
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

这些文档仍对应仓库中的评测脚本和测试，因此保留在当前目录；其中带日期的结果是历史基线，不表示本次整理重新运行了评测。

## 待实施设计与配套分析

| 文档 | 状态与用途 |
|---|---|
| [Web 实时任务工作台设计](web-execution-workbench-design.md) | 尚未实现的提案与交互原型；执行进度、工具详情、补充要求和历史恢复 |
| [Skill 上下文生命周期设计](skill-run-lifecycle-design.md) | 尚未实现的提案；Run 隔离、正文交付、裁剪和恢复 |
| [Skill 卸载与缓存复用调研](skill-unloading-cache-comparison.md) | 上述提案的源码依据与成本取舍；固定 checkout 调研 |
| [历史会话裁剪容量分析](history-pruning-analysis.md) | 2026-09-08 的离线容量回放，支撑待实施设计；不代表生产裁剪收益 |
| [裁剪容量统计数据](data/history-pruning-summary.json) | 上述分析的参数、统计和逐压缩结果 |
| [开源 Agent 框架上下文管理对比](context-framework-comparison.md) | 固定本地源码快照的跨框架研究；本项目当前行为参照模块说明 |

## 面试与历史资料

- [上下文管理架构与面试手册](context-management-interview-guide.md)：统一的面试准备入口，包含请求组装、项目深挖、指标边界和源码索引。
- [历史归档](archive/README.md)：旧压缩方案、故障复盘、被替代的简短问答和单次评测记录。

## 维护约定

- 当前实现说明随代码更新；提案明确标注“尚未实现”，研究与评测结果记录版本、日期和样本边界。
- 同一主题优先更新已有主要文档；摘要、面试材料和配套分析通过链接引用，避免复制整套实现说明。
- 方案被替代或文档重复时移入 `archive/`，保留历史正文，在归档索引记录原因和替代入口，并同步修改引用。
- 当前分析的机器可读附件放在 `data/`；已归档实验的附件随历史记录保存在 `archive/`。
