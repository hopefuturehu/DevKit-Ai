# 自动终止与长任务控制

## 目标

生产运行默认不再依赖固定的 30 步或 30 分钟总上限。只要任务持续产生新证据或状态变化，
Agent 可以继续执行；当执行路径稳定地重复且恢复无效时，运行器负责退出循环并给用户留下可恢复
的结果。评测和受监管部署仍可显式启用硬预算。

## 状态机

```text
Running
  ├─ 可验证进展 ────────────────> Running（新 progress epoch）
  ├─ 达到警告阈值 ──────────────> Warning ───────> Running
  ├─ 达到恢复阈值 ──────────────> Recovery ──────> Running
  ├─ 恢复后相同模式复发 ────────> Finalizing ────> Blocked
  ├─ 模型自然返回最终正文 ──────> Completed
  └─ 显式预算/取消/不可恢复错误 ─> LimitReached / Cancelled / Failed
```

`Finalizing` 是 `blocked`、显式预算、上下文边界和不可恢复错误的统一出口。允许调用模型时最多
调用一次，并传入空 Tool 列表；费用上限、上下文溢出等不适合继续调用模型的场景直接生成静态
收尾。用户主动取消立即停止，不追加模型调用。任何收尾路径都不会重新进入 Tool Loop。

## 进展信号

- 强进展：`ToolResult.progress.kind=strong`，例如文件内容实际变化、子 Agent 任务创建或完成。
  强进展增加 epoch，并清空停滞、重复证据和本 epoch 的恢复次数。
- 弱进展：`kind=weak`，例如读取文件、搜索、命令完成或取得新的诊断证据。
- 外部等待：`kind=waiting`，用于仍存活的受管进程和子 Agent。它不增加无进展计数，也不会被
  Tool 循环检测误杀。静默达到阈值时只警告和纠偏，默认没有自动终止阈值。
- 无进展：`kind=none`、执行失败或幂等调用返回完全相同结果。
- 模式证据：相同调用和失败结果、同一工具连续失败、幂等结果重复，以及周期长度不超过配置值
  的 Tool Call/Result 序列。

`ProgressSignal.evidence_key` 表示去除 elapsed time 等波动字段后的语义证据；控制器只持久化其
哈希，不把 Tool 参数和原始输出写入 progress checkpoint。控制器在完整 Tool 批次后作决定，
因此同一模型响应中的其他已请求 Tool 不会被半途丢弃。

## 跨 Run 恢复

每个 Tool 批次完成后，epoch、无进展计数、恢复次数和哈希化重复窗口会原子写入 SQLite 的
`progress_states`。同一 session 的下一次 Run 会校验配置指纹后恢复状态并发送
`run.progress_restored`；自然完成时清除 checkpoint，`blocked`、`failed` 和 `limit_reached` 则
保留，避免通过反复 `resume` 清零熔断证据。

## 默认策略

```toml
[agent.progress]
enabled = true
warning_after_no_progress_steps = 4
recovery_after_no_progress_steps = 7
finalize_after_no_progress_steps = 11
max_recovery_attempts_per_epoch = 1
exact_failure_warning = 2
exact_failure_recovery = 3
same_tool_failure_warning = 3
same_tool_failure_recovery = 5
idempotent_repeat_warning = 2
idempotent_repeat_recovery = 3
cycle_window_size = 16
max_cycle_period = 4
cycles_before_warning = 2
cycles_before_recovery = 3
process_inactivity_warning_seconds = 300
process_inactivity_recovery_seconds = 900
# process_inactivity_finalize_seconds = 3600

[agent.finalization]
enabled = true
model_timeout_seconds = 120
fallback_summary = true
```

以下字段默认均为 `None`，在 TOML 中省略即可：

- `agent.max_steps`
- `agent.max_wall_time_seconds`
- `agent.max_total_tool_output_bytes`
- `agent.max_consecutive_failures`
- `agent.max_cost_usd`
- `agent.process_hard_timeout_seconds`
- `subagents.max_steps`
- `subagents.max_wall_time_seconds`

如需兼容旧部署，可显式给这些字段赋正数。它们是独立硬策略，优先于进展状态机。评测适配器
继续显式传值，不改变基准任务的外部预算。

## 事件与结果

运行审计新增：

- `run.progress`
- `run.progress_restored`
- `run.stall_warning`
- `run.recovery_started`
- `run.finalizing`
- `run.blocked`
- `run.limit_reached`
- `run.cancelled`
- `subagent.blocked`

`RunResult.status=blocked` 表示执行器有可审计的停滞证据，且恢复尝试未奏效；
`termination_reason` 保存机器可读原因。`failed` 仍只表示 Provider、协议、存储或 Tool Runtime 等
不可恢复错误，`limit_reached` 表示显式资源策略或上下文/模型输出限制；二者和 `blocked` 都由
同一个终止协调器生成事实化收尾。
