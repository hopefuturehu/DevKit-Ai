# 上下文效率评测指标口径说明

> 状态：当前实现说明
>
> 核对日期：2026-08-28
>
> 依据：`src/bot/evals/context_efficiency.py`、`scripts/run_context_efficiency_benchmark.py`

本文解释 `context-efficiency` benchmark（提交 `12d2df0` 引入）产出的全部指标**如何计算**，
以及 `comparison.json` 中门禁和 effects 的推导公式。配套的评测目标、变体设计和验收规则见
[context-assembly.md](context-assembly.md#压缩和大结果外置的综合验收)。规模、检索行为和全栈
长任务的补充 case 见[上下文整体测试矩阵](context-evaluation-matrix.md)。

## 1. 产物与数据流

每个变体在独立工作目录运行 12 轮确定性任务。执行时，`RecordingContextEfficiencyProvider`
包裹真实或脚本 Provider，**每收到一个模型请求就追加一条 `ContextEfficiencyRequestTrace`**
（在 `stream()` 的 `finally` 中完成，即使请求失败也会记录）。

```text
Scripted/Live Provider ──> RecordingContextEfficiencyProvider ──> 逐请求 Trace
                              │
                              ├── phase  / logical_turn / prompt_tokens
                              ├── output_tokens / requested_tool_names
                              ├── reference_delivery_tokens / replayed_reference_tokens
                              ├── model_latency_seconds
                              └── request_sha256
```

产物文件：

| 文件 | 内容 |
|---|---|
| `artifacts/requests.jsonl` | 每个 Trace 一行（`asdict(trace)`） |
| `artifacts/turns.csv` | 每逻辑轮一行（`turns` 列表） |
| `artifacts/summary.json` | `_benchmark_summary()` 聚合出的 `metrics`/`quality`/`compaction` |
| `comparison.json` | 各变体指标行 + 门禁 + effects（`write_context_efficiency_comparison`） |
| `aggregate.json` | 多次 attempt 聚合（脚本 `_aggregate()`） |

## 2. 单请求 Trace 字段的计算

### phase

```python
_REQUEST_PHASE: _COMPACTION_NAMES.get(request.messages[-1].name or "", "agent")
```

`_COMPACTION_NAMES = {context_compaction_input: "compaction",
context_compaction_repair: "repair", context_compaction_condense: "condense"}`。
即：最后一个消息名字属于压缩工具族 → 归入非 agent 阶段，否则为 `agent`。

### logical_turn

从消息中**倒序**找最后一条 `user` 消息里的 `[CONTEXT_EFFICIENCY_TURN=NNNN]` 标记；
压缩请求没有该标记，使用运行时注入的 `logical_turn_hint`。

### prompt_tokens / output_tokens

```python
prompt_tokens = provider 报告 input_tokens      # 有 USAGE 事件时
              else TokenEstimator.request(messages, tools)   # 启发式估算
output_tokens = provider 报告 output_tokens（缺省 0）
```

- **Scripted（离线）模式**：`ScriptedContextEfficiencyProvider` 每次 `stream()` 发出
  `USAGE` 事件，数值就是 `TokenEstimator` 的估算值。因此离线门禁的全部 token 数字
  都来自启发式估算器，不是真实模型计数。
- **Live 模式**：用 Provider 返回的真实 usage；`require_reported_usage=True` 时若某请求
  没有 usage 会被视为不合格。

### requested_tool_names

从流式事件中收集 `TOOL_CALL_DELTA` 的工具名，去重后保存。

### model_latency_seconds

Live 模式从进入被包装 Provider 的 `stream()` 开始，到流正常结束或异常退出为止，使用
`time.perf_counter()` 测量。它包含网络、服务端排队和流式生成时间，不包含本地 Tool 执行时间。
scripted 模式固定记录为 0，避免墙钟噪声破坏相同 seed 的可重复性断言。

### reference_delivery_tokens / replayed_reference_tokens

对**每个请求的消息列表**执行 `_reference_delivery_cost()`：

```python
for msg in request.messages:
    if msg.role != TOOL or msg.name != "load_context_reference": continue
    if 是回执(disposable_context_delivery): continue      # 跳过短回执
    tokens = TokenEstimator.message(msg)
    delivered += tokens
    if msg.tool_call_id in self._seen_deliveries:
        replayed += tokens                                # 同一份正文第二次出现
    else:
        self._seen_deliveries.add(msg.tool_call_id)
```

关键语义：

- **`delivered`**：本次请求中出现的所有非回执引用正文的 token 数；
- **`replayed`**：同一份正文（按 `tool_call_id` 识别）在此前某请求已经出现、本请求
  再次以完整正文出现时的 token 数。`current` 变体的一次性交付把回读后的正文替换为
  短回执，因此 `replayed == 0`；
- **回执判定**：消息内容可解析为 JSON 且 `status == "disposable_context_delivery"`。
- `query-persistent` 变体用 `_PersistentDeliveryAgentRunner`（不清理 disposable 结果的
  `AgentRunner` 子类）模拟旧行为，正文残留后续请求，因此 `replayed > 0`。

### request_sha256

`messages + tools` 各自转 OpenAI 格式后按 `sort_keys` 序列化，再取 SHA-256。用于
复现和排查请求内容差异，不参与门禁。

## 3. summary.metrics 的聚合公式

| 指标 | 公式 | 说明 |
|---|---|---|
| `input_tokens` | `Σ trace.prompt_tokens`（全部请求） | **最核心指标**，门禁基准 |
| `agent_input_tokens` | `Σ prompt_tokens`（phase == agent） | 主 Agent 请求输入 |
| `compaction_input_tokens` | `Σ prompt_tokens`（phase != agent） | 压缩/repair/condense 请求输入 |
| `output_tokens` | `Σ trace.output_tokens` | |
| `total_tokens` | `input_tokens + output_tokens` | |
| `model_requests` | `len(traces)` | 所有模型请求数 |
| `agent_requests` | `count(phase == agent)` | |
| `compaction_requests` | `count(phase != agent)` | |
| `peak_request_tokens` | `max(trace.prompt_tokens)` | 单请求峰值 |
| `source_tool_calls` | `tool_names.count("context_efficiency_fixture")` | 大结果工具调用 |
| `reference_tool_calls` | `tool_names.count("load_context_reference")` | **实际发出的引用调用**，来自事件总线 `TOOL_REQUESTED`，与 token 无关 |
| `verify_tool_calls` | `tool_names.count("context_evidence_verify")` | 事实校验调用 |
| `reference_delivery_tokens` | `Σ trace.reference_delivery_tokens` | |
| `replayed_reference_tokens` | `Σ trace.replayed_reference_tokens` | 一次性交付的核心证据 |
| `externalized_source_results` | 持久化大结果中 "chars externalized" 消息数 | |
| `persisted_reference_receipts` | 持久化的引用回执消息数 | |
| `cost_usd` | 各 `RunRequest` 的 `cost_usd` 之和（scripted 恒为 0） | |
| `model_latency_seconds` | `Σ trace.model_latency_seconds` | 全部模型请求的观测总延迟 |
| `model_request_latency_p50_seconds` | 单请求延迟中位数 | |
| `model_request_latency_p95_seconds` | 最近秩法取得的单请求 p95 | 样本量是本变体的模型请求数 |
| `model_request_latency_max_seconds` | 单请求延迟最大值 | |

## 4. workload_sha256

```python
for turn in 1..12:
    sha256.update(_workload_identity_prompt(profile, turn).encode())
    sha256.update(b"\0")
    sha256.update(context_efficiency_tool_output(profile, turn).encode())
    sha256.update(b"\n")
```

`_workload_identity_prompt` 只包含 turn、source_tool、requires_lookup、fact_id、key，
**刻意排除 variant 与检索策略**；`tool_output` 由 seed 确定性生成。所以 5 个变体的
sha256 必然一致（实测 `9d90f2ef…`），用于证明"任务完全相同、只有被测策略不同"。

## 5. turns.csv 与 break_even_turn

每逻辑轮一行，除单轮指标外包含：

- `cumulative_input_tokens`：`input_tokens` 从第 1 轮到本轮的累加值；
- `compactions`：本轮开始前与结束后对 SQLite
  `count_context_compactions(statuses={"ready","superseded"})` 的差值；
- `visible_expected_facts`：本轮最终文本中可见的期望事实数。

```python
break_even_turn = 第一个使 current.cumulative < raw.cumulative 的逻辑轮
```

`raw` 每轮都携带前序所有完整大结果，累计输入增长极快；`current` 被压缩+外置压住，
因此 break-even 通常很早（实测第 1 轮）。

## 6. compactions / compaction_failures

- `compactions`：事件总线中 `CONTEXT_COMPACTION_COMPLETED` 事件数；
- `compaction_failures`：`CONTEXT_COMPACTION_FAILED` 事件数；
- `quality.context_limit_events`：上下文超限事件数。

## 7. comparison.json 的门禁公式

| 门禁 | 公式 | 意图 |
|---|---|---|
| `all_quality_gates_passed` | 所有变体 `quality.passed` | |
| `identical_workload` | 各变体 sha256 集合大小 ≤ 1 | 任务一致 |
| `compaction_saves_at_least_25pct` | `compact.input ≤ raw.input × 0.75` | 压缩净收益（含摘要请求成本） |
| `current_saves_at_least_50pct` | `current.input ≤ raw.input × 0.50` | 完整组合降总输入 |
| `externalization_beats_compaction_only` | `current.input < compact.input` | 外置进一步省 token |
| `query_uses_one_reference_call_per_lookup` | `current.ref_calls == len(lookup_turns)` | 每次定位恰好一次调用 |
| `query_reduces_reference_calls_by_3x` | `ranged.ref_calls ≥ current.ref_calls × 3` | query 优于盲分页 |
| `query_reduces_agent_requests` | `current.agent_req < ranged.agent_req` | |
| `one_shot_has_zero_replay` | `current.replayed == 0` | 一次性交付生效 |
| `persistent_delivery_replays_tokens` | `persistent.replayed > 0` | 对照组成立 |
| `one_shot_beats_persistent_delivery` | `current.input < persistent.input` | |
| `break_even_by_turn_4` | `break_even_turn ≤ 4` | 收益不能太晚 |

`acceptance.passed = all(非 None 门禁)`。

## 8. effects 公式（两两差值）

| effects 字段 | 公式（`left − right`） | 实测值 |
|---|---|---|
| `compaction_input_tokens_saved` | `raw − compact` | 995,644 |
| `current_input_tokens_saved_vs_raw` | `raw − current` | 1,265,357 |
| `current_reference_call_delta_vs_raw` | `current − raw`（`left_minus_right=False`） | +3 |
| `query_reference_calls_saved_vs_range` | `ranged − current` | 12 |
| `query_agent_requests_saved_vs_range` | `ranged − current` | 12 |
| `one_shot_replay_tokens_saved` | `persistent − current` | 1,899 |

注意 `current_reference_call_delta_vs_raw` 为 **+3**：外置相对全量内联**新增**了引用
调用。外置优化的收益是减少重复 token，不应被描述成无条件减少调用；"调用下降"的结论
只来自 query 与盲分页（`range-one-shot`）的受控比较。

## 9. aggregate.json（多 attempt 聚合）

- `median_input_tokens`：每个变体在所有 attempt 中 `input_tokens` 的**中位数**；
- `median_cost_usd`：每个变体总费用的中位数；
- `median_model_latency_seconds`：每个变体模型请求总延迟的中位数；
- `median_model_request_p95_seconds`：每个 attempt 内请求 p95 再跨 attempt 取中位数；
- `observed_effects`：`current` 相对 `raw` 的输入 token 节省、费用节省和模型延迟差；
- `live_current_uses_fewer_input_tokens`：`median(current) < median(raw)`（仅 live）；
- `acceptance`：`all_attempts_completed`、`quality`（所有 attempt 质量通过）、
  `scripted_comparison`（scripted 时离线门禁通过）、`passed = 全部非 None 项为真`。

Live 模式不做固定比例断言（真实模型存在采样波动），只对输入 token 比较中位数大小关系。
费用取决于配置价格和供应商缓存计费，延迟受网络与排队影响，两者只报告、不作为硬门禁。

## 10. 边界与注意事项

- **离线 token 全部来自 `TokenEstimator` 启发式估算**，不是真实模型计数；文档中的
  75%/50% 只作为离线可控工作负载的门禁，不用于约束 live 结果。
- `replayed_reference_tokens` 按 `tool_call_id` 判定"同一份正文重复出现"，不区分正文
  来自哪一轮。
- `reference_tool_calls` 来自事件总线，与消息内容无关；`raw` 为 0、`current` 为 3 是
  独立于 token 估算的硬证据。
- `raw` 变体的 `context_window=4,000,000`、`max_input=3,800,000`、`tool_result_inline
  _tokens=1,000,000` 是刻意配置，用于模拟"完全不压缩、不外置"的基线；`compact-inline`
  只启用压缩、仍全量内联 Tool Result。
- 同 seed（默认 19）下 workload 和指标应完全可复现；集成测试
  `test_context_efficiency_case_is_repeatable_for_the_same_seed` 守护这一点。
