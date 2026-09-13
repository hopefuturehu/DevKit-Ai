# 通用 CLI Agent 助手设计

> 状态：设计基线 v0.2；MVP 代码已实现（M0–M3 全部完成），环境验收项见 [implementation-status.md](implementation-status.md)
> 工作名：`bot`
> 设计基线：通用 Agent Core、CLI-first、local-first、OpenAI-compatible、Skill/Tool 可扩展、安全默认开启
>
> 2026-09-10：Skill 相关实现按 `875750c` 同步，完整验证范围见 [Skill 历史交付](../evaluations/skill-context-validation.md)。

## 1. 产品定义

`bot` 是一个运行在用户工作环境中的通用 Agent 助手。用户通过自然语言交代任务，Agent 可以读取上下文、制定执行步骤、调用工具、请求必要授权、持续反馈进度，并把过程保存为可恢复、可审计的会话。

产品定位不是“鲲鹏专用聊天助手”，而是“**通用 CLI Agent + 领域 Skill + 领域 Tool**”。Agent Core 提供代码、文件、命令、检索、分析等通用能力；鲲鹏应用迁移、性能分析和源码优化作为首个重点领域，通过内部维护的 Skill 与 Tool 扩展实现。

它不是一个只包装 LLM API 的聊天客户端，也不在第一版追求成为完整的个人自动化平台。首要目标是把下面这条链路做可靠：

```text
理解任务 → 获取上下文 → 调用工具 → 验证结果 → 必要时修正 → 交付结果
```

### 1.1 目标用户

- 经常在终端中工作的开发者和高级用户。
- 希望用自然语言完成代码、文件、检索、分析和自动化任务的用户。
- 需要自带模型凭据、数据主要保留在本地、执行过程透明的用户。

### 1.2 核心价值

1. **能完成工作**：不止回答问题，而是能安全地读取文件、修改文件、运行命令和验证结果。
2. **过程可信**：工具调用、权限、成本、失败和重试对用户可见。
3. **随时可控**：支持中断、转向、审批、恢复和回滚友好的文件修改方式。
4. **容易扩展**：模型、工具、Skill、MCP 和未来的消息入口都通过稳定接口接入。
5. **本地优先**：会话、配置、审计记录默认保存在本机；密钥不进入会话和日志。

### 1.3 差异化假设与验证责任

本项目的差异化不能只依赖“自研了一个 Agent Loop”。预期价值主要来自：

- 鲲鹏迁移和性能分析的内部领域知识被沉淀为可维护、可评测的 Skill；
- KSYS、Tuner 等 DevKit 工具被转换成模型可安全调用的结构化 Tool；
- 后续在客户授权和数据边界允许时，接入鲲鹏内部私有数据；
- 建立领域任务集，量化比较通用 Agent 自主探索与“自研 Agent + Skill + Tool”的效果差异。

“其他 Agent 无法做好鲲鹏任务”目前是待验证假设，不作为既定事实。项目必须通过评测证明 Skill、Tool 和内部数据分别带来的增益，形成可复现的产品与组织价值证据。

## 2. 范围与取舍

### 2.1 MVP 必须具备

- 交互式 CLI，以及适合脚本调用的单次运行模式。
- OpenAI API 协议兼容的 Provider；开发阶段暂定使用 DeepSeek V4 Flash 或 Pro 对应的 API 配置。
- 普通文本生成、流式输出和结构化 Tool Calling；执行型 Agent 模型必须提供可靠的结构化 Tool Calling。
- 文件读取、搜索、补丁编辑、Shell 命令四类本地工具。
- 运行环境探测，包括操作系统、CPU 架构和已安装命令行工具。
- 通用 Tool 接口和 Subprocess CLI Adapter；首批领域适配 KSYS 与 Tuner。
- Workspace 边界、危险操作审批、密钥脱敏和完整审计事件。
- 会话持久化、恢复、分叉和上下文压缩。
- `AGENTS.md`、项目配置、用户配置，以及自动或显式激活 Skill 的上下文装配。
- 指定单一目录内 Skill 的发现、三段式披露、多 Skill 激活和手动刷新。
- Ctrl-C 中断、运行超时、步数/费用上限、重复调用检测。
- 人类可读输出与稳定的 `--json`/JSONL 机器输出。

### 2.2 MVP 暂不实现

- Telegram、Slack、WhatsApp 等消息平台 Gateway。
- Cron、后台常驻服务和无人值守长任务。
- 自动修改 Skill。
- 对等 Agent Team、递归委托和跨进程常驻子 Agent；进程内一级后台 Worker Pool 已实现。
- 浏览器 GUI 自动化、语音、图像生成。
- 公共插件市场。
- SSH、远程安装和跨机器命令执行。
- 为跨机器分析设计专用的结构化结果包；MVP 使用用户粘贴的自由文本结果。
- 完全断网环境中的本地模型运行保证。
- Skill 安装、远程仓库、签名、多来源合并和 Skill Bundle。
- 鲲鹏内部私有数据库、RAG 或长期自动知识写入。
- 将 KSYS、Tuner 编排固化成不可偏离的严格工作流。

这些能力不是被否定，而是必须建立在稳定的 Agent Core 和安全模型之上。入口层与核心层分离后，它们可以在后续版本增加，而不改写 Agent Loop。

## 3. 核心使用体验

### 3.1 启动方式

```bash
# 进入交互会话
bot

# 带初始任务进入交互会话
bot "分析这个项目并给出重构建议"

# 非交互执行，适合脚本和 CI
bot run "运行测试并解释失败原因"

# 输出 JSONL 事件流
bot run --json "总结当前改动"

# 恢复最近会话或指定会话
bot resume
bot resume <session-id>
```

### 3.2 管理命令

```bash
bot init                     # 初始化项目级 .bot/ 配置
bot doctor                   # 检查运行环境、Provider、工具和权限
bot config get|set|edit      # 管理配置
bot session list|show|fork   # 管理会话
bot model list|set           # 管理模型
bot skill list|show          # 查看指定目录内的 Skill
bot mcp list|add|remove      # 后续接入 MCP
```

### 3.3 会话内命令

```text
/help          查看命令
/status        模型、Token、费用、工作区、权限模式
/model         查看或切换模型
/tools         查看本会话可用工具
/agents        查看后台子 Agent 的任务、状态和 required 标记
/skills        查看候选 Skill、已激活 Skill 及其路径
/skills reload 重新扫描配置指定的 Skill 目录
/permissions   查看或调整本会话权限
/compact       发布可恢复的单一活动摘要
/compact rebuild 从原始 Transcript 重建活动摘要
/compact rollback <id> 回滚到已验证的摘要版本
/remember <text> 保存显式长期记忆
/memories      查看显式与自动长期记忆
/forget <id-or-key> 删除显式记忆或抑制自动记忆
/memory extract [run-id] 手动提取已完成 Root Run
/new           新建会话
/exit          退出
```

会话内命令由 CLI 本地处理，不应把 `/model`、`/exit` 等控制指令发送给模型。

### 3.4 交互原则

