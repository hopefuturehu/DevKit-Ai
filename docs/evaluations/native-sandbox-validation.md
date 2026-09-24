# 原生沙箱验证记录

日期：2026-09-23。当前为 **P0/P1、P2 受管命令及 P3 受限 worker 工具接线，未通过完整阶段门槛，未发布 required 模式**。
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
P1 正在交付基础；P2 命令原型见下文，P3–P5 尚未交付。不能以独立探针替代业务工具接线、子 Agent、worker 协议及发布验收。

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

## 策略编译后的两平台实测

继续增加 `sandbox/srt.py`，将 resolver 的快照编译为显式 SRT 设置及完整启动环境。
新增 [test_native_sandbox.py](../../tests/integration/test_native_sandbox.py) 通过固定 helper 运行锁定的 SRT，
**仍不是受管 Native target 或完整工具接入**。11 项探针在 macOS ARM64 与 Linux ARM64 均通过：

- 工作区正常读写、精确 argv/UTF-8；外部写入及子进程写入拒绝。
- 假 SSH 凭证、`.env`、配置、状态库、启动策略和其他任务目录不可读取。
- 既有硬链接的读取/写入/rename 拒绝；已授权只读外部文件可正常读取，但不能向工作区创建硬链接。
- 只读任务不能改工作区，仍能使用自己的临时目录。
- SRT 默认共享临时目录不能读写；活跃 loopback 端点不能直接连接。
- 宿主假密钥和加载器变量不继承；可继承的宿主 FD 在启动边界关闭。
- 配置符号链接不能删除或通过重命名父目录绕过；同次调用仍能写普通源码文件。

实测改变了以下实现决策：

1. SRT 0.0.77 即使收到显式 allowWrite，仍额外加入 `/tmp/claude` 等默认写路径。先显式 deny
   能阻止写入，但补测发现 Linux 仍可读取共享哨兵：默认写挂载重新开放了读取。现改用固定
   `launcher.mjs` 调用该版本的底层 OS 包装器和代理，精确传入读写集合，不加入共享默认根；
   两平台复测共享目录读写均拒绝。仍使用独立 HOME/cache 和任务自己的 tmp。
2. SRT 启动器的 TMPDIR 与任务 tmp 分开。macOS 首次运行因 Unix socket 路径过长而失败，
   现要求控制目录的编码长度不超过 64 字节；实际夹具由宿主在 `/tmp` 创建 0700 随机短目录。
   Native target 后续必须负责创建、身份校验和清理这些资源。
3. 系统读取范围不再直接开放 `/usr` 或 macOS `/System`，避免把 `/System/Volumes` 等区域带入。
   Linux 不把 `/dev/fd` 解析为宿主测试进程的 `/proc/PID/fd` 再加入读取集合。
4. Linux 的只读/隐藏目标挂载不能保护原符号链接目录项。初始探针实际观察到 unlink 成功，
   随后为任务可写区域中的受保护链接隐藏其父控制子目录；不存在的后代拒绝条目由父规则覆盖，
   避免 bwrap 在只读父目录内创建挂载点失败。该额外保护仅在必要的链接场景生成，不默认隐藏
   整棵 `.bot`。若保护需要隐藏整个写入根或与显式读取子根冲突，则配置明确拒绝；可将该控制
   文件放入独立控制目录。worktree/Git broker 后续需使用精确的写根，不能授予整个父工作区。
5. 任务 home/tmp/cache 即使指向同一私有根下的控制目录也不能为符号链接。

最终 macOS 受影响回归 **188 passed**（包含上述 11 项），Linux 原生探针 **11 passed**，无 skip。
Linux 在原 P0 镜像上安装固定 Python 3.12.11 和与 macOS 一致版本的 pytest/pydantic 等最小测试
依赖，通过 wheel 运行；容器内策略与探针源码摘要已与工作树核对一致。实验镜像保存为
`bot-native-p1-env:20260923`，摘要 `cfd85214fca860da6bc1325c980d41df485d5f770918bdffe5ad098963785d24`。
Node/SRT/bwrap 等原 P0 依赖及外层实验权限保持不变，仍不代表普通 Docker 容器可用。

首次以 sleep 为 PID 1 的容器会保留已退出的 socat/bwrap 僵尸进程；已停止该容器，最终验证使用
`--init`，执行后仅剩 docker-init 和 sleep。这不是受管取消/崩溃恢复验收，不替代 P2/P5 生命周期检查。

证据：[macOS 编译策略与回归](../data/native-sandbox-p1-macos.json)、
[Linux 编译策略及先前失败](../data/native-sandbox-p1-linux_arm64.json)。复现 macOS：

```bash
BOT_TEST_SANDBOX_RUNTIME=/tmp/bot-srt-p0 BOT_REQUIRE_NATIVE_SANDBOX=1 \
  uv run pytest tests/integration/test_native_sandbox.py
```

未选择运行时的普通开发运行可 skip；设置 `BOT_REQUIRE_NATIVE_SANDBOX=1` 后缺依赖必须失败。
本轮仍未覆盖全部 S01–S15、allowlist、准入/启动竞态、Native target、worker、子 Agent 与性能门槛。
P0/P1 继续进行中，required 发布开关保持关闭。

### 固定 helper 的 P0 复验

helper 与锁定清单一起安装并随 wheel 分发，启动前的元数据检查逐字节核对 helper；文件缺失或
被替换时 unavailable。它检查 Node/SRT 的固定版本，目标 argv 使用上游 quote 编码；沙箱内
再由固定 Node shim 将代理 localhost 规范化为 `127.0.0.1`，随后 execve 原始 argv。
helper 自身的信号转发不宣称能够回收所有 macOS 后代，受管生命周期仍属于 P2。

已按计划用 `scripts/probe_native_sandbox.py --helper --deny-hardlinks` 重跑原 P0：
Linux 9/9；macOS 8/9，失败仍是裸信号取消后的 detached 后代。argv、UTF-8 stdin、300 KB
stdout/stderr、文件/子进程、断网、受控 HTTP 和损坏配置检查均通过。
另以 `scripts/probe_srt_process_management.py --helper --strict` 复验原有受管保守检查，4/4：
正常取消为 observed_empty；三次快速根退出均为 unknown，保留配额并阻止同会话继续执行。
夹具最终只清理独立识别的测试子进程，未知进程不被擅自接管。

证据：[macOS helper P0](../data/native-sandbox-p0-helper-macos.json)、
[Linux helper P0](../data/native-sandbox-p0-helper-linux_arm64.json)、
[macOS helper 保守清理](../data/native-sandbox-p0-helper-macos_manager_strict.json)。
旧 P0 与 P1 基础的源码摘要按其原提交保留，不将历史测试冒充最新 helper 的证据。

## P2 受管命令原型

增加内部 `NativeSandboxExecutionTarget`，复用原有流式输出、交互输入、超时、取消和严格进程观察。
`ProcessSpec` 始终保存逻辑 argv，实际 helper argv 和资源身份另存；启动环境完整替换，关闭继承 FD。
每次调用须由可信侧绑定 session/run/tool/policy，缺失、身份不符、重复使用和准入后路径变化均在
启动前拒绝。Run 的 HOME/tmp/cache 由后端创建，调用级策略和代理临时目录使用独立 0700 目录。
清理校验 device/inode/owner，并使用基于目录描述符的删除，不跟随任务放入的符号链接。

`RunCommandTool`、`RunShellTool` 和 `SubprocessCliTool` 已传递内部 scope；CLI adapter 同时补齐
session/run。测试直接提供可信作用域并执行真实后端，普通 AgentRunner 尚未完成作用域创建与绑定。
结构化预启动失败保留错误码、阶段及 executed=false；helper 启动后尚无可信状态协议，executed
保持未知，不按 stderr 文本猜测目标是否执行或是否为沙箱拒绝。

`required` 的生产工厂仍明确拒绝，Native 内部后端的有效 allowlist 也显式拒绝。安装元数据及
单组探针不替代启动完整性与能力验收。此原型不构成 P2 完成，更不是完整工具沙箱发布。

### Linux 临时文件系统中的写入语义

