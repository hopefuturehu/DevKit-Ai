# MVP 实现状态

> 更新日期：2026-09-17；按 `b2d3ecd` 核对默认压缩、可靠性、Schema 和凭据支持。
> 结论：设计中 Milestone 0–3 及可恢复单摘要上下文压缩的本地代码闭环已经实现；
> 需要真实凭据或鲲鹏 ARM 环境的项目保留为环境验收，不伪造通过结果。

## 已完成

| 设计域 | 实现结果 | 主要入口 |
|---|---|---|
| 通用 CLI | 交互、自然语言入口、单次运行、JSONL、管理命令、会话内命令、steering 与取消 | `bot`、`bot run`、`bot resume` |
| Web 工作台 | 实时工具详情与进程日志、精确审批、补充要求与停止、断线历史恢复、文件快照与 diff、子任务时间线、上下文查看、移动端与主题 | `src/bot/web/`、[使用与验证](../guides/web-workbench.md) |
| Provider | OpenAI-compatible Chat Completions，SSE 文本、结构化 Tool Call、usage 与异常归一化 | `src/bot/providers/` |
| Agent Core | Tool 进展协议、持久化 epoch、重复调用限制、响应完整性门禁、可选重复正文中断/有限恢复和禁用工具调用的统一收尾 | `src/bot/core/agent.py`、`src/bot/core/progress.py`、`src/bot/core/termination/` |
| 通用 Tool | read、search、精确 patch、argv command、受控 shell、HTTP fetch，以及受管进程的轮询、输入、终止和列表 | `src/bot/tools/builtins.py` |
| TODO list | 单一 `update_plan` 全量快照 Tool、内容/唯一性/单 active 硬校验、`plan.updated` 持久事件投影、压缩后运行时恢复及 CLI/Web 展示 | `src/bot/tools/plan.py`、`src/bot/core/agent.py` |
| 执行抽象 | `ExecutionTarget` 接口、本地异步 subprocess、增量输出、hard timeout、有期限的清理；POSIX 跟踪原 SID 与已观察后代，明确记录残留及输出完整性 | `src/bot/execution/` |
| 安全策略 | workspace/symlink 边界、敏感路径、危险命令审批、非 TTY fail-closed、环境变量 allowlist、脱敏 | `src/bot/policy/`、`src/bot/observability/` |
| 审批 | once/session/always/deny 与单键交互；常用开发命令按 workspace 和保守 argv 前缀复用，动态/高危命令保持精确匹配 | `src/bot/core/approval.py`、`src/bot/policy/engine.py` |
| 会话与审计 | SQLite migration v16、Skill 交付来源、progress checkpoint、持久恢复预算/请求审计/进程清理记录、消息/Tool/审批/子 Agent 投影、resume/fork 和用量统计 | `src/bot/sessions/` |
| 上下文 | 单一 System 与来源信封、分层项目指令、Token Budget/模型计数、原子 Tool 轮次、外置与动态 schema；默认双路径压缩和第三版取舍提示、单一活动摘要、事务发布、原文重建/回滚 | `src/bot/core/context.py`、`src/bot/core/agent.py`、`src/bot/compaction/` |
| 长期记忆 | SQLite 历史证据、`USER.md` 显式记忆、Root Run 异步提取、Markdown 自动索引、冲突/遗忘隔离、四级 Router、强制检索/归因证据门禁和一次性 Tool 交付 | `src/bot/memory/`、`docs/architecture/markdown-memory.md`、`docs/architecture/memory-routing.md` |
| 子 Agent | 内置/用户/项目 Markdown catalog 与内容哈希信任、前台/后台 task、同 child session 续接、双向 mailbox、实时进度、有界 required 汇合、Git worktree 基线/patch artifact/采用/清理 | `src/bot/subagents/` |
| Skill | Catalog 快照、Run 隔离与退出清理、显式/自动多选、完整历史交付、同版复用、压缩后原文恢复、最终请求检查、资源按需加载和 reload；新会话默认 history，旧会话兼容 legacy | `src/bot/skills/`、[实施与验证](../evaluations/skill-context-validation.md) |
| 鲲鹏扩展 | KSYS、DevKit Tuner 结构化 Subprocess Adapter；非 ARM/缺工具时返回手动命令 | `src/bot/tools/kunpeng/` |
| 领域手册 | 可偏离的 `kunpeng-performance-analysis` Skill，随 wheel 发布并由 `bot init` 安装 | `skills/kunpeng-performance-analysis/` |
| 评测 | JSONL Case 独立 fixture/状态、文件变化与 JSON Schema 验收、容器命令验收、实际 Tool 结果配对、失败分类与版本清单；保留 Skill 对照及 Memory Router scripted/live 成对 A/B | `bot eval run`、`docs/evaluations/evaluation.md`、`evals/`、`scripts/run_memory_routing_behavior.py` |

