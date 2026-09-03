# Markdown 自定义 Agent

`bot` 把 Agent 定义与调度机制分开：Markdown 描述“这个 Agent 是谁、能做什么”，
`task` 协议负责“何时运行、如何续接、结果如何回流”。当前委托深度固定为 1，
child Agent 不再持有 Agent 调度 Tool。

## 定义格式

用户级定义放在 `~/.bot/agents/*.md`，项目级定义放在 `.bot/agents/*.md`。
项目定义必须先通过 `bot agent trust`，且任意 Markdown 内容变化后信任会自动失效。

```markdown
---
schema_version: 1
name: api-reviewer
description: 检查 API 兼容性和安全边界
model: null
tools: [read_file, search_text]
isolation: read_only
skills: []
execution:
  default: foreground
  allowed: [foreground, background]
limits:
  max_steps: 20
  max_wall_time_seconds: 300
  max_cost_usd: 0.2
interaction:
  can_request_input: true
output:
  format: structured
---

你是 API 审查 Agent。先定位公开接口和调用点，再按严重程度报告问题。
```

字段语义：

- `model` 为空时继承主模型；非空时 child runner 使用该模型。
- `tools` 是严格 allowlist；`read_only` Agent 不能声明写工具。
- `isolation: worktree` 只在全局允许 worktree 写入时可用，且不直接修改主工作区。
- `execution` 限定前台/后台调用方式。
- `limits` 只能缩小全局步数、时间和费用边界，不能绕过全局上限。
- Markdown 正文作为 child system/task 指令，但不能扩大 Tool 和权限。

同名定义不会按“项目覆盖用户”静默合并；所有冲突项均禁用并报错，避免权限来源模糊。

## 管理命令

```bash
bot agent list
bot agent show api-reviewer
bot agent validate
bot agent trust
bot agent untrust
```

交互会话内可用 `/agents list`、`/agents reload`、`/agents trust` 和
`/agents untrust`；`/agents tasks` 查看当前父会话的任务。

## 父子交互协议

`task` 是首选入口。新任务传 `agent + prompt`；续接任务传 `task_id + prompt`。

- `execution: foreground` 会在有界时间内等待结果，适合主流程依赖的工作。
- `execution: background` 立即返回 `task_id`；结果进入持久化 mailbox，下一个父会话安全边界自动投递。
- `join_before_final: true` 使后台任务在父 Agent 最终回答前强制汇合，等待受
  `agents.required_wait_timeout_seconds` 限制。
- child 返回 `needs_input + questions` 时进入 `waiting_parent`。父 Agent 通过同一
  `task_id` 续接，复用 child session 和它的已有历史。
- 每次续接生成新的 `agent_task_run`；双向消息追加到 `agent_task_messages`，
  状态、用量、问题和交付确认都可审计。

`agents.auto_resume_background = false` 是默认值。开启后，未投递的后台结果会在父会话
空闲时触发一次新的父 Agent run；这会发生额外模型调用，因此必须显式开启。

## Worktree 交付

worktree Agent 可在新任务中传 `base_ref`（默认 `HEAD`）。结果会返回
`patch` artifact、`base_commit`、变更文件和 worktree 路径。`apply_agent_patch` 先执行
`git apply --check`，成功采用后默认清理受管 worktree；需要人工检查时可传
`cleanup: false`，之后再调用 `cleanup_agent_worktree`。
