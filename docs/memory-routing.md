# Memory Router 设计与验收

## 目标与非目标

这次修改只处理一个明确故障链：自动记忆作为最新真人输入之后的 synthetic `user` 被重复回放，
模型把派生文本误认为用户刚说过的话。默认策略改为：

```text
真实用户轮次
  -> deterministic Router: NONE | SUGGEST_SEARCH | REQUIRE_SEARCH | REQUIRE_EVIDENCE
  -> system runtime note（不含记忆正文）
  -> assistant Tool Call
  -> tool 角色的一次性检索/证据正文
  -> 后续窗口只保留收据
```

它不承诺“模型永不误解历史”，也不把 Router 当语义检索模型。确定性测试证明协议和数据边界；
真实模型 A/B 才能证明误归因率、漏召回率和成本的经验变化，两类结论不能混用。

## 与 eager 方案的直接取舍

| 维度 | 旧 eager-tail | `eager` 兼容模式 | 默认 Router/on-demand |
|---|---|---|---|
| 无关请求输入成本 | 每轮回放索引 | 每轮回放索引 | 不回放自动索引 |
| 最新 user 归因歧义 | 高：索引在真人输入后 | 较低：索引在 Transcript 前且有边界 | 最低：正文只作为 Tool Result |
| 隐式相关记忆召回 | 高，不依赖模型主动搜索 | 高 | 取决于词法候选和模型是否采纳 `SUGGEST` |
| 明确跨轮依赖 | 已带全文，无额外调用 | 已带全文，无额外调用 | 至少增加一次模型往返和一次 `search_memory` |
| 用户历史归因 | 无原始证据门禁 | 无原始证据门禁 | `search -> evidence` 两阶段强制门禁 |
| Prompt cache | 易变索引会改变请求 | 索引变化会使后续前缀失效 | 无关轮次稳定；检索轮次多一个动态 Tool turn |
| 回滚 | 原实现 | `memory.context_mode="eager"` | 默认 |

因此不能笼统声称 Router “全面更好”。它用明确历史任务上的额外延迟和可能的隐式召回损失，换取
无关轮次的 token 节省以及高风险归因的证据约束。

额外模型往返仍计入 `agent.max_steps`、费用和 Tool 输出预算；配置得非常紧的 Run 可能在检索后
没有剩余 step 生成答案。Router 会查询 Provider capability：支持命名函数选择时发送命名
`tool_choice`；不支持时发送 `auto`，但由 Agent 在持久化和执行前检查必需 Tool 名称。提前给答案或
调用错误 Tool 的响应会被完整丢弃并有界重试，因此兼容路径仍是 fail-closed，而不是只靠提示。
一次性交付意味着后续若再次需要正文必须重新检索。这些是需要在真实 A/B 中计量的成本，不应隐藏
在“安全性提高”的总分里。

## 已实现的可证伪测试

下面每一行都限定一个自变量和一个可直接观察的失败条件。它们不是把很多机制揉在一起的“大
而泛”任务。

