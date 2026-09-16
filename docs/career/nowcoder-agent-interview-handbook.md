# 赛文X 100+ 社招面经：Agent 高频题与代码印证手册

> 来源：[牛客《【0901更新】赛文Xの100+社招面经记录》](https://www.nowcoder.com/discuss/921546202062000128)，整理日期：2026-09-14。帖子持续更新；本文以当天页面中的 78 个唯一子帖链接为快照，代码证据以本仓库提交 `31ff58c` 为基线。

> 2026-09-16 本地实现复核至 `2bb51f4`：修正当前能力与扩展设计的混用，补充响应恢复与清理边界。外部面经来源保留 09-14 快照，本轮未重新抓取；差异见[实现核对记录](implementation-audit.md)。

这不是一篇单场面经，而是一张社招面经索引。索引覆盖约 100 场面试，当时已整理 80 多家公司/场次，并链接到 78 篇可访问的独立记录；其中 43 篇标题直接包含 Agent、AI 或智搜岗位，更多全栈、平台和服务端面经也问到了 Agent。

本文遍历索引中的全部子帖，只提取并转述 Agent 相关考点。算法题、纯语言八股、HR 信息和与 Agent 无关的业务题不展开。来源问题均做了归纳而非逐字转载；第一人称回答仍须按候选人的真实职责调整。

## 1. 我实际看到了什么

### 1.1 帖子的构成

原帖先给出作者的投递漏斗，然后列出每一轮面经的独立链接。Agent 相关样本包括：

- 360 Agent 一面、二面和 HR 面；
- B 站 Agent 一面、二面、三面和 HR 面；
- 阿里 Agent 开发、淘天 Agent、阿里国际 Agent 中台/业务 Agent、千问 Agent Infra；
- 唯品会、深信服、虾皮、懂车帝、顺丰等 Agent 岗；
- 平安银行、浦发银行、滴滴、千寻智能等 AI/全栈岗位中的 Agent 深挖；
- 高德、小满科技等更偏 Agent runtime、上下文和工具系统的专场。

### 1.2 高频主题

下面的次数来自对 78 篇子帖按关键词做的交叉归类。同一篇可以进入多个主题，数字用于判断复习优先级，不代表严谨的岗位统计。

| 主题 | 涉及子帖 | 相关问题行 | 典型追问 |
|---|---:|---:|---|
| Tool / MCP / Skill | 31 | 68 | 边界、冲突、召回、鉴权、版本与副作用 |
| 模型、成本与延迟 | 29 | 41 | 模型选择、推理时延、并发和成本优化 |
| RAG / 知识库 | 26 | 41 | 分块、向量化、rerank、更新与准召 |
| Harness / Runtime / Loop | 25 | 32 | 组件、执行循环、状态和终止条件 |
| 上下文 / Memory | 25 | 36 | 压缩、渐进披露、长短期记忆和缓存 |
| 可靠性 / 评测 / 观测 | 25 | 36 | bad case、重试、回滚、trace 和指标 |
| Coding Agent / 研发效能 | 23 | 39 | 代码质量、门禁、SDD/TDD 和灰度 |
| 安全 / 权限 / 沙箱 | 22 | 33 | 越权、密钥、隔离、工具投毒和云沙箱 |
| Multi-Agent / 编排 | 19 | 29 | 拆分依据、通信、冲突和 Leader 故障 |

### 1.3 面试官反复确认的九件事

1. 候选人能否画清一次 Agent 请求从输入到终止的完整数据流。
2. 能否分清模型接口、Tool、MCP、Skill、CLI、RAG、Memory、Workflow 和 Harness。
3. Multi-Agent 是否真的必要，而不是为了堆概念。
4. 长任务如何保存状态、判断进展、处理失败并恢复。
5. 上下文和 Skill 越来越多时如何控制 token、注意力和缓存。
6. 工具调用、代码修改和数据访问怎样做到可验证、可审计、可回滚。
7. Agent 效果如何用评测集、外部断言和线上指标证明。
8. 云端沙箱、权限、多租户、并发和成本如何工程化。
9. 候选人到底负责了什么，有没有规模、失败案例和量化结果。

## 2. 本地代码能支撑到什么程度

面试时应把“当前实现”“部分实现”和“设计题”分开。

| 能力 | 本地状态 | 证据或缺口 |
|---|---|---|
| Agent Harness / Runtime | 已实现 | [`AgentRunner`](../../src/bot/core/agent.py) 编排模型、工具、策略、上下文、存储、压缩、记忆和事件 |
| 结构化 Tool Calling | 已实现 | [`models.py`](../../src/bot/core/models.py)、[`registry.py`](../../src/bot/tools/registry.py) 和 `_execute_tool` |
| 工具策略、审批与审计 | 已实现 | [`PolicyEngine`](../../src/bot/policy/engine.py)、tool run、approval 与事件记录 |
| 长进程、重试与终止 | 已实现 | 有界清理、progress checkpoint；工具／正文重复检测默认 observe，响应自动恢复默认关闭，完整性门禁生效 |
| 上下文预算与压缩恢复 | 已实现 | [`context.py`](../../src/bot/core/context.py)、[`CompactionService`](../../src/bot/compaction/service.py) |
| 记忆检索与证据回载 | 已实现 | [`routing.py`](../../src/bot/memory/routing.py) 和 [`memory/store.py`](../../src/bot/memory/store.py) |
| 评测与外部验证 | 已实现 | [`evals`](../../src/bot/evals)、单元/集成测试和 trace 导出 |
| Skill 发现与激活 | 已实现 | 本地 Skill catalog、激活状态和 token 预算 |
| Multi-Agent | 部分实现 | 有进程内、深度最多为 1 的后台 Worker Pool；不是对等 Agent Team，也不支持递归委托 |
| RAG | 部分实现 | 有词法记忆检索和证据加载；没有通用文档摄取、embedding、向量库和 reranker |
| MCP | 未实现 | 设计中预留 Adapter；当前工具注册表不是 MCP client/server |
| 云沙箱与多租户调度 | 未实现 | 有工作区和策略边界，但没有按任务创建的容器沙箱、分布式调度和租户配额 |
| 动态模型路由/降级 | 未实现 | Provider 可配置且请求可重试，但没有按任务质量/成本自动切换模型 |
| Coding Agent 全链路发布 | 未实现 | 有编码工具、验证和策略组件；没有完整 PRD→开发→预发→灰度→发布平台 |

本轮边界见[实现核对记录](implementation-audit.md)及其源码证据；[MVP 状态表](../architecture/implementation-status.md)是 09-10 快照，不能覆盖后续变更。

## 3. 开场：30 秒与 2 分钟项目介绍

### 3.1 30 秒版本

> 我做的是一个面向长任务的 Agent runtime。它把模型、结构化工具、权限策略、上下文预算、会话状态、记忆、压缩恢复和可观测性串成一个执行闭环。系统会在“组装请求—模型决策—校验并执行工具—保存结果—继续推理”之间循环，并处理工具失败、重复调用、长进程和上下文膨胀。项目重点不是让模型能调用工具，而是让整个执行过程可控、可恢复、可验证。

### 3.2 两分钟版本

> 项目的核心是 `AgentRunner`。它先加载会话、压缩状态、记忆和运行环境，由 Runner 选择工具 schema，再由 `ContextPlanner` 规划消息并检查总输入预算。模型响应先通过完整性及必需工具门禁，可接受的 Assistant 调用消息先持久化；随后依次做名称检查、JSON Schema 校验、策略判断和必要审批，再通过执行器运行工具，脱敏并持久化结果，然后进入下一轮。
>
> 长任务不能只靠 `max_steps`。系统同时观察重复调用、连续失败、是否取得新进展和外部进程状态，并保存 checkpoint。上下文达到目标利用率时，会保留最近原文，把大输出外置成内容寻址 blob，再生成带固定字段、来源范围和哈希的单摘要。候选摘要校验通过才替换旧版本，失败不推进边界，还能回读原始消息或回滚。
>
> 可靠性方面，工具有 schema、策略、审批、可配置超时、输出限制和审计；评测侧可配置 JSON、命令或产物断言验证结果。普通运行的 completed 只是流程状态，主循环还没有通用的强制交付文件验收。边界上，本仓库没有完整 MCP、向量 RAG 和分布式云沙箱，所以这些内容我会作为扩展设计回答，不包装成已经落地的功能。

## 4. 概念边界：最高频的一张表

这组区别在 [360 一面](https://www.nowcoder.com/feed/main/detail/b1903a3fe061470694dd3e7d1c33bfab)、[B 站二面](https://www.nowcoder.com/feed/main/detail/794d53d2d5b4437eb47cb606910c8fa1)、[小满科技一面](https://www.nowcoder.com/feed/main/detail/5f3df51243ad425c83e81e359ce82c36)等多场反复出现。

| 概念 | 本质 | 决定什么 | 典型误区 |
|---|---|---|---|
| Function Calling | 模型输出结构化调用意图的接口约定 | 模型怎样表达“调用哪个函数、传什么参数” | 把它当成实际执行器 |
| Tool | 带名称、描述、schema 和执行实现的能力 | Agent 能做什么 | 只有参数校验，没有权限和结果验证 |
| MCP | 工具/资源/提示的发现与调用协议 | 不同宿主怎样接入外部能力 | 把传输协议当成可信来源或业务流程 |
| CLI | 进程级命令接口 | 人或 Agent 怎样调用本地程序 | 认为 CLI 不能做结构化输出、鉴权或幂等 |
| Skill | 可按需加载的领域说明、流程知识和配套资源 | Agent 应当怎样完成一类任务 | 认为文本说明能强制模型逐步执行 |
| Workflow | 代码或图定义的确定性节点和转移 | 哪些步骤必须固定 | 所有决策都交给 LLM，仍称作固定工作流 |
| RAG | 从外部知识集合检索证据后生成 | 回答依据是什么 | 把所有上下文注入都叫 RAG |
| Memory | 跨轮或跨任务保存、选择和回读状态 | 哪些历史值得继续使用 | 保存全部对话且每轮全部发送 |
| Harness | 模型外的执行和控制运行时 | 安全、状态、预算、恢复、终止和观测 | 把 Harness 等同于 system prompt |
| Agent | 在目标和约束下循环感知、决策、行动和验证的系统 | 下一步做什么以及何时结束 | 有一次 Function Call 就称为完整 Agent |

### 4.1 Tool、MCP、Skill、知识库发生冲突怎么办

不要背一个固定的“优先级”。MCP 是传输协议，Skill 是过程指导，知识库和工具结果是数据来源，它们不在同一语义层。

建议回答：

1. 系统/策略约束和用户明确授权不可被其他内容覆盖。
2. Skill 只指导流程，不自动成为事实来源。
3. 工具、MCP 和知识库返回都按不可信外部数据处理。
4. 根据来源权威性、权限范围、版本、时间和可复验性选择证据。
5. 高风险冲突通过二次查询、read-after-write 或人工审批解决。
6. 最终请求保存选用证据和拒绝其他证据的理由，便于审计。

本地 [`CORE_POLICY`](../../src/bot/core/context.py) 把工具和网页内容视为不可信数据；[`PolicyEngine`](../../src/bot/policy/engine.py) 决定操作是否能执行。但 MCP Adapter 当前未实现，回答时要主动说明。

## 5. Harness、Runtime 与 Agent Loop

[高德一面](https://www.nowcoder.com/feed/main/detail/0c02771670284a2cad2aab983294580b)问到了状态、渐进披露和观测，[顺丰一面](https://www.nowcoder.com/feed/main/detail/7cab90ca59084f64bc19d1cb13935937)继续追问循环、死循环、中断和上云。

### 5.1 一次循环

```text
用户目标 / 历史 / 记忆 / 环境
            │
            ▼
   上下文预算与工具选择
            │
            ▼
       ModelRequest
            │
      响应完整性 / 必需工具门禁
            │
      ┌─────┴─────┐
      ▼           ▼
   文本回答     Tool Call
                  │
       persist Assistant 调用
                  │
    schema → policy → approval → execute
                  │
        persist Tool Result / observe
                  │
        progress / retry / terminate
```

usage 和运行事件在发生时记录；独立任务 verifier 属于评测链路，不是每轮工具执行的固定步骤。

### 5.2 60 秒口述

> Harness 是模型外面的控制面，Runtime 是这套控制面在运行时的具体实现。我的主循环会先构建预算受控的请求，再接收模型文本或结构化调用。工具调用经过 schema、策略和审批后执行，结果与事件持久化，再由进展和终止逻辑决定继续、恢复还是结束。模型决定下一步意图，Harness 保证这一步能否安全执行、怎样记录、失败后怎么办以及什么时候停止。

### 5.3 模型挂了怎么办

分四类处理：

- 可重试的短暂错误：有限次数指数退避，丢弃不完整流式缓冲后重试。
- context limit：重新组装或强制压缩，而不是原请求盲重试。
- 配额/成本上限：停止或转人工，不制造重试风暴。
- provider 长期不可用：需要模型路由、熔断和备用 provider；这是本地当前缺口。

本地 [`_request_model_with_retries`](../../src/bot/core/agent.py) 处理瞬态重试和请求费用门禁，主循环另处理
每 step 最多一次的上下文超限恢复。输出 `length/max_tokens` 和主动中断则走独立的响应完整性门禁：
整批正文与工具缓冲隔离，不执行其中任何调用；自动续跑需显式开启 `agent.recovery.enabled`。
详见[P0 验收](../evaluations/task-reliability-p0-results.md)。这些路径没有跨模型自动降级。

## 6. 单 Agent、Workflow 与 Multi-Agent

[B 站三面](https://www.nowcoder.com/feed/main/detail/14d7f7ffffe74f45a8b3bce4c677d927)比较了中心式与去中心式协作，[阿里国际业务 Agent 一面](https://www.nowcoder.com/feed/main/detail/3c305b0c1565458ba05c9906322f5327)追问通信模式和结果验证，[基动一面](https://www.nowcoder.com/feed/main/detail/64e31a28581b4e21a1e8e1aee236b6b4)重点追问并发改动冲突。

### 6.1 选型原则

| 方案 | 适用场景 | 主要代价 |
|---|---|---|
| 单 Agent | 共享上下文强、任务不宜拆、工具数量可控 | 上下文膨胀、能力耦合和长链路脆弱 |
| Workflow | 合规步骤、发布门禁、固定数据处理 | 对开放问题适应差，维护分支成本高 |
| Leader + Workers | 子任务可并行、权限/上下文需隔离、结果可合并 | 调度、通信、冲突、成本和失败恢复 |
| 对等 Multi-Agent | 探索、协商或没有天然中心节点 | 难终止、难审计、共识和冲突处理复杂 |

### 6.2 为什么不使用一个全能 Agent

只有出现下列收益时才拆：并行缩短关键路径、隔离权限、缩小各自上下文、使用不同模型/工具，或让独立验证者降低自证偏差。如果任务高度共享状态、修改同一文件或协调成本超过推理成本，应继续用单 Agent 或确定性 Workflow。

### 6.3 通信必须结构化

子任务至少包含 `task_id`、目标、允许范围、输入引用、预算、截止条件和验收标准；返回包含状态、结论、证据/产物引用、未完成项和错误分类。自然语言可以解释，但调度状态不能只靠自然语言猜测。

本地 [`subagents`](../../src/bot/subagents) 支持进程内 Worker Pool、事件和结果汇总，深度最多为 1。分布式租约、Leader 选举、跨机器容灾和递归委托没有实现，因此 Leader 故障恢复只能作为设计题回答：任务状态外置、worker lease、幂等提交、超时接管和产物内容寻址。

## 7. 上下文、Skill 与 Memory

[转转面经](https://www.nowcoder.com/feed/main/detail/6204344a50a04c46a6220b2d73c426bc)集中问了压缩、用户记忆和交付物，[高德一面](https://www.nowcoder.com/feed/main/detail/0c02771670284a2cad2aab983294580b)追问 Skill 膨胀、渐进披露和 KV Cache，[阿里一面](https://www.nowcoder.com/feed/main/detail/6b51e5798cd14717bdcb60ca54ca6f11)追问注意力稀释和 Skill 生命周期。

### 7.1 核心区分

**持久化真相**可以保存完整历史，**本轮请求视图**只发送完成当前决策所需的最小充分上下文。长窗口不是免费数据库；它仍有延迟、成本、注意力和缓存问题。

### 7.2 五级治理

1. **硬预算**：从模型窗口扣除输出、协议和安全余量，再确定输入上限。
2. **选择**：先保留含 PINNED 项的完整原子组，再按优先级和新旧程度装箱；`trust` 用于来源／角色校验，不是装箱排序键。
3. **渐进披露**：先给工具或 Skill 目录，只在相关时加载完整 schema 和说明。
4. **外置**：已采集并保留的大工具结果写入内容寻址 blob，请求中放有界预览和引用；执行器已丢弃的输出不能回读。
5. **压缩与回读**：摘要旧历史、保留近期原文；细节问题再从原始消息、blob 或记忆证据回载。

### 7.3 怎样减少摘要损失

不要承诺零损失，应承诺可验证、可追溯和可恢复：

- 摘要固定保留目标、约束、进展、决策、文件、失败、下一步和关键上下文；
- 关键用户消息作为原始锚点回放；
- 正在进行的工具组不跨边界压缩；
- 候选摘要校验结构、生成完整性和来源范围；不再设置独立 4K 正文发布门限，双路径仍校验恢复后的完整请求预算；
- 保存 source hash、parent 和 covered range；
- 验证通过再短事务晋升，失败继续用旧摘要；
- 用后续细节追问和端到端任务结果评测，而非只看摘要相似度。

这些机制由 [`ContextPlanner`](../../src/bot/core/context.py)、[`CompactionService`](../../src/bot/compaction/service.py) 和 [`SessionStore`](../../src/bot/sessions/store.py) 共同实现。

默认 `a_fallback` 的候选生成与低水位检查位于 [`StrategyCompactor`](../../src/bot/compaction/strategies.py)：
前缀最多一次、独立兜底最多一次。兜底使用旧摘要和本次新增原文；它不会自动从头重读所有历史。

### 7.4 Memory 如何分层

- Working state：当前 run 的计划、工具结果和未完成项。
- Session memory：同一会话中的原始消息与压缩状态。
- User/workspace memory：跨 run 的稳定偏好、事实和项目知识。
- Evidence archive：可按引用加载的原始消息、文件或 blob。

写入前要判定稳定性、来源、作用域、敏感性和冲突；读取时要有检索理由和证据。当前本地检索是词法匹配与路由，不是向量 RAG。

## 8. RAG 与知识库

[超参数一面](https://www.nowcoder.com/feed/main/detail/8e77c295f6cb4cd4b709d14804afb987)问到了 Hybrid Search、Metadata Filter、Graph RAG 和向量索引，[阿里千问 Infra 一面](https://www.nowcoder.com/feed/main/detail/10d5334563244bc4a248c155b71fb083)追问准召、向量空间与检索加速。

### 8.1 离线链路

```text
数据接入 → 解析清洗 → 语义切分 → 元数据/ACL
        → embedding + 关键词索引 → 质量检查 → 增量更新/删除传播
```

切分应保留标题、章节、页码、父子 chunk、相邻关系、来源版本和权限。高频更新不能只做全量重建；要用稳定文档 ID、版本、变更检测、增量索引和删除 tombstone。

### 8.2 在线链路

```text
意图/权限 → query rewrite → metadata/ACL filter
         → dense + sparse recall → fusion/rerank/dedup
         → token-budget packing → 带引用生成 → 反馈与日志
```

准召优化不能只调 top-k：先按查询类型、语料和权限切片评测，再判断问题在解析、切分、embedding、召回、融合、rerank 还是生成引用。证据不足时澄清或拒答。

### 8.3 本地印证与边界

[`MemoryRouter`](../../src/bot/memory/routing.py) 能区分无需检索、建议检索、必须搜索和必须加载证据；[`memory/store.py`](../../src/bot/memory/store.py) 用 token、CJK bigram、子串和 key 做可解释评分；Agent 能回载原始证据。但通用摄取、向量索引和 rerank 尚未实现，不能把它描述成完整知识库平台。

## 9. 工具可靠性、重试与终止

[互动影游一面](https://www.nowcoder.com/feed/main/detail/24e01f1d510a486b92efa795b4835669)问了“工具显示成功但没有效果”，[浦发银行一面](https://www.nowcoder.com/feed/main/detail/c200fd35c881415086928e113028e4c8)问了重试/降级和权限，[顺丰一面](https://www.nowcoder.com/feed/main/detail/7cab90ca59084f64bc19d1cb13935937)追问死循环和中途停止。

### 9.1 八层防线

以下包含通用设计建议；具体已实现范围见列表后的说明。

1. 工具名唯一，输入用 JSON Schema。
2. 参数关系、资源存在性和路径做语义校验。
3. 根据只读、网络、敏感、破坏性和作用域执行策略。
4. 高风险动作绑定具体审批，不把一次允许扩成永久权限。
5. 设置 soft wait、hard timeout、输出上限并规范化异常。
6. 写操作使用 idempotency key 或可查询 operation id。
7. tool call 与 result 成对保存，中断时修复协议。
8. 记录状态、错误、耗时和产物引用，并做外部验证。

本地执行链是：

```text
tool lookup → jsonschema → ToolAction → PolicyEngine → approval
            → ToolContext → execute → redact/blob → persist/events
```

对应实现见 [`base.py`](../../src/bot/tools/base.py)、[`registry.py`](../../src/bot/tools/registry.py)、[`PolicyEngine`](../../src/bot/policy/engine.py) 和 [`AgentRunner._execute_tool`](../../src/bot/core/agent.py)。

本地没有通用写操作幂等键、外部 operation 查询或 exactly-once 执行协议；call ID、Tool 的
`idempotent` 注解及重复检测不能替代这些能力。`process_hard_timeout_seconds` 默认是 `None`，
长进程不会仅因同步等待到期被杀；进程清理另有默认 5 秒期限。外部验收由具体 eval case 配置，
不能据此声称普通工具调用都有业务后置条件验证。

### 9.2 工具返回 success，但实际没效果

`success` 可能只代表进程退出码为 0，不代表业务后置条件成立。排查顺序：

1. 对齐调用 ID、参数、目标环境、身份和时间。
2. 检查 transport、tool wrapper 与底层服务三层状态。
3. 对写操作做 read-after-write 或查询 operation status。
4. 检查事务是否提交、异步队列是否消费、缓存是否刷新。
5. 校验工具是否把 warning 或业务失败错误映射成成功。
6. 保存产物 hash 或状态 diff，让 verifier 判断真实效果。

### 9.3 什么时候重试、降级或停止

- Retry：限流、短暂网络失败、可确认幂等的超时。
- Replan：参数/前置条件错误、工具不存在、结果不符合预期。
- Degrade：非核心能力不可用且有明确低保真路径。
- Ask：授权、歧义或副作用范围需要用户决定。
- Stop：预算耗尽、策略拒绝、重复无进展或不可恢复错误。

本地 [`RepeatGuard`](../../src/bot/core/termination/repetition.py) 与 progress controller 共同处理重复、失败和进展。
执行前重复限制默认 `observe`，显式 `enforce` 才拦截已适配操作；流式正文重复检测是另一个开关，
也默认观测。模型请求只对可重试错误做有上限的指数退避；策略拒绝会形成失败工具结果，并非每次都立即终止整个 Run。

## 10. 评测、Bad Case 与可观测性

[卓驭一面](https://www.nowcoder.com/feed/main/detail/f9be60e7b3c54750a57b37d5d200d1e5)问了评测集构建，[懂车帝一面](https://www.nowcoder.com/feed/main/detail/8d84590d7efe4c78943b28708b4395f2)问了 trace 与日志联合定位，[唯品会一面](https://www.nowcoder.com/feed/main/detail/64c0631bfa1d4dfa9be3d5956f8a3b97)追问 bad case、AB 和漏测修复。

### 10.1 四层评测

| 层级 | 检查内容 | 示例指标 |
|---|---|---|
| 组件契约 | schema、policy、协议和权限 | 非法调用拒绝率、协议完整率 |
| 轨迹行为 | 工具选择、参数、顺序、重试和进展 | tool success、重复率、平均步骤数 |
| 最终状态 | 文件、数据库、接口或 JSON 是否满足断言 | task success、回归通过率 |
| 系统质量 | 成本、延迟、恢复和人工介入 | p50/p95、token、恢复率、接管率 |

模型自称“完成”不能作为通过条件。[`verification.py`](../../src/bot/evals/verification.py) 支持严格 JSON、工具配对和外部产物验证，[`runner.py`](../../src/bot/evals/runner.py) 要求外部断言成立。

### 10.2 评测集怎么建

从真实任务和线上 bad case 开始，按任务类型、工具、权限、长度和失败模式分层；保留正常、边界、对抗和恢复用例。每次事故要落成最小可复现 fixture，并分别标记是模型、上下文、检索、工具、策略还是环境问题，避免用 prompt 修改掩盖系统缺陷。

### 10.3 Trace 应记录什么

至少记录 run/step/tool IDs、模型与配置、输入 token、选中的工具 schema、tool 参数摘要、审批、耗时、错误分类、重试、压缩版本、证据引用和最终 verifier。本地 [`events.py`](../../src/bot/core/events.py) 提供有序带时间事件，[`trace.py`](../../src/bot/observability/trace.py) 可以导出事件与状态包。

## 11. 安全、权限与云沙箱

[深信服一面](https://www.nowcoder.com/feed/main/detail/45b40914f438434b88b60a21e3d5ec1e)问了扫库、越权和大规模调度，[小满科技一面](https://www.nowcoder.com/feed/main/detail/5f3df51243ad425c83e81e359ce82c36)问了密钥隔离和 MCP 投毒，[顺丰二面](https://www.nowcoder.com/feed/main/detail/93a26b84a6634558b7228bf350c709b5)问了云端状态与沙箱生命周期。

### 11.1 安全回答框架

- Identity：用户、Agent、工具和服务都有明确身份。
- Scope：workspace、tenant、资源类型、动作和时间窗口最小授权。
- Secret：只传引用，密钥由执行侧注入，不进入 prompt 和 tool result。
- Isolation：文件、网络、进程、CPU/内存/时间和并发配额隔离。
- Approval：不可逆或外发动作在执行前绑定目标和参数确认。
- Verification：写后验证，危险动作保留审计和可回滚路径。
- Supply chain：MCP/Skill 版本、来源、签名、schema 和发布审批。

本地 [`resolve_path`](../../src/bot/tools/base.py) 和 [`PolicyEngine`](../../src/bot/policy/engine.py) 能限制工作区、敏感信息、网络和危险命令；命令工具不用隐式 shell，并有超时和输出限制。它不是生产级容器沙箱，也没有 MCP 供应链治理，回答时应将后者标为设计扩展。

## 12. Coding Agent 与研发交付

[广发四面](https://www.nowcoder.com/feed/main/detail/6715d77233254a73b7d6b369b17927f4)追问 AI 写代码为何需要规范，[懂车帝一面](https://www.nowcoder.com/feed/main/detail/8d84590d7efe4c78943b28708b4395f2)问了只修一个 case 而不破坏其他模块，[深信服三面](https://www.nowcoder.com/feed/main/detail/b64e8fddbfc642ec9aa33bcdb9aab9aa)问了 TDD、门禁、监控和回滚。

### 12.1 一条可信交付链

```text
需求澄清与验收标准
  → 影响面/依赖/权限分析
  → 隔离 workspace 或 branch
  → 小步修改
  → 静态检查 + 单测 + 集成/端到端
  → diff/安全/兼容性审查
  → 预发或影子流量
  → 灰度、指标和自动回滚
```

AI 代码质量不能只靠更长 prompt。规范解决输入和边界，测试与 verifier 检查行为，review 检查可维护性，灰度与回滚控制未知风险。并发修改同一系统时，按文件/模块声明 ownership，独立 worktree 生成 patch，合并前重新基线化并跑受影响测试。

本仓库已有工作区边界、编码工具、外部验证和丰富测试，但没有完整发布平台。因此可以用代码证明 Harness 层能力，不能声称已经实现企业 CI/CD、影子系统和自动灰度。

## 13. 性能、成本与并发

[阿里千问 Infra 一面](https://www.nowcoder.com/feed/main/detail/10d5334563244bc4a248c155b71fb083)问了访问多个服务的时延和成本，[顺丰二面](https://www.nowcoder.com/feed/main/detail/93a26b84a6634558b7228bf350c709b5)问了大量文件、并发 sub-agent 和沙箱启动。

### 13.1 先拆总时延

```text
T_total = ΣT_model + ΣT_tool + T_queue/approval
        + T_compaction + T_retry/backoff + T_finalization
```

固定任务、模型、提交和环境，对比快慢两条 trace。先看调用次数是否增加，再看每次模型/工具是否变慢；分别检查上下文 token、schema 数量、缓存命中、外部服务、压缩、重试和终止。

### 13.2 常见优化

- 独立工具并行，存在依赖的调用保持顺序。
- HTTP 连接池、长连接和批量接口减少握手与往返。
- 工具 schema/Skill 按需加载，大结果外置。
- 轻量模型负责分类、路由和格式化，强模型处理高难决策；必须用质量门禁校准。
- 沙箱池预热和镜像分层减少冷启动；有写状态的沙箱不能盲目复用。
- 为租户、模型、工具和 sub-agent 分别限流，避免单任务占满资源。

本地能证明 schema 选择、上下文预算、模型重试、长进程轮询和事件观测；模型路由、沙箱池和分布式租户调度仍是设计题。

## 14. 高频追问速答

### 14.1 Skill 是文本，怎样保证一定执行？

不能靠文本保证。强约束必须下沉为 Workflow、状态机、Policy、schema 和 verifier；Skill 只负责指导模型，偏离时由代码门禁阻止危险状态推进。

### 14.2 Agent 必须有 Memory 吗？

不必须。一次性短任务可以只有当前状态；跨轮目标、长期偏好或历史证据才需要 Memory。是否是 Agent 取决于是否围绕目标循环决策和行动，不取决于是否堆齐所有模块。

### 14.3 ReAct 与 Plan-and-Execute 怎么选？

ReAct 适合信息逐步暴露、每步结果会改变下一步的任务；Plan-and-Execute 适合目标明确且可拆分的长任务。生产系统常用混合方式：先给粗计划，每步执行后根据证据局部重规划。

### 14.4 大量工具怎样召回？

先按权限和环境过滤，再基于名称、描述、示例和历史成功率召回少量候选，只把候选 schema 发给模型；低置信度时澄清。工具图谱可表达依赖与前后置条件，但本地当前使用的是按需 schema 激活，不是完整工具图谱。

### 14.5 如何定义 Agent 成功率？

先定义分母和成功条件：一次跑通、允许自动重试后跑通、人工介入后完成不能混成一个数字。至少分别报告 autonomous success、assisted success、verifier pass、人工介入率、成本和 p95 延迟。

### 14.6 Leader Agent 挂了怎么办？

调度状态不能只在 Leader 内存里。任务、lease、checkpoint、工具副作用和产物引用要持久化；新 Leader 取得租约后从最后验证点恢复。提交必须幂等，超时 worker 的迟到结果不能覆盖新版本。

### 14.7 Agent 如何避免越权目录或扫库？

模型只产生意图；执行层使用规范化后的真实路径、workspace allowlist、ACL、最小凭据、查询模板、结果行列限制和审计。不能只在 prompt 里写“不要访问”。

这是扩展安全方案。本地已实现路径／symlink、敏感路径、网络和命令策略；没有通用数据库接入，
也没有数据库 ACL、租户行列授权和查询模板执行层。

### 14.8 Agent 可观测性和普通日志有什么不同？

普通日志看服务事件，Agent trace 还要重建“模型看到什么、为何选择某工具、状态如何变化、证据来自哪里”。两者用 run/trace/tool IDs 关联，不能互相替代。

## 15. 代码证据速查

| 面试主题 | 源码入口 | 可以证明什么 |
|---|---|---|
| Harness 主循环 | [`src/bot/core/agent.py`](../../src/bot/core/agent.py) | 模型、工具、策略、状态、压缩和记忆的统一编排 |
| 上下文与协议 | [`src/bot/core/context.py`](../../src/bot/core/context.py) | 预算、角色边界、原子工具组、模型适配计数与协议修复；不保证所有 Provider 精确计数 |
| 工具契约 | [`src/bot/tools/base.py`](../../src/bot/tools/base.py) | read-only、destructive、network、secret、idempotent、timeout 等注解 |
| 工具注册 | [`src/bot/tools/registry.py`](../../src/bot/tools/registry.py) | 名称唯一、查找和候选子集 |
| 策略与权限 | [`src/bot/policy/engine.py`](../../src/bot/policy/engine.py) | 工作区、网络、敏感信息、shell 和危险命令决策 |
| 长进程 | [`src/bot/execution/local.py`](../../src/bot/execution/local.py) | 启动、轮询、hard timeout、elapsed 和最后输出时间 |
| 重复与终止 | [`src/bot/core/termination/repetition.py`](../../src/bot/core/termination/repetition.py) | 重复调用检测和观察状态 |
| 响应恢复与清理 | [`recovery.py`](../../src/bot/core/termination/recovery.py)、[`process_scope.py`](../../src/bot/execution/process_scope.py) | 恢复额度／截止持久化、有界清理；启用方式及平台限制见 P0 验收 |
| 可恢复压缩 | [`src/bot/compaction/service.py`](../../src/bot/compaction/service.py) | 固定摘要结构、安全边界、校验、hash、锚点和失败回退 |
| 会话与 blob | [`src/bot/sessions/store.py`](../../src/bot/sessions/store.py) | 原始消息、tool run、两阶段压缩提交和内容寻址存储 |
| 记忆 | [`src/bot/memory`](../../src/bot/memory) | 检索路由、词法评分、作用域和证据回载 |
| 子 Agent | [`src/bot/subagents`](../../src/bot/subagents) | 进程内 Worker Pool、任务事件和结果汇总 |
| 观测 | [`src/bot/core/events.py`](../../src/bot/core/events.py)、[`trace.py`](../../src/bot/observability/trace.py) | 有序事件和 trace bundle |
| 评测 | [`src/bot/evals`](../../src/bot/evals) | JSON、工具轨迹、命令和最终状态验证 |

## 16. 模拟面试题单

按下面顺序练一轮 45 分钟：

1. 两分钟介绍 Agent 项目，说明个人职责、规模、失败案例和指标。
2. 画出 Agent loop，说明 Harness 与 Runtime 的区别。
3. 对比 Tool、MCP、Skill、CLI、Function Calling 和 Workflow。
4. 说明什么场景选单 Agent、Workflow 或 Multi-Agent。
5. 设计 Leader/Worker 通信、并发冲突和 Leader 故障恢复。
6. 说明长对话、Skill 膨胀和工具 schema 膨胀如何治理。
7. 讲 RAG 离线/在线链路，以及准召下降时的定位方法。
8. 解释“工具成功但业务没效果”如何排查和验证。
9. 设计 Agent 的重试、降级、人工介入与终止状态机。
10. 构造评测集并定义 autonomous success 和 assisted success。
11. 设计权限、密钥、目录、网络与云沙箱隔离。
12. 说明 Coding Agent 如何从需求到灰度发布且可回滚。
13. 给出一次 5 分钟变 20 分钟的 trace 排障过程。
14. 主动指出本地项目未实现 MCP、完整向量 RAG 和分布式云沙箱。

## 17. 面试前核对清单

- [ ] 能在 30 秒和 2 分钟内分别讲完项目。
- [ ] 能从代码指出一次 tool call 的校验、审批、执行和持久化链路。
- [ ] 不把 MCP、Skill、Tool 和 Function Calling 混为一谈。
- [ ] 能用收益与协调成本解释为什么要或不要 Multi-Agent。
- [ ] 能解释 storage truth 与 request view 的区别。
- [ ] 能说明摘要不承诺零损失，但可验证、回读和回滚。
- [ ] 能给出 RAG 的准召分层定位，而不只说“换 embedding”。
- [ ] 能区分工具执行成功和业务后置条件成功。
- [ ] 成功率、提效、成本和延迟数字都有分母、样本和时间范围。
- [ ] 能准确说明本仓库 MCP、RAG、沙箱、模型路由和多 Agent 的实现边界。
- [ ] 所有第一人称贡献都与个人真实经历一致。

## 18. 延伸阅读

- [上下文管理架构与面试手册](context-management-interview-guide.md)：上下文、压缩、缓存和长任务的深层追问。
- [实现状态](../architecture/implementation-status.md)：当前能力与候选方向的边界。
- [请求组装](../architecture/context-assembly.md)：上下文层、顺序、预算和工具协议。
- [可恢复上下文压缩](../architecture/recoverable-context-compaction.md)：摘要状态与恢复机制。
- [长期记忆](../architecture/markdown-memory.md)：记忆存储、检索和证据边界。
- [评测指南](../evaluations/evaluation.md)：如何用外部断言验证 Agent 结果。
