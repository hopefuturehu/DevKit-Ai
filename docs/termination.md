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

`Finalizing` 最多调用模型一次，并传入空 Tool 列表。模型只能总结已完成部分、阻塞证据和最小
恢复步骤。收尾模型超时、报错或返回空正文时，运行器生成静态保底说明；不会重新进入 Tool Loop。

## 进展信号

- 强进展：Tool 通过 `metadata.progress.changed=true` 声明变化，或非只读、非幂等操作成功。
  强进展增加 epoch，并清空停滞、重复证据和本 epoch 的恢复次数。
- 弱进展：新的只读结果或首次出现的幂等结果。它允许调查继续，但短周期检测仍然保留。
- 无进展：失败、幂等调用返回完全相同结果，或 Tool 显式声明 `changed=false`。
- 模式证据：相同调用和失败结果、同一工具连续失败、幂等结果重复，以及周期长度不超过配置值
  的 Tool Call/Result 序列。

控制器只在完整 Tool 批次执行后作决定，因此同一模型响应中的其他已请求 Tool 不会被半途丢弃。

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
- `run.stall_warning`
- `run.recovery_started`
- `run.finalizing`
- `run.blocked`
- `subagent.blocked`

`RunResult.status=blocked` 表示执行器有可审计的停滞证据，且恢复尝试未奏效；
`termination_reason` 保存机器可读原因。`failed` 仍只表示 Provider、协议、存储或 Tool Runtime 等
不可恢复错误，`limit_reached` 表示显式资源策略或上下文/模型输出限制。