| 假设 | 固定条件与唯一变化 | 观测量 / 失败条件 | 自动化测试 |
|---|---|---|---|
| H1 默认请求不再把自动记忆冒充新 user | Store 中预置活动自动记忆；只运行一个无关真实 prompt | 实际 `ModelRequest.messages` 出现 `name=automatic_memory`，或除真实 prompt 外还有该 synthetic user，即失败 | `test_on_demand_memory_does_not_append_automatic_memory_to_plain_prompt` |
| H2 Router 不因一般代码词汇误触发，同时识别显式历史依赖 | 同一 Store、同一阈值，只替换最小 prompt | `previous_result` 被升级、`按上次` 未升级、归因问题未到 `REQUIRE_EVIDENCE`，任一即失败 | `test_memory_router_distinguishes_history_dependency_from_lexical_noise` |
| H3 `REQUIRE_SEARCH` 是协议门禁，不只是提示 | 一条路径支持 named choice；另一条路径不支持且先返回正文或错误 Tool | 支持时未发送命名 choice，或任一路径把未包含必需 Tool 的响应写入 Transcript/执行错误 Tool，即失败 | `test_required_memory_search_is_forced_and_delivered_once`；`test_required_memory_tool_gate_discards_unsupported_final_text`；`test_required_memory_gate_rejects_wrong_tool_without_named_tool_choice` |
| H4 错误自动记忆不能单独支撑用户归因 | 自动记忆写“用户贴出全文”；原始 user 否认，assistant 反向声称 | 请求顺序不是 `search -> evidence`，证据未保留原始 role，或最终证据窗口看不到 user 否认，即失败 | `test_historical_attribution_forces_search_then_original_evidence` |
| H5 检索正文不会在后续窗口反复回放 | 第 n 步固定 search，第 n+1 步固定另一 Tool，第 n+2 步结束 | n+1 看不到全文、n+2/SQLite 仍有全文，任一即失败 | `test_required_memory_search_is_forced_and_delivered_once` |
| H6 提取器不会把 Assistant 的用户归因与 User 否认合并成事实 | 两条固定证据：user 否认、assistant 肯定；模型候选固定为肯定事实 | `_validate_candidates` 返回该候选即失败 | `test_extractor_rejects_assistant_claim_about_user_when_user_denies_it` |
| H7 中文词法命中是可解释的，不靠不可见启发式 | 固定记忆“测试命令…”和固定 query“请验证测试命令” | 无命中、缺少 `测试/命令` matched terms、score 不达阈值即失败 | `test_memory_search_reports_why_a_chinese_query_matched` |
| H8 `eager` 回滚仍可用且不会回到尾部位置 | 同一 Runner 只把 `context_mode` 从默认切到 `eager` | 自动索引不出现、缺边界，或 Planner 把它排到 Transcript 后，即失败 | `test_agent_loads_physical_memory_files_with_separate_trust`；`test_planner_renders_stable_prefix_before_history_and_volatile_suffix` |

这些测试能证明 H1-H8 所写的结构和状态转换，不能证明某个真实模型的自然语言回答一定正确。

## 真实模型 trade-off 实验

### 已实现的可重复入口

离线先验证评测器、fixture、路由轨迹和评分器本身：

```bash
.venv/bin/python scripts/run_memory_routing_behavior.py \
  --provider scripted \
  --output .bot/benchmarks/memory-routing-scripted
```

真实 Provider 使用同一 `AgentRunner`、SQLite、Markdown Memory、Tool schema 和 Provider
adapter；强制要求 eager/on-demand、四个预注册桶和至少 3 次交错重复，且必须显式设置费用上限：

```bash
RUN_MEMORY_ROUTING_LIVE=1 \
  .venv/bin/python scripts/run_memory_routing_behavior.py \
  --provider live --repeat 3 --max-cost-usd 0.50 \
  --output .bot/benchmarks/memory-routing-live
```

每个 case 写出 `artifacts/summary.json` 和 `artifacts/requests.jsonl`；attempt 级写出
`summary.json`、`cases.csv`，顶层 `aggregate.json` 汇总 pass rate、各 gate/诊断通过率、
中位数和成对差值。语义正确、Router 决策、必要检索、原始证据和 usage 是质量 gate；“只输出
marker”和“是否走最短 Tool 路径”是独立诊断，不会把答对但多解释一句误报成 Router 失败。

### 2026-08-29 真实 canary 结果

环境为官方 DeepSeek Chat Completions 端点、`deepseek-v4-flash`、temperature 0；一个固定 fixture/
桶，eager 与 on-demand 各执行 3 次，共 24 个 case。最终复跑结果为：