- Agent 在开始较长工作前，用一句话说明当前理解和下一步。
- 每个工具调用显示名称、关键参数、状态和耗时；大输出折叠但可展开。
- 运行期间允许用户继续输入。新输入作为 `steering` 事件，在安全边界处改变后续执行。
- 审批必须说明“将执行什么、为什么需要、可能影响什么”。
- 最终答复优先报告结果、验证状态和未完成项，不复述冗长过程。

## 4. 总体架构

核心设计原则是：**入口层只负责交互，Agent Core 只处理运行，所有状态变化都产生事件。**

```text
┌──────────────────────────────────────────────────────────────┐
│                         Entry Points                         │
│ Interactive CLI │ One-shot CLI │ Future Gateway/API/ACP     │
└──────────────────────────────┬───────────────────────────────┘
                               │ RunRequest / Steering / Approval
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                         Agent Core                           │
│ Run Coordinator │ Agent Loop │ Context Assembler │ Limits   │
└───────────┬────────────────┬───────────────────┬─────────────┘
            │                │                   │
            ▼                ▼                   ▼
┌─────────────────┐ ┌─────────────────┐ ┌──────────────────────┐
│ Model Providers │ │ Tool Runtime    │ │ Policy Engine        │
│ OpenAI-compatible│ │ Registry/Runner │ │ Allow/Deny/Ask       │
└─────────────────┘ └────────┬────────┘ └──────────┬───────────┘
                             │                     │
             ┌───────────────┴──────────────┐      │
             ▼                              ▼      ▼
 Built-in / Domain Tools           Future MCP / ExecutionTarget

┌──────────────────────────────────────────────────────────────┐
│                    State & Observability                     │
│ SQLite Event Store │ Session Views │ Audit Log │ Metrics     │
└──────────────────────────────────────────────────────────────┘
```

### 4.1 为什么采用事件驱动

Agent 的一次运行不是简单的“请求—响应”，而是文本增量、推理状态、工具调用、审批、中断、重试和最终结果组成的事件流。统一事件模型可以同时服务：

- TTY 中的流式渲染；
- `--json` 的机器输出；
- 会话恢复和崩溃诊断；
- 未来的 Web/Gateway 入口；
- 评测和轨迹回放。

建议的基础事件包括：

```text
run.started
assistant.delta
assistant.reasoning.delta
assistant.message
model.response
model.request.retry
model.empty_response
tool.requested
approval.requested
approval.resolved
tool.started
tool.output
tool.completed
context.consolidated
run.steered
run.failed
run.completed
```

## 5. Agent Loop

```text
接收 RunRequest
  → 载入会话与运行限制
  → 探测当前操作系统、CPU 架构和可用工具
  → 装配 System / Project / Memory / History / Skill / Tool Context
  → 调用 Provider 并消费流式事件
      → 若得到最终文本：保存并结束
      → 若得到 Tool Call：
          → 校验 Schema
          → Policy Engine 判定 allow / deny / ask
          → 必要时等待审批
          → 执行工具并流式返回结果
          → 规范化、截断和标记工具输出
          → 保存事件并进入下一轮
  → 每轮评估可验证进展
      → 停滞警告：要求获取新证据或改变参数
      → 恢复阶段：强制切换工具、假设或实现路径
      → 恢复后复发：执行一次无 Tool 收尾并以 blocked 结束
  → 显式资源限制、取消或不可恢复错误时安全结束
```

### 5.1 运行预算与安全边界

正常 Run 默认不设置固定总步骤数和总墙钟上限。长任务依靠进展状态机持续运行，资源层仍保留
局部边界和用户取消能力：

- `max_steps`、`max_wall_time_seconds`：默认 `None`；显式设置时作为部署或评测硬策略；
- `max_input_tokens`、`max_output_tokens`；
- `max_cost_usd`：默认关闭，Provider 能提供价格信息且显式设置时生效；
- `max_tool_output_bytes`：单次工具输出边界；累计边界默认关闭，可显式配置；
- `max_consecutive_failures`：默认关闭；旧部署需要立即熔断时可显式配置；
- `process_wait_seconds`：命令 Tool 同步等待多久后返回受管进程句柄；
- `process_hard_timeout_seconds`：默认 `None`；显式设置时是受管进程绝对存活上限；
- `process_inactivity_warning_seconds`、`process_inactivity_recovery_seconds`：静默进程的提醒和检查
  阈值；默认不设置自动 finalize 阈值，因此安静但仍存活的长计算不会被误杀；
- `max_managed_processes`：单个 Runtime 可同时持有的受管进程上限；
- `cancel_token`：CLI、信号和未来 Gateway 共用的取消机制。

上下文窗口、单次输出大小、并发进程数量和权限审批属于局部安全边界，不因取消全局任务硬上限而
取消。SWE-bench、Terminal-Bench 等评测入口继续显式传入步骤、墙钟和费用预算，以保证样本可比。

### 5.2 防循环

检测四类无进展行为：

1. 完全相同且失败的 Tool Call 重复出现；
2. 同一工具用不同参数持续失败；
3. 幂等工具反复返回相同结果。
4. 固定周期的 Tool Call/Result 序列循环。

Tool 通过 `ToolResult.progress` 显式返回强进展、弱进展、无进展或外部等待。强进展开启新 epoch；
外部等待根据静默时长提醒但默认不会自动终止；无进展累积停滞计数。控制状态按 session 写入
SQLite 并跨 Run 恢复。首次达到阈值时注入警告，随后进入一次受控恢复阶段；恢复后相同模式复发
时撤销 Tool 定义，只允许模型生成一次事实化收尾。预算、上下文和失败也复用该终止协调器。

### 5.3 Markdown Agent 与父子调度

子 Agent 的能力定义来自内置、用户级和项目级 Markdown catalog。项目定义需要按
workspace 真实路径与全部定义内容哈希显式信任；同名定义全部禁用，不静默覆盖。
Markdown frontmatter 限定 model、Tool allowlist、Skill、隔离级别、执行模式和资源上限；
正文只提供行为指令，不能扩大底层权限。

父 Agent 通过以下内部控制 Tool 使用一级 Worker Pool：

- `task`：默认前台等待，可转后台；传入 `task_id` 时续接原 child session；
- `send_task_message`：将后续指令持久化后在后台续接；
- `spawn_agent`：先持久化任务和独立 child session，再立即返回 `task_id`；
- `get_agent_status`：按父会话边界查询状态、结果和独立用量；
- `await_agents`：事件驱动等待多个任务，超时只返回状态，不取消任务；
- `cancel_agent`：幂等取消 queued/running/waiting-approval 任务；
- `apply_agent_patch` / `cleanup_agent_worktree`：校验采用 patch 交付并回收受管 worktree。

内置 profile 为 `explorer`、`reviewer` 和 `coder`。前两者只获得本地只读 Tool allowlist；
`coder` 必须在从显式 `base_ref`（默认 `HEAD`）创建的 detached Git worktree 中工作，
不直接写主工作区。每个任务
创建独立 `AgentRunner`、`SkillManager`、`DefaultPolicyEngine` 和 child session；Provider、
SQLite Store、ExecutionTarget 与无状态 Tool 实例可以共享。child runner 不注入 Worker Pool，
因此委托深度由结构保证为 1。

