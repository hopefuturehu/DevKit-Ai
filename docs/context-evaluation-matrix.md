# 上下文整体测试矩阵

本文记录上下文机制的整体评测入口。目标不是用一个分数替代所有问题，而是把“是否省 token”、
“是否增加 Tool 调用”“规模增大后是否仍正确”“模型会不会自主检索”和“长任务跨生命周期后是否
丢上下文”拆成可定位的 case。

## 四个补充 case 与原缺口

| Case | 补全的缺口 | 主要覆盖 | 不负责证明 |
|---|---|---|---|
| `context-efficiency-live` | 离线 token 估算不能代表真实 Provider | 同工作负载 `raw/current` A/B；Provider usage、费用、模型延迟、任务质量 | 存储规模与长时间稳定性 |
| `context-blob-scale` | 只有单 blob 正确性，没有数量/体积曲线 | content-addressed 去重、query、range、并发访问、越权隔离、fork 授权、DB 增长、p50/p95/max、query 峰值 Python 分配 | 模型是否会作出正确检索决策；跨进程吞吐；尚不存在的 GC/保留策略 |
| `context-retrieval-behavior` | 综合 case 在 prompt 中直接指定了 query 策略 | 不该查、预览命中、中段命中、空结果恢复、多匹配、多 blob 选址；query/range 与调用次数 | 大量 blob 的存储复杂度；scripted 模式不能替代真实模型能力 |
| `context-full-stack-soak` | 各层和生命周期以前分别测试，缺少组合压力 | AGENTS、memory、active skill、Tool schema 卸载、压缩、Tool Result 外置、runtime resume、session fork、child blob grant | 真实 Provider 波动；真正的 subagent worker 调度和多进程竞争 |

原有 `context-efficiency` 仍是压缩、外置、query 与一次性交付的主要受控 A/B。新增 case 是补充其
外部有效性、规模行为、自主决策和生命周期交互，不改变原 case 的比较口径。

## 1. 真实 Provider A/B

```bash
RUN_CONTEXT_EFFICIENCY_LIVE=1 \
  .venv/bin/python scripts/run_context_efficiency_benchmark.py \
  --provider live --variants raw current --repeat 3 \
  --max-cost-usd 0.50 \
  --output .bot/benchmarks/context-efficiency-live
```

至少运行 3 次，使用中位数降低偶发采样、网络和排队抖动。配置必须包含输入/输出价格。结果中的
token 来自 Provider usage；费用来自运行器按 usage 和配置价格计算的 `RunResult.cost_usd`；延迟
由 Provider stream 开始到结束的单调时钟测量。`aggregate.json` 提供：

- `median_input_tokens`；
- `median_cost_usd`；
- `median_model_latency_seconds`；
- `median_model_request_p95_seconds`；
- `observed_effects` 中 `current` 相对 `raw` 的 token、费用和延迟差。

质量仍由确定性事实、验证 Tool、最终累计报告、Transcript 不可变、压缩状态和协议事件评分。
Live 的硬门禁是所有尝试完成、质量通过、usage 完整且 `current` 输入 token 中位数低于 `raw`；
费用和延迟因供应商价格、缓存计费和网络环境差异，只作为观测结果。

## 2. Blob 规模与查询

快速门禁：

```bash
.venv/bin/python scripts/run_context_blob_scale.py \
  --suite fast --output .bot/benchmarks/context-blob-scale
```

显式长跑：

```bash
RUN_CONTEXT_BLOB_SCALE_SOAK=1 \
  .venv/bin/python scripts/run_context_blob_scale.py \
  --suite soak --output .bot/benchmarks/context-blob-scale
```

`fast` 默认写入 24 个、在 4 KiB/64 KiB/256 KiB 间循环的 blob；`soak` 默认写入 120 个、在
64 KiB/1 MiB/8 MiB 间循环的 blob。命令行可覆盖数量、尺寸、重复次数和线程并发数。

`operations.csv` 的每条操作直接包围 `SQLiteSessionStore` 的 put/query/range 调用并使用
`perf_counter` 计时，`summary.json` 汇总 p50/p95/max。SQLite 表统计给出唯一 blob 数、逻辑
字节数、授权数和数据库文件体积。已知 head/middle/tail marker 验证 offset 和内容；同内容二次写入
验证 SHA-256 去重；未授权 session、fork 和显式 grant 验证访问边界。最大尺寸 query 使用
`tracemalloc` 记录 Python 峰值分配。

当前 `search_context_blob` 会从 SQLite 读出完整 BLOB 后在进程内搜索，因此这个 case 能显示大文件
query 的时间和内存增长，但不会把它误称为数据库内流式搜索。线程并发也会经过 store 的进程内锁，
主要验证排队后的正确性和尾延迟，不代表多进程吞吐。产品当前没有 blob 数量/容量上限和 GC，故
benchmark 报告增长曲线，但没有虚构 retention 通过项。

## 3. 模型检索行为

