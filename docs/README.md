# 文档索引

文档按用途分目录；本页提供阅读入口，完整清单见各分类索引。
第一次了解项目，建议按[项目 README](../README.md) → [实现状态](architecture/implementation-status.md) → [总体设计](architecture/design.md)阅读。

## 按用途查找

| 分类 | 内容 | 常用入口 |
|---|---|---|
| [使用指南](guides/README.md) | 功能使用与配置 | [Web 工作台](guides/web-workbench.md)、[自定义 Agent](guides/custom-agents.md) |
| [架构与实现](architecture/README.md) | 项目边界、运行机制、上下文与记忆 | [请求组装](architecture/context-assembly.md)、[长任务控制](architecture/termination.md)、[长期记忆](architecture/markdown-memory.md) |
| [设计演进](designs/README.md) | 实施方案、行为变更、候选设计与交互原型 | [Skill 首版方案](designs/active-skill-layer-removal-plan.md)、[双路径默认策略](designs/compaction-dual-path-default.md) |
| [评测指南与记录](evaluations/README.md) | 运行方法、指标、公开基准、压缩实验和功能验证 | [整体测试矩阵](evaluations/context-evaluation-matrix.md)、[Flash 三轮复测](evaluations/context-strategy-flash-repeats.md)、[公开基准结果](evaluations/public-benchmark-40-results.md) |
| [调研与对比](research/README.md) | 开源框架、压缩输入、缓存与正确性研究 | [框架对比](research/context-framework-comparison.md)、[Claude Code 压缩](research/claude-code-compaction.md) |
| [面试与求职资料](career/README.md) | 项目表达、开发经历与招聘样本 | [牛客 Agent 面经专项](career/nowcoder-agent-interview-handbook.md)、[上下文面试手册](career/context-management-interview-guide.md)、[负责人简历成稿](career/resume-materials.md) |
| [历史归档](archive/README.md) | 已被替代的方案、重复问答与早期实验 | 归档原因、适用边界和替代入口见分类索引 |
| [数据附件](data/README.md) | 机器可读结果、来源清单与开发历史 | 从对应报告进入，避免脱离样本与计量口径解读 |

## 按问题阅读

- **理解一次模型请求如何构成**：[请求组装](architecture/context-assembly.md) → [输入 token 计数](architecture/input-token-calibration.md) → [单摘要压缩](architecture/recoverable-context-compaction.md)。
- **查压缩策略的变更和依据**：[设计演进](designs/README.md) → [Flash 三轮复测](evaluations/context-strategy-flash-repeats.md) → [结果复核与归因边界](evaluations/context-strategy-flash-causal-analysis.md)。
- **定位反复调用工具或任务不结束**：[长任务控制](architecture/termination.md) → [四项调整方案](designs/repeated-tool-execution-plan.md) → [修复验证](evaluations/repeat-guard-validation.md)。
- **检查 Skill 的交付与恢复**：[首版方案](designs/active-skill-layer-removal-plan.md) → [实施与验证](evaluations/skill-context-validation.md) → [后续生命周期设计](designs/skill-run-lifecycle-design.md)。
- **运行任务集并解释成绩**：[通用任务评测](evaluations/evaluation.md) → [Terminal-Bench](evaluations/terminalbench-evaluation.md) / [SWE-bench](evaluations/swebench-evaluation.md) → [公开基准部分结果](evaluations/public-benchmark-40-results.md)。

## 维护约定

- 新文档放入对应分类目录，并在该目录的 `README.md` 登记；本页只保留分类和常用入口。
- 同一主题优先更新已有主要文档；配套分析和面试材料通过链接引用，避免复制整套实现说明。
- 架构说明随代码更新。设计文档区分已实现、部分实现和候选方案；分类不代表实现状态。
- 研究和评测保留原版本、日期、模型、样本与指标口径；目录整理不代表重新测量或更新结论。
- 被替代或重复的文档移入 `archive/`，在归档索引注明原因和替代入口，并同步修复引用。
- JSON 附件放在 `data/`；已归档实验的独有附件随记录保留在 `archive/`。历史提交清单和来源哈希中的旧路径按采集版本解释。
- 文档链接相对当前文件书写；命令和源码路径以仓库根目录为基准。迁移时同时检查跨目录文档、源码、脚本和附件引用。
