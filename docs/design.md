# 通用 CLI Agent 助手设计

> 状态：设计基线 v0.2；MVP 代码已实现，环境验收项见 [implementation-status.md](implementation-status.md)
> 工作名：`bot`（后续可替换）  
> 设计基线：通用 Agent Core、CLI-first、local-first、OpenAI-compatible、Skill/Tool 可扩展、安全默认开启

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
- 自动创建或自动修改 Skill。
- 自动写入长期记忆。
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
/compact       用 LLM Episode 摘要手动压缩上下文
/consolidate   分批整合所有待处理 Episode
/memory-cards  查看当前有效 Memory Card
/memory-history <id> 查看 Card 版本链
/memory-forget <id>  可审计地撤销 Card
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
model.empty_response
tool.requested
approval.requested
approval.resolved
tool.started
tool.output
tool.completed
context.consolidated
memory.consolidation.started|completed|failed
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
  → 达到限制、取消或不可恢复错误时安全结束
```

### 5.1 运行限制

每次 Run 都应有明确预算，而不是让模型决定何时停止：

- `max_steps`：最大模型轮次/工具轮次；
- `max_wall_time`：最大运行时间；
- `max_input_tokens`、`max_output_tokens`；
- `max_cost`：Provider 能提供价格信息时生效；
- `max_tool_output_bytes`：单次和累计工具输出限制；
- `max_consecutive_failures`：连续工具失败熔断；
- `cancel_token`：CLI、信号和未来 Gateway 共用的取消机制。

### 5.2 防循环

至少检测三类无进展行为：

1. 完全相同且失败的 Tool Call 重复出现；
2. 同一工具用不同参数持续失败；
3. 幂等工具反复返回相同结果。

首次达到阈值时给模型反馈；再次达到阈值则阻断执行并要求模型换方案或向用户求助。非交互模式默认硬停止。

### 5.3 后台子 Agent Worker Pool

父 Agent 通过四个内部控制 Tool 使用一级后台 Worker Pool：

- `spawn_agent`：先持久化任务和独立 child session，再立即返回 `task_id`；
- `get_agent_status`：按父会话边界查询状态、结果和独立用量；
- `await_agents`：事件驱动等待多个任务，超时只返回状态，不取消任务；
- `cancel_agent`：幂等取消 queued/running/waiting-approval 任务。

内置 profile 为 `explorer`、`reviewer` 和 `coder`。前两者只获得本地只读 Tool allowlist；
`coder` 必须在从当前 `HEAD` 创建的 detached Git worktree 中工作，不直接写主工作区。每个任务
创建独立 `AgentRunner`、`SkillManager`、`DefaultPolicyEngine` 和 child session；Provider、
SQLite Store、ExecutionTarget 与无状态 Tool 实例可以共享。child runner 不注入 Worker Pool，
因此委托深度由结构保证为 1。

上下文采用显式委托：child session 只得到 objective、constraints、acceptance criteria 和经父
session 授权的 `context_ref`，不使用 `fork_session`，也不复制父历史。child 结果以不可信
Tool 数据返回；完整结果和包含 staged、unstaged、untracked 内容的 diff 使用 blob 引用。
如果 SQLite 状态库配置在工作区内，其 DB/WAL/SHM/journal 路径会注入 child Tool 的禁读列表，
防止绕过 blob 授权直接扫描父会话数据。`required=true` 的任务在父 Agent 最终回答前自动
等待并原子回流，父运行异常结束时取消；detached 任务只在交互进程存活期间继续，
当前设计不把进程内 Worker Pool 宣称为 daemon。

状态机为：

```text
queued → running ↔ waiting_approval → completed | failed | limit_reached
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
  seal_memory_episode_range(session_id, run_id, end_position)
  consolidated_memory_cursor(session_id)
  search_active_memory_cards(workspace, session_id, query)
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

按稳定程度和优先级确定性装配：

