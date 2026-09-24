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
