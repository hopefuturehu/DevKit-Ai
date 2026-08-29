# Markdown 长期记忆

## 边界

系统将历史事实和派生记忆分开保存：

- SQLite 是会话、Run、消息、Tool Result 和证据位置的事实源；
- Markdown 是当前长期记忆的可读投影；
- SQLite 的 `memory_extraction_runs` 只保存提取状态、源哈希、模型、用量和错误，
  不保存记忆正文。

默认目录为工作区的 `.bot/memory`。该目录对普通 Agent 文件和 Shell Tool 整体拒绝访问，
模型只能通过受限的记忆 Tool 检索。用户仍可在 Agent 外直接查看和编辑文件。

## 文件布局

```text
.bot/memory/
├── USER.md
├── MEMORY.md
├── CONFLICTS.md
├── FORGET.md
├── topics/<kind>/*.md
└── conflicts/*.md
```

- `USER.md`：`/remember` 和用户手工维护的显式记忆，是 User 信任域。
- `topics/`：自动提取器维护的原子记忆，是 Untrusted 信任域的正文事实源。
- `MEMORY.md`：由活动主题文件原子重建的短索引，不是第二份可独立编辑的事实源。
- `conflicts/`：同一个稳定 key 出现不同内容时保存新观察。
- `CONFLICTS.md`：冲突的可读投影；冲突内容不作为确定事实注入。
- `FORGET.md`：`/forget` 写入的自动记忆 key，阻止提取器重新创建。

文件位置而不是 frontmatter 的 `origin/status` 决定信任。自动提取器没有写入
`USER.md` 的接口，普通 Tool 也不能通过修改 Markdown 自行提升信任。

## 提取生命周期

`AgentRunner` 为当前 Run 生成 ID 后，启动一次后台扫描并显式排除当前 ID。因此扫描只会
处理启动前已经完成的历史 Run，不会与本次 `run.completed` 产生竞态。

候选 Run 必须满足：

- `runs.status = completed`；
- Session 没有 `parent_session_id`，即不是 Subagent；
- 工作区与当前 Runtime 一致；
- 尚未成功提取，或失败次数没有达到 `memory.max_attempts`。

提取输入只包含带位置的用户、Assistant 和有限 Tool 消息，不包含 reasoning。用户消息和
最终 Assistant 正文优先进入 `memory.max_source_tokens` 预算。每条消息还受
`memory.max_message_chars` 限制。

模型最多返回 `memory.max_candidates_per_run` 条候选，类型限定为：

- `user_preference`
- `workspace_fact`
- `decision`
- `procedure`
- `pitfall`

程序随后验证置信度、长度、证据位置、用户偏好的 User 证据和敏感信息。模型只能建议
稳定 key，不能决定信任级别、目标路径或覆盖旧内容。

## 巩固规则

稳定 key 由 `kind + memory_key` 规范化得到：

- key 和正文都相同：合并证据、置信度和更新时间；
- key 相同但正文不同：保留旧活动记忆，将新观察写入冲突区；
- key 在 `FORGET.md`：跳过；
- 没有同 key：创建新的活动主题文件。

主题和索引使用临时文件、`fsync`、原子 rename，并通过进程级文件锁串行化。提取失败不
影响用户 Run；SQLite 记录失败原因并在后续周期有界重试。Runtime 关闭时取消后台任务，
未完成工作留给下次启动。

## 读取路径与信任

每次 Run 开始时，在 `context.memory_tokens` 总预算内按顺序加载：

1. `USER.md` 中的显式条目，`ContextTrust.USER`，优先级 500；
2. `MEMORY.md` 自动索引，最多 `memory.index_tokens`，`ContextTrust.UNTRUSTED`，
   优先级 400。

这两个 `ContextTrust` 值不会发送给模型。进入当前 OpenAI-compatible 请求时，两者都序列化成
`role=user`，在 role 层面相同，仅以 `name=explicit_memory`/`name=automatic_memory` 及正文边界
区分；显式记忆位于会话前的稳定层，自动记忆则位于完整近期会话之后。若本轮还有 runtime
note，请求尾部的典型顺序是
`最新真人 user -> automatic_memory(user) -> runtime note(system)`。因此自动记忆虽然在内部是
`UNTRUSTED`，模型并不会从 wire role 得到一个更低的权限层，也不能依赖“最后一条 user”判断
用户当前意图。完整顺序及与本地开源框架的差异见
[Role 分配与最终消息位置](context-framework-comparison.md#53-role-分配与最终消息位置)。

模型还可调用：

- `search_memory(query, limit)`：检索显式与自动记忆；
- `load_memory_evidence(memory)`：按记忆绑定的 session/run/position 回读同工作区 SQLite
  原文，不能任意跨工作区浏览历史。

自动记忆不能覆盖 Core Policy、项目指令或用户输入。涉及版本、路径、命令和配置的内容
应在当前工作区重新验证。

## 命令

```text
/remember <text>
/memories
/forget <id-or-key>
/memory extract [run-id]
```

`/forget` 删除显式条目；对于自动记忆，它会把同 key 的活动和冲突记录标记为
`forgotten`，并写入 `FORGET.md`。直接手工删除主题文件只删除当前副本，未来仍可能重新
学习同一个 key。

旧版 SQLite `memories` 中的未删除条目在 Runtime 构建时幂等导入 `USER.md`，成功后对旧行
软删除，防止下次重复迁移。

## 配置

```toml
[memory]
enabled = true
path = "./.bot/memory"
auto_extract = true
# model = "low-cost-memory-model"
max_runs_per_cycle = 3
max_attempts = 3
max_candidates_per_run = 5
max_source_tokens = 24000
max_message_chars = 8000
max_output_tokens = 2048
min_confidence = 0.75
index_tokens = 2000
search_limit = 8
```
