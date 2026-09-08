# 面试问答

> 已于 2026-09-08 归档：本题已由[面试手册](../context-management-interview-guide.md)覆盖。
> 下文保留旧回答；默认自动记忆注入方式和 user 锚点以[当前请求组装说明](../context-assembly.md)为准。

本文档用于记录面试问题及经过讨论后确认的回答。

## 1. 这个项目的 Prompt 有哪些部分？

### 回答

这个项目的 Prompt 不是一个固定字符串，而是在每次调用模型前动态组装成
`ModelRequest(messages, tools)`。整体结构可以概括为：**稳定指令、长期上下文、会话历史、
动态状态，以及单独传递的 Tool Schema**。

`messages` 按以下顺序组装：

1. **Core Policy**：内置的 Agent 身份、安全、权限、工具使用和任务完成规则。
2. **Project Instructions**：从仓库根目录到当前工作目录逐层发现的 `AGENTS.md`，越靠近当前
   目录的规则作用域越具体。
3. **Environment**：操作系统、CPU 架构、工作目录和可用命令等运行环境信息。
4. **Skill Catalog**：当前可用 Skill 的精简目录。
5. **Tool Catalog**：Tool Schema 超出预算时生成的未加载工具目录，仅在需要渐进披露时出现。
6. **Active Skill**：已激活 Skill 的说明和正文。
7. **Explicit Memory**：用户显式确认的长期记忆。
8. **Compaction Summary**：长会话压缩后唯一活动的摘要；它必须位于所覆盖历史之后的原始消息
   之前。旧 `Snapshot` 只作为兼容层保留，默认不注入。
9. **Recent Conversation / Tool Result**：压缩游标之后的用户消息、Assistant 消息、Tool Call
   和 Tool Result。当前用户问题也属于这一层。
10. **Automatic Memory**：异步提取的 Markdown 记忆索引。
11. **Runtime Note**：后台进程、停滞恢复、终止要求和临时约束等动态信息。

Tool Schema 不混入 `messages`，而是通过请求顶层的 `tools` 字段单独传递。组装完成后，
`ContextPlanner` 会根据 Token 预算、保留策略、优先级和原子组选择实际发送的内容；Assistant
的 Tool Call 与对应 Tool Result 作为一个原子组，不能拆开保留。

这种顺序形成了“**稳定前缀 → 因果历史 → 易变尾部**”：稳定内容靠前有利于 Prompt Cache
复用，压缩摘要和近期消息保持正确的时间关系，高频变化的自动记忆改为按需 Tool 检索，运行时提示放在尾部，
避免使前面的长上下文缓存失效。
