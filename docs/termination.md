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
调用一次，保留兼容的 Tool 声明并使用 `tool_choice=none`；运行器拒绝收尾模型意外返回的
工具调用。费用上限、上下文溢出等不适合继续调用模型的场景直接生成静态
收尾。用户主动取消立即停止，不追加模型调用。任何收尾路径都不会重新进入 Tool Loop。

## 进展信号

- 强进展：`ToolResult.progress.kind=strong`，例如文件内容实际变化、子 Agent 任务创建或完成。
  强进展增加 epoch，并清空通用停滞与本 epoch 的恢复次数；独立重复限制只随相关资源变化失效。
- 弱进展：`kind=weak`，例如首次读取文件、搜索或取得变化后的诊断证据。重复证据计为
  `none`，不能抵扣停滞；未知工具执行成功也不自动升级为强进展。
- 外部等待：`kind=waiting`，用于仍存活的受管进程和子 Agent。它不增加无进展计数，也不会被
  Tool 循环检测误杀。静默达到阈值时只警告和纠偏，默认没有自动终止阈值。
- 无进展：`kind=none`、执行失败或幂等调用返回完全相同结果。
- 模式证据：相同调用和失败结果、同一工具连续失败、幂等结果重复，以及周期长度不超过配置值
  的 Tool Call/Result 序列。

`ProgressSignal.evidence_key` 表示去除进程 UUID、elapsed time 等波动字段后的语义证据。
命令结果使用终态、退出码、分别计算的 stdout/stderr 摘要；受管进程的摘要覆盖从启动以来
保留的输出，轮询消费输出不改变摘要。`evidence_complete` 区分完整与截断证据，
`subject_key` / `resource_version` 标识受影响资源及版本。

控制器持久化哈希、计数、交付位置和已有 blob 引用，不保存原始参数或输出。执行前可拒绝
受限调用，每个已声明调用仍产生对应结果；完整批次闭合后才决定是否结束 Run。

## 独立重复调用限制

默认 `repeat_guard_mode=observe`，记录执行前本会限制的调用，沿用通用停滞收尾；
稳定身份与新证据判断在两种模式中都生效。`observe` 因此不等价于旧版行为。
启用 `enforce` 后，同一操作三次取得相同完整观察，后续请求在执行前拒绝。第二次警告，
第三次提供恢复提示；连续两个模型响应仅碰撞限制且没有新证据或有效等待时，统一收尾为
`blocked`。一个响应中的多次拒绝只占一次机会，首次建立限制的批次不消耗纠偏机会。

首版强制入口覆盖 `run_command`、`run_shell`、`read_file`、`search_text`、终态
`poll_process` 和 `load_context_reference`。其他工具继续参与进展监控，不自动套用新的
执行前限制。规范化排除命令的 `wait_seconds`，保留 argv/script、cwd、环境、硬超时和
交互模式；读取区间与查询参数仍区分不同操作，不推断任意 shell 程序是否语义等价。

异步完成结果关联原始启动操作。再次启动命令前，会观察已知后台进程的状态与累计摘要，
因此不主动轮询也不能把已完成的重复操作永远隐藏成等待。仍存活的进程保留 `WAITING`；
不完整结果不用于新增执行前阻断。

拒绝结果明确标记 `executed=false` / `reason_code=repeated_observation`，不创建进程、
不重复请求审批、不算真实执行失败。权限拒绝和参数错误也不能替换已确认的完整结果证据。
检查权限先于返回已有证据引用；读取检查当前文件内容版本，以允许外部修改后的回读。
压缩覆盖原始交付后，允许一次对应文件或 blob 回读，该凭据跨 resume 持久化，不能通过
反复压缩同一证据清空限制。文件版本检查仍会读文件，不把 Tool 拒绝次数当作节省的文件 I/O。

相同输出不证明没有副作用。确需重复静默执行时，可通过宿主配置 `repeat_tool_limits`
提供明确额度；模型参数不能增加额度。已建立的限制不会因单纯增大阈值自动消失，可切换
观测模式或使用新会话处理新的任务。未知资源依赖与外部搜索范围变化仍需谨慎评估误拦。

详见[实施方案](repeated-tool-execution-plan.md)与[验证报告](repeat-guard-validation.md)。

## 跨 Run 恢复

每个 Tool 批次完成后，epoch、无进展计数、恢复次数和哈希化重复窗口会原子写入 SQLite 的
`progress_states`。v2 状态绑定工作区与执行环境，目标改变时不沿用旧限制。配置变动保留独立
限制，v1 迁移保留通用计数并丢弃含波动身份的旧指纹，迁移原因进入恢复事件。
同一 session 的下一次 Run 会恢复状态并发送
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
repeat_guard_mode = "observe" # "enforce" 开启执行前限制
repeat_warning = 2
repeat_limit = 3
repeat_blocked_turns = 2
repeat_capacity = 256
# repeat_tool_limits = { run_command = 5 } # 可信配置的重复额度，run_shell 可继承此值
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
- `tool.repeat_observed`
- `tool.repeat_blocked`
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