1. **Core Policy**：安全规则、工具协议、不可被项目内容覆盖的系统约束；
2. **User Identity**：用户级偏好和全局指令；
3. **Project Context**：从工作区根到当前目录发现的 `AGENTS.md` 与项目配置；
4. **Active Skills**：本次由用户显式启用或模型可靠匹配的 Skill；
5. **Memory**：经过用户确认的长期事实和偏好；
6. **Session History**：当前会话消息和工具结果；
7. **Volatile State**：时间、工作目录、Git 状态和当前运行限制。

上下文装配必须输出可检查的 manifest，用户可通过 `/status` 或调试命令看到“加载了什么、来自哪里、占用多少 Token”。

### 7.2 压缩策略

上下文管理采用五级防线，而不是对消息数组做一次性字符串摘要：

1. **预算级**：用模型上下文窗口减去输出、协议和安全预留，得到硬输入上限；
   每次请求先做 Unicode 感知保守估算，Provider 支持时再做精确计数。
2. **装配级**：所有内容进入带 layer、source、trust、retention、priority 和
   atomic group 的 Context Ledger；每次模型调用都重新规划，Assistant Tool Call 与对应
   Tool Result 不可拆分。
3. **卸载级**：完整 Tool 输出和巨型消息进入内容寻址 blob，模型只接收 head/tail、hash
   与可分页读取的 `context_ref`；Tool schema 超预算时只保留目录和动态激活入口；Skill
   Catalog、Skill 正文和资源分别管理。
4. **Episode 级**：旧消息前缀只在 Tool Call/Result 原子组边界切分为不可变 Episode。
   LLM 为 Episode 生成带目标、主题、深度和来源的结构化摘要；Harness 校验完整性后原子
   发布。只有从位置 1 开始连续 ready 的 Episode 才能推进恢复游标。
5. **恢复级**：恢复时重新发现 Core/Project/Environment；游标前加载相关 Episode 摘要，
   游标后加载原始消息，再召回 workspace/session Memory Card。若 Provider 仍报告
   context-length，只允许一次强制 Episode 整合/外置重试；仍失败则按层输出不可压缩项报告。

`/compact` 会立即建立 Episode 恢复点，`/status` 显示硬/目标预算、连续摘要游标、待处理
和已整合 Episode、压缩率与检索指标。原始消息、Tool Run 和事件始终是事实来源，不因压缩
而删除。旧 `ContextSnapshot` 结构和表仅为数据库/API 兼容保留，新运行不创建、复制或消费
snapshot。详细状态机和验收不变量见 [LLM Episode 记忆整合](memory-consolidation.md)。

### 7.3 长期记忆

显式记忆仍支持用户直接写入，例如“记住我默认使用 pnpm”。自动整合采用受约束的候选流程：

1. LLM 从指定 Episode 提出带稳定 `memory_key`、来源位置和证据引用的候选操作；
2. Harness 确定性验证用户权威、成功 Tool 结果、blob、作用域和目标 Card；
3. 同一稳定键更新同一 Card，所有更新、冲突消解、Prune 和用户撤销均写入版本链；
4. 检索采用 SQLite 倒排词项加确定性重排，命中后可按限定 Episode 引用回溯原文。

LLM 不能直接写 Memory Card；无效、低置信、伪造来源或越权候选只保留拒绝审计。

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
- 自动激活默认设置软数量上限和总 Token 预算，超过预算时由模型缩小范围，必要时再请求用户判断；
- 已加载的 Skill 不重复注入；会话压缩后保留激活记录，需要时可重新加载正文；
- MVP 只扫描配置指定的一个 Skill 根目录，不实现安装、远程仓库、签名校验或项目级/用户级/内置级多来源合并；
- 指定目录内出现重复 Skill 名称时视为配置错误，相关 Skill 不进入候选目录，并通过 `bot doctor` 和日志报告。

模型通过 Agent Core 提供的内部控制动作 `activate_skill(name, reason)` 请求加载 Skill；该动作只读取已进入 Catalog 的 Skill，不执行外部程序，也不绕过 Policy Engine。用户输入中的 `$skill-name` 由 CLI 解析为显式激活请求，再与原始任务一同交给 Agent Core。

多个 Skill 同时匹配时按关系处理：