## 验证证据

当前默认双路径与第三版提示的接入、9 项入口验证及 48 次真实候选生成实验见
[落地记录](../designs/compaction-dual-path-default.md)和[提示实测](../evaluations/compaction-selection-20260917.md)。
候选通过率不代表完整任务成功率。进程清理与响应恢复的 2026-09-14 离线验收见
[可靠性记录](../evaluations/task-reliability-p0-results.md)；重复检测默认 `observe`，自动恢复默认关闭，
响应完整性门禁直接生效。下列旧测试数字均保留其原执行日期，本次文档核对不算重新运行。

2026-09-10 在 `875750c` 对应实现上完成 `ruff check src tests scripts/run_skill_context_smoke.py`，
以及 `pytest tests/unit tests/integration --disable-warnings --maxfail=3`：452 passed、7 skipped。
新增 Skill 专项 27 项，覆盖长正文、连续压缩、来源事务、工具整组保护、引用损坏、预算失败、
fork/restart 和并发取消。DeepSeek V4 Pro 的三个 history 场景及三个无关后续任务通过；同次
legacy 对照有一次严格格式失败。原始 usage 和费用结论边界见[验证记录](../evaluations/skill-context-validation.md)。

下列是项目既有的完整验证入口；上述 Skill 验收没有重新运行 wheel 构建或环境专项验收：

```bash
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/pytest -q
.venv/bin/pip check
.venv/bin/pip wheel . --no-deps --no-build-isolation --wheel-dir /tmp/bot-wheels
```

测试覆盖除 Provider 流、完整 Agent Loop、审批作用域、五级上下文管理、
超大单消息、可恢复摘要游标、Provider 超限重试、Skill 多选、路径逃逸、进程组终止、
KSYS/Tuner 参数映射、CLI JSONL 和 Eval runner 外，还覆盖子 Agent 并发上限、状态 CAS、
取消竞态、崩溃恢复、跨工作区调度隔离、审批让出并发槽、父/子上下文与 blob 隔离、
内部状态库禁读、执行层 Tool allowlist、required 结果原子汇合、包含新文件内容的 Git worktree
写隔离，以及显式/自动 Markdown 记忆、旧 SQLite 迁移、提取幂等、敏感内容过滤、
冲突隔离、遗忘抑制、分信任注入和证据引用。
构建后的 wheel 另行执行 `bot init` 烟测，
确认内置 Skill 可释放到工作区。

## 仍需环境验收

以下项目仍需各自的部署环境或更完整的样本；局部真实模型 smoke 不能替代这些验收：

- 目标 OpenAI-compatible 服务的完整兼容与成本回归；已有 DeepSeek 专项实测，但尚无跨模型、长期任务的完整基线；
- 在安装 KSYS 的 Linux 环境验证 collect/report/diff/stability-check 的实际 CLI 版本和输出；
- 在鲲鹏 ARM 主机验证 DevKit Tuner 的各任务、权限、采集开销和输出解析；
- 运行 `evals/generic.jsonl` 与 `evals/kunpeng.jsonl` 的多轮重复实验，形成带/不带 Skill 的统计对比；
- 在客户部署边界内验证 API 地址、日志保留、代理设置和数据出境策略。

## 按设计延期

Skill 的通用历史投影、跨压缩回滚去重、独立微裁剪及批量收据回收尚未实现；历史交付减少
自动加载的重复正文，但部分显式加载及压缩场景输入增加，未证明普遍节费。

SSH ExecutionTarget、MCP/Python/HTTP 通用 Adapter、私有数据库/RAG、Skill 安装签名和多来源、Gateway、计划任务、对等 Agent Team、递归委托、进程外 daemon 和专用跨机器结果包均保持接口或演进空间，但没有纳入当前实现。当前已实现的是进程内、最大深度为 1 的后台子 Agent Worker Pool；它不承诺在 CLI 进程退出后继续运行。凭据支持环境变量/工作区 `.env` 引用和 `model.api_key` 直接值；操作系统 Keychain 尚未接入。

与交付文件版本绑定的运行时验收（P1）和小规模长期记忆切换仍是设计，未落地；当前记忆仍为
`on_demand` Router。模型自然结束或局部测试通过不保证交付物满足任务验收。
