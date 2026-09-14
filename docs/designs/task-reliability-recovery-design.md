# 任务可靠性修复：进程取消、流式重复恢复与交付验证

日期：2026-09-14。状态：**P0-A / P0-B 已实施并完成离线验收；P1 待实施**。原设计代码核对基线：`4c9d8e6`。
实施行为、启用方式及边界见 [P0 验收记录](../evaluations/task-reliability-p0-results.md)。
依据：[256K / 512K 失败复盘](../evaluations/context-length-flash-failures.md)。
本设计细化已有 [输出截断恢复方案](output-truncation-recovery.md)，复用
[自动终止架构](../architecture/termination.md)；不另建一个无限重试循环。

## 1. 决策和范围

先交付两个独立的 P0 修复，再增加 P1 交付检查：

| 优先级 | 修复 | 要改变的实际行为 |
|---|---|---|
| P0-A | 有期限的进程取消与残留跟踪 | 取消不再等待逃离原进程组的子进程自然退出；未清理完明确失败 |
| P0-B | 流式重复检测、响应完整性检查、有限恢复 | 在重复正文耗尽 32K 输出前打断；允许回到有工具的主循环一次 |
| P1 | 与文件版本绑定的交付检查 | 临时探针通过不等于交付文件通过；文件修改后旧验证自动失效 |

保持现有模型选择、输入预算、压缩策略和实验总额度。上述改造解决运行可靠性及验证缺口，
不保证模型能正确还原相机或棋盘格公式。首版检测阈值是候选值，必须经日志回放和误拦检查。

## 2. P0-A：进程取消

### 2.1 当前问题与接口落点

当前 [`LocalExecutionTarget`](../../src/bot/execution/local.py) 将进程退出、输出 EOF、
进程组退出共同绑定到 `managed.finished`；`terminate_process()` 对该事件无期限等待。
`_managed_process_alive()` 又可能在 `termination_complete` 被设置后提前判定不再存活。
[`TerminateProcessTool`](../../src/bot/tools/builtins.py:631) 当前对返回的 snapshot 无条件
给出 `success=True`，不能只在底层增加超时后继续沿用这个结果语义。

新增内部模块 `execution/process_scope.py` 和 `execution/process_transport.py`，分别负责
归属识别和可独立关闭的进程/管道生命周期；`LocalExecutionTarget` 保持现有执行入口。

### 2.2 分开四种事实

`_ManagedProcess` 增加 `root_exited`、`pipes_closed`、`cleanup_task`、`cleanup_result`，
由唯一的生命周期协调器决定结果；`finished` 仅表示本次执行观察已形成可返回结果，
不再单独证明所有后代已退出。

`ProcessSnapshot` 增加以下有默认值的字段；已有保存记录可缺省读取：

```text
cleanup_status: not_requested | pending | observed_empty | verified_empty | incomplete | unknown
containment: session_scan | cgroup | root_only
output_complete: bool
remaining_processes: [{pid, start_time, relation}]
cleanup_elapsed_seconds: float
```

`ProcessStatus` 增加 `terminating`、`cleanup_failed`。根进程 `returncode` 保留原义，不能
替代后代的清理结果。`truncated` 继续表示输出保留上限；`output_complete=false` 另行表示
管道被提前关闭，不能将缺少尾部的输出作为完整重复证据。

