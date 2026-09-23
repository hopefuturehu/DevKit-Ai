# 原生沙箱验证记录

日期：2026-09-23。当前为 **P0 实测原型，未通过阶段门槛，未发布 required 模式**。
bot 的实际执行路径仍为 Local；本报告中的探针独立于业务运行时。

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
构建上下文；该配方尚未单独执行 docker build 验证。本轮实测由同一基础镜像和依赖手动准备，
镜像摘要为 `b859c6d32b2ae7425696d06bfdecd2b0ffddac12f9c2b5898955409c02beae9f`。
后续需用配方重建并重测。探针在任一必要检查失败时返回非零，这是预期行为。

## 原始证据与后续门槛

- [macOS 首轮](../data/native-sandbox-p0-macos.json)
- [Linux 外层环境缺能力](../data/native-sandbox-p0-linux-arm64.json)
- [Linux 修正外层环境后](../data/native-sandbox-p0-linux-arm64-fullproc.json)

P0 尚需：解决硬链接及 macOS 生命周期接入方式、验证 allowlist、执行可复现 Linux 构建并补齐依赖清单。
P1–P5 尚未开始交付；不能以本轮独立探针替代配置上限、业务工具接线、子 Agent、worker 协议及发布验收。