首次受管测试向工作区父目录下的假 sentinel 写入时，目标报告成功；可信侧核对宿主 sentinel
仍为 `preserve`。这是 SRT 将 denyRead('/') 展开为根目录子项的 tmpfs 掩蔽后，进程在临时挂载中
创建了同名文件，**没有修改宿主哨兵**。单独 remount '/' 不能约束这些独立子挂载，第二次试验仍失败。

固定 `bwrap-launcher.sh` 现按 SRT 0.0.77 的实际参数语法记录挂载，最后将仍为 tmpfs 的掩蔽挂载
非递归 remount 为只读，保留显式可写子 bind。未知参数或无法读取参数文件时拒绝启动，不猜测或
降级。SRT vendor 文件未改动；伴随脚本随 helper 安装、打包、摘要核验，并由安装器设置可执行位。
最终同一测试观察到拒绝，正常工作区及私有 tmp/home 写入仍成功。大型 --args profile 分支仍需
专项实测，不能从普通参数规模的结果推断已覆盖。

### macOS 清理观察的两种结果

早期原型回归曾为 201 passed；最终代码的一轮复测出现 203 passed、2 failed、2 teardown errors。
两个失败均为目标已退出、输出已关闭，但观察到新孤儿，返回 cleanup_failed/unknown 并保留资源。
只读核对其中的 PID+birth 后，确认包含 nfcd/NFStorageServer（UID 260）、mdworker_shared
（UID 501）和 online-auth-agen（UID 0）；还有进程在核对前已退出。没有按进程名或时间接近
认领、杀死这些进程，也没有自动删除未确认回收的目录。最初一次未保存完整归属诊断的失败原因
仍保持未确认，不用后来的诊断替代它。

按照原方案 S11，测试分别校验 observed_empty 与 unknown：前者释放控制目录，后者须同时满足
根进程退出、输出完整、已知活进程为空、存在未归属进程、资源及配额保留、同会话下一次启动被拒绝。
仅在这些断言通过时，测试清理阶段才允许保留 unknown 资源，并将实际分支写入 JUnit 属性。
这验证的是保守失败契约，**不把 unknown 计作回收成功，也未解决 macOS 的误阻塞成本**。

最终 macOS 受影响回归 **205 passed**，包含 20 项真实原生测试；该轮超时和取消实际均为
observed_empty。Linux 最终 **27 passed**，包含同样的 20 项原生测试及 7 项资源/启动检查；
该轮超时和取消也均为 observed_empty。两平台均无 skip。未跑全仓回归、完整恢复、并发作用域或性能验收。

仍待完成：普通 AgentRunner 与父子作用域接线、跨作用域 poll/input/terminate/output 边界、
Run 结束时释放与崩溃恢复验收、可信启动状态通道、运行时完整性和实时能力检查、准入并发协调、
worker/client、Git broker、工具分类、CLI/Web/eval 全覆盖以及 P5 安全/业务/性能门槛。

当前原始证据：[macOS P2 与先前失败](../data/native-sandbox-p2-macos.json)、
[Linux P2 与影子写入发现](../data/native-sandbox-p2-linux_arm64.json)。最终 Linux 复测镜像为
`bot-native-p2-env:20260923`，摘要 `1d7a76edc178c951df6654eee55d5715d17dad84089ab365cf0e91475ab4a818`；
wheel、启动器和测试文件已与宿主源码核对。测试结束后容器仅剩 init/sleep，镜像保存后容器已移除。

更新启动器后的 P0 复验：[macOS 8/9](../data/native-sandbox-p0-managed-macos.json)、
[Linux 9/9](../data/native-sandbox-p0-managed-linux_arm64.json)、
[macOS 保守管理 4/4](../data/native-sandbox-p0-managed-macos_manager_strict.json)。macOS 仍保留裸 helper
无法确认 detached-child 回收的失败；保守管理用例没有 false completion。

修改的 Python 文件通过 Ruff lint；12 个文件通过格式检查。`tools/builtins.py` 的既有格式差异
在 HEAD 与当前文件中相同，已执行检查并保留，未混入无关格式修改。Node/Bash 语法检查通过。

## P2 调用视图与 AgentRunner 接线

后续增加 `NativeInvocationTarget`：可信主进程为每次工具调用固定 workspace/session/run/tool
身份，工具不能覆盖这些字段或提交另一个 policy/scope。共享后端继续管理进程，调用视图在
poll、读取输出、发送输入和终止之前检查 session/run，列表也只返回同一 session/run 的进程。
释放视图不会终止后台进程或关闭共享 target；未启动的预留 scope 被丢弃，调用标识不能重放。
Run 的只读模式和额外保护路径固定，不能在后续调用中放宽。返回快照深拷贝控制目录身份，
调用方修改返回值不会改变清理目标。运行时指纹包含 workspace 和全部伴随资产，并在实例创建
时固定；恢复记录校验同一 Run 的目录和策略一致性，但完整恢复验收仍未完成。

该提交中的 AgentRunner 已自动创建、注入和释放调用视图。内部 Native 路径当时只接受经过审计的六种
命令/进程工具的精确实现类型；当时尚未接入的文件工具和任意派生自定义工具在调用前拒绝。
这是接线过程中的关闭措施，不能代替 P3 文件 worker 和 P4 完整注册分类。重复读取辅助检查
也不会在原生模式下走宿主文件内容读取；受限 file_version 接口仍待接入。

真实测试覆盖跨 session 和跨 run 的四种进程操作、未知 ID、列表隔离、拒绝后输出未被消费/
输入未被注入/进程未被终止、调用释放后继续交互、身份伪造、scope 重放、只读与保护路径隔离，
以及真实 AgentRunner 的绑定和未接入工具拒绝。macOS 受影响回归 **141 passed**（13 项受管
原生测试），另有生命周期/工具/安装发现回归 **72 passed**。Linux 从当前 wheel 安装后
**31 passed**（24 项原生测试及 7 项启动资源检查）。均无失败、错误或 skip；两平台该轮超时/
取消均为 observed_empty。Linux 有一条 pytest 缓存目录不可写警告，不影响测试或隔离。

首次新增 AgentRunner 测试误用了不存在的 RunResult.run_id，出现 3 passed/1 failed；修正后
以持久化事件的 run_id 和工具记录验证实际绑定。没有改变隔离实现来绕过该测试错误。
详见 [本轮证据](../data/native-sandbox-p2-scopes.json)，包含源码与 wheel 摘要、报告摘要和用例。
Linux 镜像 `bot-native-scopes-env:20260923` 已保留，测试容器已停止并移除。

生产工厂和 allowlist 仍关闭；父子/worktree 委派、完整恢复、启动证明、实时能力检查、并发
准入、worker、brokers、外围入口与 P5 门槛仍待交付，六阶段目标保持不变。

## P3 独立 worker 操作与协议原型

新增标准库实现 [worker.py](../../src/bot/sandbox/worker.py) 及可信侧协议 codec
[client.py](../../src/bot/sandbox/client.py)。固定 read/search/apply_patch/fetch/file_version 五种操作；
请求只接受固定字段、操作参数和 host context，不接受任意 Python 函数、代码、pickle 或策略更新。
协议要求一个完整 JSON 行和 EOF，再执行操作；重复键、非有限数字、额外字段、多个请求在执行前拒绝。
响应逐操作校验 ID、字段、类型、metadata、进展类型和输出预算；worker 不能通过响应设置 executed、
进程状态或其他宿主控制字段。错误响应也不得夹带成功证据。命令 stdout 不进入此 codec。

读文件和版本操作检查普通文件、大小、硬链接及读取前后身份；search 保留匹配/进展语义并增加
输出和遍历上限。patch 保留唯一匹配、原子替换、权限位和前后哈希，替换前再次比较目标元数据，
失败清理临时文件。该比较不等于跨进程的原子 compare-and-swap，完整并发验收仍待进行。
原型上限为请求 2 MB、响应 8 MB、文本输出 1 MB、单文件 16 MB、搜索单文件 5 MB/25 万项。
这些约束须在后续工具接线时明确处理，不能让协议失败冒充操作从未执行。

