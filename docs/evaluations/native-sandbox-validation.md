# 原生沙箱验证记录

日期：2026-09-23。当前为 **P0 实测及 P1 策略基础，未通过完整阶段门槛，未发布 required 模式**。
off 的实际执行路径仍为 Local；required 由工厂明确拒绝，没有宿主兜底。本报告中的探针独立于业务运行时。进程管理增加了供原生
后端启用的可选保守检查，默认 off 路径未启用该检查。

## 已锁定的运行时

- Node `24.14.0`：下载前指定版本，解包前核验官方 SHA-256。
- `@anthropic-ai/sandbox-runtime` `0.0.77`：完整 npm 依赖树与 integrity 保存于
  [package-lock.json](../../runtime/sandbox/package-lock.json)。npm 安装禁用生命周期脚本。
- 安装器只显式执行一次，不在工具执行时下载；目标必须在项目目录外，已有目录拒绝覆盖，失败不发布半成品。
- Linux ARM64 实测依赖：bubblewrap `0.6.1-1ubuntu0.3`、socat `1.7.4.1-3ubuntu4`、
  ripgrep `13.0.0-2ubuntu0.1`。Python `3.10.12` 与 psutil `5.9.0` 仅运行独立探针，
  不代表 bot 已支持 Python 3.10。

## 首轮实测

| 检查 | macOS 26.6.2 ARM64 | Linux 5.15.49 ARM64 |
|---|---|---|
| 工作区正常读写 | 通过 | 通过 |
| 外部路径、假 .env、状态/策略文件拒绝 | 通过 | 通过 |
| 指向外部目录的符号链接、受保护路径 rename | 拒绝 | 拒绝 |
| 预先存在的跨边界硬链接读写 | **失败** | **失败** |
| 孙进程继承外部写入限制 | 通过 | 通过 |
| 空参数、引号、换行、中文、shell 元字符保真 | 通过 | 通过 |
| stdin UTF-8、300 KB stdout、独立 stderr | 通过 | 通过 |
| 直接 TCP IPv4/IPv6、UDP IPv4、Unix socket | 通过 | 通过 |
| 空配置/损坏配置不启动目标命令 | 通过 | 通过 |
| 仅 SIGTERM SRT CLI 后脱离会话的后代回收 | **失败** | 本次夹具通过 |
| allowlist、完整 S01–S15、业务回归、性能验收 | 未验证 | 未验证 |

所有探针只接触自行创建的临时目录、假秘密、受控本地监听器及有限子进程。
网络检查先直接访问监听器作为正向对照，再确认沙箱进程无法到达它们。
没有用连接超时或命令未启动冒充隔离成功。大输出仅持久化 SHA-256。

### 硬链接发现

两平台均阻止直接打开 `outside/secret`，但可以读取预先放入工作区的同 inode 硬链接；
也能通过工作区内硬链接修改外部哨兵。这是路径规则与文件 inode 别名之间的边界，
不能以 `Path.resolve()` 或符号链接检查覆盖。

后续 resolver 必须为可访问目录中的既有硬链接建立明确的拒绝/准入规则，并验证编译后的
OS 策略，覆盖 link/rename/并发变化。未解决前不发布 required，不能把这项删除或降为提示。

### 生命周期发现

macOS 上 CLI 收到 SIGTERM 后退出码为 0，但一个 detached Node 子进程仍存活。
探针根据 PID 与创建时间识别并强制清理了它；此行为记录为失败，没有冒充 SRT 自行清理成功。
Native target 必须接管后代与代理资源生命周期；单独观察 wrapper 退出不足以宣布完成。
Linux 本次夹具通过不代表 spawn 竞态、重启恢复等 S11/S15 条目已验收。

### Linux 环境边界

Linux 使用 Docker Desktop 内的真实 ARM64 Linux 内核，非 x86 仿真。
外层容器没有挂载宿主目录、凭证或 Docker socket，探针运行用户为 `65534:65534`。
为允许内层 bubblewrap 的完整 user/PID/network/mount namespace，实验容器开启：

```text
--cap-add SYS_ADMIN
--security-opt seccomp=unconfined
--security-opt systempaths=unconfined
--sysctl net.ipv6.conf.all.disable_ipv6=0
--sysctl net.ipv6.conf.default.disable_ipv6=0
```

