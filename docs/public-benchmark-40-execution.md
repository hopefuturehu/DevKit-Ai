# 40 题公开基准执行记录

用户已授权使用 `deepseek-v4-flash` 运行 Terminal-Bench 2.1 的 20 题与 SWE-bench Lite 的 20 题，
分析失败并记录文档，总费用不设上限。此授权取代预检时的零生成预算。

2026-09-09 08:10（Asia/Shanghai）按用户要求停止本轮执行，实际覆盖 18/40 个不同题目
（Terminal 15、SWE 3）。剩余任务及排队恢复没有继续运行；已有结果和失败分析见
[部分结果报告](public-benchmark-40-results.md)。

## 冻结范围

任务清单：[public-regression-40-v1.json](../evals/public-regression-40-v1.json)。
SHA-256：`4cc70058f018ab0dc66b5401be1be28345751e10895d720389fab711368e2d1c`。

种子 `bot-public-regression-v1`。Terminal 的 16 个类别、SWE 的 12 个仓库各至少一题；
再向原始样本最多的类别/仓库各分配一题，分别补足 20。同规模按名称排序，组内按 `SHA256(seed:id)` 排序。
不按模型成绩、镜像是否缓存或参考解是否通过换题。SWE 的 issue 标题用于人工检查修复类型覆盖，未读取模型运行结果。
冻结脚本为 `scripts/freeze_public_benchmark_suite.py`；官方 SWE 参考补丁和测试数据只写到评测侧，未写入清单。

## 运行约定

- 使用冻结源码和独立任务容器；模型改为 `deepseek-v4-flash`，不启用子 Agent。
- Harbor 固定 0.20.0，SWE 官方 harness 固定 4.1.0。记录实际镜像 ID/digest，不只保留可变 tag。
- 保留 Bot 默认 131072 token 上下文配置和 Provider 默认 thinking 行为；这是 Bot 配置，不是模型服务的最大上下文。
- Terminal 每题沿用官方 agent/verifier timeout，内部最多 240 步，内部时间至少覆盖官方 task timeout。
- SWE 每题最多 240 步、7200 秒；费用上限显式关闭。步骤/时间耗尽单独归因。
- 初期串行运行。Docker 虚拟磁盘约 58 GiB，开始时仅约 10.6 GiB 可用，与宿主机 155 GiB 空闲不是同一容量。
  按需准备所选镜像；完成一题后清理本次新增且不再使用的镜像，保留所有运行前已有镜像。
- 先从清单内完成 3 个 Terminal、3 个 SWE 的冒烟，再展开其余任务；每题先检查官方参考解及负向控制。
- 每个 attempt 独立目录和 `run_id`。保留首次失败和后续复测，不用复测成功覆盖原始成绩。
- 完成标准是全部 40 题有真实模型执行及官方判分证据，失败有依据充分的分析；环境故障不能冒充模型失败。

模型服务公开说明 Flash 当前别名对应 V4-Flash-0731，默认 thinking 为 high；
官方价格区分峰谷时段和缓存命中。因此 Bot 的双单价费用字段仅作为估算，不作为实际账单。
来源：[模型与价格](https://api-docs.deepseek.com/quick_start/pricing/)、
[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。

原始产物目录：`artifacts/public-benchmark/20260909-flash/`。
执行中的成绩、失败分析和对照实验见 [结果记录](public-benchmark-40-results.md)，
可核对的摘要与原始产物哈希见 [证据清单](public-benchmark-40-evidence.json)。