fetch 使用明确的本机受控 HTTP 代理，逐跳检查 URL 白名单，缺代理或 deny 模式不会直接连接。
响应按上限增量读取；声明长度未收齐时失败。当前请求 identity 编码，服务器仍返回压缩内容时
明确拒绝；压缩兼容、TLS CONNECT/证书、DNS 与私网出口尚未验收，allowlist 仍关闭。

macOS Python 3.14 与 Linux Python 3.12 各 **35 passed**，无 skip；测试包括独立 `-I -S` 进程
不加载项目同名模块和 PYTHONPATH、第二条请求不导致部分写入、patch 并发变化/替换失败、
UTF-8 截断、路径别名/FIFO/大文件及畸形响应、模拟本机 HTTP 代理和不完整响应。Linux 使用
安装后的 wheel、非 root 用户和普通容器，无本轮原生嵌套沙箱权限。首次在外层沙箱内运行的
macOS HTTP 夹具因 bind EPERM 失败，之后在外层沙箱之外完成验证，没有放宽生产隔离策略。

该提交的测试是操作语义与协议测试，**不证明 worker 已在原生沙箱运行，也不表示 P3 完成**。当时 worker
可信安装、解释器与库路径验证、受管 IPC 的退出/超时/取消/截断处理、四个业务工具和重复读取
file_version 接线仍待实现。required 生产入口仍拒绝启动，内部 Native Agent 的文件工具入口
仍拒绝，未回退宿主路径；off 模式保持原行为。
详见 [P3 原型证据](../data/native-sandbox-p3-worker-protocol.json)。

## P3 可信安装、受管 IPC 与工具接线

文件工具现已在内部 Native 路径接入独立 worker：read_file、search_text、apply_patch、fetch_url
在进行任何宿主文件内容或 HTTP 访问之前委派给调用视图。AgentRunner 的重复读取版本检查也通过
同一后端的 file_version 执行。精确实现类型白名单相应增加四种文件/网络工具；任意派生工具仍拒绝，
这仍不是 P4 要求的完整注册分类与 broker 审计。

安装器增加 `--worker-python /absolute/path/to/python3.12`，只接受工作区外的可信 CPython
（>= 3.12）。安装时执行固定探测脚本，登记解释器、标准库和已加载动态库的 SHA-256、平台与版本；
工具启动时只做只读校验，不执行候选解释器、不搜索项目 PATH、不在线补装。文件、父目录所有者和
可写权限也参与检查，运行期间身份变化会拒绝启动。本机 Homebrew 解释器因父目录可被组写入被拒绝，
本轮显式选择私有安装的 CPython 3.12.11，没有放宽权限检查或修改 Homebrew 目录权限。

worker 以固定绝对路径和 `-I -S -B -X pycache_prefix=<runtime>/python-cache` 启动，标准库与
动态库加入只读根，site-packages/dist-packages 被保护。受控 cache 必须是可信空目录且任务不能写入；
普通标准库的 __pycache__ 不参与清单，因为该启动方式不读取这些缓存，也不产生新 pyc。两平台的
本轮实际安装各登记 755 个文件。此证据不补足 Node/SRT 全部可执行依赖的完整性门槛。

IPC 使用受管 stdin/stdout 单次请求与 EOF，核对退出状态、完整输出、截断、ASCII JSONL、请求 ID
和响应 schema。超时、取消、协议异常或 pipe 失败都等待受管清理；非取消失败中，cleanup_failed
优先于普通 IPC 错误，归属不明资源保持占用。worker 进程只供内部 broker 使用，不出现在模型可见的
进程列表，不能通过通用 poll/input/terminate/output 操作接管。scope、后端和清理状态由父进程附加，
不是从 worker 自报字段采信。

本轮结果：macOS 受影响原生/Agent/工具/生命周期回归 **154 passed**，最终 worker、IPC、安装校验
单元测试 **77 passed**；Linux 非 root、已安装 wheel 的原生及相关单元测试 **110 passed**。
随后以非 root 用户运行完整安装器，从锁定下载到最终目录发布均成功；使用这份全新运行时的
**26 项原生复验全部通过**。当前 wheel 的全部 122 个 Python 文件及 5 个运行时资产与源码逐字节一致。
真实文件测试验证项目内读、搜索、patch、版本检查，项目外与 .env 拒绝，项目同名 json.py 不加载，
只读调用不能 patch，以及 deny 模式 fetch 拒绝。Agent 实测在宿主内容读取被测试夹具禁止时，仍能
通过 worker 读取，并在外部修改文件后经 file_version 恢复重复读取。网络 allowlist、TLS 和压缩兼容
并未由这些断网用例证明。

保留早期失败：首次 Agent 试跑遇到新出现的系统 amfid，后续一轮遇到归属不明进程（核对时其中一个
为同 UID 的 mdworker_shared，其余已退出），macOS 严格进程观察因此返回 unknown/cleanup_failed。
没有接管这些进程、删除其保留资源或把失败改记成功。另一次 6 项 IPC 断言错误把正常退出后的
not_requested 状态误判为失败；修正测试后仍验证输出收齐、无存活进程，主动终止路径要求 observed_empty。
这些试跑与最终结果分别记录，不以最终通过覆盖已发现的 macOS 可用性限制。

详见 [P3 接线证据](../data/native-sandbox-p3-worker-integration.json)。历史协议证据中的超长用例名已用
原名称 SHA-256 表示，原始报告与历史源码摘要保持不变；新测试使用固定短 ID，避免将 8 MB 参数写入报告。
生产工厂与 allowlist 仍关闭。P3 全部兼容性与并发行为、P0–P2 的启动证明/完整性/准入和恢复、
P4 子任务/worktree/Git/broker/外围接线，以及 P5 完整矩阵、日常任务和性能验收仍未交付。
Linux 实验环境保留为 `bot-native-worker-env:20260923`，镜像摘要见证据文件；测试容器已移除。

## P0/P1 安装树完整性与 P2 启动准入

显式安装器现在会在锁定的 Node 下载校验及 npm ci 完成后生成 `runtime-files.json`，
`installation.json` 保存其 SHA-256。清单覆盖 node 与 node_modules 全部普通文件内容、目录、
权限位和内部符号链接目标。macOS 本轮安装包含 5,408 个文件、1,119 个目录及 4 个内部链接。
安装后新建、删除或篡改依赖，即使没有改变 package.json 中的版本号，也会使检查失败。
已有运行时缺少该清单时不可用，检查代码不会自动补装、补签或执行候选程序来“证明可信”。

首次检查逐文件核对内容并记录设备、inode、owner/group、权限、link count、大小、mtime/ctime。
后续启动核对完整文件集合、目录和链接身份，保持快照不变；替换为相同字节的另一份文件也会使当前
运行实例失效。可信的新实例可以重新核对相同内容并建立新身份。允许 npm 必需的树内链接，但拒绝
逃出已登记子树的链接、循环、硬链接和特殊文件。固定 launcher、worker、安装记录等顶层资产也检查
独立普通文件、owner/写权限、边界与读取期间身份，不能仅依靠根目录不可写。

清单摘要加入 Native 的进展/恢复身份；元数据校验不改变能力声明，仍为 unverified。
操作系统辅助程序和动态加载依赖的完整证明、自动真实隔离探针、可信启动状态通道仍待交付。
该实现也不构成对拥有宿主同 UID 权限的其他可信程序、root 或受损内核的隔离保证。

两平台均完成新的显式安装。最终 macOS 和 Linux ARM64 各 **89 passed**，无 skip；Linux 使用
非 root 完整安装及实际 wheel。测试包含真实安装的 Node 和 npm 依赖在 target 创建后被修改：
随后调用在目标启动前失败，executed=false，没有目标副作用或已登记进程。
此前 macOS 一轮有 84 passed、1 failed、1 teardown error：十个新出现的归属不明进程触发保守
清理失败，独立核对时均已退出，不能确认来源；没有接管它们或删除保留资源。单独复核该 Agent 用例
通过，补齐顶层资产检查后整轮 89 项也通过，原失败证据仍单列保存。