上下文采用显式委托：child session 只得到 objective、constraints、acceptance criteria 和经父
session 授权的 `context_ref`，不使用 `fork_session`，也不复制父历史。child 结果以不可信
Tool 数据返回；完整结果和包含 staged、unstaged、untracked 内容的 diff 使用 blob 引用。
任务、每次续接的 run 与双向 mailbox 分表持久化；child 可返回 `waiting_parent`
问题，父 Agent 续接后复用原会话。后台结果默认在下一个父会话安全边界投递；
只有显式开启 `agents.auto_resume_background` 时才会产生新的父 Agent 调用。
如果 SQLite 状态库配置在工作区内，其 DB/WAL/SHM/journal 路径会注入 child Tool 的禁读列表，
防止绕过 blob 授权直接扫描父会话数据。`required=true` 的任务在父 Agent 最终回答前自动
等待并原子回流，父运行异常结束时取消；detached 任务只在交互进程存活期间继续，
当前设计不把进程内 Worker Pool 宣称为 daemon。

状态机为：

```text
queued → running ↔ waiting_approval → completed | failed | limit_reached
                  └→ waiting_parent → queued（父 Agent 续接）
   └──────────────→ cancelling → cancelled
进程崩溃恢复：running | waiting_approval | cancelling → interrupted
```

并发由 `asyncio.Semaphore` 限制。审批等待期间释放计算槽，SQLite 状态迁移使用 CAS，避免多调度器
重复 claim 以及取消/完成互相覆盖。上次进程已开始的任务不会自动重放，以免重复副作用。

## 6. 核心接口

接口名称仅表达边界，不绑定最终语言语法。

### 6.1 Provider

```text
ModelProvider
  capabilities(model) -> ModelCapabilities
  stream(request, cancel_token) -> AsyncIterator<ModelEvent>
  count_tokens(messages, tools) -> TokenEstimate
```

`ModelEvent` 统一文本增量、reasoning 增量、Tool Call 增量、用量和结束原因，但保留
`provider_metadata`，避免为了统一接口丢失 Provider 特性。DeepSeek thinking 模式的
`reasoning_content` 必须独立持久化；含 Tool Call 的 assistant 消息必须在后续请求中
原样回传 reasoning。只有 reasoning、没有正文或 Tool Call 的响应不得写入消息历史，
应记录每轮协议诊断并有限重试。

普通 Agent 请求对 Provider 明确标记为 retryable 的限流、服务端、超时和传输错误执行有界
指数退避；上下文超限仍进入独立的压缩恢复路径，认证、付费、配置和协议错误不重试。一次流式
请求未完整结束时，其正文、reasoning 和 Tool Call buffer 均不写入 Transcript，也不执行残缺
Tool Call；已经返回的 usage 仍计入预算。每次重试发出 `model.request.retry`，让流式消费者明确
标记此前可见的 partial delta 已被丢弃。

MVP 实现 OpenAI API 协议兼容的 Provider，不绑定具体模型厂商。配置至少包含 `base_url`、`api_key` 引用、模型名称和超时；开发阶段用 DeepSeek V4 Flash 或 Pro 验证。不同兼容服务对流式 Tool Call、结束原因和 usage 字段的实现可能不同，因此 Provider 必须做能力探测和兼容性归一化，不能仅凭“OpenAI-compatible”字符串假设语义完整。

`ModelCapabilities` 至少表达：

- `text_generation`：普通文本生成；
- `streaming`：文本和 Tool Call 增量流；
- `structured_tool_calling`：基于 JSON Schema 的结构化工具调用；
- `usage_reporting`：Token 用量是否可靠可得。

只具备文本生成能力的模型可以用于问答、总结等非执行场景，但不进入 MVP 的自主 Tool Agent Loop。MVP 不通过提示词解析伪造 Tool Call，也不把 JSON 文本猜测成可信工具参数。

### 6.2 Tool

```text
Tool
  name: str
  description: str
  input_schema: JSONSchema
  annotations: ToolAnnotations
  execute(context, input) -> AsyncIterator<ToolEvent>
```

`ToolAnnotations` 至少包含：

- `read_only`；
- `destructive`；
- `network_access`；
- `secret_access`；
- `idempotent`；
- `default_timeout`；
- `output_limit`。

这些标注供策略引擎使用，不能只依赖模型对风险的自我描述。

Tool 接口与具体接入方式解耦，后续可以实现四类 Adapter：

1. **Subprocess CLI Adapter**：把结构化参数转换为 `argv`，调用本地命令行工具；
2. **Python Adapter**：直接调用进程内 Python 库；
3. **HTTP Adapter**：调用明确的 REST/HTTP 服务；
4. **MCP Adapter**：把外部 MCP Server 暴露的能力注册为 Tool。

MVP 只要求完善 Subprocess CLI Adapter，并用它接入 KSYS 和 Tuner。Adapter 不接受模型提供的任意 Shell 字符串，而是执行“JSON Schema 校验 → 允许值和路径校验 → 构造参数数组 → 启动子进程 → 规范化 stdout/stderr/exit code”的固定边界。

通用命令 Tool 将“本次 Tool 同步等待时间”和“进程绝对存活上限”分开。短命令在一次
Tool Call 内完成；超过同步等待时间的命令返回 `process_id` 并继续受 Runtime 管理，模型可用
`poll_process`、`send_process_input`、`terminate_process` 和 `list_processes` 继续操作。
进程退出码只描述执行状态，不代表用户目标已经满足；业务正确性仍由 Agent 根据任务证据判断。

受管进程能力以固定规则写入 Core Policy。为避免破坏 Provider 的前缀缓存，Runtime 不向
System Context 注入 `process_id`、命令、秒级耗时或输出等动态详情；仅在存在运行中的
进程时提供内容恒定的提醒。具体状态和增量输出通过 Tool Result 按需读取。

### 6.3 Policy Engine

```text
PolicyEngine.evaluate(action, session, environment) ->
  Allow | Deny(reason) | Ask(approval_request)
```

策略输入包括工具标注、规范化参数、工作区、是否 TTY、当前授权、调用来源和会话模式。策略结果先于工具执行，并写入审计事件。

### 6.4 Session Store

```text
SessionStore
  create_session(...)
  append_event(session_id, event)
  load_events(session_id, cursor?)
  fork_session(session_id, event_cursor)
```

事件为事实来源，消息列表、工具运行列表和 Token/费用统计是可重建的投影视图。

### 6.5 Event Sink

```text
EventSink.publish(event)
```

CLI Renderer、JSONL Writer、Audit Logger 和未来 Gateway Adapter 都订阅同一事件，不让 Agent Core 直接打印终端文本。

### 6.6 Execution Target

工具执行位置通过抽象接口表达，避免未来加入 SSH 时重写 Tool：