- **互补**：允许同时激活，例如迁移分析和性能分析；
- **包含或明显重叠**：优先采用更具体的 Skill，避免重复上下文；
- **语义冲突**：用户显式选择优先；若均为自动选择且无法可靠消解，则向用户说明冲突并请求取舍；
- **常用组合**：MVP 后可增加 Skill Bundle 或领域总入口 Skill，但不将其做成强制执行工作流。

运行时不得把多个 Skill 改写、拼接成一个无法追踪来源的提示。每个 Skill 独立保留名称、版本、文件路径和激活顺序，并产生以下事件：

```text
skill.discovered
skill.activated
skill.resource_loaded
skill.skipped
skill.conflict_detected
```

领域 Skill 应围绕用户目标组织，而不是机械地为每个命令行工具创建一个 Skill。例如 `kunpeng-performance-analysis` 可以根据诊断情况组合调用 KSYS 和 Tuner；KSYS、Tuner 仍是只负责结构化命令执行和结果返回的 Tool。Skill 提供可偏离的专家操作手册，不承担权限控制、参数校验或强制状态机职责。

MVP 的目录配置和发现约定如下：

```toml
[skills]
path = "./skills"
auto_activate = true
max_auto_activated = 3
```

```text
skills/
├── kunpeng-migration/
│   ├── SKILL.md
│   └── references/
└── kunpeng-performance-analysis/
    ├── SKILL.md
    └── references/
```

启动时扫描该目录的直接子目录并解析其中的 `SKILL.md`，生成内存中的 Skill Catalog。目录不存在、文件无法读取、元数据不合法或名称重复时，只禁用对应 Skill 并给出诊断，不阻止通用 Agent 启动。MVP 不监听目录变化；用户修改 Skill 后，通过重启会话或执行 `/skills reload` 重新扫描。

### 7.5 鲲鹏领域扩展

鲲鹏能力按“用户目标”拆分 Skill，而不是按命令行工具拆分：