进程退出与管道关闭确实可以分开发生，Python 提供独立的 `process_exited()` 和
`pipe_connection_lost()` 回调。[Python 进程协议](https://docs.python.org/3/library/asyncio-protocol.html#subprocess-protocols)
因此首版使用公开的 `loop.subprocess_exec()` / `SubprocessTransport` 封装，独立记录根退出，
通过 `get_pipe_transport(fd).close()` 关闭本方管道；避免依赖 `_transport` 私有属性。
迁移时保留既有交互输入、输出限额、增量游标和 UTF-8 增量解码行为。

### 2.3 清理范围：覆盖本次故障，明确平台能力

POSIX 首版采用 **原 session + 已观察到的后代身份**，不只保存初始 PGID：

1. 启动时继续 `start_new_session=True`，记录根 PID、SID、启动时间、执行环境标识。
2. 每个 execution target 共享一个进程快照采集器，活跃期间初始每 250ms 采集一次，
   取消前及 TERM/KILL 阶段立即重采集。使用 `psutil` 枚举 PID/PPID/启动时间，并在 POSIX
   用 `os.getsid(pid)` 关联同 session 成员；新增依赖的版本范围在实施时按支持平台验证。
3. 清理集合是同一 session 的已验证成员，以及此前观察为其后代的身份集合。子进程
   `setpgrp()` 后 SID 不变，覆盖本次 `timeout → geo` 故障及本地复现。
4. 每次发信号前再次校验身份；不按进程名、不按整个用户、不扫描后随意广播 kill。
   Linux 支持时持有 pidfd 并发信号；其他平台按 PID/启动时间复核，明确为 best effort。
   [Python pidfd 信号接口](https://docs.python.org/3/library/signal.html#signal.pidfd_send_signal)

轮询不能保证捕获在两次快照之间就 `setsid()`、double-fork 并重新归属的所有后代。
所以 `session_scan` 最好只能返回 **observed_empty**，不能宣称严格隔离。
权限不足、身份无法复核或仍有未知管道持有者时返回 `unknown`，不伪报清理完成。
Windows 首版保留根进程后端，标为 `root_only`；需要同等后代保证时另接 Job Object 后端。

Linux 的严格后端作为后续独立工作：宿主已委派可写 cgroup 时，启动命令前由 helper
完成加入专属子 cgroup 的握手，再 exec，避免先运行后迁入的 fork 竞态。最后用
`cgroup.kill` 并检查 `cgroup.events` 的 populated 状态。该接口覆盖整个子树，处理并发
fork；不假设 Harbor 容器默认有写权限。[Kernel cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html#core-interface-files)

### 2.4 统一取消算法

候选配置放在 `[agent.process_cleanup]`，由宿主控制，不暴露给模型工具参数：

```toml
total_timeout_seconds = 5.0
term_grace_seconds = 2.0
kill_grace_seconds = 2.0
drain_grace_seconds = 1.0
```

总期限覆盖身份采集、发信号、等待及释放资源；不是每个后代各等 5 秒。
验证阶段和不受信子进程都不能绕开这个期限。快照采集不能同步阻塞事件循环；采用单个
后台采集任务和带时间戳的结果，超时返回 unknown，不不断启动不可取消的线程。

```text
running → terminating
  → 立即采集范围，向已确认成员发 TERM
  → 到 TERM 阶段截止仍有成员：再次采集、发 KILL
  → 到清理截止：最多再排空输出至总期限
  → 关闭本方管道、停止读写/监控任务、记录一次结果
       范围已空且输出完整 → cancelled
       已知残留/观察失败/输出无法确认 → cleanup_failed
```

无静默进程超时：该流程只在用户取消、工具取消、显式硬超时或 runtime 关闭时运行。
根进程正常退出但后代仍工作时继续呈现活跃状态，不能把安静的后台任务直接杀掉。

工具取消、硬超时、用户取消和 `aclose()` 复用同一 `cleanup_task`，重复请求只能等待
同一任务，不能双重回收。用 shield 防止外层取消取消掉清理任务，但 shield 内部仍有
独立总期限。`aclose()` 对所有目标并行清理并共享关闭截止时间，失败也必须在 finally
中关闭本方句柄，不能像当前路径在抛错前跳过任务释放。

已知残留和 unknown 保留为未解决记录，占用受管额度；不能通过设置 `finished` 或
清空字典释放额度。当前 run 进入 `blocked / process_cleanup_incomplete`，停止新建
可写进程，保留观察和重试清理能力；其他独立 scope 不受影响。显式全局截止/用户取消
优先保留其原结束原因，同时附上清理失败。重启后核对环境及身份，不能对旧 PID 盲目发信号。

工具清理成功：`success=true`，报告 observed/verified 范围；未成功：`success=false`、
`status=failed`、`reason_code=process_cleanup_incomplete`。只有确认发生的清理状态变化
可以产生 STRONG 进展，重复 terminate 或 unknown 不刷新 epoch。

## 3. P0-B：流式重复检测和恢复

### 3.1 拦截位置及响应隔离

新增 `core/termination/stream_guard.py` 的纯状态检测器，接入
[`_request_model_with_retries()`](../../src/bot/core/agent.py:2893) 的 delta 接收位置。
它产生独立 `StreamInterrupted` 结果，不包装成可重试 ProviderError。

主循环在当前 `_parse_tool_call()` **之前**统一分类：

```text
完整正常响应 → 原工具解析、权限检查、消息保存和执行
本地重复中断 → 保存审计片段、隔离整条响应 → 恢复判定
finish_reason=length/max_tokens → 保存审计片段、隔离整条响应 → 恢复判定
用户取消/全局预算截止 → 原终止路径，不新增恢复请求
```

即使截断时某个工具参数恰好能解析为合法 JSON，也不执行该响应中的任何调用。
同批完整/不完整调用一起隔离；不将半截 assistant(tool_calls) 写入活动历史，避免留下
无对应 tool result 的协议消息。已经执行完成的以前批次不回滚、不重复执行。

### 3.2 第一版检测规则

只对 main 阶段普通正文自动干预。reasoning 通道、工具参数、代码围栏和结构化输出
先只观测；最终总结复用检测但仅允许静态 fallback，不允许再次回到主循环。
收到工具调用 delta 后，该响应停止正文重复自动中断；输出完整性规则仍覆盖它。

候选参数：

```toml
[agent.stream_guard]
mode = "observe" # 通过回放与集成验收后，目标评测显式设为 enforce
min_response_chars = 8192
window_chars = 32768
check_every_chars = 256
min_period_chars = 128
max_period_chars = 4096
min_repetitions = 6
min_repeated_span_chars = 4096
confirmations = 3
```

检测只做保守的连续周期重复，不用另一模型判断“是否有价值”：

1. 增量识别正文/代码边界；折叠空白但保留数字、标点和标识符，不能把变化的坐标或
   列表项归成相同文本。代码或结构化片段打断连续检测窗口，不能跳过它们后拼接命中。
2. 对最近 32K 规范化字符的逆序串计算 Z-array，检查后缀是否由某个 128–4096 字符
   周期连续重复至少 6 次，且覆盖至少 4096 字符。字节/字符内容直接核对，不仅比较哈希。
3. 周期块必须包含多句普通文字，排除纯空白/分隔符；连续三次检查发现同一最小周期
   （允许块的循环移位）且重复覆盖继续增长，才触发。字符数不冒充 tokens。
4. 内存固定为窗口规模；单次检查 O(window)，每 256 新字符最多一次，不按每个 SSE
   chunk 遍历全历史。块很大的 delta 内部也按相同检查间隔处理，保证分块方式不改变判定。

模式含 `off / observe / enforce`。observe 只记录一次 would_interrupt，不更改输出。
上述规则能否在目标响应 16K 字符以内触发，是待验证的验收目标，不是已测量成果。
对未加围栏但合法重复的正文，初版仍可能误拦，因此先通过非循环长文本、代码、表格、
中文内容的负例回放，不能仅用这一条 512K 样本调出阈值就全量启用。

### 3.3 中断、保存与恢复请求

检测器命中后，停止向用户转发后续重复 delta，并显式关闭 provider iterator 和对应
HTTP response；新增可关闭流的 Provider 契约/兼容适配，关闭期限候选 1 秒。共享
httpx client 保留，只关闭本次响应。不能依靠跳出 async for 让 GC 以后关闭连接。
本地关闭不证明远端立刻停止生成，不能把未返回 usage 的请求计成免费。

将已接收文本/工具缓冲经现有 redactor 保存为受会话权限控制的 blob，事件保留原因、
字符范围及引用。活动历史保留中断前原请求的消息前缀；异常输出不整段回灌。
它尚未成为正常消息，因此无需删除或改写之前已提交的历史。

在尾部追加宿主生成的恢复控制消息，记录 `origin=runtime`、中断事件及片段引用。
兼容 Chat API 时投影为尾部 user 消息；本地来源元数据和用户新输入区分，不把它计为
新的用户任务。保留原 system 和工具声明，不切换 `tool_choice=none`。

恢复消息控制在 1,024 字符以内，先使用结构化事实：

> 上次响应因连续重复中断，该响应没有执行工具。请使用现有证据选择一个可验证的下一步；
> 需要修改时将变化写入目标文件并验证，证据不足时明确说明。不要继续复制刚才的推测。

普通 length 但没有重复时，可附最多 2,048 字符的非重复尾部与 blob 引用，用于拆分
过大的 patch 或继续交付；重复片段默认只给引用，避免将循环重新带入上下文。
引用片段仍作为不受信任务内容处理，不能升级成宿主指令。

首版沿用当前 thinking 模式；不自动切换模型、温度或开启 thinking。启用恢复的 Provider
必须通过完整消息序列协议测试；已有 reasoning 空/缺失字段兼容债务沿用原方案单独修复。
本次 Flash 的非思考模式可作为首个验证范围，不能假定其他模式已经兼容。

### 3.4 恢复额度与结束语义

新增 `core/termination/recovery.py` 的 `RecoveryController`，供 stream repetition、
output length、后续 delivery check 共用，**不是 Provider 的网络重试计数**：

```toml
[agent.recovery]
enabled = false # 目标评测在完整验收后启用
max_attempts_per_episode = 1
max_attempts_per_task = 2
max_episode_seconds = 120
max_response_output_tokens = 8192
```

episode 表示从异常到取得新证据的一段执行；一次恢复可继续正常工具循环，而非只允许
一个工具。120 秒是该恢复阶段的总额度；它与原运行剩余时间取较小值，不重置 1,740 秒
等外部上限。下次模型响应输出 cap 取原值和 8K 的较小者，原输入硬预算及输出预留不扩大。

“恢复有效”至少要求完整调用实际执行，并取得首次诊断证据、交付文件变化或新的验证结果。
仅改计划、写不相关临时文件、重复读取、再次输出长篇文字，不自动获得新的恢复资格。
无交付契约时可使用现有 WEAK/STRONG 信号但保守限制；任何 epoch 增长都不能清零任务总额度。
说明性任务可正常完成，不强制为了证明进展而写文件。

任务总额度绑定 `session + task_generation + execution_scope`，在恢复消息和事件写入前
事务扣减，resume 沿用；用户发起新任务才显式增加 task_generation。压缩、改配置、
runtime 合成 user 消息和重复 resume 不清零。使用 `recovery_states` 独立表避免借通用
progress epoch 意外刷新次数；与恢复消息插入、事件 outbox 在同一事务提交。

恢复后再次重复且无额度：`blocked / stream_repetition_after_recovery`；纯输出截断持续
失败保留 `limit_reached / model_output_limit`，附 recovery_exhausted。
有效任务结果不由“模型又回复了一次”判断。用户取消、费用或时间用尽优先于恢复。
既有单次 finalizer 仍禁止工具；重复终止时优先静态事实摘要，其他情况最多一次模型总结。

### 3.5 用量记录和缓存影响

新增本地 `request_attempt_id`，每次真实 API 尝试生成一个；网络重试、恢复和 finalizer
分别计数。新增 `model.request.started / interrupted / finished` 事件，`model.usage`
携带 attempt ID。主动中断的 `provider_finish_reason` 保持 null，不伪造服务端 length。

有 terminal usage 则保留精确值；没有则记录 `usage_status=missing`，仅另列本地输出估算。
更新 [`context_strategy_audit.py`](../../src/bot/evals/context_strategy_audit.py)：同一 attempt
只计一次，避免同时把 delta 中断、interrupted 事件和 retry 算成多个请求；兼容旧日志。
缓存率仍只用已知 token 的加权比例，并明确未知请求数。

启用严格费用上限时，在请求前按输入估算与输出 cap 做保守预留；中断缺 usage 时不释放
其额度，剩余费用不足则不发起恢复。预留只是执行策略，不冒充服务端账单。

恢复追加尾部通常保留历史复用条件，但不保证 Provider 命中；不为恢复强制压缩、替换
system 或改成 finalizer 的工具选择。实验须单列正常主请求、被中断请求、恢复请求与收尾。

## 4. P1：交付检查绑定实际文件

### 4.1 明确何时可以拦截“完成”

引入可选 `DeliverySpec`，放在 run/task 的宿主元数据，包含 required_files、可见检查命令、
依赖清单、每项超时、成功条件和来源。只接受用户明确要求或任务适配器提供的契约；
模型可以提出候选检查，不能自行将它升级为“完整覆盖验收”的可信契约，也不能改弱契约。
不解析任意 prompt/shell 来自动推断所有交付物，不把 benchmark 隐藏验收提供给模型。

有可信契约时在主模型自然 stop、准备 `completed` 之前检查；没有契约时保留现有完成行为，
展示 available/unverified 的证据状态，不强制纯问答写文件。新字段区分：

```text
completion_status: 现有 RunResult.status
delivery_status: not_required | unverified | verified | failed | stale
verification_scope: 声明的检查 ID 集合
```

`verified` 只表示声明范围通过，不代表任意任务的正确性证明；官方 reward 独立计算。
存在可信必需检查且失败时不能报告 completed，进入一次共用额度内的交付恢复；仍失败
则 blocked / delivery_verification_failed。全局截止时只保存现状，不再启动验证或模型。

### 4.2 验证对象必须与交付版本一致

由新的 `DeliveryVerifier` 记录：

```text
contract_hash / execution_scope
artifact_manifest = 路径、类型、内容 SHA-256（目录按声明清单递归）
check_id / argv / cwd / dependency_manifest / environment_fingerprint
verification_mode: snapshot | live_workspace
started_at / finished_at / returncode / output_blob_ref
verified_manifest_hash / verdict / limitations
```

模型说“通过”、计划变为 completed、临时探针退出 0，都不能生成该记录。
检查命令仍经过原工具权限、预算及进程管理入口，不能在 verifier 内部另开绕过策略的 shell。

严格可复用的验证在依赖清单的独立不可变输入快照上执行，输出写临时目录；返回前重新
哈希当前交付文件，必须等于被验证的版本。若检查只能在活跃工作区运行，则标记
`verification_mode=live_workspace`，不能把前后两次 hash 相同当作证明期间未发生修改。
异步写入未结束、依赖不完整或超时使结果 unverified/stale，而不是通过。

应用补丁的版本事件可用于即时失效；为覆盖 `sed`、Python、run_shell 和外部编辑，完成
入口还要重新核验文件 manifest，不能只监听 ApplyPatchTool。
检查后任何交付物/声明依赖内容变化，使旧记录 stale；同一版本相同检查可复用结果，
避免每次模型 stop 都重跑昂贵命令。

这会让 256K 组的“探针包围盒接近”与 `/app/mystery.c` 的相机错误分开呈现。
文件存在、可编译、大小合规、输出匹配是不同检查，不用其中一项替代其他项。

## 5. 事件、状态与实施边界

关键新增事件：

| 事件 | 最小事实 |
|---|---|
| `process.cleanup_started/finished` | process ID、范围能力、阶段耗时、残留、输出完整性 |
| `model.stream_repetition` | attempt ID、检测模式、周期/次数/覆盖、字符偏移、证据引用 |
| `model.request.interrupted` | 本地原因、已接收字符、usage 状态、连接关闭结果 |
| `run.recovery_started/finished` | 原异常、task/episode、剩余额度、实际恢复结果 |
| `artifact.verified/invalidated` | 契约、文件版本、检查 ID、结果及来源 |

枚举新增与缺省字段同时更新工具状态映射、进展检测、CLI/Web 展示、导出和审计；旧结果
仍可读取。观察模式不伪造恢复成功、节省费用或任务通过。清理状态持久化记录身份与范围，
但执行环境改变后不复用旧进程句柄；恢复预算与交付验证也按 scope 校验。

| 批次 | 主要代码位置 | 完成标准 |
|---|---|---|
| A | `execution/local.py`、`execution/base.py`、新增 process_scope/transport、`tools/builtins.py`、runtime 配置接线 | 逃离原 PGID 场景取消有期限；残留如实呈现；交互/输出回归通过 |
| B1 | 新增 stream_guard、`core/agent.py`、Provider 流关闭契约、`core/events.py`、审计 | 仅观测回放；完整性门禁阻止所有截断调用；缺 usage 不漏记 |
| B2 | 新增 recovery、`sessions/store.py`、尾部控制消息、finalizer 兼容 | 一次有工具恢复，任务次数跨 resume，所有外部预算和取消仍有效 |
| C | DeliverySpec/Verifier、完成门禁、文件版本与结果字段 | 同版本验证可复用，修改后失效，纯问答不被拦截 |

P0-A 可以先独立发布。B1/B2 验收后在受控任务启用 enforce；P1 的 required 检查仅对
具备可信契约的任务启用。cgroup 严格隔离、Windows Job Object、近似语义重复检测和
通用自然语言交付物提取属于后续工作，不作为首版成功的假设。

## 6. 验收用例与效果评测

先确定性回归，再真实任务；以下是计划，尚未执行新实现的验收：

| 范围 | 必须覆盖的用例和断言 |
|---|---|
| 进程 | 普通子进程、setpgrp、TERM 忽略、根先退出但管道仍开、已知 setsid 后代、未知后代/权限失败；取消应在配置总期限加测试调度容差内返回 |
| 归属 | 其他 run 的进程不受影响；PID 复用拒绝发信号；重复 terminate、硬超时与 aclose 竞态只生成一次清理结果 |
| 资源 | 成功清理后无活跃已知成员、无读写/监控任务或句柄泄漏；失败保留残留身份且不释放受管额度 |
| 重复 | 原 512K 第 111 响应离线回放，目标在 16K 字符内命中；不同 delta 切分、中文、代码围栏、变化表格/数字、长而不重复文字负例 |
| 副作用 | 半截 JSON、合法 JSON+length、同批完整+截断调用都执行 0 次；恢复后的完整调用只执行一次 |
| 恢复 | 中断后一次有效工具执行可继续；再次相同循环正确结束；resume/压缩/计划更新不能清零任务额度 |
| 协议与费用 | HTTP 流按期限关闭、共享 client 可复用；缺 usage 明确未知；预算不足、用户取消或截止之后零新增模型调用 |
| 交付 | 探针通过而目标错误、验证后 sed 改写、依赖改写、后台写入、验证超时、同版本复用、纯问答无文件要求 |

优先扩展 `tests/unit/test_execution_tools_policy.py`、`tests/unit/test_provider.py`、
`tests/integration/test_agent_loop.py` 和审计测试；检测器使用纯函数测试及日志回放。
PID/管道场景用本地有界进程，不依赖真实模型；恢复及交付状态用 deterministic Provider。

真实对照先固定一个上下文预算，比较原版 / 仅 A / A+B / A+B+C，每组至少 3 次同题，
同期记录后端模型、工具及费用。阶段性实测后再做 128K/256K/512K 的预算比较，不同时
把模型、输出 cap、压缩阈值和恢复策略都改掉。新模型输入不包含事后参考源码及隐藏验收。

报告取消延迟、残留次数、重复检测误拦、恢复后有效执行率、官方通过率、缺 usage 数和
额外费用；恢复率分母是仍有预算且符合恢复条件的事件。单次成功或降低截断次数均不能
直接当作稳定成功率提升。

原设计交付时未修改运行时；后续 P0 实施与验证见上述验收记录。P1 仍保留为设计，未发起新的付费实验。
