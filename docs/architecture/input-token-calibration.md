# 输入 token 计数与安全余量

2026-09-10：已接入官方 DeepSeek V4 Flash tokenizer 和消息编码，替代该模型完整请求的
字符比例估算。适用于官方 DeepSeek 端点、精确模型名 `deepseek-v4-flash`；其他模型继续
使用已有计数路径，不套用 Flash 的词表。

## 使用

安装项目依赖后，一次性下载固定版本的 tokenizer（约 6.4 MB）：

```bash
.venv/bin/python -m bot.providers.install_tokenizer
```

当前工作环境已完成安装。词表保存在 `~/.cache/bot/tokenizers/`，下载及读取均校验
SHA-256；正常请求不会联网下载词表。官方编码代码随项目附带，并保留 MIT 许可与来源。

## 实际行为

- 计数与发送使用同一 Provider payload，包含实际消息、工具 schema 和生效的 thinking
  模式。官方 V4 编码器在 chat 模式省略历史 reasoning，在带工具的 thinking 模式保留
  已传入的 reasoning；不修改持久历史，也不为了计数伪造 reasoning。
- `tokens` 是本地 tokenizer 预测；`budget_tokens = tokens + max(256, ceil(tokens × 5%))`。
  这只是工程余量，不是统计置信上界。原有输出、协议及安全预留仍由上下文预算扣除。
- 词表缺失或无法编码时明确标记 `heuristic:deepseek_tokenizer_unavailable`，采用旧粗估
  加 `max(2048, ceil(tokens × 50%))` 的降级预算，不把旧估算冒充 tokenizer 结果。
- 主循环自动压缩触发、请求装箱和最终发送检查，以及现有摘要范围规划、摘要请求发送、
  A/D 交接生成与发布检查，都使用模型输入预算。原始启发式估算仍用于单条内容的初步
  排序、尾部选择及外置；它不再单独决定受支持模型完整请求是否能发送。
- 请求整体可容纳时，Planner 保留完整候选历史，避免旧字符估算过高导致无谓丢弃。
  必须重新装箱时仍以完整工具调用/结果组为单位。

Provider 的 usage metadata 记录 `input_token_estimate`（预测、预算、来源、有效模式）、
`input_token_error`（API prompt_tokens 减预测）和 `input_budget_exceeded`。主请求的
`MODEL_USAGE` 与摘要请求完成事件均保存这些字段。没有有效 usage 的请求不生成实际误差，
也不把缓存未命中量当作完整输入。估算不会覆盖 API 原始 usage 或填入 `exact_tokens`。

5% 余量没有自动调小，也没有在线训练系数。后续先观察误差；模型或服务模板变化后再通过
独立样本决定是否调整。

## 已有请求的离线核对

使用原 L1 v2 保存的请求及 API usage 回算，没有新增模型调用，也没有重新跑任务：

```bash
.venv/bin/python scripts/analyze_input_token_estimates.py \
  --input .bot/benchmarks/handoff-l0-l1-20260909-v2 \
  --output docs/data/input-token-calibration.json
```

517 条有输入 usage 的记录、472 个不同请求 hash，覆盖 6 个历史来源，包括预热、正式
续跑及归档尝试；另外 15 条缺输入 usage，未用于误差计算。每份原始请求通过 hash 校验。
所有记录均为 `thinking=disabled`，不能外推为生产 thinking 模式已得到同样精度。

| 指标 | 原字符估算 | 官方编码＋tokenizer |
|---|---:|---:|
| 最大绝对误差 | 41,806 tokens | **3 tokens** |
| 最大低估量 | 27,418 tokens | **1 token** |
| 绝对相对误差中位数 | 22.47% | **0.00598%** |
| 绝对相对误差 p95 | 145.48% | **0.02276%** |

新预算在本批记录上没有被 API 输入突破（0/517）。这是历史样本覆盖情况，不是未来超限率
保证。相对误差分母为 API 输入，p50/p95 使用 nearest-rank；重复请求不当作独立任务。
逐来源结果见[机器可读数据](../data/input-token-calibration.json)。

三个原始预热请求可直观看出变化：

| 请求 | 原估算 | 新预测 | API 输入 |
|---|---:|---:|---:|
| near-budget | 87,125 | 114,539 | 114,538 |
| chess-large-tail | 74,709 | 33,001 | 32,998 |
| sympy-in-progress | 72,980 | 40,762 | 40,759 |

本轮只验证计数与保护路径，不更新原 L1 的容量/成本结论；新策略的整题费用和触发频率仍待
L2 验证。词表不可用的降级预算也不构成任意输入的数学上界。

本次验证：`pytest tests/unit tests/integration -rs` 为 **425 通过、7 跳过**（需要显式启用
Docker 的既有验收测试）；Ruff 检查通过，wheel 构建及 tokenizer 代码/许可证打包检查通过。
新增测试覆盖模式切换、词表缺失、usage 对照、旧 Provider 兼容路径、主请求触发/发送保护、
工具原子组装箱和交接预算；本地拒绝的摘要不会被计为已发送请求或产生虚构费用。

## 来源与版本

- [官方编码实现与说明](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/tree/60d8d70770c6776ff598c94bb586a859a38244f1/encoding)
- [DeepSeek token 说明](https://api-docs.deepseek.com/quick_start/token_usage/)
- 词表 revision：`60d8d70770c6776ff598c94bb586a859a38244f1`。
- tokenizer SHA-256：`8f9f37ca37fdc4f5fd36d5cf4d3b0e8392edb4e894fd10cc0d70b4957c8633cf`。

本地预测和 API 仍有 1—3 tokens 差异，因此保留余量并持续记录实际误差，不声明服务端精确计数。