```text
kunpeng-migration
kunpeng-performance-analysis
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

### 8.3 Shell 执行

- 使用参数数组直接启动进程；只有确实需要管道、重定向、通配符时才进入 Shell 模式。
- 对复合命令解析为多个 segment，分别评估风险。
- 工作目录必须经 `realpath` 校验，防止 `..` 和 symlink 逃逸。
- 子进程使用独立进程组，取消时终止整个进程树。
- 环境变量采用 allowlist 传递；默认剥离云凭据和 Token。
- 输出做 ANSI 控制字符清理、大小限制和敏感值脱敏。

### 8.4 文件工具

- 写入默认限制在 Workspace 内。
- 禁止修改 `.env*`、SSH/云凭据、系统配置和 Agent 自身凭据。
- 编辑采用 patch，并在执行前后记录摘要与文件 hash。
- 防御 symlink/TOCTOU：校验规范路径，并尽可能以安全文件描述符完成写入。
- Agent 不能通过 Shell 绕过文件工具限制；Shell 与文件工具必须共享同一策略层。

### 8.5 审批语义

支持：`allow once`、`allow for session`、`always allow matching rule`、`deny`。永久规则保存的是结构化匹配条件，而不是模糊的整段自然语言。

非 TTY/无人值守场景遇到 `Ask` 必须 fail closed，除非调用方提前提供了明确策略。

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

- `sessions`：会话元数据、工作区、父会话、创建/更新时间；
- `runs`：一次用户请求对应的运行、状态和预算使用；
- `events`：有序事件流，payload 使用带版本号的 JSON；
- `messages`：便于查询的消息投影；
- `tool_runs`：工具参数摘要、状态、耗时和结果摘要；
- `approvals`：请求、决定、范围和策略来源；
- `memories`：显式记忆、来源、版本和删除状态；
- `memory_episodes`、`memory_consolidation_runs`、`memory_candidates`：Episode 状态机、
  LLM 整合事务、候选及拒绝原因；
- `memory_cards`、`memory_card_versions`、`memory_card_terms`、`memory_retrievals`：
  版本化语义记忆、倒排索引和检索/访问指标；
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
api_key_ref = "env:BOT_MODEL_API_KEY"
name = "<deepseek-v4-flash-or-pro-model-id>"
temperature = 0.2
context_window_tokens = 131072

[agent]
max_steps = 30
max_wall_time_seconds = 1800
max_cost_usd = 2.0

[subagents]
enabled = true
max_concurrent = 3
max_queued = 32
max_tasks_per_session = 16
max_steps = 15
max_wall_time_seconds = 900
allow_worktree_writes = true
worktree_dir = ".bot/agent-worktrees"

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
recent_conversation_tokens = 48000
memory_tokens = 8000
tool_schema_tokens = 16000
tool_result_inline_tokens = 4000

[memory]
enabled = true
auto_consolidate = true
# model = "<optional-memory-model-id>"
session_gate = 5
time_gate_hours = 24
context_utilization_gate = 0.70
max_episodes_per_run = 8
max_consolidation_batches = 16
max_source_chars = 60000
max_output_tokens = 4096
episode_summary_tokens = 12000
min_confidence = 0.65
max_active_cards = 500
stale_after_days = 90
retrieval_limit = 24
retrieval_candidate_limit = 200
refresh_every_steps = 5
failure_warning_threshold = 3

[skills]
path = "./skills"
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
│   ├── core/                # Agent Loop、Run、事件、限制、取消
│   ├── context/             # 指令发现、Token 预算、压缩
│   ├── providers/           # OpenAI-compatible adapter、能力探测、registry
│   ├── tools/               # Tool API、registry、built-ins、subprocess adapter
│   │   └── kunpeng/         # KSYS、Tuner 等领域 Tool adapter
│   ├── execution/           # LocalExecutionTarget、未来 SSH 接口
│   ├── policy/              # 文件、Shell、网络、审批策略
│   ├── sessions/            # SQLite store、migration、projection
│   ├── skills/              # Skill 发现、匹配、加载
│   ├── subagents/           # 后台 Worker Pool、profile、状态机和控制 Tool
│   ├── config/              # Schema、分层加载、凭据引用
│   └── observability/       # 日志、脱敏、usage、trace
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── golden/
│   └── evals/
└── docs/
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

### Milestone 0：可验证骨架

- 配置、事件模型、OpenAI-compatible Provider、Tool/Policy/ExecutionTarget 接口；
- Mock Provider 驱动的 Agent Loop；
- 流式文本、结构化 Tool Call、JSONL 输出和最小测试基建。

完成标准：无需真实模型即可回放一条包含工具调用的完整运行。

### Milestone 1：可用的本地 Agent

- 使用 DeepSeek V4 Flash 或 Pro 配置验证 OpenAI-compatible Provider；
- 交互 CLI、流式输出和会话内中断；
- read/search/patch/shell 工具；
- Workspace 限制、审批和本地环境架构探测；
- 指定目录的 Skill Catalog、三段式披露、自动/显式激活和 `/skills reload`。

完成标准：可在临时仓库中完成通用代码任务，能够正确激活一个或多个测试 Skill，所有动作可见、可取消。

### Milestone 2：首批鲲鹏领域能力

- 通用 Subprocess CLI Adapter；
- KSYS 与 Tuner 的结构化 Tool Schema 和本地 Adapter；
- `kunpeng-performance-analysis` Skill；
- x86 环境下生成 ARM 手动执行指导，并接受用户粘贴的自由文本结果；
- 建立初版鲲鹏领域对比评测集。

完成标准：Agent 能根据任务和环境选择 KSYS/Tuner 或人工采集路径，给出有证据的性能分析建议；Skill 提供专家指导但不强制固定流程。

### Milestone 3：可靠会话与可量化评测

- SQLite 事件存储；
- resume/fork；
- 上下文发现、Token 预算和压缩；
- usage/cost/doctor；
- 通用任务集与鲲鹏任务集的稳定回放和对比报告。

完成标准：进程异常退出后可恢复，长会话压缩后仍保持任务约束，并能量化 Skill 与领域 Tool 带来的增益。

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