离线、确定性地验证工作负载和评分器：

```bash
.venv/bin/python scripts/run_context_retrieval_behavior.py \
  --provider scripted \
  --output .bot/benchmarks/context-retrieval-behavior
```

真实模型复验：

```bash
RUN_CONTEXT_RETRIEVAL_LIVE=1 \
  .venv/bin/python scripts/run_context_retrieval_behavior.py \
  --provider live --repeat 3 --max-cost-usd 0.50 \
  --output .bot/benchmarks/context-retrieval-behavior-live
```

任务 prompt 只说明数据源、目标 key 和验收 marker，不要求使用 `query`、range 或任何固定读取
策略。六个场景分别是：

1. `irrelevant`：只需元数据，预期不读取正文；
2. `preview`：证据已在内联预览，预期不增加引用调用；
3. `middle`：证据在正文中段，预期一次 query；
4. `no_match_retry`：scripted 路径首次使用过期 hint 得到空结果并恢复；live 模型若忽略过期 hint
   直接使用权威 key，也视为更优的一次调用路径；
5. `multi_match`：一次 query 返回干扰项和权威项，必须选对事实；
6. `multi_blob`：两个外置结果中只查询目标 source 的 blob。

调用次数和参数直接取自 `tool.requested` 事件；空匹配取自持久化的一次性交付 receipt；目标 blob
通过 source Tool Call 对应的 `context_ref` 与实际 query 参数比对；事实质量由确定性 verify Tool
和最终精确 marker 双重校验。scripted Provider 证明执行链、采集和评分器可重复，只有 live 重复
结果才能证明目标模型在没有策略提示时也会正确选择。

## 4. 全栈长任务与生命周期

快速门禁：

```bash
.venv/bin/python scripts/run_context_full_stack_soak.py \
  --suite fast --output .bot/benchmarks/context-full-stack
```

显式长跑：

```bash
RUN_CONTEXT_FULL_STACK_SOAK=1 \
  .venv/bin/python scripts/run_context_full_stack_soak.py \
  --suite soak --output .bot/benchmarks/context-full-stack
```

`fast` 为 24 轮，`soak` 为 120 轮。每轮经过生产 `AgentRunner` 和真实 SQLite store。运行中固定
触发压缩，关闭并重建 store/runner 模拟 resume，随后 fork session 并继续。每次最终模型请求都
检查 AGENTS marker、SQLite 长期记忆、显式 skill body、主 Tool schema、此前所有事实和单活动摘要。
大量无关 Tool schema 用于确认 schema 已按预算卸载，而主 Tool 经激活后仍可用。

大 Tool Result 必须逐轮外置；fork 必须继承 Transcript 和其中引用的 blob 权限；模拟 child session
在显式 grant 前必须读不到 blob，grant 后可读，而无关 session 始终不可读。这里组合验证的是
subagent 使用的 session/blob 授权边界，不会冒充已经运行了完整 subagent worker 调度；worker
并发、取消和结果合并继续由独立 subagent integration tests 负责。

## 指标如何拿到

| 指标方向 | 原始来源 | 典型指标 |
|---|---|---|
| Token/费用 | Provider `USAGE`；离线 Provider 使用与运行器相同的 `TokenEstimator`；`RunResult.cost_usd` | input/output/total、每轮累计、A/B 中位数、费用 |
| 调用行为 | `MemoryEventSink` 中的 `tool.requested` 事件及 arguments | source/query/range/verify 调用数、空结果重试、目标 reference |
| 请求形态 | Provider wrapper 收到的 `ModelRequest` | message/tool schema 数、活动摘要数、层 marker、重复交付 token |
| 延迟 | 包围 Provider stream 或 SQLite store 操作的 `perf_counter` | p50/p95/max、变体总模型延迟 |
| 存储/内存 | SQLite 表和 DB/WAL/SHM 文件；`tracemalloc` | 唯一 blob、逻辑/磁盘字节、授权数、query 峰值分配 |
| 质量/正确性 | 确定性 marker、verify Tool、Transcript digest、事件类型 | 事实精确率、无虚构、不可变性、访问隔离、无协议/上下文错误 |

所有 case 都把“质量通过”放在“节省更多”之前。token 更少但事实丢失、选错 blob、出现越权访问或
只因任务提前失败而少调用，都不会被记为有效优化。

## CI 分层与仍缺内容

- 普通 CI：scripted `context-efficiency`、缩小参数的 blob-scale、完整 retrieval behavior、full-stack
  fast，以及各 CLI opt-in 门禁；
- 定时/手动：blob-scale soak、full-stack soak；
- 有密钥且配置价格的受控环境：两个 live case，至少重复 3 次。

这次补全后仍没有统一跨模型基线仓库、真实 Provider 的长期定时趋势面板、blob retention/GC、
数据库内流式或索引检索、多进程 blob 压测，以及把真实 subagent worker 调度并入 120 轮 soak。
这些应作为后续 case 独立增加，不能从当前结果外推。