这是**外层实验容器前提**，不是生产部署建议。SRT 内层未启用较弱嵌套模式、较弱网络模式或
任意 Unix socket。首轮未放开外层 proc 挂载时，bwrap 报 `Can't mount proc`，目标未启动；
该失败记录单独保留。普通默认 Docker 容器因此不能在此报告中标记为支持。

## 复现

macOS 需从未被另一层 Seatbelt 限制的终端执行：

```bash
uv run python scripts/install_sandbox_runtime.py --destination /tmp/bot-srt-p0
uv run python scripts/probe_native_sandbox.py --runtime /tmp/bot-srt-p0 --output /tmp/p0-macos.json
```

Linux 实验镜像配方位于 [Dockerfile.probe](../../runtime/sandbox/Dockerfile.probe)，以仓库根目录为
构建上下文。已完成从配方构建并直接运行镜像，镜像摘要为
`a2e8c5648b5c0817ec984cfde031912ad5bf736a563f3f8a1f1bba11c66cc680`。
镜像只 COPY 锁定清单和探针，无宿主目录挂载；本机构建日志中的上下文传输量约 24.5 KB。
附有专用 dockerignore，但旧构建器是否识别它未验证；生产安装不依赖 Docker 构建。

```bash
docker build -f runtime/sandbox/Dockerfile.probe -t bot-native-p0-repro .
docker run --name bot-native-p0-check --cap-add SYS_ADMIN \
  --security-opt seccomp=unconfined --security-opt systempaths=unconfined \
  --sysctl net.ipv6.conf.all.disable_ipv6=0 \
  --sysctl net.ipv6.conf.default.disable_ipv6=0 \
  bot-native-p0-repro --deny-hardlinks --output /tmp/p0-linux.json
docker cp bot-native-p0-check:/tmp/p0-linux.json /tmp/p0-linux.json
```

探针在任一必要检查失败时返回非零，这是预期行为。

## 收紧规则后的复测

新增 `--deny-hardlinks` 候选策略：扫描工作区普通文件的 link count，将大于 1 的路径同时编译为
OS 的 read/write deny。两平台实测均拒绝这些别名的读取、写入、再链接和 rename。
这证明 OS 规则能够保护已识别的别名；**不证明一次扫描已经解决准入和并发变化**。
后续 resolver 仍需稳定的根目录检查、作用域协调和竞态测试，不能只复制探针里的扫描代码。

新增受控 HTTP 检查：允许的精确 IP:port 请求成功；不在白名单内的目标、允许域名解析到
loopback、跳转到该 loopback 域名均被代理拒绝。使用 `127.0.0.1.nip.io` 时先在可信侧确认
DNS 确实返回 `127.0.0.1`，实际目标为自行启动的本地服务器。该检查依赖外部 DNS，
不冒充自建 DNS rebinding 验证；私网/链路本地、TLS CONNECT 和 SOCKS 仍需补测。

macOS 当前严格读取规则下，SRT 生成的 `HTTP_PROXY=...localhost:port` 无法在目标进程中解析。
探针将**访问代理的地址**固定为 `127.0.0.1` 后通过，目标域名仍交由可信侧代理解析。
未放开 mDNS/任意 DNS 通道；生产启动器仍需落实该规范化，allowlist 尚未发布。

| 复测 | 结果 |
|---|---|
| Linux 配方构建镜像，收紧别名及受控 HTTP 后 | 9/9 探针通过 |
| macOS 同一组探针 | 8/9，通过项相同；裸 CLI 清理仍失败 |
| macOS 原有受管执行，普通取消 | 通过 |
| macOS 原有受管执行，父进程快速退出/关闭流/子进程 setsid | 3/3 复现错误 completed |
| macOS 保守检查，普通取消 | 通过，observed_empty |
| macOS 保守检查，同一快速退出夹具 | 3/3 返回 cleanup_failed/unknown，保留占用并阻止同会话后续执行 |

保守检查在启动前保存进程身份基线；发现无法归属的新孤儿或不可观察的新进程时，不能判定
调用已清空。它不按名称、时间接近或裸 PID 自动认领/杀进程。缺启动前基线时不启动目标；
恢复时保存基线，使用独立的 progress/cleanup scope，避免混用普通 Local 的恢复记录。

