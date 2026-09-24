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

## 工具执行分类与会话管理动作

Native 运行器使用宿主代码内的固定清单，区分 `confined`（经原生 target/worker）、
`brokered`（限定的宿主管理动作）和 `pure`（无宿主资源访问的计算）。当前内置工具没有独立
的 `pure` 入口；`update_plan` 包含计划持久化，整体归为 `brokered`。

| 已接入入口 | 分类与边界 |
|---|---|
| 命令、shell、read/search/patch/fetch | `confined`，通过现有 Native 调用视图或文件 worker |
| 进程查询、轮询、输入、终止 | `brokered`，复用调用归属和既有输出限制 |
| `update_plan` | `brokered`，仅更新宿主选择的活动会话，参数不能指定其他 session、Run 或路径 |
| `activate_tools` | `brokered`，只调整当前会话已登记工具的模型可见列表，不增加权限 |
| 上下文引用、会话历史、压缩来源读取 | `brokered`，校验活动 Run/工作区与 SQLite 会话授权 |

注册时核对精确实现类型、名称、执行方法、参数 Schema 和权限声明；模型或插件自报分类
不提供权限。既有注册表交给 Native Runner 时会整体检查，子注册表继承限制，读取目录和
调用前继续检查，防止注册后替换实现。未审计自定义工具或子类直接拒绝。

会话读取不能用参数指定 SQL、任意文件路径或其他会话。会话 broker 的正文和错误文本分别
限制为配置上限与 64,000 UTF-8 字节的较小值；发生截断时显式标记，不能算完整证据。计划
写入前再次检查活动 Run 归属。运行中提示和重复调用观察也只采用当前 session/Run 的进程。
Native 的显式会话压缩先检查工作区归属并完成后端启动，再使用压缩器。

Skill、记忆和子 Agent/worktree broker 仍待接入。相关服务在 Native Run 启动门禁中拒绝，
不能先启动辅助工作；模型猜测这些动作名称也会被拒绝。`ksys/tuner` 的宿主设备权限仍不支持，
不会自动提权或改用宿主执行。固定清单约束工具分派，不是任意宿主 Python 代码的隔离机制。

上述接线不表示 P4 或整体发布完成；生产 `required` 工厂和 Native allowlist 仍关闭。