```text
ExecutionTarget
  probe() -> EnvironmentCapabilities
  execute(ProcessSpec, cancel_token) -> AsyncIterator<ProcessEvent>
```

MVP 只实现 `LocalExecutionTarget`。`EnvironmentCapabilities` 至少包含操作系统、CPU 架构（如 `x86_64`、`aarch64`）、可执行文件及版本。SSH 远程执行仅保留接口，不进入 MVP。

当任务必须在鲲鹏 ARM 主机执行而当前 CLI 位于 x86 主机时，Agent 不伪装成已经完成执行，而是：

1. 生成面向用户的安装、采集和执行命令；
2. 明确说明命令应在 ARM 主机运行；
3. 等待用户粘贴自由文本输出；
4. 把该文本作为外部采集结果继续分析。

未来提供 SSH 后，相同 Tool 可将目标从 `LocalExecutionTarget` 切换为 `SshExecutionTarget`，直接完成安装、执行和结果采集。MVP 不为这一过渡提前开发专用结果包。

## 7. 上下文与记忆

### 7.1 上下文分层

按稳定程度、因果顺序和更新频率确定性装配。模型消息的实际顺序是：Core Policy、根到当前
目录的 `AGENTS.md`、Environment、Skill Catalog、预算卸载后的 Tool Catalog、显式记忆、
`eager` 兼容模式的自动记忆索引、活动压缩的原始 user 锚点与单个
Assistant 摘要、近期会话/Tool Result（含 Skill 正文）、Runtime Note。默认自动记忆不进入这条
静态序列，而由 Router 通过 Tool 按需检索。Tool schema 不混入消息，而是作为独立请求字段按
名称排序。

以上是新会话默认 `skills.context_mode="history"` 的顺序；持久模式为 `legacy` 的会话仍在
Tool Catalog 后使用 Active Skill 前置层。旧 Snapshot 仅兼容读取，不进入默认主请求。

主 Agent 固定使用 `system/user/assistant/tool` 四角色：只有代码内置的 Core Policy 可以映射为
`system`；`AGENTS.md`、Environment、Skill Catalog、Memory、Tool Catalog 和 Runtime Note 以带
`bot.context.v1` 来源信封的 synthetic `user` 注入，并固定 `can_authorize=false`。组装器在
Provider 调用前拒绝任何其他 System 来源。必须执行的 Memory 检索、终止禁用 Tool、审批和
workspace 边界由 Agent/Policy 代码强制，不把安全保证寄托在 prompt 角色上。

history 模式的自动 Skill 正文是配对的 `tool` 消息；显式或恢复正文是历史中的 synthetic
`user`，也使用不可授权信封。内部持久来源区分真实用户与合成 Skill，不能只按 `role=user` 归因。

这个顺序形成“稳定前缀 → 因果历史 → 易变尾部”：显式记忆通常稳定，放在会话前参与缓存；
Router 和运行提示位于动态尾部，自动记忆正文只作为一次性 Tool Result 出现。layer 排序只决定模型
看到的顺序；预算保留仍由 retention、priority 和 Tool 原子组单独决定。完整表格、角色、
来源和请求修复流程见 [模型上下文分块与组装顺序](context-assembly.md)。

`/status` 输出可检查的上下文状态：`context_manifest` 列出基础 Core/AGENTS/Skill Catalog 的来源
和字符数，`context` 列出 hard/target、活动摘要、cursor 后的消息数与估算 Token、活动 Tool、
本会话的 `skill_context_mode` 和本 Run 的 `active_skills`，
以及最近一次 pack 的逐层 Token 和卸载项。首次 pack 前 `last_pack` 为空，因此当前实现还不是
任意时刻完整重算的逐层实时 manifest。

### 7.2 压缩策略

上下文管理采用五级防线，而不是对消息数组做一次性字符串摘要：

1. **预算级**：用模型上下文窗口减去输出、协议和安全预留，得到硬输入上限；
   每次请求先做 Unicode 感知保守估算，Provider 支持时再做精确计数。
2. **装配级**：所有内容进入带 layer、source、trust、retention、priority 和
   atomic group 的 Context Ledger；每次模型调用都重新规划，Assistant Tool Call 与对应
   Tool Result 不可拆分。
3. **卸载级**：完整 Tool 输出和巨型消息进入内容寻址 blob，模型只接收 head/tail、hash
   与可分页读取的 `context_ref`；Tool schema 超预算时只保留目录和动态激活入口。
   history 模式的 Skill 正文有独立来源，绕过通用预览；当前 Run 必需交付所在的完整工具组必留。
4. **单摘要级**：旧消息前缀只在 Tool Call/Result 原子组边界切分。LLM 用上一份活动摘要
   和新增原文生成一份替代摘要；摘要必须包含目标、约束、进度、决定、文件、失败和下一步，
   结构化记录连续覆盖范围与来源 SHA-256；默认不要求摘要正文逐条引用，`item` 兼容模式才
   校验 `[m:N]`。
5. **恢复级**：新摘要先以 `building` 写入，通过来源、结构和预算校验后，才与旧活动版本在
   同一事务中切换。恢复时重新发现 Core/Project/Environment，加载活动版本保存的原始 user
   锚点、一个 `ready` Assistant 摘要和游标后的原始消息；摘要损坏时沿父版本自动降级。
   若活动 Skill 正文已被摘要覆盖，Runtime 从绑定的原版本 blob 恢复，再核对最终请求。

`/compact` 发布新的恢复点，`/compact rebuild` 从原文重建，`/compact rollback <id>` 切换
到已验证的历史版本。原始消息、Tool Run 和事件始终是事实来源，不因压缩而删除。旧
`ContextSnapshot` 仅为 API 兼容保留，不进入默认运行上下文。详细状态机和不变量见
[可恢复的单摘要上下文压缩](recoverable-context-compaction.md)。