另记录 30 次缓存元数据校验的微基准：本机首次检查约 1.08 秒，缓存 p50 约 149 ms、p95 约
156 ms，总 CPU 时间约 4.47 秒。这里只测校验，不包含工具执行、模型或任务比较，不是 P5 性能验收。
本轮未额外保存含多份篡改夹具的容器镜像；Linux 报告与安装摘要已导出，实验容器已回收。
源码、wheel 和报告摘要见 [完整性验证证据](../data/native-sandbox-runtime-integrity.json)。

## P0/P1 自动隔离探测与 P2 启动门禁（2026-09-24）

内部 Native target 现在在首次业务调用之前运行固定探针，经相同策略编译器、固定 launcher 和
受管进程路径验证真实隔离。探针使用宿主创建的独立临时工作区、假秘密及 TCP4/TCP6、UDP4/UDP6、
Unix 本机端点；宿主在受限访问前后验证端点可用。检查包括正常读写、项目外和 .env 读取拒绝、
自身及子进程越界写入拒绝、argv 保真、私有 HOME/tmp、直接网络拒绝及代理明确返回 403。
另一个只读探针必须先报告就绪，再经主动取消达到 observed_empty。所有探针资源确认清理后，
才允许业务进程启动；仅检查安装元数据仍不能报告 enforced。

成功结果只在当前 target 内复用，并发启动共享同一个探测任务。取消或失败不会自动重放探针，
也不会回退宿主执行。重复取消不能打断共享清理；清理失败优先报告 CLEANUP_FAILED，并保留未回收
资源。已有 preflight-* 目录会阻止新实例启动，但此目录只是一项阻断证据，不是进程仍存活或可以
接管 PID 的证明，完整崩溃恢复仍待交付。能力记录仅把本轮验证的 file_read/file_write/network_filter
标为 enforced；process_isolation 保持 unverified，resource_limits 保持 unavailable。

Linux 首轮 **122 passed、13 failed**，失败集中在代理探测。固定探针其他检查均通过，HTTP 请求
得到 ECONNRESET；临时诊断仅打开桥接错误输出，确认内侧 socat 连接代理 Unix socket 时得到 ENOENT。
锁定 SRT 先挂载代理 socket，再生成文件系统限制，denyRead('/') 对 /tmp 的掩蔽覆盖了先前挂载。
固定 launcher 现在核对 SRT 返回的端点确为私有 TMPDIR 的直接 socket，并仅将这些端点加入后续读取
恢复列表，没有开放其父目录或策略文件。AF_UNIX seccomp 限制保持启用。新增真实回归同时验证代理
返回 403、同目录策略文件和假秘密不可读、同目录其他 Unix socket 不可连接。

macOS 首轮 **134 passed、1 failed、1 teardown error**：探针进程清理未确认，业务命令未启动。
该次错误未保存内层进程身份，无法确认具体归属；单独复核相关用例通过，不能据此改记原失败。
随后检查发现原 pytest 临时目录已不存在。原夹具使用会被后续测试轮换的 tmp_path，存在丢失保留
现场的风险；现已改为独立 /tmp/bot-native-test-* 目录，仅在确认清理成功后删除，失败时把路径写入
JUnit。生产探针清理错误也增加有界的进程身份诊断，不记录业务 argv、环境或输出。

两平台均重新执行显式安装，固定 Node 24.14.0/SRT 0.0.77，不修改旧安装。最终 macOS ARM64 与
Linux ARM64 各 **136 passed，无 skip**；Linux 使用非 root UID/GID 65534 和当前已安装 wheel。
wheel 的 124 个 Python 文件及 5 个运行时资产与源码一致，Linux 对应安装内容也逐字节一致。
Linux 验证容器退出前只剩 init、测试环境保持进程和核对命令，报告导出后已回收该容器。
原失败、修复定向验证、最终结果与摘要分别保存在 [自动探测证据](../data/native-sandbox-preflight.json)。

这不是六阶段完成或生产发布：可信的逐命令启动状态通道、系统辅助程序/动态加载依赖证明、
并发准入、恢复、完整子任务/worktree/Git/broker 及 CLI/Web/eval 接线仍待完成。doctor 尚未提供
实时探测入口，allowlist 和生产 required 工厂仍关闭；完整 S01–S15、十项日常任务、性能及全仓
回归仍按原验收范围推进。此次启动探针也不构成多租户或完整进程隔离证明。

## P2 可信启动状态与关闭竞态（2026-09-24）

每次 Native 调用新增主进程与已校验外层 Node launcher 之间的独立匿名管道。主进程仅向该
launcher 传递写端；包装器及业务子进程只继承标准输入输出。协议限制为 4 KiB、两个 JSONL 帧，
校验版本、调用标识、状态转换和 EOF，等待上限为 10 秒。业务 stdout/stderr 保持普通输出，
即使包含完全相同的状态 JSON，也不具备改变启动状态、能力或执行结论的权限。

`failed` 仅在可信启动器确认尚未派发目标时产生，可给出 `executed=false`。`dispatched` 表示
包装器已经创建，不证明业务目标已完成 exec，故 `executed` 仍为 null/unknown。缺失、截断、
错误身份、超长或超时的通道返回 LAUNCH_FAILED，并保留 unknown；先登记进程再等待通道，
失败或取消都经过受管回收。回收不明优先报告 CLEANUP_FAILED，保留资源和占用。已有受管硬超时
即使发生在启动握手期间，也继续保留 TIMED_OUT 语义，不根据目标输出推断 POLICY_DENIED。

同时修复 Local/Native 共同生命周期中的关闭竞态：close 等待已获准但尚未完成登记的 spawn；
并发 close 共享清理任务，调用者重复取消不会打断清理。后端前置关闭失败时仍回收已登记进程，
仅在所有清理确认后释放 Run 资源；失败后允许显式重试清理。新增真实子进程测试延迟 transport
交付，覆盖启动和关闭同时取消、并发关闭、清理失败优先级以及占用保留。

Tool 与 worker 结果透传可信启动状态。Agent 对明确的 `executed=false` 排除恢复进展；对已有
成功结果、新内容和有效 progress 的调用，unknown 不额外抹除其证据。这不把 unknown 改记为 true。

Linux 首轮连续出现取消探针未就绪，13 failed、6 passed 后中止以诊断，未作为完整回归。记录
证明两帧已收到，但 EOF 未到达；同一管道写端仍存在于 socat、包装器及内侧进程。单独使用低编号
FD 的实验未复现，而固定 FD 256 可稳定复现：Linux 下仅设置 Node `stdio: inherit` 不能保证任意
继承 FD 被关闭。修复在 SRT 启动辅助进程之前，经 Node/libuv 重新打开管道获得 CLOEXEC，再关闭
继承的原描述符。新增高编号 FD 测试扫描描述符 3–1023，按设备/inode 查找管道副本并尝试伪造帧，
不只检查原描述符号码。实际目标不得持有该管道，长驻探针须及时得到 EOF。独立高 FD 实验和
Linux 正常调用、普通/高 FD 伪造的 3 项定向测试均证明修复有效；未采用额外固定等待来隐藏竞态。

macOS 早期验证还记录了自检遇到未归属进程后的保守阻断，保留根目录与 PID/启动时间诊断，
没有误杀或用后续 PID 消失补判原清理成功。新增取消测试曾误从不含输出的进程列表等待就绪；
已改为显式读取输出，并在 finally 中等待测试任务结束。修复前完整回归的 140 passed/3 failed/
2 teardown errors，以及后续 142 passed/1 failed/1 teardown error 均单独保存；后者失败用例
单独通过，仍不把该整轮计为通过。macOS 的观察式进程归属在当前宿主上会保守阻断，是尚需接受
与处理的可用性限制，不能因此削弱 unknown 的失败语义。

修复后的 Linux ARM64 最终 **144 passed，无 skip**，使用 UID/GID 65534 和实际安装的 wheel。
macOS 最终整轮为 **141 passed、3 failed、2 teardown errors**；其中一项是高 FD 夹具申请 256
超过该宿主默认 RLIMIT_NOFILE=256，现按 Darwin 使用 128，Linux 保留 256。三项定向复验均能
继续到真实执行，但又出现 **3 failed、3 teardown errors**，全部因进程归属 unknown，不能报告
macOS 验收通过。只读身份核对记录到 cloudd、CloudTelemetryService、TrustedPeersHelper、
mdworker_shared 等仍存活的进程；名称不构成排除或接管依据，没有据此放宽规则或杀进程。

