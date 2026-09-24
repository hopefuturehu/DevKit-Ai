# 原生沙箱诊断

返回[使用指南](README.md)。实现计划见[六阶段执行方案](../designs/native-sandbox-execution-plan.md)，
真实验证结果见[沙箱验证记录](../evaluations/native-sandbox-validation.md)。

目前提供 macOS/Linux 原生后端的专项诊断，生产 `sandbox.mode="required"` 工厂仍未开放。
固定探针通过只说明该次检查成功，不代表全部工具、网络 allowlist、恢复窗口或发布门槛已经验收。

## 查看安装与遗留资源

```sh
bot -C /absolute/workspace doctor --sandbox
bot -C /absolute/workspace doctor --sandbox --json
```

这两个命令只读检查安装元数据和所选状态目录的遗留资源，不启动 Native 进程、不下载运行时、
不创建状态目录或准入锁、不访问模型、不打开会话数据库。能力仍显示 `unverified`。
配置加载仍遵守宿主安全上限；项目配置不能通过诊断入口放宽它。

默认运行时目录是 `~/.bot/sandbox/runtime`。默认状态目录是
`~/.bot/sandbox/diagnostics/<工作区规范路径的 SHA-256 前 32 位>`，仅供诊断使用，
不会自动扫描所有业务状态目录。可由宿主显式选择绝对路径：

```sh
bot -C /absolute/workspace doctor --sandbox \
  --sandbox-runtime /absolute/pinned-runtime \
  --sandbox-state /absolute/private-state --json
```

## 恢复并运行固定探针

```sh
bot -C /absolute/workspace doctor --sandbox-probe \
  --sandbox-runtime /absolute/pinned-runtime \
  --sandbox-state /absolute/private-state --json
```

`--sandbox-probe` 会修改所选私有状态区域：先交给 Native 后端认证和恢复可信遗留，再创建
一次性资源，经真实隔离路径检查文件读写、网络拒绝、只读调用和取消清理。结束前等待资源回收。
它不执行项目命令、不调用模型，不修改配置中的 `sandbox.mode`，也不降级为 Local。
安装无效时不会自动安装或启动探针。

探针采用固定的拒绝网络配置，不包含项目额外读写根或网络 allowlist，因此不能用于认证这些
自定义授权。恢复既有资源时仍要求原工作区、状态目录、运行时和资源身份匹配；诊断不会为了
继续运行而忽略损坏记录、接管仍活着的宿主或重放崩溃前的业务命令。

收到 SIGINT、SIGTERM 或 SIGHUP 后会等待清理；清理期间的重复信号不会跳过这一步。
进程归属不明时保留资源和准入占用，返回失败。macOS 的进程清理依赖观察，可能出现这种状态。

## 读取结果

| 字段 | 含义 |
|---|---|
| `runtime.metadata_valid` | 安装元数据检查结果，不等同于实际隔离成功 |
| `capabilities` | 当前已验证能力；只读检查不会提升能力状态 |
| `probe.status` | `not_run`、`running`、`passed`、`failed` 或 `cancelled` |
| `cleanup.status` | `not_run`、`complete` 或 `incomplete`，与探针结果分开报告 |
| `recovery_before` / `recovery_after` | 所选状态区域在执行前后的只读清单 |
| `recovery_*.status` | `empty`、`pending` 或 `unknown`；仅为当次观察 |
| `recovery_*.cleanup_authority` | 恒为 `false`；显示清单不能提供 PID 或删除权限 |
| `business_execution_enabled` / `business_execution_attempted` | 当前均为 `false` |
| `ok` / `exit_code` | 本次诊断操作结果，不代表生产沙箱发布状态 |

清单只显示有界的目录、占用 ID、阶段和宿主存活状态，不显示恢复日志中的 argv、环境变量或
业务输出。任何记录无法读取或认证时保留 `unknown`。真正的清理权限来自 Native 后端重新
认证的私有恢复记录，而非此 JSON、审计事件或 SQLite 负载。

| 退出码 | 含义 |
|---|---|
| `0` | 所请求诊断成功；只读检查成功仍不代表探针已运行 |
| `1` | 安装、自检、恢复或清理未成功，或存在未确认遗留 |
| `2` | 配置或选项无效 |
| `130` / `143` / `129` | 中断 / SIGTERM / SIGHUP，且清理没有报告不完整 |

清理不完整优先返回 `1`，即使先收到退出信号。`--json`、`--sandbox-state` 和
`--sandbox-runtime` 必须与 `--sandbox` 或 `--sandbox-probe` 一起使用。

遇到 `pending` 或 `unknown` 时保留状态目录和准入文件，记录 JSON，并使用原运行时和
相同工作区复核。不能仅凭 PID、进程名或“目录现在不存在”断言旧资源已安全回收；这也不构成
清除占用或手工删除恢复记录的依据。