**该检查仍是进程观察，不是内核 containment。** 无关程序新建孤儿进程也会导致保守阻塞，
本次实测已出现这种情况。需要继续评估作用域归属和兼容性，不能为了减少误报直接忽略
无法归属的进程。探针最后只清理独立识别的自建夹具子进程，其他进程不受影响。

受影响回归共 **60 passed**，覆盖进程清理、runtime 关闭、执行工具策略、安装器及新增 guard。
最初在 Codex 外层受限环境内运行时，`psutil.pids()` 的 sysctl 被拒绝，导致生命周期用例失败；
该次运行已中断，随后在正常进程观察权限下重跑并通过。未执行全仓回归或性能发布验收。

## 原始证据与后续门槛

- [macOS 首轮](../data/native-sandbox-p0-macos.json)
- [Linux 外层环境缺能力](../data/native-sandbox-p0-linux-arm64.json)
- [Linux 修正外层环境后](../data/native-sandbox-p0-linux-arm64-fullproc.json)
- [macOS 收紧规则后](../data/native-sandbox-p0-macos-hardened.json)
- [Linux 配方构建镜像复测](../data/native-sandbox-p0-linux-arm64-hardened.json)
- [macOS 原有进程管理](../data/native-sandbox-p0-macos-manager.json)
- [macOS 保守进程检查](../data/native-sandbox-p0-macos-manager-strict.json)
- [机器可读阶段状态与源码摘要](../data/native-sandbox-validation.json)

P0 尚需：收敛硬链接准入/并发方案和 macOS 生命周期保守阻塞、补全 allowlist 出口证据与能力清单。
P1 正在交付基础；P2–P5 尚未交付。不能以独立探针替代业务工具接线、子 Agent、worker 协议及发布验收。

## P1 策略与配置基础

- 新增不可变 SandboxConfig、SandboxPolicy、ExecutionScope 和能力/错误模型。每个调用使用独立
  策略摘要；父策略的只读、禁止路径和网络范围会保留到普通子策略中。
- 用户配置、显式指定配置、宿主环境和显式覆盖形成安全上限。项目只能缩小 sandbox/permissions；
  `model_copy` 产生的未验证配置在工厂/resolver 再校验。项目开启 required 后，派生配置也不能关闭。
  配置来源信息不接受 TOML 注入，不进入普通配置序列化。
- 路径准入仅读取元数据。既有普通文件硬链接同时进入读写拒绝集合；文件/目录身份、时间及
  link count 改变、扫描错误或超限均拒绝。扫描中重复遇到目录时不会覆盖先前变化。
- 秘密、状态库及 sidecar、配置、记忆、Agent 配置、Git 元数据与其他任务区域生成显式保护；
  对符号链接控制路径同时保留配置名称及真实目标。只读策略仅保留本任务 home/tmp/cache 写权限。
- `.git` 文件不由主进程跟随到外部文件；需要后续 Git broker 提供经过验证的元数据根目录。
  工厂在 required 下保持关闭，CLI 给出 `SANDBOX_UNAVAILABLE`，不构建模型 provider 或启动目标。
- doctor 检查工作区外的安装记录、锁定清单和依赖位置，不通过用户 PATH 选启动器，不执行候选
  二进制。只有安装事实时能力显示 unverified；安装无效为 unavailable；硬资源限制仍 unavailable。
  元数据一致不是可执行内容完整性证明，也不是 OS 探测成功，不能据此开放执行。

验证：配置、策略、准入、doctor、CLI 和既有进程清理等受影响回归 **162 passed**。其中纯配置与
策略等 97 项可在外层沙箱内执行；CLI 端到端和进程观察需要正常 macOS sysctl 权限。
新增 wheel 包含三份锁定清单，已构建并逐字节核对。实际 P0 安装的元数据检查返回 Node 24.14.0 /
SRT 0.0.77、metadata_valid=true，各 OS 隔离能力仍为 unverified。

尚未证明：上述策略编译后的真实 OS 边界、准入与并发启动协调、启动器完整性、实时能力探针、
worktree 委派、完整工具覆盖以及 S01–S15。P1 单元测试不是 P0/P5 原生安全验收。