Agent 与生命周期回归 **110 passed**，关闭/状态/回收相关单元组合 **61 passed**。修复版 wheel
的 125 个 Python 文件和 5 个运行时资产与源码一致；Linux 实际导入的安装文件逐字节一致。
Linux 报告导出并确认仅剩容器 init/保持进程后，已回收本次验证容器。实际变更文件的 Ruff lint
和 Node 语法检查通过；builtins.py 的整文件格式问题已在 HEAD 复现，未混入无关格式重写。

原始失败、FD 诊断、安装摘要、文件摘要和最终结果见
[启动状态验证证据](../data/native-sandbox-launch-status.json)。Linux 测试文件摘要单列保存：
macOS 高 FD 夹具兼容修正发生在该 Linux 整轮之后，Linux 分支仍使用 256，生产源码与 wheel
没有改变。本增量不代表 P2 或六阶段完成；macOS 归属问题、系统辅助程序/动态加载依赖证明、
并发准入、恢复及完整 P4/P5 仍需推进，required 工厂和 allowlist 保持关闭。

## P0/P1 系统辅助程序完整性（2026-09-24）

显式安装新增 `host-executables.json`：固定 `/bin/sh`、`/bin/bash`、`/usr/bin/env`、
`/usr/bin/which`，以及 macOS 的 `/usr/bin/sandbox-exec` 或 Linux 的 `/usr/bin/bwrap`、
`/usr/bin/socat`、`/usr/bin/rg`、`/usr/bin/true`。不再从 Homebrew、`/usr/local/bin` 或继承
的 PATH 发现替代程序。固定位置缺依赖时安装失败；旧安装缺少清单时必须显式重装到新目录，
执行路径不自动补装或迁移。

安装器仅读取这些候选程序，建立摘要，不通过运行它们证明可信。检查包含真实文件、管理员
所有权、写权限、硬链接数量、有界读取以及读取前后身份；同时遍历中间符号链接和父目录。
首次检查重新核对内容，后续启动复核缓存的文件、链接及父目录身份。清单摘要纳入安装记录和
Native 恢复身份。运行时只读策略保留链接名称及最终目标，Linux 对可写区域中的链接父目录
继续生成保护规则，避免仅限制最终 inode、却允许替换链接入口。

两平台均完成显式安装。Linux ARM64 使用 UID/GID 65534 和最终 wheel，**147 passed，无 skip**。
macOS 整轮 **146 passed、1 failed、1 teardown error**：文件工具用例在自检期间发现 6 个
无法归属的进程，业务 worker 尚未启动，清理返回 unknown。只读身份复核时 2 个 PID 已不存在，
其余相同启动时间的进程名称为 transparencyd、followupd、ProtectedCloudKeySyncing、
mdworker_shared；名称与后续消失均不构成原清理成功或接管进程的证据。失败目录仍保留，
没有认领、终止这些进程或放宽判断。该文件工具用例单独复核 **1 passed**，不能据此把整轮
改记为通过。

变更相关单元组合 **104 passed**；配置、准入及启动资源组合在正常 macOS 权限下 **57 passed**。
后者在外层受限沙箱中的首次运行有 55 passed、2 failed，原记录单独保存。新回归覆盖内容篡改、
等字节替换、链接改指向/循环、可写父目录、PATH 冒充、旧清单拒绝及安装失败不发布半成品。
真实集成用例确认安装清单遭修改时业务副作用不会发生。最终 wheel 的 126 个 Python 文件及
5 个运行时资产与源码一致，Linux 实际安装内容也逐字节一致；报告导出并核对进程后已回收容器。

该清单记录管理员已安装的程序字节，不证明其发行来源，也不覆盖动态库、加载器配置、完整
解释器链、ACL、操作系统或内核完整性。运行时元数据有效仍不等于隔离 enforced。动态加载依赖、
并发准入与恢复、doctor 实时入口、P4/P5 和 macOS 观察式清理的可用性限制继续保留为门槛。
生产 required 与 allowlist 仍关闭。源码、安装、完整结果及失败现场见
[系统辅助程序验证证据](../data/native-sandbox-host-executables.json)。

## P0/P1 加载器配置与写权限边界（2026-09-24）