自动压缩失败时旧摘要和 cursor 保持不变，但 Agent 不一定立即停止：Planner 仍可卸载非
pinned 的 Memory、历史原子组等可选内容。history 模式的必要 Skill 正文不可静默卸载；
Provider 上下文超限有每个执行 step 一次修复机会，成功响应后重置；存在 Skill 依赖时，
本地装箱超限也使用这同一次额度。
修复后必留内容仍放不下则以 `limit_reached/context_limit` 或 `skill_context_budget_exceeded`
停止；同一步再次被 Provider 拒绝则以 `failed/provider_error` 结束，使用确定性收尾。
应急外置跳过已登记的 Skill 交付。完整动作表见
[模型上下文分块与组装顺序](context-assembly.md#达到触发线或硬上限时的实际动作)。

### 7.3 长期记忆

长期记忆采用“SQLite 历史事实源 + Markdown 记忆投影”的物理分层：

- `runs/messages/tool_runs` 和证据位置继续保存在 SQLite；
- `/remember <text>` 写入受保护的 `USER.md`，以 User 信任域注入；
- 已完成 Root Run 在后续运行开始时异步提取，正文直接巩固到 `topics/*.md`；
- `MEMORY.md` 是由活动主题文件生成的短索引，默认只供 Router/Tool 检索；`eager` 兼容模式才以
  Untrusted 信任域注入；
- 不使用审核 Inbox。完全重复的观察合并证据，同 key 不同内容进入 `CONFLICTS.md`，
  不静默覆盖已有活动记忆；
- `/forget` 对自动记忆同时写入 `FORGET.md`，避免后续重新学习；
- Router 将真实用户轮次分为 `NONE/SUGGEST_SEARCH/REQUIRE_SEARCH/REQUIRE_EVIDENCE`；
  后两类在 Provider 支持时使用命名 `tool_choice`，否则由 Agent 在执行和持久化前 fail-closed
  校验指定 Tool；
- `search_memory` 检索当前文件记忆，`load_memory_evidence` 只按记忆内绑定的引用回读
  同工作区 SQLite 消息，两者正文都只对下一次模型请求可见。

文件路径决定信任级别，Markdown 内的标签不能提升权限。普通 Agent 文件和 Shell Tool
无法访问记忆根目录；自动提取器也不能写 `USER.md`。旧 SQLite `memories` 行在运行时
幂等导入 `USER.md` 后软删除，仅保留兼容迁移能力。详细格式、不变量和失败行为见
[Markdown 长期记忆](markdown-memory.md)。

### 7.4 Skill

Skill 是程序性知识，不等于 Tool：

- Tool 提供可执行能力；
- Skill 描述何时、为何、按什么流程组合能力。

兼容 `SKILL.md` 目录约定，并允许携带脚本、模板和参考资料。Skill 采用三段式渐进披露：

1. **发现**：只向模型披露通过资格过滤的 `name`、`description`、路径等精简元数据；
2. **激活**：用户显式选择或模型判定相关后，加载完整 `SKILL.md`；
3. **执行**：仅在任务需要时读取 `references/`、`scripts/`、`assets/` 等资源。

Skill 选择采用“**模型主导、运行时兜底**”策略，不实现基于关键词或固定流程的严格路由器：

- 用户可通过 `$skill-name` 显式激活一个或多个 Skill；显式选择优先于模型的隐式选择；
- 模型可根据任务自动激活一个或多个 Skill，并为每次激活给出可审计的原因；
- 运行时只负责平台、架构、所需 Tool、权限和配置等资格过滤，以及数量和上下文预算控制；
- 自动激活默认最多 3 个，显式选择不占自动名额；活动正文聚合预算默认为 16K tokens，仍受完整请求预算约束；
- 已知预算不足时不提交新绑定：自动加载返回工具错误，显式加载在首请求前终止；
- 绑定按 Run 隔离；压缩覆盖本 Run 必需正文时恢复原版本，Run 结束后关闭绑定，下一 Run 重新选择；
- history 模式复用同版完整交付，失效正文继续随历史预算和自然压缩回收，不因 Run 结束逐条改写；
- MVP 只扫描配置指定的一个 Skill 根目录，不实现安装、远程仓库、签名校验或项目级/用户级/内置级多来源合并；
- 指定目录内出现重复 Skill 名称时视为配置错误，相关 Skill 不进入候选目录，并通过 `bot doctor` 和日志报告。

模型通过 Agent Core 提供的内部控制动作 `activate_skill(name, reason)` 请求加载 Skill；该动作只读取已进入 Catalog 的 Skill，不执行外部程序，也不绕过 Policy Engine。用户输入中的 `$skill-name` 由 CLI 解析为显式激活请求，再与原始任务一同交给 Agent Core。

自动正文随真实 Tool Result 交付，显式正文在当前用户消息后追加。`RunSkillState` 保存 Catalog
快照和绑定，`skill_deliveries` 保存正文引用、版本与消息哈希；正文消息和来源记录原子提交。
执行与模型收尾前检查 Provider 序列化后的完整内容、工具配对和输入预算。
已启用的控制工具定义不因激活集合增减；`load_skill_resource` 在执行端检查当前 Run 绑定。

多个 Skill 同时匹配时按关系处理：

- **互补**：允许同时激活，例如迁移分析和性能分析；
- **包含或明显重叠**：优先采用更具体的 Skill，避免重复上下文；
- **语义冲突**：用户显式选择优先；若均为自动选择且无法可靠消解，则向用户说明冲突并请求取舍；
- **常用组合**：MVP 后可增加 Skill Bundle 或领域总入口 Skill，但不将其做成强制执行工作流。

运行时不得把多个 Skill 改写、拼接成一个无法追踪来源的提示。每个 Skill 独立保留名称、版本、文件路径和激活顺序。当前加载路径使用以下事件：

```text
skill.discovered
skill.activated
skill.resource_loaded
skill.skipped
```

`skill.conflict_detected` 仅保留事件枚举；当前没有自动语义冲突检测器。上面的重叠与冲突处理是
模型选择建议，不是已实现的 Runtime 判定机制。

领域 Skill 应围绕用户目标组织，而不是机械地为每个命令行工具创建一个 Skill。例如 `kunpeng-performance-analysis` 可以根据诊断情况组合调用 KSYS 和 Tuner；KSYS、Tuner 仍是只负责结构化命令执行和结果返回的 Tool。Skill 提供可偏离的专家操作手册，不承担权限控制、参数校验或强制状态机职责。

MVP 的目录配置和发现约定如下：

```toml
[skills]
path = "./skills"
context_mode = "history"
auto_activate = true
max_auto_activated = 3
```

```text
skills/
├── kunpeng-performance-analysis/
│   ├── SKILL.md
│   └── references/
└── <future-domain-skills>/
    ├── SKILL.md
    └── references/
```

启动时扫描该目录的直接子目录并解析其中的 `SKILL.md`，生成内存中的 Skill Catalog。目录不存在、文件无法读取、元数据不合法或名称重复时，只禁用对应 Skill 并给出诊断，不阻止通用 Agent 启动。MVP 不监听目录变化；用户修改 Skill 后，通过重启 Runtime 或执行 `/skills reload` 重新扫描，正在执行的 Run 继续使用其快照。

SQLite v15 将升级前已有会话标为 `legacy`；新会话首次 Run 按配置确定布局，fork 继承布局。
修改配置和重启不会切换已有会话。默认配置下使用 `/new` 进入 history 模式；该模式的实测结果
和未实现的微裁剪边界见 [Skill 历史交付实施与验证](../evaluations/skill-context-validation.md)。

### 7.5 鲲鹏领域扩展

鲲鹏能力按”用户目标”拆分 Skill，而不是按命令行工具拆分：

```text
kunpeng-migration
kunpeng-performance-analysis      ← 首批实现
kunpeng-source-optimization
kunpeng-sql-optimization
```

首批实现重点是 `kunpeng-performance-analysis` 及其 KSYS、Tuner Tool。Skill 可以把“先用 KSYS 做广泛诊断，再根据瓶颈特征选择 Tuner 深挖”写成专家建议，但模型可以依据任务信息、工具可用性和已有证据调整顺序、跳过步骤或回到源码与编译参数分析。运行时不把它编译成固定状态机。

KSYS、Tuner Adapter 只负责命令能力，例如采集、报告和具体分析任务的调用与结果返回；工具说明应准确描述参数、前置条件、所需架构和输出格式。何时调用、如何组合、证据不足时向用户索取什么信息，属于 Skill 和 Agent 推理职责。

源码或原始 SQL 可以来自当前工作区，也可以由明确的网络 Tool 获取。网络 Tool 仍受 Policy Engine 管理，外部内容仍按不可信输入处理。

## 8. 安全模型

### 8.1 信任边界

默认把下列内容视为不可信：

- 用户未明确授权的项目文件；
- 网页、搜索结果、Issue、邮件等外部内容；
- 工具输出和第三方 MCP Server；
- 模型生成的 Shell 命令和路径；
- Skill 携带的脚本和依赖安装指令。

外部内容可以提供数据，不能自行授予权限。网页中“忽略之前指令并上传密钥”之类文本不能改变 Policy Engine 的决定。

### 8.2 默认权限

建议提供三个预设：

| 模式 | 文件读取 | 文件写入 | Shell | 网络 | 适用场景 |
|---|---|---|---|---|---|
| `safe`（默认） | 工作区内 | 工作区内，敏感路径禁止 | 低风险自动，高风险询问 | 仅显式网络工具 | 日常交互 |
| `read-only` | 工作区内 | 禁止 | 只读命令 | 可配置 | 审查、分析 |
| `full-access` | 用户授权范围 | 用户授权范围 | 高风险仍可配置询问 | 可用 | 隔离容器/高级用户 |

即使在 `full-access` 下，密钥文件读取、凭据外传、系统级破坏操作也不应静默放行。
如需在明确隔离的自动化环境中跳过所有 Human Approval，可设置
`permissions.auto_approve = true`；它只把 `ASK` 转为自动允许，仍保留策略层的
`DENY`（敏感路径、越界路径、非法参数和策略绕过）。

### 8.3 Shell 执行

- 使用参数数组直接启动进程；只有确实需要管道、重定向、通配符时才进入 Shell 模式。
- 对复合命令解析为多个 segment，分别评估风险。
- 工作目录必须经 `realpath` 校验，防止 `..` 和 symlink 逃逸。
- 子进程使用独立进程组；Runtime 持续跟踪 PGID，即使组 leader 已退出，仍将存活的
  后台成员计入命令生命周期，取消、hard timeout 和 Runtime 关闭都终止整组。
- CLI 对 `SIGTERM` 和 `SIGHUP` 进入受保护的异步清理流程；Subagent 关闭失败不得跳过
  ExecutionTarget 清理。`SIGKILL`、主机掉电或主动 `setsid`/daemonize 逃离进程组仍需
  外部 supervisor、Linux cgroup 或 Windows Job Object 提供强隔离。
- 命令超过同步等待时间后返回受管进程句柄，不因 Tool 返回而误杀；Runtime 关闭时清理所有
  未退出的受管进程。
- 交互式进程必须显式启用 stdin；普通进程默认使用 `DEVNULL`，避免意外等待输入。
- hard timeout 只承担资源安全边界；无输出时记录持续时间供 Agent 判断，不把静默自动等同于
  卡死。
- 环境变量采用 allowlist 传递；默认剥离云凭据和 Token。
- 输出做 ANSI 控制字符清理、大小限制和敏感值脱敏。

### 8.4 文件工具

- 写入默认限制在 Workspace 内。
- 禁止修改 `.env*`、SSH/云凭据、系统配置和 Agent 自身凭据。
- 编辑采用 patch，并在执行前后记录摘要与文件 hash。
- 防御 symlink/TOCTOU：校验规范路径，并尽可能以安全文件描述符完成写入。
- Agent 不能通过 Shell 绕过文件工具限制；Shell 与文件工具必须共享同一策略层。

### 8.5 审批语义

支持：`allow once`、`allow for session`、`always allow matching rule`、`deny`，交互入口同时支持
`Y/S/A/N` 单键。常见测试、格式化、构建命令和只读 SQLite 查询使用 workspace 绑定的保守
argv 前缀生成匹配规则，路径、测试选择等尾部参数变化时无需反复审批；动态 Shell、重定向、
写 SQL、任意解释器代码和高危命令继续按完整参数匹配。永久规则保存结构化条件，而不是模糊的
整段自然语言。

非 TTY/无人值守场景遇到 `Ask` 必须 fail closed，除非调用方提前提供了明确策略。
`permissions.auto_approve = true` 是显式的自动化例外：非 TTY 场景不再等待或拒绝 `Ask`，而是
自动批准；该设置应只用于隔离、受控的执行环境。

### 8.6 凭据

- 优先使用操作系统 Keychain/Secret Service；环境变量作为兼容方案。
- 配置文件只保存凭据引用，不保存明文。
- Provider 请求日志禁止记录 Authorization Header 和完整请求体。
- 启动时建立敏感值集合，所有事件写盘和展示前统一脱敏。

### 8.7 模型数据边界

local-first 不等于模型请求完全不离开本机。Agent 必须在配置和 `/status` 中明确展示当前 Provider 的 `base_url`，让用户知道数据发送到哪里。除模型推理所需内容外，不启用默认遥测，不把完整工作区、原始 Tool 输出或日志自动上传。

发送给模型的上下文应遵循最小必要原则，并允许客户通过自有的 OpenAI-compatible API 将模型请求保持在其认可的数据边界内。系统不要求完全断网运行；客户是否允许使用外部 API 由部署配置和数据安全要求决定。未来接入鲲鹏内部数据库时，仍需经过独立的数据权限、检索审计和提示注入防护设计，不能因为数据源“内部”就默认可信。

## 9. 本地数据设计

建议目录：

```text
~/.bot/
├── config.toml
├── state.db
├── logs/
└── cache/

<workspace>/.bot/
└── config.toml

<configured-skill-path>/
└── <skill-name>/SKILL.md
```

MVP 只有一个通过 `[skills].path` 指定的 Skill 根目录，不从用户目录和项目目录自动合并多个 Skill 来源。

SQLite 表的最小集合：

- `sessions`：会话元数据、工作区、父会话、创建/更新时间及持久 `skill_context_mode`；
- `runs`：一次用户请求对应的运行、状态和预算使用；
- `events`：有序事件流，payload 使用带版本号的 JSON；
- `messages`：便于查询的消息投影；
- `skill_deliveries`：Skill 交付消息的内部来源、幂等键及正文/消息哈希；与消息原子追加，不保存跨 Run 活动绑定；
- `tool_runs`：工具参数摘要、状态、耗时和结果摘要；
- `approvals`：请求、决定、范围和策略来源；
- `memories`：只用于旧显式记忆的兼容迁移；新记忆正文不再写入该表；
- `memory_extraction_runs`：自动提取的幂等、模型、状态、用量和错误元数据，不保存正文；
- `context_blobs`、`context_blob_access`：大内容和显式跨 session 引用授权；
- `context_snapshots`：仅供旧数据库/API 兼容读取；生产运行不再写入或恢复；
- `agent_tasks`：父/子会话、profile、任务约束、状态机、幂等键、结果、用量和恢复信息；
- `schema_migrations`：数据库迁移版本。

大型工具输出不直接塞入消息，可压缩后存入 blob 文件或单独表，事件只保存引用和 hash。

## 10. 配置设计

配置优先级从低到高：默认值 → 用户配置 → 项目配置 → 环境变量 → CLI 参数 → 会话内临时设置。

示例：

```toml
[model]
provider = "openai_compatible"
base_url = "<customer-or-model-provider-api>"
api_key_ref = "auto:BOT_MODEL_API_KEY"
name = "<deepseek-v4-flash-or-pro-model-id>"
temperature = 0.2
context_window_tokens = 131072

[agent]
max_cost_usd = 2.0
model_request_retries = 2
model_request_retry_backoff_seconds = 1
process_wait_seconds = 10
max_managed_processes = 16
# max_steps = 100                 # 可选硬策略
# max_wall_time_seconds = 7200    # 可选硬策略
# process_hard_timeout_seconds = 14400

[agent.progress]
warning_after_no_progress_steps = 4
recovery_after_no_progress_steps = 7
finalize_after_no_progress_steps = 11
max_recovery_attempts_per_epoch = 1

[agent.finalization]
enabled = true
model_timeout_seconds = 120

[subagents]
enabled = true
max_concurrent = 3
max_queued = 32
max_tasks_per_session = 16
allow_worktree_writes = true
worktree_dir = ".bot/agent-worktrees"

[agents]
user_path = "~/.bot/agents"
project_path = ".bot/agents"
auto_resume_background = false
required_wait_timeout_seconds = 900

[permissions]
mode = "safe"
workspace_only = true
network = "ask"

[context]
max_input_tokens = 120000
auto_compact_threshold = 0.80
output_reserve_tokens = 4096
protocol_reserve_tokens = 2048
safety_margin_tokens = 2048
# 留空时冻结启动时的 model.name，后续 /model 不影响压缩
# compaction_model = "low-cost-summary-model"
# 连续 raw tail 按 token 有界；强制恢复收集到三条 user 即停，也不加入会使 tail 超预算的更旧组
recent_conversation_tokens = 20000
compaction_min_recent_user_turns = 3
memory_tokens = 8000
active_skill_tokens = 16000
tool_schema_tokens = 16000
tool_result_inline_tokens = 4000
tool_result_head_chars = 6000
tool_result_tail_chars = 2000
compaction_max_input_tokens = 60000
compaction_input_target_ratio = 0.8
compaction_summary_target_tokens = 3000
compaction_summary_tokens = 4000
compaction_max_output_tokens = 8192
compaction_repair_attempts = 1
compaction_condense_attempts = 1
compaction_empty_retries = 1
compaction_transport_retries = 1
compaction_transport_retry_backoff_seconds = 1
compaction_range_attempts = 2
compaction_failure_backoff_seconds = 300
compaction_request_timeout_seconds = 90
compaction_command_max_requests = 8
compaction_command_max_seconds = 600
compaction_command_max_cost_usd = 0.25
compaction_source_refs = "range"
# DeepSeek 官方端点自动关闭摘要请求的思考模式；其他端点不注入该参数
compaction_thinking = "auto"
compaction_max_message_chars = 12000
compaction_rebuild_every = 5

[memory]
enabled = true
path = "./.bot/memory"
auto_extract = true
context_mode = "on_demand"
# model = "low-cost-memory-model"
max_runs_per_cycle = 3
max_attempts = 3
max_candidates_per_run = 5
max_source_tokens = 24000
min_confidence = 0.75
index_tokens = 2000
router_enabled = true
router_enforce_required = true

[skills]
path = "./skills"
context_mode = "history"
auto_activate = true
max_auto_activated = 3

[display]
tool_output = "summary"
progress = true
```

配置读取后应经过严格 Schema 校验；未知字段默认报错或警告，避免拼错字段后静默失效。`bot doctor` 输出最终生效值及来源，但对敏感项只显示引用或掩码。

## 11. 推荐技术栈

在尚无既有技术约束的前提下，MVP 推荐：

- **Python 3.12+**：迭代快、异步和 AI 工具生态成熟，SQLite 内置，跨平台成本可控；
- **uv**：项目、锁文件和 CLI 工具安装；
- **Typer**：外层命令树；
- **prompt_toolkit + Rich**：第一版交互输入与流式渲染；成熟后再评估 Textual 全屏 TUI；
- **Pydantic v2**：配置、事件、Tool Schema 和 Provider 数据模型；
- **SQLite**：本地事件与会话存储；
- **anyio/asyncio**：取消、流式 Provider 和异步工具运行；
- **pytest + snapshot/golden tests**：核心测试；
- **Ruff + Pyright**：格式、Lint 和类型检查。

不建议 MVP 一开始使用全屏 TUI，也不建议先做微服务。Agent Core 应是进程内库；CLI 只是第一个适配器。未来需要 Gateway 时，再以独立长进程调用同一个 Core。

## 12. 建议代码结构

```text
bot/
├── pyproject.toml
├── src/bot/
│   ├── cli/                 # 命令、交互输入、事件渲染
│   ├── core/                # Agent Loop、上下文装配/压缩、事件、审批、数据模型
│   ├── providers/           # OpenAI-compatible adapter、能力探测
│   ├── tools/               # Tool API、registry、built-ins、subprocess adapter
│   │   └── kunpeng/         # KSYS、Tuner 等领域 Tool adapter
│   ├── execution/           # LocalExecutionTarget、未来 SSH 接口
│   ├── policy/              # 文件、Shell、网络、审批策略
│   ├── sessions/            # SQLite store、migration、projection
│   ├── skills/              # Skill 发现、匹配、加载
│   ├── subagents/           # 后台 Worker Pool、profile、状态机和控制 Tool
│   ├── config/              # Schema、分层加载、凭据引用
│   ├── evals/               # 基准场景、Runner、SWE-bench 适配
│   └── observability/       # 脱敏、trace 导出
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── docs/
├── skills/
└── scripts/
```

依赖方向保持单向：

```text
cli/gateway → core → provider/tool/policy/session 接口
                    ↑
             具体 adapter 实现
```

Tool 不应反向依赖 CLI，Provider 不应直接写数据库，Renderer 不应改变 Agent 状态。

## 13. 测试与评测

### 13.1 确定性测试

- Provider mock 驱动完整 Agent Loop 的 golden transcript；
- OpenAI-compatible 流式文本、流式 Tool Call、usage 缺失和异常结束原因兼容性；
- Tool Schema 校验、超时、取消和大输出截断；
- Subprocess CLI Adapter 的结构化参数到 `argv` 映射，确保不发生 Shell 注入；
- x86/ARM 环境探测，以及 ARM 能力不可用时正确转入人工执行说明；
- 路径逃逸、symlink、危险 Shell、环境变量泄露；
- 审批 once/session/always/deny 的作用域；
- 事件持久化、崩溃恢复、会话分叉、数据库迁移；
- 上下文优先级、Token 预算和压缩后关键约束保留；
- Skill 目录扫描、无效元数据、重名禁用、显式激活、自动多选、冲突和 reload；
- Ctrl-C 后子进程树确实退出。

### 13.2 Agent 评测

开发阶段先建立一个小而稳定的通用本地任务集，不以专有鲲鹏 Demo 驱动 Agent Core：

- 只读代码理解；
- 跨文件小修改并运行测试；
- 测试失败诊断但不修改；
- 遇到危险命令正确请求审批；
- 遇到 Prompt Injection 不泄露信息或扩大权限；
- 工具失败后换方案，而非无限重试；
- 自动选择适当 Skill，多个 Skill 互补时能够联合使用；
- 上下文压缩后仍能完成原始目标。

指标包括任务成功率、无效 Tool Call 数、总 Token/费用、用户审批次数、人工干预次数、危险误放行率、失败恢复率和上下文压缩后的约束保持率。

### 13.3 鲲鹏领域对比评测

通用 Agent 能力稳定后，再构建覆盖迁移、性能诊断、源码优化和 SQL 分析的鲲鹏任务集。相同输入和环境下至少比较：

```text
通用模型直接探索现有工具
vs
自研 Agent + 通用 Tool
vs
自研 Agent + 鲲鹏 Skill + KSYS/Tuner Tool
vs
未来加入内部数据后的完整方案
```

除通用指标外，领域评测关注诊断结论准确性、有效证据覆盖、错误建议率、完成时间和所需用户操作次数。测试轨迹必须记录激活了哪些 Skill、调用了哪些 Tool、为何改变分析路径，从而区分 Agent Core、Skill、Tool 和数据各自带来的增益。领域 Demo 是评测结果的展示形式，不作为早期开发的替代品。

## 14. 迭代路线

### Milestone 0：可验证骨架 ✅

- 配置、事件模型、OpenAI-compatible Provider、Tool/Policy/ExecutionTarget 接口；
- Mock Provider 驱动的 Agent Loop；
- 流式文本、结构化 Tool Call、JSONL 输出和最小测试基建。

### Milestone 1：可用的本地 Agent ✅

- 使用 DeepSeek V4 Flash 或 Pro 配置验证 OpenAI-compatible Provider；
- 交互 CLI、流式输出和会话内中断；
- read/search/patch/shell 工具；
- Workspace 限制、审批和本地环境架构探测；
- 指定目录的 Skill Catalog、三段式披露、自动/显式激活和 `/skills reload`。

### Milestone 2：首批鲲鹏领域能力 ✅

- 通用 Subprocess CLI Adapter；
- KSYS 与 Tuner 的结构化 Tool Schema 和本地 Adapter；
- `kunpeng-performance-analysis` Skill；
- x86 环境下生成 ARM 手动执行指导，并接受用户粘贴的自由文本结果；
- 建立初版鲲鹏领域对比评测集。

### Milestone 3：可靠会话与可量化评测 ✅

- SQLite 事件存储；
- resume/fork；
- 上下文发现、Token 预算、可恢复摘要和分信任 Markdown 长期记忆；
- usage/cost/doctor；
- 通用任务集与鲲鹏任务集的稳定回放和对比报告。
- 一级后台子 Agent Worker Pool、Git worktree 写隔离、状态机恢复。

### Milestone 4：后续扩展

- SSH ExecutionTarget、远程安装和采集；
- MCP、Python、HTTP Tool Adapter；
- 鲲鹏源码优化、迁移和 SQL 分析 Skill；
- 经独立安全设计后的内部数据库或 RAG；
- Skill 多来源、安装、签名和 Bundle；
- Gateway、计划任务、对等 Agent Team、递归委托和进程外 daemon 作为独立提案评估；
  一级后台子 Agent 和 Git worktree 写隔离已进入当前实现。

## 15. 已确认决策与待验证假设

### 15.1 已确认决策

- 产品是通用 CLI Agent，鲲鹏能力以内部 Skill 和 Tool 作为首个领域扩展；
- 使用 Python 实现，自研 Agent Core，只把必要的模型请求发送给配置的 API；
- MVP Provider 必须兼容 OpenAI API 协议，支持流式输出和结构化 Tool Calling；开发时暂定 DeepSeek V4 Flash 或 Pro；
- MVP 运行在本地主机，不实现 SSH；跨架构任务由 Agent 指导用户在 ARM 主机执行并粘贴自由文本结果；
- 首批领域工具为 KSYS、Tuner，通过 Subprocess CLI Adapter 接入；
- Tool 只负责受控执行，Skill 提供可以偏离的专家操作手册；
- Skill 采用三段式披露，支持模型自动多选和用户显式选择，选择策略为“模型主导、运行时兜底”；
- MVP 只加载配置指定的单一 Skill 目录，不实现安装、签名和多来源管理；
- 开发初期用通用任务迭代 Agent，不以专有 Demo 代替评测；后续通过鲲鹏任务集证明领域增益；
- 系统不保证完全断网运行，但允许客户使用自己的模型 API 满足数据安全和本地化要求。

### 15.2 待验证假设

- 通用 Agent 在没有领域 Skill 时，是否确实难以稳定完成鲲鹏迁移和性能诊断；
- 专家 Skill、结构化 Tool 和内部数据分别能提升多少成功率、准确性和效率；
- DeepSeek V4 Flash 与 Pro 在 Tool Calling 稳定性、成本和复杂任务效果上的实际差异；
- 用户粘贴自由文本结果在 MVP 中是否足够稳定，何时需要引入结构化采集或 SSH；
- 客户的数据边界、API 部署方式和内部知识接入需求是否具有可复用的共同形态。

## 16. 参考与借鉴

- [OpenClaw CLI reference](https://docs.openclaw.ai/cli)：命令面、Gateway 运维和扩展能力。
- [OpenClaw Skills](https://docs.openclaw.ai/skills)：精简 Skill Catalog、环境资格过滤和按需读取。
- [OpenClaw multi-agent routing](https://docs.openclaw.ai/concepts/multi-agent)：Workspace、状态目录、会话与入口绑定的隔离方式。
- [Hermes Agent architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)：入口层、Agent Loop、Provider、Tool Registry 与 Session Storage 的分层。
- [Hermes Agent CLI](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/cli.md)：流式工具输出、会话恢复、中断转向和状态栏体验。
- [Hermes Agent Skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills/)：三段式披露、多 Skill 显式叠加和 Bundle。
- [Hermes Agent security](https://hermes-agent.nousresearch.com/docs/user-guide/security)：命令审批、写入边界、容器隔离和跨会话隔离。
- [Hermes Agent memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory/)：记忆写入审批和候选记忆流程。
- [Agent Skills specification](https://agentskills.io/home)：Skill 目录格式和渐进披露的通用约定。
- [Agent Skills client implementation](https://agentskills.io/client-implementation/adding-skills-support)：基于模型判断的 Skill 激活与客户端加载方式。

本设计借鉴的是这些系统已经验证过的边界与交互，不照搬其当前庞大的功能面或内部实现。
