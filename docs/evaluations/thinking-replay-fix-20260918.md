# DeepSeek thinking 持续关闭修复与验证

日期：2026-09-18。适用范围：官方 DeepSeek Chat Completions 端点；此次实测
`deepseek-v4-flash` 与 `deepseek-v4-pro`，不外推到其他兼容代理。

## 问题与行为变化

原适配器遇到任意历史工具调用缺少非空 `reasoning_content` 时，就在请求层设置
`thinking=disabled`。模型一次返回零推理 token 后，后续工具调用、新用户轮次及保留该历史的
前缀压缩都会持续触发关闭。显式 `enabled` 则会被本地拒绝。

修复后，模式仅由请求配置决定：未设置时不发送 thinking 参数，`enabled` / `disabled`
原样发送。历史消息不再改变模式。带 tools 且未显式关闭时，发送边界为所有 assistant 消息
回传已有 reasoning，包括普通回答；缺失或空值使用 `reasoning_content=""` 协议占位，覆盖
旧工具历史与派生压缩摘要。该占位不代表恢复了历史推理，也不写回消息库或更改来源哈希。
显式关闭或请求没有 tools 时，官方端点的发送投影不携带 reasoning。

命名 tool_choice 与默认/开启 thinking 冲突时明确报配置错误，提示使用 auto 或显式关闭；
不再暗中改变模式。Agent 原有能力协商本就不会给官方端点强制指定命名工具。

## 实际接口对照

固定合成历史：用户要求读取数值 → assistant 调用 get_value → tool 返回 7。
每次最多生成 512 tokens。直接使用原始 HTTP 请求隔离适配器影响：

| 历史 reasoning 字段 | 请求 thinking | HTTP | 正文 | reasoning tokens |
|---|---|---:|---|---:|
| 缺失 | enabled | 400 | 字段必须回传的协议错误 | — |
| 空字符串 | enabled | 200 | 7 | 0 |
| 单空格 | enabled | 200 | 7 | 0 |
| 空字符串 | 未设置 | 200 | 7 | 11 |

这证明本次端点区分“字段缺失”与“字段为空”，不能根据零 reasoning tokens 推断后续必须
关闭 thinking；开启模式也不保证每一步产生非空推理。

随后用修复后的生产 Provider 流式发送合成历史，额外加入无 reasoning 的
`assistant(name=context_compaction)` 派生摘要，验证两个模型及两种开启方式：

| 模型 | 请求配置 | 发送模式记录 | 正文 / finish | reasoning tokens |
|---|---|---|---|---:|
| deepseek-v4-flash | 未设置 | provider_default | 7 / stop | 21 |
| deepseek-v4-flash | enabled | enabled | 7 / stop | 16 |
| deepseek-v4-pro | 未设置 | provider_default | 7 / stop | 24 |
| deepseek-v4-pro | enabled | enabled | 7 / stop | 33 |

4/4 通过，均实际返回非空推理。这里只验证协议接受、续跑和模式保持，不代表任务质量或速度
提升的统计结论。原始用量、响应 ID 与字段状态见
[机器记录](../data/thinking-replay-fix-20260918.json)。

## 可观测性与回归

Provider metadata 新增 `requested_thinking`、`sent_thinking`、
`reasoning_placeholder_indices`；响应记录 `reasoning_content_state`，区分 absent、null、
empty、nonempty。字段状态取整次响应中信息最完整的状态，后续空 delta 不覆盖已有非空状态。
输入估计中的 `effective_thinking=provider_default` 表示未传参数，不再误记为显式 enabled；
tokenizer 仍按官方文档的默认 thinking 模板估算。通用保守估计也纳入普通回答的 reasoning，
避免 tokenizer 不可用时漏算新增回放内容。

单元测试覆盖缺失/空/非空 reasoning、三种配置、普通回答与摘要、非官方端点不受影响、
命名工具冲突、字段诊断与 fallback token 预算。集成测试经真实 SSE 解析和工具执行，关闭并
重新创建 runner / SQLite store，再在同一会话续跑，确认空推理历史不再关闭 thinking，
普通回答中的已有 reasoning 也被原样回传。原有压缩、收尾、Skill 上下文回归一并验证。

最终 202 项相关测试通过，Ruff 与 `git diff --check` 通过。进程管理相关测试在沙箱内受
进程权限限制而失败，沙箱外完整重跑通过。另以只读方式回放实际修复轮三次压缩的请求快照，
分别包含 107、124、149 条 assistant 消息：默认模式不被改为关闭，显式 enabled 不再被
历史检查拒绝，消息正文与持久化记录不变。真实历史未发送到外部接口。

历史数据库不需迁移；已运行的常驻 bot 进程需要重新加载新代码。

协议依据：[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。