系统可执行文件的摘要不覆盖加载配置。Linux 的库解析还使用 `/etc/ld.so.cache`，系统
`/etc/ld.so.preload` 会影响程序启动；不能仅靠去掉 LD_* 环境变量处理。
依据：[Linux 动态加载器手册](https://www.man7.org/linux/man-pages/man8/ld.so.8.html)。

显式安装新增 `loader-controls.json`。Linux 记录库缓存、ld.so.conf 及其配置目录的内容摘要和
文件身份，要求 ld.so.preload 不存在（空文件或悬空链接也拒绝）。macOS 校验 `/usr/lib/dyld`
内容，并记录 System/Library、Preboot Cryptex 和旧版位置下已知 dyld 缓存目录的身份与条目。
**共享缓存载荷未做内容摘要或签名验证**，不把它标为完整动态库证明。缺失路径记录最近已有父
目录，配置/缓存条目新增、删除、替换或权限变化均使后续检查失效，不自动采用新状态。

Native 把这些控制路径及符号链接目标传入 resolver；任何任务写根只要覆盖或进入该区域，
在工作区扫描和原生探测前就拒绝。规则也覆盖尚未创建的控制文件，避免依靠 Linux 对不存在
路径的挂载来保护它。保护只添加写限制，不为任务开放原始共享缓存或其父目录的读取。
清单摘要纳入恢复身份；旧安装缺少清单时要求显式重装。安装器的标准库辅助模块现使用独立
包命名空间，允许相对导入而不加载 bot 应用。

两端安装成功，相关单元组合 **136 passed**。Linux ARM64 非 root 整组 **181 passed，无 skip**；
macOS 整组 **179 passed、2 failed、1 teardown error**。一项启动失败用例因为两个无法归属的新
进程返回 cleanup_failed/unknown，优先级高于测试预期的 launch_failed；可信状态仍明确记录
executed=false。只读复核时两个 PID 已不存在，原目录仍保留，不能追认原清理完成。

另一项高 FD 检查匹配到了业务 Node 的 fd 5；定向诊断看到该 FD 可写，但可信管道此前已得到
EOF 和恰好两帧，未收到伪造数据。XNU 的管道 inode 来自内核地址哈希，可在对象销毁后复用，
原测试在管道释放后仅比较 dev/inode，存在误匹配窗口。
依据：[XNU 管道 stat 与对象回收实现](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/sys_pipe.c)。
测试现保留一个不继承的读端直到目标扫描结束，使管道对象存活，且不阻塞写端关闭后的 EOF；
继续扫描所有 FD 副本并尝试伪造状态，没有放宽断言或修改生产通道。普通 FD、高 FD 和启动
失败的定向复核 **3 passed**。另以主动继承 FD 128 的正向对照确认扫描仍能发现真实继承，
且写入确实到达原管道。Python 简化分配循环未复现复用，原生失败与诊断仍分别保留。

最终 wheel 的 127 个 Python 文件与 5 个运行时资产均核对一致，Linux 实际安装内容一致。
FD 夹具修正晚于 Linux 与 macOS 整轮，原测试摘要单列保存；生产代码和 wheel 没有改变。
Linux 证据导出后已回收验证容器。完整结果见
[加载器控制验证证据](../data/native-sandbox-loader-controls.json)。

该增量尚未覆盖 ELF/Mach-O 传递依赖、动态加载及搜索路径、全部配置来源或共享缓存签名。
并发工作区准入、恢复、doctor 实时入口、P4/P5 和 macOS 观察式进程归属问题继续推进。
六阶段未完成，生产 required 与 allowlist 仍关闭。

## P1/P2 跨进程工作区准入（2026-09-24）

Native 现在在工作区扫描前取得持久占用，覆盖实际策略的读取、写入与拒绝子树。相交的读写或
写写调用返回 `SANDBOX_UNAVAILABLE / admission_conflict / executed=false`；读取共享、相互
独立的区域可以并行。Run 私有 HOME/tmp/cache 也计入写区域，因此同一 Run 的调用会保守串行。
父工作区明确排除的 worktree 子树可以单独占用，但本次不交付 P4 worktree 委派或 Git broker。

同一宿主 UID 固定使用规范 `/tmp/bot-native-admission-<uid>`，不按 HOME、workspace 或 state_root
分区。注册表要求当前用户拥有的私有真实目录，记录与永久 mutex 文件要求独立 0600 普通文件。
有界 JSON、目录/文件身份、摘要和宿主 PID/启动时间用于发现替换或错误接管。短时非阻塞
`flock` 只保护记录事务；业务进程运行期间靠持久记录保留区域，不长期占用系统文件锁。
这符合 [Linux flock](https://www.man7.org/linux/man-pages/man2/flock.2.html) 与
[Apple flock](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/flock.2.html)
的协作锁语义，不约束未参与该协议的宿主写入者。

扫描失败、未启动作用域撤销、启动准备/派发失败会沿资源清理路径释放占用。运行中取消、超时
或正常退出则必须确认进程和调用控制资源清理后才释放；清理失败继续保留配额与持久占用。
目标进程不能读取、创建或删除注册表文件。宿主退出后记录不会自动过期，恢复必须匹配注册表
身份和不可变占用摘要，并确认原宿主身份已不再存活。转移的是清理记账，不能据此认领任意
进程、重启任务或判断后代已退出；完整崩溃恢复认证仍属于 S15 门槛。

Linux 首轮 **206 passed、1 failed**，失败是跨实例写冲突的错误阶段。SRT 在写调用期间为缺失
的 `.git` 建立保护用挂载占位文件；原 resolver 在取得占用前检查 `.git` 类型，错误地把竞争调用
判为需要 Git broker 的工作树。已把这项检查移到取得占用后，且失败时释放未启动占用；真正的
Git 元数据间接引用仍拒绝。首轮和诊断失败分别保存，不改写成通过。

修正后 Linux ARM64 非 root 整组 **208 passed，无 skip**；macOS 整组 **206 passed、2 failed、
2 teardown errors**，新增 7 项原生准入测试在两平台均通过。macOS 一项只读调用在收尾时遇到
1 个无法归属的进程；另一项 Agent 接线测试在启动自检时遇到 12 个，业务目标尚未启动。
两者均返回 cleanup_failed/unknown，分别保留测试根目录及占用记录。后续只读检查时这些 PID
已不存在，未据此追认原清理成功，也未删除记录、资源或按名称接管进程。原整轮仍记为失败。

准入与 resolver 单元组合 **54 passed**，进程生命周期组合 **102 passed**，启动资源/状态/
配置相邻组合 **72 passed**。最终 wheel 的 128 个 Python 文件和 5 个运行时资产与源码逐字节
一致，Linux 实际安装内容一致；本次没有改变 Node/SRT 资产。Linux 导出报告并确认只剩
init/sleep 后已回收容器，其注册表最终只剩永久 mutex。完整证据见
[准入协调验证记录](../data/native-sandbox-admission-coordination.json)。

该实现仍是协作式协调，不能锁住任意宿主程序；注册表竞争拒绝而不自动重放业务，缺少完整
恢复元数据的崩溃可能保守留下占用。完整恢复、动态依赖证明、doctor 实时入口、P4 broker 与
全部外围入口、P5 任务/性能/发布验收继续按原计划推进。六阶段尚未完成，required 和 allowlist
仍关闭。

## P2 可信恢复日志与宿主崩溃（2026-09-24）

Native 现在把恢复记录保存在私有准入注册表中。创建子进程前先写入 prepared 记录，绑定运行时、
系统启动周期、状态目录、调用资源、原宿主身份和启动前进程基线；根进程身份登记后，再保存
PID、启动时间、父进程与 session 身份。临时文件写入、fsync、rename 和目录 fsync 沿用同一份
占用记录的事务，不依赖宿主是否来得及发出清理事件。启动前的 prepared 写入失败时不创建
进程；若进程已创建、登记日志写入失败，则先回收，执行状态保留 unknown，不能沿用准入错误
中的 executed=false。若 rename 后持久化报错，只在记录与本次写入完全一致时保留正确的内存
身份，以便后续清理。

SQLite/事件负载只用于审计，不能提供进程信号或目录删除权限。恢复入口主动扫描受保护的占用
记录，即使调用从未发出清理事件也可被发现。只有原宿主已退出、运行时和资源身份匹配、根
进程身份已持久化的记录才可转移清理记账；仍需新一轮进程观察与清理完成才能释放占用。缺少
日志的旧版占用、prepared/unidentified 窗口或身份变化均保留资源并阻止新业务，不猜 PID、不按
年龄过期，也不重放命令。这是私有注册表与可信宿主的权限边界，不是抵抗任意同 UID 宿主写入者
的密码学认证。

日志不保存原始 argv、环境变量或业务输出，恢复的逻辑命令使用明确占位文本，输出保持不完整，
executed 为 unknown。控制目录已不存在可表示清理完成了一部分，但不据此认定进程已退出。
恢复后的 numeric SID 也不作为新增进程归属依据：宿主离线期间 session 编号可能被复用，且
新的 session leader 也可能已退出。回收只使用已记录的 PID/启动时间和可确认的父子关系；
无法归属的脱离进程仍返回 unknown。

真实崩溃测试由受控测试宿主在确定边界停住，随后 SIGKILL 该宿主，覆盖 prepared、registered、
运行时身份变化、控制目录已删除四种情况。伪造事件使用另一个仍存活进程的真实 PID/启动时间，
验证不能将清理重定向到它；恢复作用域还检查复用旧 SID 的模拟孤儿不会被认领。prepared
用例的夹具只有因为明确停在 fork 之前、且不能返回派发，才回收其确定未启动的资源；生产实现
没有这种证明，因此继续保留不完整记录。

Linux 首轮整组 **237 passed**，新增两种崩溃阶段后的定向复核 **4 passed**，两组存在重叠，不
累加为 241 个独立用例。macOS 整组 **236 passed、3 failed、3 teardown errors**；四个新增崩溃
阶段均通过。三项失败分别发生在读取共享用例的启动自检、注册表访问用例收尾和文件 worker
的越界读取用例收尾，都有无法归属的进程导致 unknown。最终记录分别有 3、2、2 个歧义身份；
只读复核时它们已不存在，三个测试根目录和三份占用仍保留，未据此追认原清理成功。

随后复核代码发现恢复时的 SID 复用边界，已收紧该行为；macOS 四个恢复场景的定向验证
**4 passed**，该版本 Linux ARM64 非 root 整组 **239 passed，无 skip**。再补充创建进程后的
注册表拒绝、持久化报错两种测试，确认先回收且不虚报 executed=false；最终 macOS 定向验证
**6 passed**，最终 Linux 整组 **241 passed，无 skip**。最终 wheel 的 129 个 Python 文件与 5 个
运行时资产均与源码一致，Linux 实际安装内容逐字节一致。三个 Linux 验证容器均在导出证据、
确认只剩 init/sleep 且注册表只剩 mutex 后回收。此前 macOS 冒烟运行的
**2 passed、1 failed** 仍单列保存：失败发生在固定自检，原报告没有足够信息确定具体原因。
外层受限沙箱中的首轮单元组合 **43 passed、4 failed** 也保留；正常宿主权限下相关生命周期
复核 **11 passed**。恢复/策略组合 **59 passed**，相邻清理/工具/状态回归 **71 passed**。

完整结果、修正前后源码与 wheel 摘要、Linux 安装核对、macOS 保留现场及容器回收证据见
[恢复日志验证记录](../data/native-sandbox-recovery-journal.json)。完整 S15 仍未通过：根身份
落盘前的窗口、嵌套自检资源与启动恢复流程、CLI 诊断等继续推进；动态依赖证明、doctor 实时
入口、P4 全部工具和 broker、P5 安全/任务/性能/发布验收也保持原范围。六阶段未完成，生产
required 和 allowlist 仍关闭。

## 启动恢复与 Run 生命周期整合（2026-09-24）

Native target 现在在新业务前共享一次异步恢复：仅从受保护日志恢复身份，回收所有已认证的
旧调用，确认 observed_empty 后才删除旧 Run 的 HOME/tmp/cache 并进入隔离自检。并发等待者
共用结果，重复取消不能打断回收；清理失败优先于取消，失败结果不自动重试或重放业务。
恢复过的 Run 和释放的调用 ID 在当前 manager 中禁止复用，这不是跨重启的持久墓碑。
`startup_recovery` 的 executed=false 描述尚未启动的新 Run；旧进程仍保持 executed=unknown
和 output_complete=false。

AgentRunner 在恢复和自检完成后才调用模型、调度记忆提取及启动子任务。启动失败、超时或
取消通过 Run 生命周期结束；启动尚未完成时不调用模型终结器。Runtime 构造不再为 Native
查询 SQLite 清理负载，避免审计数据成为恢复权限来源；Local 行为保留。Native 即使没有
运行中进程也要求异步关闭，未使用过的 target 关闭时仍发现和处理崩溃残留。

命令、worker 和流式调用均先等待恢复。伪造调用身份在恢复或自检之前拒绝；未迭代的流不
申请文件系统占用，释放视图仍会使调用 ID 失效。流式视图通过 `aclosing` 等待内层清理，
防止关闭外层流时底层进程尚未回收。真实宿主崩溃用例扩展为 11 种阶段，覆盖原四种日志
状态以及启动、命令、worker、流式、提前关闭流、未使用即关闭和恢复中重复取消；业务标记
要求只写入一次，另一个无关存活进程不能被审计伪造数据误杀。

本轮验证保留了各次失败及修改顺序，各组有重叠，不能累加为独立用例总数：

| 验证范围 | 结果与解释 |
| --- | --- |
| macOS 初次 10 个崩溃阶段 | 8 passed、2 failed、1 teardown error；命令阶段在崩溃屏障前的自检归属 unknown，worker 夹具误用了 `read_file`。改为 `read` 后 worker 通过，命令复核仍 unknown |
| macOS 中间版本整组 | 261 个独立用例，255 passed、6 failed、5 teardown errors；不作为最终源码整组通过证据 |
| macOS 扩展定向组 | 12 passed、1 failed；11 种崩溃阶段和权限冻结通过，身份用例仍按旧的立即绑定语义断言。随后明确惰性流和释放 ID 防重放契约 |
| macOS 最终定向验证 | 身份/未启动流重放及崩溃提前关闭流 2 passed；权限冻结修正夹具后另行 1 passed |
| Linux ARM64 安装包整组 | 非 root、要求原生后端，264 passed、1 failed，无 skip。权限冻结夹具两次复用 `changed` 调用 ID，第二次被防重放提前拒绝；改用独立 ID 后定向复核 1 passed，生产代码未再修改 |
| 最终启动与流生命周期测试 | 17 passed；验证共享恢复、取消、Runtime 和 Agent 顺序。这类 stub 测试本身不证明 OS 隔离 |
| Agent、CLI、Web、子任务/worktree、记忆及清理相邻回归 | 194 passed；不是全仓发布验收 |

macOS 整组失败中，恢复取消、自检及文件 worker 收尾出现归属 unknown；身份伪造用例还暴露
了先自检后校验输入的顺序问题，现已修正。权限冻结当次业务 snapshot 为 failed，但原记录
被截断，不足以确定具体原因，不能全部归为同一种清理问题。增强了后续夹具的有界状态记录，
不收集原始业务参数、环境或输出。七个失败现场和七份占用继续保留；只读观察提取到 28 个
歧义身份，其中六个当时仍以相同 PID/启动时间存在。没有按名称认领、发信号或删除资源，
也没有把后来不存在的身份追认为原先清理成功。

最终 wheel 的 129 个 Python 文件和 5 个运行时资产与源码一致，Linux 实际安装逐字节核对
一致。修正夹具前后的测试源码摘要分别保存。Linux 容器无宿主挂载，在导出报告并确认只剩
init/sleep、占用注册表只剩 mutex 后停止并删除。12 个改动 Python 文件通过 Ruff 和格式检查。
完整错误、原始报告摘要及运行环境见 [启动恢复验证记录](../data/native-sandbox-startup-recovery.json)。

本增量完成普通启动恢复接线，完整 S15 仍未通过。嵌套自检崩溃的父目录/socket 身份尚未完整
入日志；当前继续拒绝状态/运行时作用域不匹配的记录，不能猜测清理。根身份落盘前窗口、
恢复诊断、动态依赖证明、doctor 实时入口、P4 的工具与宿主 Git/worktree broker、P5 全矩阵、
任务、性能和发布验收仍待完成。生产 required 工厂和 Native allowlist 保持关闭。

## 嵌套自检资源恢复（2026-09-24）

固定自检现在在首次创建探针进程之前，持久化 schema 2 的资源清单。清单位于宿主私有目录，
绑定父/子运行时及启动周期作用域、父目录、work/runs/socket 目录身份，以及宿主 PID、启动
时间和认领标识。文件要求为独立的 0600 普通文件，使用不跟随链接的有界读取、身份复核、
原子替换和文件/目录 fsync；缺失、旧版、权限不合格或身份不符的记录均拒绝恢复。

资源认领在共享准入互斥锁中完成，原宿主或其他恢复者仍存活时不能接管。父 target 只把经
资源清单认证的精确子状态区域交给固定探针 manager；进程回收权限仍来自原受保护的准入
日志，资源文件不能单独授予 PID 权限。恢复关闭子 manager，不重新执行旧探针；新 Run
仍须单独完成一次新的隔离自检，不能复用旧探针的能力结果。这里的信任边界是私有目录和
沙箱的写入拒绝，并非抵抗任意未受限同 UID 写入者的密码学签名。

仅在子进程清理确认后删除 socket 与父目录。删除持有准入锁，任何仍冲突的占用都阻止删除，
包括子 manager 未恢复出进程、但另一个 owner 仍持有占用的情况。异步清理返回后，再复核
work/runs 身份，防止删除替换后的目录；socket 目录已经删除支持幂等恢复。未知归属、递归
自检目录、旧 schema 或无法认证的资源继续保留，不按名称、时间或孤立的 PID 不存在判断回收。

新增真实崩溃测试覆盖五个窗口：资源已持久化但尚未 fork、只读取消探针运行中、子 manager
已关闭但目录未删、socket 已删但父目录未删，以及恢复者认领后再次崩溃、由第三个宿主恢复。
用例同时检查无关的受控存活进程未被终止，恢复本身不重新执行探针，新的自检仍必需。

Linux ARM64 非 root 最终安装包整组 **296 passed，无 skip**。macOS 同组 **294 passed、
2 failed、2 teardown errors**，五个新增嵌套自检崩溃用例均通过。两项失败来自原有的普通
崩溃测试：stream 在恢复清理时归属 unknown；cancel 在测试宿主的自检中归属 unknown，
尚未到预定崩溃屏障，后续嵌套回收也未确认。未把这两项改为 skip 或追认为通过。

macOS 首轮五个场景的 **2 passed、3 failed、3 teardown errors** 同样保留：registered 的
旧资源恢复断言已通过，随后新的自检 unknown；after_processes 和 after_socket 在预定屏障
之前就因清理 unknown 失败。加上整组失败，共保留五个测试根目录、五份准入占用和四份
自检资源清单。只读观察从有界诊断提取了 37 个歧义身份，其中 28 个当时仍以相同 PID/启动
时间存在；这不是完整进程清单，也不构成认领、发信号或删除资源的权限。

最终单元/启动组合 **72 passed**，相邻资源、策略与生命周期回归 **83 passed**；两组与平台
整组有重叠。首批新单元测试的 **11 passed、1 failed** 是缺少必填字段的策略夹具，补齐后
通过。Linux 首次安装输入归档遗漏运行时资产，安装器在创建目标目录前因文件缺失终止；
补齐安装资料后成功，未改变锁定版本或使用运行时回退。审查补充的删除前 work/runs 身份
复核及两项替换测试已包含在最终两平台整组中。

最终 wheel 的 130 个 Python 文件和 5 个运行时资产与源码一致，Linux 实际安装逐字节核对
一致。容器无宿主挂载，在确认只剩 init/sleep、注册表只剩 mutex 后回收。七个修改或新增的
Python 文件通过 Ruff 和格式检查。完整错误、版本摘要、保留现场及容器证据见
[嵌套自检恢复记录](../data/native-sandbox-preflight-recovery.json)。

完整 S15 仍未通过：资源清单落盘前、根进程身份落盘前、递归删除中断等窗口继续保持阻断，
尚需完整矩阵与诊断验收。旧 schema 1 现场不自动迁移或清除。动态依赖证明、doctor 实时
入口、P4 工具和宿主 broker、P5 安全/任务/性能/发布验收仍保持原范围；六阶段未完成，
生产 required 工厂和 Native allowlist 仍关闭。

## 沙箱专项 doctor 与恢复诊断（2026-09-24）

增加 `doctor --sandbox` 只读入口和 `doctor --sandbox-probe` 显式恢复/固定自检入口，支持
选择运行时、私有状态目录和 JSON 输出。默认状态区域按工作区划分，仅供诊断使用；检查
既有业务现场必须显式选择对应状态目录。使用方法见[原生沙箱诊断](../guides/native-sandbox.md)。

只读入口不构造 Native target，不创建注册表或锁，不启动模型、会话数据库或业务工具。
安装元数据有效时能力仍为 unverified；缺锁、记录损坏或身份检查失败返回 unknown，不能
为了生成正常报告而补写现场。清单显示限量的目录、占用标识、阶段和宿主存活状态，不显示
恢复日志中的 argv、环境或业务输出，也不授予清理权限。

显式探针复用 Native 的认证恢复、真实隔离自检和异步关闭。它使用固定的拒绝网络配置和
一次性资源，不测试项目额外读写根或 allowlist，不执行项目命令，不修改原配置或开放生产
required 工厂。报告分别记录安装、自检、能力和清理结果。SIGINT/SIGTERM/SIGHUP 复用已有
CLI 关闭处理，重复信号不打断清理；清理失败优先返回 1，不能被中断退出码掩盖。

macOS 最终 **5 passed**：只读成功、真实探针、缺失运行时、连续两次 SIGTERM 后完成实际
回收、SIGKILL 自检宿主后由 doctor 恢复可信资源。初轮同样 5 passed，最终加入测试宿主超时
回收和只读健康夹具初始化后复核。测试通过不覆盖此前已记录的 macOS 归属 unknown 问题。

Linux ARM64 以 UID/GID 65534 验证已安装 wheel，整组 **353 passed、1 failed**。失败用例的
夹具已经创建准入目录但没有初始化 mutex；只读 doctor 正确返回 unknown，未创建锁。
随后在健康夹具中显式初始化锁，定向 **1 passed**。生产实现没有因此放宽；单元测试继续
验证锁缺失时拒绝且不创建文件。原始整组失败完整保留，不改写为整组通过。

相邻 CLI、配置、准入、恢复和关闭回归 **97 passed**；初始子集 72 passed，两者及平台测试
有重叠。六个 Python 文件通过 Ruff 与格式检查。wheel 中 131 个 Python 文件和 5 个运行时
资产逐项匹配源码及 Linux 安装位置，确认没有误测归档中的源码包。安装准备曾修正镜像
sleep 入口参数及测试归档读权限，未改变沙箱策略或固定依赖。容器确认只剩 init/sleep 且
注册表只有 mutex 后已移除。

完整版本、源码摘要、原始错误与 JSON 输出见
[doctor 验证记录](../data/native-sandbox-doctor.json)。本次完成诊断入口增量；完整动态依赖
证明、S15 剩余窗口、P4 工具分类和管理 broker、P5 两平台安全/日常任务/性能/全仓回归仍未
交付。生产 required 工厂和 Native allowlist 继续关闭，六阶段目标未完成。

## P4 工具分类与会话 broker（2026-09-24）

工具注册表现有固定执行分类，覆盖 13 个已知 Tool 实现及 16 个内部动作。分类由宿主实现
决定，不采用模型、项目或插件自报的类别。Native 注册检查精确类型、名称、execute 方法、
Schema 和权限声明；既有注册表交给 Native Runner 时整体检查，子注册表继承约束，目录
暴露和调用前继续复核。新增或注册后替换的未审计实现不能绕过该检查。

命令和文件/网络 worker 归为 confined；进程控制沿用 Native 调用归属和既有输出限制；
计划发布、工具目录激活、上下文引用、会话历史与压缩来源读取归为 brokered。计划虽然
包含纯计算，但其对外动作含持久化，整体不标记为 pure。当前没有独立 pure 内置入口。

会话 broker 只使用宿主选择的 workspace/session/活动 Run，通过参数化 SQLite 查询核对
归属。严格 Schema 拒绝额外的 session、Run、路径或 SQL 参数，引用和压缩来源仍检查
原会话授权。计划事件发布前再次检查 Run 归属。会话 broker 的正文和错误文本分别有界，
截断会显式标记并取消完整进展证据。分类进入宿主生成的执行事件，不由工具输出提供权限。

审计同时收紧两个辅助入口：Native 进程提示和重复调用观察仅采用当前 session/Run 的
快照；显式会话压缩先检查工作区并完成后端启动。Skill、记忆、子 Agent/worktree broker
仍在固定清单中标记待接入，在 Run 启动和模型猜测调用时拒绝，不能提前启动辅助任务。
ksys/tuner 仍不支持宿主设备或额外权限，明确返回 SANDBOX_UNAVAILABLE，不转为宿主执行。

最终 macOS **2 passed**，Linux 已安装 wheel **251 passed、无 skip**。两个真实 Agent
场景分别启用/关闭重复读取检查，覆盖命令和 worker 的原生身份、未审计子类注册拒绝、
当前会话计划、已授权/其他会话 blob、伪造 session 参数和已知设备工具拒绝。Linux 另含
进程控制归属及 CLI、配置、会话、进展和启动/关闭回归，以 UID/GID 65534 运行。wheel 的
131 个 Python 文件与 5 个沙箱资产匹配源码和实际 site-packages 安装；测试容器只剩
init/sleep、准入注册表只剩 mutex 后已移除，无宿主工作区、凭据或 Docker socket 挂载。

最终分类与生命周期单元组合 **51 passed**，其中新增边界测试 42 项。正常宿主环境的
Agent/上下文/生命周期组合 **175 passed**。之前扩大回归的 **267 passed、1 failed**
暴露了真实生命周期回归：把待接入服务检查放在构造阶段，会阻断未使用即关闭 Runtime 的
异步恢复用法。已将服务检查移至 Run 启动及显式压缩入口，注册完整性仍在构造时检查；
修正后的定向组合通过。各组存在重叠，不能累加为独立用例总数。

另一次 175 项组合误在外层受限环境运行，**163 passed、12 failed**，错误集中于 Local
清理和重复调用判断。该环境实测拒绝执行 `/bin/ps`；随后正常宿主环境中的同一组 175 项
通过。原失败仍完整保留，原测试目录及不跟随符号链接的取证副本保留，复核采用独立
basetemp 避免轮换原现场。累积失败后曾准备中断 pytest，但 PID/启动身份复查时宿主已
自行结束，未发送任何信号；原会话以 exit 1 正常完成报告。现场副本和当前进程状态都不
提供清理权限，也不构成追认旧清理成功的证据。

完整源码、安装摘要、分类清单和逐轮错误见
[工具边界验证记录](../data/native-sandbox-tool-boundary.json)，使用说明见
[原生沙箱诊断指南](../guides/native-sandbox.md)。这些记录不覆盖此前 macOS Native 的
unknown 归属失败。P4 标记为进行中，尚需 Skill/记忆资源 broker、父子/worktree 策略、
固定 Git 管理操作与共享准入、CLI/Web/eval 和其他宿主入口；P0/P1/P2/P3 剩余验收以及
P5 安全、日常任务、性能与全仓回归范围不变。生产 required/allowlist 仍关闭。