- 24/24 case 通过；四个预注册假设均为 3/3；Provider usage 完整；总费用 `$0.127851`；
- 两种方案四桶的事实答案都是 3/3，归因冲突也都以原始 `role=user` 的否认为准；
- on-demand 的无关请求没有 Memory Tool、没有自动记忆重放；相关三个桶均正确召回；
- DeepSeek 在三个相关桶都倾向二次 search，并继续读 evidence，所以 on-demand 的最短 Tool 路径率
  是 0/3。这个结果被保留为效率代价，不影响语义/门禁 pass。

| 桶 | on-demand 相对 eager 的模型请求中位差 | 输入 token 成对中位差 | 费用成对中位差 | 端到端成对中位差 |
|---|---:|---:|---:|---:|
| `irrelevant` | 0 | -355 | -$0.000647 | -1.263 s |
| `implicit_relevant` | +1 | +1,834 | +$0.001693 | +2.323 s |
| `explicit_history` | +1 | +1,374 | +$0.001094 | +0.894 s |
| `attribution_conflict` | +1 | +1,698 | +$0.001448 | +0.501 s |

机器可读的脱敏聚合结果保存在
[`memory-routing-live-canary-2026-08-29.json`](memory-routing-live-canary-2026-08-29.json)。原始本地
产物目录为 `/private/tmp/bot-memory-routing-live-20260829-v4`，不纳入 Git。

真实运行还发现并修复了两个原评测无法发现的 Provider 兼容问题：DeepSeek thinking mode 拒绝
命名 `tool_choice`；临时关闭 thinking 生成的 Tool Call 又会使后续 thinking 请求因缺失
`reasoning_content` 返回 400。最终实现把官方 DeepSeek 标记为不支持 named choice，并由 Agent
执行指定 Tool 名称门禁；Provider 发送边界还会在显式命名 choice 或无 reasoning Tool 链下保持
non-thinking，避免构造无效 wire 请求。该行为与
[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 要求携带 `tools` 时
回传先前 reasoning 的协议一致。

这组 canary 能证明“当前模型和固定四个 fixture 上，生产协议可跑通且预注册机制结果可重复”，也能
量出这些 fixture 的实际方向和数量级。它只有每桶一个语义样本，不能据此宣称总体误归因率、总体
漏召回率或统计显著性；下面的多样本实验仍是作此类外推的门槛。

### 多样本统计实验门槛

要对“误归因减少多少、漏召回增加多少、成本变化多少”下结论，使用成对、最小对照数据，而不是
一个长任务总分。冻结模型版本、temperature=0、Tool schema 和历史数据；每条样本在
`eager` 与 `on_demand` 各运行 3 次并交错顺序。至少包含以下四个互斥桶，每桶不少于 30 条：

| 桶 | 唯一变化 | 主要指标 | 预先约定的判定 |
|---|---|---|---|
| `irrelevant` | prompt 与所有自动记忆无关 | Router FP、输入 token、模型请求数 | `on_demand` 不调用 Memory Tool；质量不低于 eager；输入 token 更低 |
| `implicit_relevant` | prompt 不说“之前”，但词法上需要一个已存流程 | Router FN、任务事实准确率 | 统计 `SUGGEST` 后未检索造成的失败；这是 Router 的主要预期代价 |
| `explicit_history` | prompt 明确含“按上次/之前约定” | 检索召回、额外请求、端到端延迟 | 必须 search；答案引用正确 key；单次额外往返单独报告，不混入质量分 |
| `attribution_conflict` | 自动记忆与原始 user 消息相反 | 用户误归因率、evidence 调用率 | 只有原始 `role=user` 决定标签；未读 evidence 或采信 assistant 均失败 |

输出必须逐样本保存 Router 决策、Tool 调用序列、Provider usage、延迟、最终结构化标签和证据位置。
质量差异用同一样本的配对结果报告；二元错误率使用 McNemar 检验或配对 bootstrap 95% 区间，
token/延迟报告中位数及配对差值区间。未跑完每桶至少 30 条的多样本真实 Provider 实验前，可以
报告上述固定 canary 的协议、质量和成本结果，但不能宣称已经测得真实模型的总体误归因率、总体
漏召回率或总体质量提升。
